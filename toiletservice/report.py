import hashlib
import json
from .store import all_rows,connect
from .source import ingest

TABLES=['facility','defect','opening','contract','shift','patrol','complaint','verification','remedy','complaint_link',
        'water_calibration','water_meter','water_baseline','water_pipeline','water_anomaly','water_reading',
        'water_test','water_witness','water_fault','water_fault_shift','decision_log']

def report(db):
    result={table:[dict(row) for row in all_rows(db,f'SELECT * FROM {table} ORDER BY rowid')] for table in TABLES}
    result['source_file']=[dict(row) for row in all_rows(db,'SELECT id,command,sha FROM source_file ORDER BY id')]
    result['event']=[dict(row) for row in all_rows(db,'SELECT id,kind,sha FROM event ORDER BY rowid')]
    from .water_witness import coverage
    result['water_coverage']=coverage(db)
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
