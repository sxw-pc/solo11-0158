from datetime import datetime
from .store import one,all_rows,log

def contract(db,e):
    if e['starts']>=e['ends'] or int(e['minutes'])<=0 or int(e['minimum_supply'])<0:
        raise ValueError('invalid contract time or threshold')
    if not one(db,'SELECT id FROM facility WHERE id=?',(e['facility_id'],)):raise ValueError('unknown facility')
    db.execute('INSERT INTO contract(id,facility_id,vendor,starts,ends,minutes,minimum_supply) VALUES(?,?,?,?,?,?,?)',
               (e['id'],e['facility_id'],e['vendor'],e['starts'],e['ends'],int(e['minutes']),int(e['minimum_supply'])))
    log(db,'shift',e['id'],'contracted','coverage signed',e['id'])
    return 'contracted'

def evaluate(db,e):
    c=one(db,'SELECT * FROM contract WHERE id=?',(e['contract_id'],))
    if not c:return 'blocked','no signed contract'
    if not c['starts']<=e['check_in']<e['check_out']<=c['ends']:return 'blocked','shift outside contract window'
    duration=(datetime.fromisoformat(e['check_out'])-datetime.fromisoformat(e['check_in'])).total_seconds()/60
    if duration<c['minutes']:return 'short','contract minutes not met'
    if int(e['supply'])<c['minimum_supply']:return 'short','minimum supply not met'
    overlap=all_rows(db,"SELECT * FROM shift WHERE worker=? AND state IN ('accepted','pending_patrol') AND check_in<? AND check_out>?",
                     (e['worker'],e['check_out'],e['check_in']))
    if overlap:return 'conflict','worker booked at overlapping facility'
    opening=one(db,'SELECT * FROM opening WHERE facility_id=? ORDER BY id DESC LIMIT 1',(c['facility_id'],))
    if opening and opening['state']=='closed':return 'suspended','facility closed during service'
    return 'pending_patrol','requires independent cleanliness inspection'

def shift(db,e):
    state,reason=evaluate(db,e)
    db.execute('INSERT INTO shift(id,contract_id,worker,check_in,check_out,supply,state,reason) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'],e['contract_id'],e['worker'],e['check_in'],e['check_out'],int(e['supply']),state,reason))
    log(db,'shift',e['id'],state,reason,e['id'])
    return state

def patrol(db,e):
    s=one(db,'SELECT * FROM shift WHERE id=?',(e['shift_id'],))
    if not s:raise ValueError('unknown shift')
    if e['inspector']==s['worker'] or e['at']<s['check_out']:
        state='invalid';reason='patrol not independent or predates shift end'
    elif int(e['score'])<80:
        state='failed';reason='cleanliness below contract threshold'
    elif s['state']!='pending_patrol':
        state='invalid';reason='shift has unresolved precondition'
    else:
        state='passed';reason='independent patrol confirms service'
        db.execute("UPDATE shift SET state='accepted',reason=? WHERE id=?",(reason,e['shift_id']))
    db.execute('INSERT INTO patrol(id,shift_id,inspector,score,at,state) VALUES(?,?,?,?,?,?)',
               (e['id'],e['shift_id'],e['inspector'],int(e['score']),e['at'],state))
    log(db,'shift',e['shift_id'],state,reason,e['id'])
    from .complaint import reconcile_complaints
    c=one(db,'SELECT facility_id FROM contract WHERE id=?',(s['contract_id'],))
    reconcile_complaints(db,c['facility_id'])
    return state

def reconcile_shifts(db,facility_id):
    opening=one(db,'SELECT state FROM opening WHERE facility_id=? ORDER BY id DESC LIMIT 1',(facility_id,))
    if opening and opening['state']=='closed':
        for s in all_rows(db,"SELECT s.id FROM shift s JOIN contract c ON c.id=s.contract_id WHERE c.facility_id=? AND s.state='pending_patrol'",(facility_id,)):
            db.execute("UPDATE shift SET state='suspended',reason='facility closed after check-in' WHERE id=?",(s['id'],))
            log(db,'shift',s['id'],'suspended','facility closure changes shift outcome')
