import sqlite3

SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS facility(id TEXT PRIMARY KEY,zone TEXT NOT NULL,stalls INTEGER NOT NULL,accessible INTEGER NOT NULL,opens TEXT NOT NULL,closes TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS defect(id TEXT PRIMARY KEY,facility_id TEXT NOT NULL,kind TEXT NOT NULL,stall_count INTEGER NOT NULL,accessible_loss INTEGER NOT NULL,opened_at TEXT NOT NULL,closed_at TEXT,witness TEXT);
CREATE TABLE IF NOT EXISTS opening(id INTEGER PRIMARY KEY,facility_id TEXT NOT NULL,state TEXT NOT NULL,available INTEGER NOT NULL,accessible_available INTEGER NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS contract(id TEXT PRIMARY KEY,facility_id TEXT NOT NULL,vendor TEXT NOT NULL,starts TEXT NOT NULL,ends TEXT NOT NULL,minutes INTEGER NOT NULL,minimum_supply INTEGER NOT NULL,remedy_minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shift(id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,worker TEXT NOT NULL,check_in TEXT NOT NULL,check_out TEXT NOT NULL,supply INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS patrol(id TEXT PRIMARY KEY,shift_id TEXT NOT NULL,inspector TEXT NOT NULL,score INTEGER NOT NULL,at TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS complaint(id TEXT PRIMARY KEY,facility_id TEXT NOT NULL,at TEXT NOT NULL,category TEXT NOT NULL,reporter TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,due_at TEXT);
CREATE TABLE IF NOT EXISTS verification(id TEXT PRIMARY KEY,complaint_id TEXT NOT NULL,inspector TEXT NOT NULL,at TEXT NOT NULL,confirmed INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remedy(id TEXT PRIMARY KEY,complaint_id TEXT NOT NULL,worker TEXT NOT NULL,at TEXT NOT NULL,inspector TEXT NOT NULL,passed INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS complaint_link(complaint_id TEXT NOT NULL,shift_id TEXT,defect_id TEXT,link_reason TEXT NOT NULL,PRIMARY KEY(complaint_id,shift_id,defect_id));
CREATE TABLE IF NOT EXISTS water_calibration(id TEXT PRIMARY KEY,unit TEXT NOT NULL,issued_at TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS water_meter(id TEXT PRIMARY KEY,role TEXT NOT NULL,master_id TEXT,facility_id TEXT,unit TEXT NOT NULL,calibration_id TEXT NOT NULL,covers_from TEXT,covers_to TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS water_baseline(id TEXT PRIMARY KEY,facility_id TEXT NOT NULL,master_id TEXT,expected_use INTEGER NOT NULL,unit TEXT NOT NULL,period_from TEXT NOT NULL,period_to TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS water_pipeline(id TEXT PRIMARY KEY,master_id TEXT NOT NULL,facility_ids TEXT NOT NULL,shared INTEGER NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS water_anomaly(id TEXT PRIMARY KEY,master_id TEXT NOT NULL,period_from TEXT NOT NULL,period_to TEXT NOT NULL,unit TEXT NOT NULL,master_reading INTEGER NOT NULL,expected_total INTEGER NOT NULL,excess INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,attribution TEXT);
CREATE TABLE IF NOT EXISTS water_reading(id TEXT PRIMARY KEY,anomaly_id TEXT NOT NULL,facility_id TEXT NOT NULL,meter_id TEXT NOT NULL,calibration_id TEXT NOT NULL,at TEXT NOT NULL,unit TEXT NOT NULL,value INTEGER NOT NULL,baseline INTEGER NOT NULL,excess INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS water_test(id INTEGER PRIMARY KEY,event_id TEXT UNIQUE,anomaly_id TEXT NOT NULL,test_ref TEXT NOT NULL,stage_revision TEXT NOT NULL,calibration_id TEXT, null_start INTEGER,read_a INTEGER,read_b INTEGER,null_end INTEGER,null_offset INTEGER,closure_revoked INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,superseded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS water_witness(id INTEGER PRIMARY KEY,anomaly_id TEXT NOT NULL,method TEXT NOT NULL,attribution TEXT,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,superseded INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS water_fault(id TEXT PRIMARY KEY,anomaly_id TEXT NOT NULL,facility_id TEXT NOT NULL,defect_id TEXT,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT);
CREATE TABLE IF NOT EXISTS water_fault_shift(fault_id TEXT NOT NULL,shift_id TEXT NOT NULL,PRIMARY KEY(fault_id,shift_id));
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,system TEXT NOT NULL,object_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,event_id TEXT,at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

def connect(path):
    db=sqlite3.connect(path)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db

def one(db,sql,args=()):return db.execute(sql,args).fetchone()
def all_rows(db,sql,args=()):return db.execute(sql,args).fetchall()
def log(db,system,key,state,reason,event_id=None):
    db.execute('INSERT INTO decision_log(system,object_id,state,reason,event_id) VALUES(?,?,?,?,?)',
               (system,key,state,reason,event_id))
