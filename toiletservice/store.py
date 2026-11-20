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
CREATE TABLE IF NOT EXISTS complaint(id TEXT PRIMARY KEY,facility_id TEXT NOT NULL,at TEXT NOT NULL,category TEXT NOT NULL,reporter TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,due_at TEXT,notice_case_id TEXT);
CREATE TABLE IF NOT EXISTS notice_case(id TEXT PRIMARY KEY,facility_id TEXT NOT NULL,opening_version INTEGER NOT NULL,contract_kind TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notice_review(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,inspector TEXT NOT NULL,at TEXT NOT NULL,mismatch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS verification(id TEXT PRIMARY KEY,complaint_id TEXT NOT NULL,inspector TEXT NOT NULL,at TEXT NOT NULL,confirmed INTEGER NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remedy(id TEXT PRIMARY KEY,complaint_id TEXT NOT NULL,worker TEXT NOT NULL,at TEXT NOT NULL,inspector TEXT NOT NULL,passed INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS complaint_link(complaint_id TEXT NOT NULL,shift_id TEXT,defect_id TEXT,link_reason TEXT NOT NULL,PRIMARY KEY(complaint_id,shift_id,defect_id));
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
