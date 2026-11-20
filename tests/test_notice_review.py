import json
import tempfile
import unittest
from pathlib import Path

from toiletservice.__main__ import HANDLERS, import_file
from toiletservice.report import report, audit
from toiletservice.store import connect


ROOT = Path(__file__).resolve().parents[1]


class NoticeReviewTest(unittest.TestCase):
    def test_manual_notice_result_changes_linked_complaint_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = connect(root / 'notice.sqlite')

            def events(command, items):
                file = root / f'{len(list(root.glob("*.json")))}.json'
                file.write_text(json.dumps({'events': items}), encoding='utf8')
                import_file(db, command, file)

            import_file(db, 'facility-import', ROOT / 'fixtures' / 'facility-ok.json')
            import_file(db, 'facility-import', ROOT / 'fixtures' / 'facility-bad.json')
            events('shift-import', [dict(id='CN', kind='contract', facility_id='wc-a',
                vendor='clean', starts='2026-04-01T08:00:00', ends='2026-04-01T20:00:00',
                minutes=60, minimum_supply=1, remedy_minutes=120)])
            events('notice-import', [dict(id='NF', kind='notice_case', facility_id='wc-a',
                opening_version=2, contract_kind='final'),
                dict(id='NC', kind='notice_case', facility_id='wc-a',
                opening_version=2, contract_kind='continuous')])
            events('complaint-import', [dict(id='QF', kind='complaint', facility_id='wc-a',
                at='2026-04-01T10:30:00', category='notice', reporter='reader-a', notice_case_id='NF'),
                dict(id='QC', kind='complaint', facility_id='wc-a',
                at='2026-04-01T10:30:00', category='notice', reporter='reader-b', notice_case_id='NC')])
            events('notice-import', [dict(id='RF', kind='notice_review', case_id='NF',
                inspector='inspector', at='2026-04-01T10:40:00', mismatch=False),
                dict(id='RC', kind='notice_review', case_id='NC',
                inspector='inspector', at='2026-04-01T10:40:00', mismatch=True)])
            snapshot = report(db)
            complaints = {row['id']: row for row in snapshot['complaint']}
            self.assertEqual((complaints['QF']['state'], complaints['QC']['state']),
                             ('rejected', 'confirmed'))
            self.assertEqual(complaints['QC']['due_at'], '2026-04-01T12:40:00')
            self.assertEqual(snapshot['opening'][-1]['state'], 'closed')
            self.assertTrue(audit(db, HANDLERS)['replay_ok'])
            db.close()


if __name__ == '__main__':
    unittest.main()
