from .store import one,all_rows,log

def register(db,e):
    if int(e['stalls'])<0 or int(e['accessible'])<0 or int(e['accessible'])>int(e['stalls']):
        raise ValueError('invalid stall inventory')
    old=one(db,'SELECT * FROM facility WHERE id=?',(e['facility_id'],))
    if old and int(e['version'])<=old['version']:
        if int(e['version'])==old['version'] and (old['stalls'],old['accessible'],old['zone'])!=(int(e['stalls']),int(e['accessible']),e['zone']):
            log(db,'facility',e['facility_id'],'version_conflict','parallel inventory revision',e['id'])
            return 'version_conflict'
        return 'stale'
    db.execute('INSERT INTO facility(id,zone,stalls,accessible,opens,closes,version) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET zone=excluded.zone,stalls=excluded.stalls,accessible=excluded.accessible,opens=excluded.opens,closes=excluded.closes,version=excluded.version',
               (e['facility_id'],e['zone'],int(e['stalls']),int(e['accessible']),e['opens'],e['closes'],int(e['version'])))
    return recompute(db,e['facility_id'],e['id'])

def defect(db,e):
    if not one(db,'SELECT id FROM facility WHERE id=?',(e['facility_id'],)):raise ValueError('unknown facility')
    db.execute('INSERT INTO defect(id,facility_id,kind,stall_count,accessible_loss,opened_at) VALUES(?,?,?,?,?,?)',
               (e['id'],e['facility_id'],e['defect_kind'],int(e['stall_count']),int(bool(e.get('accessible_loss'))),e['opened_at']))
    return recompute(db,e['facility_id'],e['id'])

def repair(db,e):
    d=one(db,'SELECT * FROM defect WHERE id=?',(e['defect_id'],))
    if not d:raise ValueError('unknown defect')
    if not e.get('witness') or e['closed_at']<=d['opened_at']:raise ValueError('independent repair witness and later time required')
    db.execute('UPDATE defect SET closed_at=?,witness=? WHERE id=?',(e['closed_at'],e['witness'],e['defect_id']))
    return recompute(db,d['facility_id'],e['id'])

def recompute(db,key,event_id=None):
    f=one(db,'SELECT * FROM facility WHERE id=?',(key,))
    if not f:raise ValueError('unknown facility')
    defects=all_rows(db,'SELECT * FROM defect WHERE facility_id=? AND closed_at IS NULL',(key,))
    water=any(d['kind']=='water' for d in defects)
    available=0 if water else max(0,f['stalls']-sum(d['stall_count'] for d in defects))
    accessible=0 if water else max(0,min(available,f['accessible']-sum(d['accessible_loss'] for d in defects)))
    state='closed' if available==0 else 'limited' if available<f['stalls'] or accessible<f['accessible'] else 'open'
    reason='water outage' if water else 'no usable stalls' if available==0 else 'accessibility limited' if accessible<f['accessible'] else 'stall fault' if available<f['stalls'] else 'full service'
    old=one(db,'SELECT * FROM opening WHERE facility_id=? ORDER BY id DESC LIMIT 1',(key,))
    if not old or (old['state'],old['available'],old['accessible_available'])!=(state,available,accessible):
        version=1 if not old else old['version']+1
        db.execute('INSERT INTO opening(facility_id,state,available,accessible_available,reason,version) VALUES(?,?,?,?,?,?)',
                   (key,state,available,accessible,reason,version))
        log(db,'facility',key,state,reason,event_id)
        from .shift import reconcile_shifts
        reconcile_shifts(db,key)
        from .complaint import reconcile_complaints
        reconcile_complaints(db,key)
    return state
