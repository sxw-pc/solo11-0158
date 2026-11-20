import hashlib
import json
from .store import all_rows,connect,one
from .source import ingest

TABLES=['facility','defect','opening','contract','shift','patrol','complaint','verification','remedy','complaint_link','notice_case','notice_review','notice_evidence','notice_epoch_evidence','notice_certification','decision_log']

def notice_summary(db):
    """Derived review view: both contracts, complaints, per-epoch originals,
    gaps and the backing opening version. No new decision is made here."""
    summary=[]
    for case in all_rows(db,'SELECT * FROM notice_case ORDER BY id'):
        opening=one(db,'SELECT * FROM opening WHERE facility_id=? AND version=?',
                    (case['facility_id'],case['opening_version']))
        linked=[dict(row) for row in all_rows(db,
            'SELECT id,state,reason,due_at FROM complaint WHERE notice_case_id=? ORDER BY id',(case['id'],))]
        evidence_rows=all_rows(db,'SELECT * FROM notice_evidence WHERE case_id=? ORDER BY rowid',(case['id'],))
        evidence=[]
        for item in evidence_rows:
            epochs=[dict(row) for row in all_rows(db,
                'SELECT epoch,page_version,visual_text,ax_texts_json,browser_clock_ms,captured_at,screenshot_sha256,dom_sha256,ax_tree_sha256,screenshot_file,dom_file,ax_tree_file FROM notice_epoch_evidence WHERE evidence_id=? ORDER BY epoch',(item['id'],))]
            certifications=[dict(row) for row in all_rows(db,
                'SELECT id,basis,mismatch,conclusive,missing_epochs_json,wrong_epochs_json,review_at FROM notice_certification WHERE evidence_id=? ORDER BY rowid',(item['id'],))]
            evidence.append({'evidence_id':item['id'],'capture_id':item['capture_id'],
                'browser_product':item['browser_product'],'monotonic_ok':bool(item['monotonic_ok']),
                'gaps':json.loads(item['gaps_json']),'epochs':epochs,'certifications':certifications})
        summary.append({'case_id':case['id'],'contract_kind':case['contract_kind'],
            'state':case['state'],'reason':case['reason'],
            'opening_version':None if not opening else opening['version'],
            'opening_state':None if not opening else opening['state'],
            'complaints':linked,'evidence':evidence})
    return summary

def report(db):
    result={table:[dict(row) for row in all_rows(db,f'SELECT * FROM {table} ORDER BY rowid')] for table in TABLES}
    result['source_file']=[dict(row) for row in all_rows(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    result['event']=[dict(row) for row in all_rows(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    result['notice_summary']=notice_summary(db)
    return result

def audit(db,handlers):
    replay=connect(':memory:')
    for source in all_rows(db,'SELECT command,sha,raw FROM source_file ORDER BY id'):
        if hashlib.sha256(source['raw']).hexdigest()!=source['sha']:
            return {'replay_ok':False,'mismatches':['source_file_sha']}
        events=json.loads(source['raw'])['events']
        with replay:
            for event in events:
                kind=event['kind']
                if kind not in handlers[source['command']]:raise ValueError('recorded command/event mismatch')
                if ingest(replay,event):handlers[source['command']][kind](replay,event)
            replay.execute('INSERT INTO source_file(command,sha,raw) VALUES(?,?,?)',
                           (source['command'],source['sha'],source['raw']))
    old,new=report(db),report(replay)
    for r in (old,new):
        for entry in r['decision_log']:entry.pop('at',None)
    return {'replay_ok':old==new,'mismatches':[k for k in old if old[k]!=new[k]],'event_count':len(old['event'])}
