import json
import shutil
import tempfile
import unittest
from pathlib import Path

from toiletservice.__main__ import HANDLERS, import_file
from toiletservice.report import report, audit, notice_summary
from toiletservice.store import connect
from toiletservice.evidence import load_capture, EvidenceError

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'evidence'
FULL = 'evidence/notice-wc-a-full'
BUSY = 'evidence/notice-wc-a-busy'
GAPS = 'evidence/notice-wc-a-scheduled-gaps'


class NoticeEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = connect(self.root / 'notice.sqlite')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def events(self, command, items):
        path = self.root / f'{len(list(self.root.glob("*.json")))}.json'
        path.write_text(json.dumps({'events': items}, ensure_ascii=False), encoding='utf8')
        return import_file(self.db, command, path)

    def seed_ledger(self):
        """wc-a on opening v2 (closed via real water outage), contract C1 = 120 min."""
        import_file(self.db, 'facility-import', ROOT / 'fixtures' / 'facility-ok.json')
        import_file(self.db, 'facility-import', ROOT / 'fixtures' / 'facility-bad.json')
        self.events('shift-import', [dict(id='C1', kind='contract', facility_id='wc-a',
            vendor='clean', starts='2026-04-01T08:00:00', ends='2026-04-01T20:00:00',
            minutes=60, minimum_supply=1, remedy_minutes=120),
            dict(id='S1', kind='shift', contract_id='C1', worker='worker-a',
            check_in='2026-04-01T09:00:00', check_out='2026-04-01T10:00:00', supply=4)])

    def open_cases_and_complaints(self):
        self.events('notice-import', [dict(id='NF', kind='notice_case', facility_id='wc-a',
            opening_version=2, contract_kind='final'),
            dict(id='NC', kind='notice_case', facility_id='wc-a',
            opening_version=2, contract_kind='continuous')])
        self.events('complaint-import', [dict(id='QF', kind='complaint', facility_id='wc-a',
            at='2026-04-01T10:30:00', category='notice', reporter='reader-a', notice_case_id='NF'),
            dict(id='QC', kind='complaint', facility_id='wc-a',
            at='2026-04-01T10:30:00', category='notice', reporter='reader-b', notice_case_id='NC')])

    def certify_both(self, capture_dir, evidence_id, certify_id, at='2026-04-01T10:45:00'):
        self.events('notice-import', [
            dict(id=evidence_id, kind='notice_evidence', case_id='NF',
                 capture_dir=capture_dir, at=at),
            dict(id=evidence_id + '-c', kind='notice_evidence', case_id='NC',
                 capture_dir=capture_dir, at=at),
            dict(id=certify_id + 'F', kind='notice_certify', case_id='NF',
                 evidence_id=evidence_id, at=at),
            dict(id=certify_id + 'C', kind='notice_certify', case_id='NC',
                 evidence_id=evidence_id + '-c', at=at)])

    def test_real_four_epoch_capture_decides_both_contracts_separately(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        # Both complaints start pending.
        before = {row['id']: row for row in report(self.db)['complaint']}
        self.assertEqual((before['QF']['state'], before['QC']['state']), ('pending', 'pending'))

        capture = load_capture(FULL)
        self.assertEqual(sorted(capture['frames']), [1, 2, 3, 4])
        self.assertEqual(capture['gaps'], [])
        # Acceptance facts from the actual Edge/CDP-style browser capture.
        for epoch in (1, 3):
            self.assertEqual(capture['frames'][epoch]['visual_text'], '正常开放')
            self.assertIn('暂停开放', capture['frames'][epoch]['ax_texts'])
        for epoch in (2, 4):
            self.assertEqual(capture['frames'][epoch]['visual_text'], '暂停开放')
            self.assertIn('暂停开放', capture['frames'][epoch]['ax_texts'])
        self.assertTrue(all(frame['version'] == 'v2' for frame in capture['frames'].values()))
        # Screenshot hashes come from runtime browser bytes: wrong/right epochs
        # differ, identical visuals share a hash, and nothing here is hard-coded.
        hashes = {epoch: capture['frames'][epoch]['screenshot_sha256'] for epoch in (1, 2, 3, 4)}
        self.assertEqual(hashes[1], hashes[3])
        self.assertEqual(hashes[2], hashes[4])
        self.assertNotEqual(hashes[1], hashes[2])
        self.assertTrue(all(len(h) == 64 for h in hashes.values()))

        self.certify_both(FULL, 'EV1', 'K1')

        snapshot = report(self.db)
        cases = {row['id']: row for row in snapshot['notice_case']}
        complaints = {row['id']: row for row in snapshot['complaint']}
        # Final contract: last complete epoch (4) aligned -> QF rejected.
        self.assertEqual(cases['NF']['state'], 'aligned')
        self.assertEqual(complaints['QF']['state'], 'rejected')
        self.assertIsNone(complaints['QF']['due_at'])
        # Continuous contract: epochs 1 and 3 provably wrong -> QC confirmed,
        # remedy 120 minutes after the 10:45 certification.
        self.assertEqual(cases['NC']['state'], 'mismatch')
        self.assertEqual(complaints['QC']['state'], 'confirmed')
        self.assertEqual(complaints['QC']['due_at'], '2026-04-01T12:45:00')
        # Same page originals, different verdicts: no cross-contract copying.
        self.assertNotEqual(complaints['QF']['state'], complaints['QC']['state'])

        # The web page ending correctly never lifts the real water outage.
        self.assertEqual(snapshot['opening'][-1]['state'], 'closed')
        self.assertEqual(snapshot['opening'][-1]['reason'], 'water outage')
        self.assertEqual(snapshot['shift'][0]['state'], 'suspended')

        summary = notice_summary(self.db)
        by_case = {item['case_id']: item for item in summary}
        self.assertEqual(by_case['NF']['opening_state'], 'closed')
        self.assertEqual(len(by_case['NC']['evidence'][0]['epochs']), 4)
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_gap_only_capture_keeps_continuous_case_pending_and_lists_gaps(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        # Only epochs 2 and 4 were sampled; 1 and 3 are declared gaps.
        capture = load_capture(GAPS)
        self.assertEqual(sorted(capture['frames']), [2, 4])
        self.assertEqual(capture['gaps'], [1, 3])

        self.certify_both(GAPS, 'EVG', 'KG')
        snapshot = report(self.db)
        cases = {row['id']: row for row in snapshot['notice_case']}
        complaints = {row['id']: row for row in snapshot['complaint']}
        # Final contract still passes on epoch 4.
        self.assertEqual(cases['NF']['state'], 'aligned')
        self.assertEqual(complaints['QF']['state'], 'rejected')
        # Continuous contract must NOT be auto-written as aligned: pending gap.
        self.assertEqual(cases['NC']['state'], 'pending_gap')
        self.assertIn('[1, 3]', cases['NC']['reason'])
        self.assertEqual(complaints['QC']['state'], 'pending')
        self.assertIsNone(complaints['QC']['due_at'])
        certification = snapshot['notice_certification'][-1]
        self.assertEqual(json.loads(certification['missing_epochs_json']), [1, 3])
        self.assertEqual(certification['conclusive'], 0)
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_captured_wrong_epoch_survives_later_gaps_busy_machine(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        # Fixed shutters caught epoch 1 (wrong) and epoch 4 (right), missed 2/3.
        capture = load_capture(BUSY)
        self.assertEqual(sorted(capture['frames']), [1, 4])
        self.assertEqual(capture['gaps'], [2, 3])
        self.assertEqual(capture['frames'][1]['visual_text'], '正常开放')

        self.certify_both(BUSY, 'EVB', 'KB')
        snapshot = report(self.db)
        cases = {row['id']: row for row in snapshot['notice_case']}
        complaints = {row['id']: row for row in snapshot['complaint']}
        # The final epoch cannot testify for the whole window.
        self.assertEqual(cases['NF']['state'], 'aligned')
        self.assertEqual(cases['NC']['state'], 'mismatch')
        self.assertEqual(complaints['QC']['state'], 'confirmed')
        cert = next(row for row in snapshot['notice_certification'] if row['case_id'] == 'NC')
        self.assertEqual(json.loads(cert['wrong_epochs_json']), [1])
        self.assertEqual(json.loads(cert['missing_epochs_json']), [2, 3])

    def test_late_full_capture_resolves_the_gap_case_without_erasing_history(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        # First batch: gaps only, NC stays pending.
        self.certify_both(GAPS, 'EVG', 'KG', at='2026-04-01T10:45:00')
        # Later a complete re-capture of the same page arrives; recalculate.
        self.certify_both(FULL, 'EV2', 'K2', at='2026-04-01T11:05:00')
        snapshot = report(self.db)
        cases = {row['id']: row for row in snapshot['notice_case']}
        complaints = {row['id']: row for row in snapshot['complaint']}
        self.assertEqual(cases['NC']['state'], 'mismatch')
        self.assertEqual(complaints['QC']['state'], 'confirmed')
        self.assertEqual(complaints['QC']['due_at'], '2026-04-01T13:05:00')
        # The old pending-gap receipt remains queryable in the decision log.
        log_states = [row['state'] for row in snapshot['decision_log']
                      if row['object_id'] == 'NC']
        self.assertIn('pending_gap', log_states)
        self.assertIn('mismatch', log_states)
        self.assertEqual(len(snapshot['notice_evidence']), 4)  # two batches x two cases
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_same_evidence_replay_is_idempotent(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        self.certify_both(FULL, 'EV1', 'K1')
        first = report(self.db)
        # Re-import the identical event file: file-level replay shortcut.
        self.certify_both(FULL, 'EV1', 'K1')
        second = report(self.db)
        self.assertEqual(first['decision_log'], second['decision_log'])
        self.assertEqual(first['complaint'], second['complaint'])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def _mutable_copy(self, source_name):
        target = self.root / source_name
        shutil.copytree(EVIDENCE / source_name, target)
        return target

    def test_altered_screenshot_byte_hash_is_rejected(self):
        target = self._mutable_copy('notice-wc-a-full')
        png = target / 'epoch-1.png'
        png.write_bytes(png.read_bytes() + b' ')
        with self.assertRaises(EvidenceError):
            load_capture(str(target))

    def test_altered_dom_text_is_rejected_even_if_manifest_rewritten(self):
        target = self._mutable_copy('notice-wc-a-full')
        dom = target / 'epoch-1.html'
        tampered = dom.read_text(encoding='utf8').replace('正常开放', '暂停开放')
        dom.write_text(tampered, encoding='utf8')
        # Re-hash the DOM inside the manifest to mimic a clever forger: the raw
        # DOM/AX cross-check and label check must still catch it.
        manifest_path = target / 'capture.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        import hashlib
        frame = next(f for f in manifest['frames'] if f['epoch'] == 1)
        frame['dom_sha256'] = hashlib.sha256(dom.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
        with self.assertRaises(EvidenceError):
            load_capture(str(target))

    def test_cross_epoch_spliced_manifest_is_rejected(self):
        target = self._mutable_copy('notice-wc-a-full')
        manifest_path = target / 'capture.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        # Splice epoch 2's label onto epoch 1's original files: two different
        # epochs must never be stitched into one bundle.
        first = next(f for f in manifest['frames'] if f['epoch'] == 1)
        second = next(f for f in manifest['frames'] if f['epoch'] == 2)
        first['visual_text'] = second['visual_text']
        first['ax_static_text'] = second['ax_static_text']
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
        with self.assertRaises(EvidenceError):
            load_capture(str(target))

    def test_gap_list_that_hides_a_missing_epoch_is_rejected(self):
        target = self._mutable_copy('notice-wc-a-scheduled-gaps')
        manifest_path = target / 'capture.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        manifest['gaps'] = []  # pretend epochs 1 and 3 were never missing
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
        with self.assertRaises(EvidenceError):
            load_capture(str(target))

    def test_non_monotonic_epoch_capture_cannot_testify(self):
        target = self._mutable_copy('notice-wc-a-full')
        manifest_path = target / 'capture.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
        manifest['monotonic']['ok'] = False
        manifest['monotonic']['violations'] = [{'from': {'epoch': '3'}, 'to': {'epoch': '2'}}]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
        with self.assertRaises(EvidenceError):
            load_capture(str(target))

    def test_immutable_event_id_conflict_blocks_a_different_capture(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        self.events('notice-import', [dict(id='EV1', kind='notice_evidence',
            case_id='NF', capture_dir=GAPS, at='2026-04-01T10:45:00')])
        with self.assertRaises(ValueError):
            self.events('notice-import', [dict(id='EV1', kind='notice_evidence',
                case_id='NF', capture_dir=FULL, at='2026-04-01T10:46:00')])
        # The failed transaction left no partial second evidence row.
        self.assertEqual(len(report(self.db)['notice_evidence']), 1)

    def test_tampered_capture_event_rolls_back_whole_transaction(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        target = self._mutable_copy('notice-wc-a-full')
        (target / 'epoch-2.png').write_bytes(b'not-a-real-png')
        with self.assertRaises(EvidenceError):
            self.events('notice-import', [dict(id='EVX', kind='notice_evidence',
                case_id='NF', capture_dir=str(target), at='2026-04-01T10:45:00')])
        snapshot = report(self.db)
        self.assertEqual(snapshot['notice_evidence'], [])
        self.assertEqual(snapshot['notice_epoch_evidence'], [])
        # The rejected event itself was rolled back too.
        self.assertNotIn('EVX', {row['id'] for row in snapshot['event']})
        # The case is untouched and still awaiting its first review.
        self.assertEqual(snapshot['notice_case'][0]['state'], 'pending_review')

    def test_manual_review_remains_a_working_regression_path(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        # Browser certification says the continuous contract mismatches.
        self.certify_both(FULL, 'EV1', 'K1', at='2026-04-01T10:45:00')
        complaints = {row['id']: row for row in report(self.db)['complaint']}
        self.assertEqual(complaints['QC']['state'], 'confirmed')
        # A later independent manual review supersedes the browser verdict.
        self.events('notice-import', [dict(id='RC', kind='notice_review', case_id='NC',
            inspector='inspector', at='2026-04-01T11:30:00', mismatch=False)])
        snapshot = report(self.db)
        complaints = {row['id']: row for row in snapshot['complaint']}
        cases = {row['id']: row for row in snapshot['notice_case']}
        self.assertEqual(complaints['QC']['state'], 'rejected')
        self.assertEqual(cases['NC']['state'], 'aligned')
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])

    def test_withdrawn_inconclusive_recertification_returns_complaint_to_pending(self):
        self.seed_ledger()
        self.open_cases_and_complaints()
        # Confirmed first.
        self.certify_both(FULL, 'EV1', 'K1', at='2026-04-01T10:45:00')
        self.assertEqual(report(self.db)['complaint'][1]['state'], 'confirmed')
        # A later re-published capture only retains epochs 2 and 4: the breach
        # evidence is withdrawn from the newest lineage and the case reopens.
        self.certify_both(GAPS, 'EV3', 'K3', at='2026-04-01T12:00:00')
        snapshot = report(self.db)
        complaints = {row['id']: row for row in snapshot['complaint']}
        cases = {row['id']: row for row in snapshot['notice_case']}
        self.assertEqual(cases['NC']['state'], 'pending_gap')
        self.assertEqual(complaints['QC']['state'], 'pending')
        self.assertIsNone(complaints['QC']['due_at'])
        # Historical confirmed receipt is still queryable.
        self.assertIn('confirmed', [row['state'] for row in snapshot['decision_log']
                                    if row['object_id'] == 'QC'])
        self.assertTrue(audit(self.db, HANDLERS)['replay_ok'])


if __name__ == '__main__':
    unittest.main()
