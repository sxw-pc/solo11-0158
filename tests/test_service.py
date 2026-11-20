import json
import tempfile
import unittest
from pathlib import Path
import toiletservice
from toiletservice.store import connect
from toiletservice.__main__ import import_file,HANDLERS
from toiletservice.report import report,audit

ROOT=Path(__file__).resolve().parents[1]

class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/'test.sqlite')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def fixture(self,command,name):return import_file(self.db,command,ROOT/'fixtures'/name)
    def events(self,command,events):
        p=Path(self.temp.name)/'events.json'
        p.write_text(json.dumps({'events':events}))
        return import_file(self.db,command,p)
    def base(self):self.fixture('facility-import','facility-ok.json')
    def test_module_path(self):
        print('module file:',toiletservice.__file__)
        self.assertEqual(Path(toiletservice.__file__).resolve().parents[1],ROOT)
    def test_three_business_ledgers(self):
        self.base();self.fixture('shift-import','shift-ok.json');self.fixture('complaint-import','complaint-ok.json')
        r=report(self.db)
        self.assertEqual(r['opening'][-1]['state'],'open')
        self.assertEqual(r['shift'][0]['state'],'disputed')
        self.assertEqual(r['complaint'][0]['state'],'closed')
        self.assertEqual(r['complaint_link'][0]['shift_id'],'S1')
        self.assertEqual(len(r['source_file']),3)
    def test_water_fault_closes_facility_and_suspends_pending_shift(self):
        self.base()
        self.events('shift-import',[{'id':'C2','kind':'contract','facility_id':'wc-a','vendor':'clean','starts':'2026-04-01T08:00:00','ends':'2026-04-01T20:00:00','minutes':60,'minimum_supply':1,'remedy_minutes':120},
          {'id':'S2','kind':'shift','contract_id':'C2','worker':'w2','check_in':'2026-04-01T09:00:00','check_out':'2026-04-01T10:00:00','supply':2}])
        self.fixture('facility-import','facility-bad.json')
        r=report(self.db)
        self.assertEqual(r['opening'][-1]['state'],'closed')
        self.assertEqual(r['shift'][0]['state'],'suspended')
    def test_accessible_fault_limits_service_and_reopens_complaint(self):
        self.base();self.fixture('shift-import','shift-ok.json')
        self.events('complaint-import',[{'id':'Q3','kind':'complaint','facility_id':'wc-a','at':'2026-04-01T12:00:00','category':'accessible','reporter':'r'},
          {'id':'V3','kind':'verify','complaint_id':'Q3','inspector':'ins','at':'2026-04-01T12:10:00','confirmed':True},
          {'id':'R3','kind':'remedy','complaint_id':'Q3','worker':'w','at':'2026-04-01T12:20:00','inspector':'i','passed':True}])
        self.events('facility-import',[{'id':'D3','kind':'defect','facility_id':'wc-a','defect_kind':'accessible','stall_count':1,'accessible_loss':1,'opened_at':'2026-04-01T12:30:00'}])
        r=report(self.db)
        self.assertEqual(r['opening'][-1]['state'],'limited')
        self.assertEqual(r['opening'][-1]['accessible_available'],0)
        self.assertEqual(r['complaint'][0]['state'],'reopened')
    def test_fault_repair_restores_opening(self):
        self.base();self.fixture('facility-import','facility-bad.json')
        self.events('facility-import',[{'id':'FR1','kind':'repair','defect_id':'D1','closed_at':'2026-04-01T11:00:00','witness':'inspector-x'}])
        self.assertEqual(report(self.db)['opening'][-1]['state'],'open')
    def test_worker_overlap_is_not_two_completed_shifts(self):
        self.base();self.fixture('shift-import','shift-ok.json');self.fixture('shift-import','shift-bad.json')
        self.assertEqual(report(self.db)['shift'][1]['state'],'conflict')
    def test_self_patrol_fails(self):
        self.base();self.events('shift-import',[{'id':'C3','kind':'contract','facility_id':'wc-a','vendor':'clean','starts':'2026-04-01T08:00:00','ends':'2026-04-01T20:00:00','minutes':30,'minimum_supply':1,'remedy_minutes':120},
          {'id':'S3','kind':'shift','contract_id':'C3','worker':'w','check_in':'2026-04-01T09:00:00','check_out':'2026-04-01T10:00:00','supply':2},
          {'id':'P3','kind':'patrol','shift_id':'S3','inspector':'w','score':95,'at':'2026-04-01T10:10:00'}])
        self.assertEqual(report(self.db)['shift'][0]['state'],'pending_patrol')
        self.assertEqual(report(self.db)['patrol'][0]['state'],'invalid')
    def test_late_remedy_and_self_check(self):
        self.base();self.fixture('shift-import','shift-ok.json');self.fixture('complaint-import','complaint-bad.json')
        self.assertEqual(report(self.db)['remedy'][0]['state'],'invalid')
        self.events('complaint-import',[{'id':'R4','kind':'remedy','complaint_id':'Q2','worker':'w','at':'2026-04-01T16:30:00','inspector':'i','passed':True}])
        self.assertEqual(report(self.db)['complaint'][0]['state'],'late')
    def test_contract_sla_changes_remedy_deadline(self):
        self.base()
        self.events('shift-import',[{'id':'CS','kind':'contract','facility_id':'wc-a','vendor':'fast-clean','starts':'2026-04-01T08:00:00','ends':'2026-04-01T20:00:00','minutes':30,'minimum_supply':1,'remedy_minutes':30}])
        self.events('complaint-import',[{'id':'QS','kind':'complaint','facility_id':'wc-a','at':'2026-04-01T12:00:00','category':'supply','reporter':'resident'},
          {'id':'VS','kind':'verify','complaint_id':'QS','inspector':'inspector','at':'2026-04-01T12:10:00','confirmed':True,'reason':'empty dispenser'},
          {'id':'RS','kind':'remedy','complaint_id':'QS','worker':'worker','at':'2026-04-01T12:50:00','inspector':'inspector','passed':True}])
        r=report(self.db)
        self.assertEqual(r['complaint'][0]['due_at'],'2026-04-01T12:40:00')
        self.assertEqual(r['complaint'][0]['state'],'late')
    def test_no_contract_preserves_unpriced_complaint(self):
        self.base()
        self.events('complaint-import',[{'id':'QN','kind':'complaint','facility_id':'wc-a','at':'2026-04-01T12:00:00','category':'supply','reporter':'resident'},
          {'id':'VN','kind':'verify','complaint_id':'QN','inspector':'inspector','at':'2026-04-01T12:10:00','confirmed':True,'reason':'empty dispenser'}])
        self.assertEqual(report(self.db)['complaint'][0]['state'],'pending_contract')
    def test_verified_water_complaint_changes_opening_and_remedy_restores(self):
        self.base();self.fixture('shift-import','shift-ok.json')
        self.events('complaint-import',[{'id':'QW','kind':'complaint','facility_id':'wc-a','at':'2026-04-01T12:00:00','category':'water','reporter':'resident'},
          {'id':'VW','kind':'verify','complaint_id':'QW','inspector':'onsite','at':'2026-04-01T12:10:00','confirmed':True,'reason':'no water'}])
        r=report(self.db)
        self.assertEqual(r['opening'][-1]['state'],'closed')
        self.assertEqual(r['complaint_link'][0]['defect_id'],'verified-VW')
        self.events('complaint-import',[{'id':'RW','kind':'remedy','complaint_id':'QW','worker':'plumber','at':'2026-04-01T13:00:00','inspector':'onsite','passed':True}])
        r=report(self.db)
        self.assertEqual(r['opening'][-1]['state'],'open')
        self.assertEqual(r['complaint'][0]['state'],'closed')
    def test_file_replay_and_conflict(self):
        self.base();self.assertEqual(self.fixture('facility-import','facility-ok.json')[0]['result'],'file_replayed')
        with self.assertRaises(ValueError):self.events('facility-import',[{'id':'F1','kind':'facility','facility_id':'wc-a','zone':'other','stalls':9,'accessible':1,'opens':'08:00:00','closes':'20:00:00','version':2}])
        self.assertEqual(len(report(self.db)['source_file']),1)
    def test_raw_file_audit_detects_mutated_decision(self):
        self.base();self.fixture('shift-import','shift-ok.json');self.fixture('complaint-import','complaint-ok.json')
        self.assertTrue(audit(self.db,HANDLERS)['replay_ok'])
        self.db.execute("UPDATE shift SET state='forged' WHERE id='S1'");self.db.commit()
        self.assertEqual(audit(self.db,HANDLERS)['mismatches'],['shift'])

if __name__=='__main__':unittest.main()
