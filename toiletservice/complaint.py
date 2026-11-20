from datetime import datetime,timedelta
from .store import one,all_rows,log

def submit(db,e):
    f=one(db,'SELECT * FROM facility WHERE id=?',(e['facility_id'],))
    if not f:raise ValueError('unknown facility')
    if not e.get('reporter') or e['category'] not in {'water','accessible','cleanliness','supply','stall','notice'}:
        raise ValueError('reporter and known category required')
    notice_case_id=e.get('notice_case_id')
    if e['category']=='notice':
        case=one(db,'SELECT * FROM notice_case WHERE id=?',(notice_case_id,))
        if not case or case['facility_id']!=e['facility_id']:
            raise ValueError('notice complaint needs a matching notice case')
    if not f['opens']<=e['at'][-8:]<f['closes']:
        state,reason='pending','reported outside published service hours'
    else:state,reason='pending','awaiting independent verification'
    db.execute('INSERT INTO complaint(id,facility_id,at,category,reporter,state,reason,notice_case_id) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'],e['facility_id'],e['at'],e['category'],e['reporter'],state,reason,notice_case_id))
    log(db,'complaint',e['id'],state,reason,e['id'])
    return state

def verify(db,e):
    c=one(db,'SELECT * FROM complaint WHERE id=?',(e['complaint_id'],))
    if not c:raise ValueError('unknown complaint')
    if not e.get('inspector') or e['at']<c['at'] or e['inspector']==c['reporter']:
        raise ValueError('independent later verification required')
    confirmed=bool(e['confirmed'])
    reason=e.get('reason','verified on site')
    db.execute('INSERT INTO verification(id,complaint_id,inspector,at,confirmed,reason) VALUES(?,?,?,?,?,?)',
               (e['id'],e['complaint_id'],e['inspector'],e['at'],int(confirmed),reason))
    if confirmed:
        contracts=all_rows(db,'SELECT * FROM contract WHERE facility_id=? AND starts<=? AND ends>? ORDER BY starts DESC,id',
                           (c['facility_id'],c['at'],c['at']))
        if not contracts:
            deadline=None;state='pending_contract';detail='verified defect but no covering service contract'
        else:
            deadline=(datetime.fromisoformat(e['at'])+timedelta(minutes=contracts[0]['remedy_minutes'])).isoformat()
            state='confirmed';detail=f'verified defect; remedy due under contract {contracts[0]["id"]}'
    else:deadline=None;state='rejected';detail='independent verification did not confirm defect'
    db.execute('UPDATE complaint SET state=?,reason=?,due_at=? WHERE id=?',(state,detail,deadline,c['id']))
    log(db,'complaint',c['id'],state,detail,e['id'])
    if confirmed and c['category']=='cleanliness':
        for s in all_rows(db,"SELECT s.id,s.state FROM shift s JOIN contract t ON t.id=s.contract_id WHERE t.facility_id=? AND s.check_out<=? ORDER BY s.check_out DESC LIMIT 1",(c['facility_id'],c['at'])):
            db.execute('INSERT INTO complaint_link(complaint_id,shift_id,defect_id,link_reason) VALUES(?,?,?,?)',
                       (c['id'],s['id'],'','cleanliness complaint contradicts prior patrol'))
            if s['state']=='accepted':
                db.execute("UPDATE shift SET state='disputed',reason='verified complaint after accepted patrol' WHERE id=?",(s['id'],))
                log(db,'shift',s['id'],'disputed','verified cleanliness complaint after accepted patrol',e['id'])
    if confirmed and c['category'] in {'water','accessible','stall'}:
        defect_id='verified-'+e['id']
        kind='water' if c['category']=='water' else c['category']
        db.execute('INSERT INTO complaint_link(complaint_id,shift_id,defect_id,link_reason) VALUES(?,?,?,?)',
                   (c['id'],'',defect_id,'verified public complaint opens facility defect'))
        db.execute('INSERT INTO defect(id,facility_id,kind,stall_count,accessible_loss,opened_at) VALUES(?,?,?,?,?,?)',
                   (defect_id,c['facility_id'],kind,1 if kind!='water' else 0,1 if kind=='accessible' else 0,e['at']))
        from .facility import recompute
        recompute(db,c['facility_id'],e['id'])
    return state

