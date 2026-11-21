import json
import tempfile
import unittest
from pathlib import Path

from toiletservice.store import connect, all_rows, one
from toiletservice.__main__ import import_file, HANDLERS
from toiletservice.report import report, audit
from toiletservice import water_witness

ROOT = Path(__file__).resolve().parents[1]
F = ROOT / 'fixtures'


class WaterWitnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'test.sqlite')
        self.evidence_base()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def fixture(self, command, name):
        return import_file(self.db, command, F / name)

    def events(self, command, events):
        p = Path(self.temp.name) / 'events.json'
        p.write_text(json.dumps({'events': events}))
        return import_file(self.db, command, p)

    def evidence_base(self):
        self.fixture('facility-import', 'water-facilities.json')
        self.fixture('water-import', 'water-calibration.json')
        self.fixture('water-import', 'water-meters.json')

    def pending_shifts(self):
        self.fixture('shift-import', 'water-shifts.json')

    def anomaly_coverage(self, anomaly_id):
        return next(c for c in water_witness.coverage(self.db) if c['anomaly_id'] == anomaly_id)

    def open_water_defects(self, facility_id):
        return all_rows(self.db, "SELECT * FROM defect WHERE facility_id=? AND kind='water' AND closed_at IS NULL",
                        (facility_id,))

    # 1. master meter alone: two compatible explanations, neither facility closes
    def test_master_only_keeps_two_compatible_explanations_and_closes_nothing(self):
        self.pending_shifts()
        out = self.fixture('water-import', 'water-anomaly-night.json')
        self.assertEqual([o['result'] for o in out], ['ambiguous'])
        cov = self.anomaly_coverage('AN1')
        self.assertEqual(cov['state'], 'ambiguous')
        self.assertIsNone(cov['attribution'])
        for fac in cov['facilities']:
            self.assertEqual(len(fac['witnesses']), 1)
            self.assertEqual(fac['witnesses'][0]['state'], 'compatible')
            self.assertIsNone(fac['signed_fault'])
            self.assertEqual(self.open_water_defects(fac['facility_id']), [])
        self.assertEqual(one(self.db, "SELECT state FROM opening WHERE facility_id='wc-a' ORDER BY id DESC LIMIT 1")
                         ['state'], 'open')
        self.assertEqual(one(self.db, "SELECT state FROM opening WHERE facility_id='wc-b' ORDER BY id DESC LIMIT 1")
                         ['state'], 'open')
        self.assertEqual({r['state'] for r in all_rows(self.db, 'SELECT state FROM shift')}, {'pending_patrol'})

    # 2. same-calibration branch reads covering the period decide via conservation
    def test_branch_reads_attribute_to_a_and_suspend_only_a_shift(self):
        self.pending_shifts()
        self.fixture('water-import', 'water-anomaly-reception.json')
        self.fixture('water-import', 'water-branch-reads.json')
        cov = self.anomaly_coverage('AN2')
        self.assertEqual(cov['state'], 'attributed')
        self.assertEqual(cov['attribution'], 'wc-a')
        a = next(f for f in cov['facilities'] if f['facility_id'] == 'wc-a')
        b = next(f for f in cov['facilities'] if f['facility_id'] == 'wc-b')
        self.assertEqual(a['branch_reading']['value'], 5)
        self.assertEqual(a['branch_reading']['excess'], 3)
        self.assertEqual(a['signed_fault']['state'], 'signed')
        self.assertEqual(b['branch_reading']['value'], 2)
        self.assertEqual(b['branch_reading']['excess'], 0)
        self.assertIsNone(b['signed_fault'])
        self.assertEqual(len(self.open_water_defects('wc-a')), 1)
        self.assertEqual(self.open_water_defects('wc-b'), [])
        self.assertEqual(one(self.db, "SELECT state FROM opening WHERE facility_id='wc-a' ORDER BY id DESC LIMIT 1")
                         ['state'], 'closed')
        self.assertEqual(one(self.db, "SELECT state FROM opening WHERE facility_id='wc-b' ORDER BY id DESC LIMIT 1")
                         ['state'], 'open')
        shifts = {r['id']: r['state'] for r in all_rows(self.db, 'SELECT id,state FROM shift')}
        self.assertEqual(shifts['WS1'], 'suspended')
        self.assertEqual(shifts['WS2'], 'pending_patrol')
        self.assertEqual(a['signed_fault']['affected_shifts'], ['WS1'])

    # 3. isolation test 1/6/3/1 with offset 1 still attributes to A
    def test_isolation_test_attributes_to_a(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        self.assertEqual(self.fixture('water-import', 'water-isolation-valid.json')[0]['result'], 'attributed')
        cov = self.anomaly_coverage('AN3')
        self.assertEqual(cov['attribution'], 'wc-a')
        self.assertEqual(len(self.open_water_defects('wc-a')), 1)
        test = cov['valid_tests'][0]
        self.assertEqual((test['null_start'], test['read_a'], test['read_b'], test['null_end']), (1, 6, 3, 1))
        self.assertEqual(test['null_offset'], 1)
        self.assertEqual(test['state'], 'valid')

    # 4. no-load end 2: the A conclusion cannot be issued for this receipt
    def test_null_load_drift_keeps_raw_receipt_and_refuses_fault(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        self.assertEqual(self.fixture('water-import', 'water-isolation-null-drift.json')[0]['result'], 'invalid')
        cov = self.anomaly_coverage('AN3')
        self.assertIsNone(cov['attribution'])
        self.assertEqual(cov['state'], 'ambiguous')
        self.assertEqual(cov['valid_tests'][0]['state'], 'invalid')
        self.assertIn('no-load', cov['valid_tests'][0]['reason'])
        self.assertEqual(self.open_water_defects('wc-a'), [])
        self.assertEqual(self.open_water_defects('wc-b'), [])

    # 5. closure order revoked during test: retain receipt, demand recertification
    def test_revoked_closure_order_invalidates_receipt(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        self.assertEqual(self.fixture('water-import', 'water-isolation-revoked.json')[0]['result'], 'invalid')
        row = one(self.db, "SELECT * FROM water_test WHERE event_id='IT3'")
        self.assertEqual(row['state'], 'invalid')
        self.assertEqual(row['closure_revoked'], 1)
        self.assertIn('closure order revoked', row['reason'])
        self.assertEqual(self.open_water_defects('wc-a'), [])

    # 6. mixing calibration versions is rejected, not silently merged
    def test_calibration_version_mix_is_rejected(self):
        self.fixture('water-import', 'water-anomaly-reception.json')
        with self.assertRaises(ValueError):
            self.events('water-import', [
                {'id': 'BR-MIX', 'kind': 'branch_read', 'anomaly_id': 'AN2', 'facility_id': 'wc-a',
                 'meter_id': 'M-A', 'calibration_id': 'CAL2', 'at': '2026-04-03T09:30:00',
                 'unit': 'm3', 'value': 5}])
        self.assertEqual(one(self.db, "SELECT COUNT(*) c FROM water_reading")['c'], 0)

    def test_isolation_test_mixing_calibrations_is_invalid(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        res = self.events('water-import', [{
            'id': 'IT-MIX', 'kind': 'isolation_test', 'anomaly_id': 'AN3', 'test_ref': 'T-MIX',
            'stage_revision': 'r1', 'at': '2026-04-04T03:30:00', 'null_offset': 1,
            'closure_revoked': False, 'records': [
                {'stage': 'null_start', 'test_ref': 'T-MIX', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1},
                {'stage': 'a_only', 'test_ref': 'T-MIX', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 6},
                {'stage': 'b_only', 'test_ref': 'T-MIX', 'stage_revision': 'r1',
                 'calibration_id': 'CAL2', 'unit': 'm3', 'value': 3},
                {'stage': 'null_end', 'test_ref': 'T-MIX', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1}]}])
        self.assertEqual(res[0]['result'], 'invalid')
        self.assertEqual(self.open_water_defects('wc-a'), [])

    # 7. duplicate import of the same synthetic original is a replay, not a second curve
    def test_duplicate_source_import_is_replayed_once(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        self.fixture('water-import', 'water-isolation-valid.json')
        again = self.fixture('water-import', 'water-isolation-valid.json')
        self.assertEqual(again[0]['result'], 'file_replayed')
        self.assertEqual(one(self.db, 'SELECT COUNT(*) c FROM water_test')['c'], 1)
        self.assertEqual(one(self.db, "SELECT COUNT(*) c FROM defect WHERE kind='water'")['c'], 1)

    def test_cherry_picked_readings_from_different_tests_cannot_form_a_curve(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        res = self.events('water-import', [{
            'id': 'IT-PICK', 'kind': 'isolation_test', 'anomaly_id': 'AN3', 'test_ref': 'T-PICK',
            'stage_revision': 'r1', 'at': '2026-04-04T03:30:00', 'null_offset': 1,
            'closure_revoked': False, 'records': [
                {'stage': 'null_start', 'test_ref': 'T-PICK', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1},
                {'stage': 'a_only', 'test_ref': 'T-OTHER', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 6},
                {'stage': 'b_only', 'test_ref': 'T-PICK', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 3},
                {'stage': 'null_end', 'test_ref': 'T-PICK', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1}]}])
        self.assertEqual(res[0]['result'], 'invalid')
        self.assertIn('same actual test', one(self.db, "SELECT reason FROM water_test WHERE event_id='IT-PICK'")
                      ['reason'])

    # 8. late correction changes the conclusion; old conclusion and suspension stay reviewable
    def test_late_correction_revises_attribution_and_keeps_history(self):
        self.pending_shifts()
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        self.fixture('water-import', 'water-isolation-valid.json')
        self.assertEqual(one(self.db, "SELECT state FROM shift WHERE id='WS1'")['state'], 'suspended')
        # late correction under the same actual test, new stage revision: branches swap
        res = self.events('water-import', [{
            'id': 'IT1-R2', 'kind': 'isolation_test', 'anomaly_id': 'AN3', 'test_ref': 'T-ISO-1',
            'stage_revision': 'r2', 'at': '2026-04-04T04:00:00', 'null_offset': 1,
            'closure_revoked': False, 'records': [
                {'stage': 'null_start', 'test_ref': 'T-ISO-1', 'stage_revision': 'r2',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1},
                {'stage': 'a_only', 'test_ref': 'T-ISO-1', 'stage_revision': 'r2',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 3},
                {'stage': 'b_only', 'test_ref': 'T-ISO-1', 'stage_revision': 'r2',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 6},
                {'stage': 'null_end', 'test_ref': 'T-ISO-1', 'stage_revision': 'r2',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1}]}])
        self.assertEqual(res[0]['result'], 'attributed')
        cov = self.anomaly_coverage('AN3')
        self.assertEqual(cov['attribution'], 'wc-b')
        # A's old defect is closed with the recantation witness and remains on record
        old = one(self.db, "SELECT * FROM defect WHERE id='water-AN3-wc-a'")
        self.assertIsNotNone(old['closed_at'])
        self.assertEqual(len(self.open_water_defects('wc-a')), 0)
        self.assertEqual(len(self.open_water_defects('wc-b')), 1)
        # old signed fault row kept with withdrawn state; a new signed row carries the B attribution
        faults = {r['id']: r['state'] for r in all_rows(self.db, 'SELECT id,state FROM water_fault')}
        self.assertEqual(faults['wf-AN3'], 'withdrawn')
        new_signed = [i for i, s in faults.items() if s == 'signed']
        self.assertEqual(len(new_signed), 1)
        withdrawn = [r for r in all_rows(self.db, 'SELECT * FROM water_fault') if r['state'] == 'withdrawn']
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(withdrawn[0]['facility_id'], 'wc-a')
        self.assertEqual(one(self.db, "SELECT facility_id FROM water_fault WHERE id=?", (new_signed[0],))
                         ['facility_id'], 'wc-b')
        self.assertEqual(one(self.db, "SELECT state FROM shift WHERE id='WS1'")['state'], 'suspended')
        self.assertEqual(one(self.db, "SELECT state FROM shift WHERE id='WS2'")['state'], 'suspended')
        tests = all_rows(self.db, 'SELECT stage_revision,superseded FROM water_test ORDER BY id')
        self.assertEqual([(t['stage_revision'], t['superseded']) for t in tests], [('r1', 1), ('r2', 0)])
        supersede_log = one(self.db, "SELECT * FROM decision_log WHERE state='superseded' ORDER BY id LIMIT 1")
        self.assertIsNotNone(supersede_log)

    # 9. branch reads outside reception hours are refused; service is not paused
    def test_branch_read_outside_reception_hours_is_refused(self):
        self.fixture('water-import', 'water-anomaly-reception.json')
        with self.assertRaises(ValueError):
            self.events('water-import', [
                {'id': 'BR-NIGHT', 'kind': 'branch_read', 'anomaly_id': 'AN2', 'facility_id': 'wc-a',
                 'meter_id': 'M-A', 'calibration_id': 'CAL1', 'at': '2026-04-03T06:30:00',
                 'unit': 'm3', 'value': 5}])
        self.assertEqual(one(self.db, 'SELECT COUNT(*) c FROM water_reading')['c'], 0)
        self.assertEqual(one(self.db, "SELECT state FROM opening WHERE facility_id='wc-a' ORDER BY id DESC LIMIT 1")
                         ['state'], 'open')

    # 10. conservation failure: evidence supports a shared-main anomaly only
    def test_conservation_failure_outputs_shared_scope_without_two_faults(self):
        self.fixture('water-import', 'water-anomaly-reception.json')
        self.events('water-import', [
            {'id': 'BR-AX', 'kind': 'branch_read', 'anomaly_id': 'AN2', 'facility_id': 'wc-a',
             'meter_id': 'M-A', 'calibration_id': 'CAL1', 'at': '2026-04-03T09:30:00', 'unit': 'm3', 'value': 4},
            {'id': 'BR-BX', 'kind': 'branch_read', 'anomaly_id': 'AN2', 'facility_id': 'wc-b',
             'meter_id': 'M-B', 'calibration_id': 'CAL1', 'at': '2026-04-03T09:30:00', 'unit': 'm3', 'value': 1}])
        cov = self.anomaly_coverage('AN2')
        self.assertEqual(cov['state'], 'shared_scope')
        self.assertIsNone(cov['attribution'])
        self.assertEqual({f['witnesses'][-1]['state'] for f in cov['facilities']}, {'shared_scope'})
        self.assertEqual(self.open_water_defects('wc-a'), [])
        self.assertEqual(self.open_water_defects('wc-b'), [])

    # 11. malformed receipts are retained as invalid or rejected without a fault
    def test_malformed_isolation_receipt_does_not_crash_or_sign(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        res = self.events('water-import', [{
            'id': 'IT-BAD', 'kind': 'isolation_test', 'anomaly_id': 'AN3', 'test_ref': 'T-BAD',
            'stage_revision': 'r1', 'at': '2026-04-04T03:30:00', 'null_offset': 1,
            'closure_revoked': False, 'records': [
                {'stage': 'null_start', 'test_ref': 'T-BAD', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 1},
                {'stage': 'a_only', 'test_ref': 'T-BAD', 'stage_revision': 'r1',
                 'calibration_id': 'CAL1', 'unit': 'm3', 'value': 'not-a-number'}]}])
        self.assertEqual(res[0]['result'], 'invalid')
        self.assertEqual(self.open_water_defects('wc-a'), [])

    def test_missing_receipt_fields_roll_back_as_value_error(self):
        self.fixture('water-import', 'water-anomaly-maintenance.json')
        with self.assertRaises(ValueError):
            self.events('water-import', [{'id': 'IT-MISSING', 'kind': 'isolation_test',
                                          'anomaly_id': 'AN3', 'test_ref': 'T-MISS'}])
        self.assertEqual(one(self.db, 'SELECT COUNT(*) c FROM water_test')['c'], 0)

    # 12. full audit replay from raw source bytes stays identical
    def test_raw_replay_audit_matches_with_water_ledger(self):
        self.pending_shifts()
        self.fixture('water-import', 'water-anomaly-reception.json')
        self.fixture('water-import', 'water-branch-reads.json')
        verdict = audit(self.db, HANDLERS)
        self.assertTrue(verdict['replay_ok'], verdict['mismatches'])


if __name__ == '__main__':
    unittest.main()