def remedy(db,e):
    c=one(db,'SELECT * FROM complaint WHERE id=?',(e['complaint_id'],))
    if not c:raise ValueError('unknown complaint')
    if c['state'] not in {'confirmed','overdue','reopened'}:
        state,reason='invalid','complaint is not awaiting remedy'
    elif e['worker']==e['inspector'] or e['at']<c['at']:
        state,reason='invalid','remedy cannot be self verified or predate complaint'
    elif not e['passed']:
        state,reason='failed','independent reinspection found defect unresolved'
    elif c['due_at'] and e['at']>c['due_at']:
        state,reason='late','remedy completed after contractual deadline'
    else:state,reason='closed','verified remedy within deadline'
    db.execute('INSERT INTO remedy(id,complaint_id,worker,at,inspector,passed,state,reason) VALUES(?,?,?,?,?,?,?,?)',
               (e['id'],c['id'],e['worker'],e['at'],e['inspector'],int(bool(e['passed'])),state,reason))
    if state in {'closed','late','failed'}:
        db.execute('UPDATE complaint SET state=?,reason=? WHERE id=?',(state,reason,c['id']))
    if state in {'closed','late'}:
        for link in all_rows(db,"SELECT defect_id FROM complaint_link WHERE complaint_id=? AND defect_id<>''",(c['id'],)):
            db.execute('UPDATE defect SET closed_at=?,witness=? WHERE id=?',(e['at'],e['inspector'],link['defect_id']))
        from .facility import recompute
        recompute(db,c['facility_id'],e['id'])
    log(db,'complaint',c['id'],state,reason,e['id'])
    return state

def reconcile_complaints(db,facility_id):
    opening=one(db,'SELECT * FROM opening WHERE facility_id=? ORDER BY id DESC LIMIT 1',(facility_id,))
    if not opening:return
    for c in all_rows(db,"SELECT * FROM complaint WHERE facility_id=? AND state='closed'",(facility_id,)):
        if (c['category']=='water' and opening['state']=='closed') or (c['category']=='accessible' and opening['accessible_available']==0):
            db.execute("UPDATE complaint SET state='reopened',reason='facility evidence still shows defect' WHERE id=?",(c['id'],))
            log(db,'complaint',c['id'],'reopened','facility evidence still shows defect')

def reconcile_notice(db,case_id,event_id=None):
    case=one(db,'SELECT * FROM notice_case WHERE id=?',(case_id,))
    if not case:return
    review=one(db,'SELECT * FROM notice_review WHERE case_id=? ORDER BY at DESC,id DESC LIMIT 1',(case_id,))
    certification=one(db,'SELECT * FROM notice_certification WHERE case_id=? ORDER BY rowid DESC LIMIT 1',(case_id,))
    # Manual independent reviews and browser-derived certifications are two
    # intake paths into the same complaint ledger. Only the newest decision
    # counts. An inconclusive certification (gaps only) withdraws any earlier
    # browser conclusion and returns the complaint to pending, but can never
    # override a manual independent review.
    newest_manual_at=review['at'] if review else None
    if certification and not certification['conclusive'] and (
            newest_manual_at is None or certification['review_at']>newest_manual_at):
        for c in all_rows(db,'SELECT * FROM complaint WHERE notice_case_id=?',(case_id,)):
            reason='browser evidence withdrawn; declared epochs lack intact same-version evidence'
            if (c['state'],c['reason'],c['due_at'])!=('pending',reason,None):
                db.execute('UPDATE complaint SET state=?,reason=?,due_at=? WHERE id=?',
                           ('pending',reason,None,c['id']))
                log(db,'complaint',c['id'],'pending',reason,event_id)
        return
    decision=None
    if review:
        decision={'at':review['at'],'mismatch':bool(review['mismatch']),
                  'source':'manual','order_id':review['id']}
    if certification and certification['conclusive']:
        auto={'at':certification['review_at'],'mismatch':bool(certification['mismatch']),
              'source':'browser','order_id':certification['id']}
        if decision is None or auto['at']>decision['at'] or (
                auto['at']==decision['at'] and auto['order_id']>decision['order_id']):
            decision=auto
    if decision is None:return
    for c in all_rows(db,'SELECT * FROM complaint WHERE notice_case_id=?',(case_id,)):
        if decision['mismatch']:
            contracts=all_rows(db,'SELECT * FROM contract WHERE facility_id=? AND starts<=? AND ends>? ORDER BY starts DESC,id',
                               (c['facility_id'],c['at'],c['at']))
            if contracts:
                deadline=(datetime.fromisoformat(decision['at'])+timedelta(minutes=contracts[0]['remedy_minutes'])).isoformat()
                if decision['source']=='browser':
                    state,reason='confirmed','browser capture certification found a notice mismatch'
                else:
                    state,reason='confirmed','independent notice review found a mismatch'
            else:
                deadline=None;state,reason='pending_contract','notice mismatch without covering contract'
        else:
            deadline=None
            if decision['source']=='browser':
                state,reason='rejected','browser capture certification found the notice aligned'
            else:
                state,reason='rejected','independent notice review found alignment'
        if (c['state'],c['reason'],c['due_at'])!=(state,reason,deadline):
            db.execute('UPDATE complaint SET state=?,reason=?,due_at=? WHERE id=?',(state,reason,deadline,c['id']))
            log(db,'complaint',c['id'],state,reason,event_id)
