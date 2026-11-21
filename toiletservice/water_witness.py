"""Offline water-meter evidence intake and fault attribution.

Two facilities share one master meter. A master meter cannot by itself decide
which branch explains an anomaly: every attestation is derived from local
synthetic receipts only. Reception hours accept branch meter reads (service is
never paused to collect them); maintenance windows accept simulated isolation
test receipts. A water defect is handed to the existing facility entry only when
all compatible evidence points at one facility and at least one independent
attestation passes.
"""
from .store import one, all_rows, log

# --- coverage versions: meters, calibration, baselines, shared pipeline ----

def calibration(db, e):
    if one(db, 'SELECT id FROM water_calibration WHERE id=?', (e['id'],)):
        return 'replayed'
    db.execute('INSERT INTO water_calibration(id,unit,issued_at,version) VALUES(?,?,?,?)',
               (e['id'], e['unit'], e['issued_at'], int(e['version'])))
    log(db, 'water', e['id'], 'recorded', f"calibration {e['id']} version {e['version']}", e['id'])
    return 'recorded'


def meter(db, e):
    if one(db, 'SELECT id FROM water_meter WHERE id=?', (e['id'],)):
        return 'replayed'
    role = e['role']
    if role not in {'master', 'branch'}:
        raise ValueError('meter role must be master or branch')
    if role == 'master':
        master_id = e['id']
        facility_id = None
    else:
        master_id = e['master_id']
        facility_id = e['facility_id']
        if not one(db, 'SELECT id FROM facility WHERE id=?', (facility_id,)):
            raise ValueError('unknown facility')
    db.execute('INSERT INTO water_meter(id,role,master_id,facility_id,unit,calibration_id,covers_from,covers_to,version)'
               ' VALUES(?,?,?,?,?,?,?,?,?)',
               (e['id'], role, master_id, facility_id, e['unit'], e['calibration_id'],
                e.get('covers_from'), e.get('covers_to'), int(e['version'])))
    log(db, 'water', e['id'], 'recorded', f'{role} meter {e["id"]} coverage version {e["version"]}', e['id'])
    return 'recorded'


def baseline(db, e):
    if one(db, 'SELECT id FROM water_baseline WHERE id=?', (e['id'],)):
        return 'replayed'
    if not one(db, 'SELECT id FROM facility WHERE id=?', (e['facility_id'],)):
        raise ValueError('unknown facility')
    db.execute('INSERT INTO water_baseline(id,facility_id,master_id,expected_use,unit,period_from,period_to,version)'
               ' VALUES(?,?,?,?,?,?,?,?)',
               (e['id'], e['facility_id'], e.get('master_id'), int(e['expected_use']), e['unit'],
                e['period_from'], e['period_to'], int(e['version'])))
    log(db, 'water', e['id'], 'recorded', f"baseline {e['id']} for {e['facility_id']}", e['id'])
    return 'recorded'


def pipeline(db, e):
    if one(db, 'SELECT id FROM water_pipeline WHERE id=?', (e['id'],)):
        return 'replayed'
    facilities = e['facility_ids']
    if len(facilities) < 2 or any(not one(db, 'SELECT id FROM facility WHERE id=?', (f,)) for f in facilities):
        raise ValueError('pipeline must cover at least two known facilities')
    db.execute('INSERT INTO water_pipeline(id,master_id,facility_ids,shared,version) VALUES(?,?,?,?,?)',
               (e['id'], e['master_id'], ','.join(facilities), 1 if e.get('shared', True) else 0, int(e['version'])))
    log(db, 'water', e['id'], 'recorded', f"shared pipeline {e['id']} covering {','.join(facilities)}", e['id'])
    return 'recorded'


# --- helpers ----------------------------------------------------------------

def _anomaly(db, anomaly_id):
    return one(db, 'SELECT * FROM water_anomaly WHERE id=?', (anomaly_id,))


def _pipeline_facilities(db, master_id):
    p = one(db, 'SELECT * FROM water_pipeline WHERE master_id=? ORDER BY version DESC,id', (master_id,))
    return p['facility_ids'].split(',') if p else []


def _period_baselines(db, a):
    out = {}
    for f in _pipeline_facilities(db, a['master_id']):
        b = one(db, 'SELECT * FROM water_baseline WHERE facility_id=? AND unit=? AND period_from<=? AND period_to>=?'
                ' ORDER BY version DESC,id', (f, a['unit'], a['period_from'], a['period_to']))
        if b:
            out[f] = b
    return out


def _sign_fault(db, a, facility_id, method, event_id, at):
    """Hand the attribution to the existing facility defect entry."""
    fault = one(db, 'SELECT * FROM water_fault WHERE anomaly_id=? AND state=?', (a['id'], 'signed'))
    if fault and fault['facility_id'] == facility_id:
        return fault
    if fault and fault['facility_id'] != facility_id:
        db.execute("UPDATE defect SET closed_at=?,witness=? WHERE id=? AND closed_at IS NULL",
                   (at, 'water-witness-reattribution:' + a['id'], fault['defect_id']))
        db.execute("UPDATE water_fault SET state='withdrawn',reason='attribution revised by later evidence' WHERE id=?",
                   (fault['id'],))
        log(db, 'water', fault['id'], 'withdrawn', 'attribution revised by later evidence', event_id)
    defect_id = 'water-' + a['id'] + '-' + facility_id
    existing = one(db, 'SELECT id FROM defect WHERE id=?', (defect_id,))
    if not existing:
        db.execute('INSERT INTO defect(id,facility_id,kind,stall_count,accessible_loss,opened_at) VALUES(?,?,?,?,?,?)',
                   (defect_id, facility_id, 'water', 0, 0, at))
    seq = one(db, 'SELECT COUNT(*) c FROM water_fault WHERE anomaly_id=?', (a['id'],))['c']
    fault_id = 'wf-' + a['id'] if seq == 0 else f'wf-{a["id"]}-r{seq}'
    db.execute('INSERT INTO water_fault(id,anomaly_id,facility_id,defect_id,state,reason,event_id) VALUES(?,?,?,?,?,?,?)',
               (fault_id, a['id'], facility_id, defect_id, 'signed',
                f'attributed by {method} with independent attestation', event_id))
    log(db, 'water', fault_id, 'signed', f'water fault handed to facility {facility_id} via {method}', event_id)
    from .facility import recompute
    recompute(db, facility_id, event_id)
    for s in all_rows(db, "SELECT s.id FROM shift s JOIN contract c ON c.id=s.contract_id "
                          "WHERE c.facility_id=? AND s.state='suspended'", (facility_id,)):
        db.execute('INSERT OR IGNORE INTO water_fault_shift(fault_id,shift_id) VALUES(?,?)', (fault_id, s['id']))
    return one(db, 'SELECT * FROM water_fault WHERE id=?', (fault_id,))


def _withdraw_fault(db, a, event_id, at, reason):
    fault = one(db, "SELECT * FROM water_fault WHERE anomaly_id=? AND state='signed'", (a['id'],))
    if not fault:
        return None
    db.execute("UPDATE defect SET closed_at=?,witness=? WHERE id=? AND closed_at IS NULL",
               (at, 'water-witness-withdrawn:' + a['id'], fault['defect_id']))
    db.execute('UPDATE water_fault SET state=?,reason=? WHERE id=?', ('withdrawn', reason, fault['id']))
    log(db, 'water', fault['id'], 'withdrawn', reason, event_id)
    from .facility import recompute
    recompute(db, fault['facility_id'], event_id)
    return fault


def _set_attribution(db, a, facility_id, method, event_id, at, reason):
    db.execute("UPDATE water_witness SET superseded=1 WHERE anomaly_id=?", (a['id'],))
    db.execute('INSERT INTO water_witness(anomaly_id,method,attribution,state,reason,event_id)'
               " VALUES(?,?,?,'determined',?,?)",
               (a['id'], method, facility_id, reason, event_id))
    db.execute("UPDATE water_anomaly SET state='attributed',attribution=?,reason=? WHERE id=?",
               (facility_id, reason, a['id']))
    log(db, 'water', a['id'], 'attributed', reason, event_id)
    return _sign_fault(db, a, facility_id, method, event_id, at)


def _set_shared_scope(db, a, method, event_id, reason):
    db.execute("UPDATE water_witness SET superseded=1 WHERE anomaly_id=?", (a['id'],))
    facilities = _pipeline_facilities(db, a['master_id'])
    for f in facilities:
        db.execute('INSERT INTO water_witness(anomaly_id,method,attribution,state,reason,event_id)'
                   " VALUES(?,?,?,'shared_scope',?,?)",
                   (a['id'], method, f, reason, event_id))
    db.execute("UPDATE water_anomaly SET state='shared_scope',attribution=NULL,reason=? WHERE id=?",
               (reason, a['id']))
    log(db, 'water', a['id'], 'shared_scope', reason, event_id)
    _withdraw_fault(db, a, event_id, a['period_to'], reason)


# --- master meter anomaly: opens the case with compatible explanations ------

def anomaly(db, e):
    if _anomaly(db, e['id']):
        return 'replayed'
    master = one(db, "SELECT * FROM water_meter WHERE id=? AND role='master'", (e['master_id'],))
    if not master:
        raise ValueError('unknown master meter')
    if e['unit'] != master['unit']:
        raise ValueError('anomaly unit does not match master meter unit')
    baselines = _period_baselines(db, {**e, 'master_id': e['master_id']})
    facilities = _pipeline_facilities(db, e['master_id'])
    if len(baselines) != len(facilities):
        raise ValueError('aligned service baseline missing for a shared pipeline facility')
    expected_total = sum(b['expected_use'] for b in baselines.values())
    reading = int(e['master_reading'])
    excess = reading - expected_total
    if excess <= 0:
        raise ValueError('master reading shows no excess over combined baselines')
    reason = (f'master excess {excess} {e["unit"]} cannot be split by the master meter alone; '
              'each branch remains a compatible explanation')
    db.execute('INSERT INTO water_anomaly(id,master_id,period_from,period_to,unit,master_reading,'
               'expected_total,excess,state,reason,attribution) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
               (e['id'], e['master_id'], e['period_from'], e['period_to'], e['unit'], reading,
                expected_total, excess, 'ambiguous', reason))
    for f in facilities:
        db.execute('INSERT INTO water_witness(anomaly_id,method,attribution,state,reason,event_id)'
                   " VALUES(?,'master_meter',?,'compatible',?,?)",
                   (e['id'], f, reason, e['id']))
    log(db, 'water', e['id'], 'ambiguous', reason, e['id'])
    return 'ambiguous'


# --- reception-hours branch meter reads -------------------------------------

def branch_read(db, e):
    if one(db, 'SELECT id FROM water_reading WHERE id=?', (e['id'],)):
        return 'replayed'
    a = _anomaly(db, e['anomaly_id'])
    if not a:
        raise ValueError('unknown anomaly')
    meter = one(db, "SELECT * FROM water_meter WHERE id=? AND role='branch'", (e['meter_id'],))
    if not meter or meter['facility_id'] != e['facility_id']:
        raise ValueError('branch meter does not belong to facility')
    facility = one(db, 'SELECT * FROM facility WHERE id=?', (e['facility_id'],))
    tod = e['at'][-8:]
    if not (facility['opens'] <= tod < facility['closes']):
        raise ValueError('branch reads are only accepted during published reception hours')
    master_cal = one(db, 'SELECT calibration_id FROM water_meter WHERE id=?', (a['master_id'],))['calibration_id']
    if e['calibration_id'] != master_cal:
        raise ValueError('branch reading mixes calibration versions with master meter')
    if e['unit'] != a['unit']:
        raise ValueError('branch reading unit not aligned with anomaly')
    if not (meter['covers_from'] is None or meter['covers_from'] <= e['at']) or \
       not (meter['covers_to'] is None or meter['covers_to'] >= e['at']):
        raise ValueError('branch meter coverage does not include read time')
    if not (a['period_from'] <= e['at'] <= a['period_to']):
        raise ValueError('branch reading does not cover the anomaly period')
    baseline = one(db, 'SELECT * FROM water_baseline WHERE facility_id=? AND unit=? AND period_from<=? AND period_to>=?'
                   ' ORDER BY version DESC,id', (e['facility_id'], e['unit'], a['period_from'], a['period_to']))
    if not baseline:
        raise ValueError('no aligned service baseline for branch reading')
    value = int(e['value'])
    excess = value - baseline['expected_use']
    db.execute('INSERT INTO water_reading(id,anomaly_id,facility_id,meter_id,calibration_id,at,unit,value,baseline,excess)'
               ' VALUES(?,?,?,?,?,?,?,?,?,?)',
               (e['id'], a['id'], e['facility_id'], e['meter_id'], e['calibration_id'], e['at'],
                e['unit'], value, baseline['expected_use'], excess))
    log(db, 'water', e['id'], 'recorded',
        f'branch read for {e["facility_id"]}: {value} {e["unit"]} (excess {excess})', e['id'])
    return _evaluate_branch(db, a, e['id'])


def _evaluate_branch(db, a, event_id):
    reads = {r['facility_id']: r for r in all_rows(
        db, 'SELECT * FROM water_reading WHERE anomaly_id=?', (a['id'],))}
    facilities = _pipeline_facilities(db, a['master_id'])
    if not all(f in reads for f in facilities):
        return 'partial_coverage'
    if sum(reads[f]['value'] for f in facilities) != a['master_reading']:
        reason = 'branch readings do not conserve to master reading; shared main remains under investigation'
        _set_shared_scope(db, a, 'branch_read', event_id, reason)
        return 'shared_scope'
    positive = [f for f in facilities if reads[f]['excess'] > 0]
    if len(positive) != 1:
        reason = 'branch readings implicate more than one branch; shared main remains under investigation'
        _set_shared_scope(db, a, 'branch_read', event_id, reason)
        return 'shared_scope'
    target = positive[0]
    reason = (f'conservation across same-calibration branch reads attributes full excess '
              f'{a["excess"]} {a["unit"]} to {target}')
    _set_attribution(db, a, target, 'branch_read', event_id, max(reads[f]['at'] for f in facilities), reason)
    return 'attributed'


# --- maintenance-window simulated isolation test ----------------------------

def isolation_test(db, e):
    a = _anomaly(db, e['anomaly_id'])
    if not a:
        raise ValueError('unknown anomaly')
    if one(db, 'SELECT id FROM water_test WHERE event_id=?', (e['id'],)):
        return 'replayed'
    for key in ('anomaly_id', 'test_ref', 'stage_revision', 'at'):
        if not e.get(key):
            raise ValueError(f'isolation receipt requires {key}')
    records = e.get('records', [])
    ref, revision = e['test_ref'], e['stage_revision']
    required = ('test_ref', 'stage_revision', 'calibration_id', 'unit', 'stage', 'value')
    invalid = None
    if not isinstance(records, list) or len(records) != 4 \
            or {r.get('stage') for r in records} != {'null_start', 'a_only', 'b_only', 'null_end'}:
        invalid = 'isolation receipt must carry exactly null_start, a_only, b_only, null_end stages'
    elif any(any(k not in r for k in required) for r in records):
        invalid = 'isolation receipt record is missing required fields'
    elif any(r.get('test_ref') != ref or r.get('stage_revision') != revision for r in records):
        invalid = 'all four records must reference the same actual test and stage revision'
    elif len({r['calibration_id'] for r in records}) != 1:
        invalid = 'calibration versions mixed inside one isolation test'
    if invalid is None:
        master_cal = one(db, 'SELECT calibration_id FROM water_meter WHERE id=?', (a['master_id'],))['calibration_id']
        if records[0]['calibration_id'] != master_cal:
            invalid = 'isolation test calibration does not match master meter calibration epoch'
        elif any(r['unit'] != a['unit'] for r in records):
            invalid = 'isolation test units not aligned with anomaly'
    if invalid is None:
        try:
            by_stage = {r['stage']: int(r['value']) for r in records}
        except (TypeError, ValueError):
            by_stage = None
            invalid = 'isolation receipt values must be integer meter readings'
        if by_stage is not None:
            null_start, read_a, read_b, null_end = (by_stage['null_start'], by_stage['a_only'],
                                                    by_stage['b_only'], by_stage['null_end'])
            try:
                offset = int(e['null_offset'])
            except (KeyError, TypeError, ValueError):
                invalid = 'receipt no-load offset missing or not an integer'
            if invalid is None and bool(e.get('closure_revoked')):
                invalid = 'field closure order revoked during the test; recertification required'
            elif invalid is None and null_start != null_end:
                invalid = f'no-load readings differ ({null_start} vs {null_end}); recertification required'
            elif invalid is None and offset != null_start:
                invalid = f'receipt no-load offset {offset} does not match no-load start {null_start}'
            elif invalid is None and read_a + read_b - 2 * null_start != a['master_reading']:
                invalid = 'isolation branches do not conserve to the master reading'
    prior = one(db, 'SELECT * FROM water_test WHERE anomaly_id=? AND test_ref=? AND superseded=0',
                (a['id'], ref))
    parsed = {}
    for r in records:
        try:
            parsed[r.get('stage')] = int(r['value'])
        except (KeyError, TypeError, ValueError):
            continue
    if invalid is not None:
        db.execute('INSERT INTO water_test(event_id,anomaly_id,test_ref,stage_revision,calibration_id,'
                   'null_start,read_a,read_b,null_end,null_offset,closure_revoked,state,reason,superseded)'
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,'invalid',?,0)",
                   (e['id'], a['id'], ref, revision,
                    records[0]['calibration_id'] if records and 'calibration_id' in records[0] else None,
                    parsed.get('null_start'), parsed.get('a_only'), parsed.get('b_only'), parsed.get('null_end'),
                    int(e.get('null_offset', 0)), int(bool(e.get('closure_revoked'))), invalid))
        log(db, 'water', e['id'], 'invalid', invalid, e['id'])
        return 'invalid'
    if prior:
        db.execute('UPDATE water_test SET superseded=1 WHERE id=?', (prior['id'],))
        db.execute("UPDATE water_witness SET superseded=1 WHERE anomaly_id=? AND method='isolation_test'", (a['id'],))
        log(db, 'water', prior['event_id'], 'superseded',
            f'late correction revision {revision} replaces stage revision {prior["stage_revision"]}', e['id'])
    db.execute('INSERT INTO water_test(event_id,anomaly_id,test_ref,stage_revision,calibration_id,'
               'null_start,read_a,read_b,null_end,null_offset,closure_revoked,state,reason,superseded)'
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,'valid',?,0)",
               (e['id'], a['id'], ref, revision, records[0]['calibration_id'], null_start, read_a, read_b,
                null_end, offset, int(bool(e.get('closure_revoked'))),
                f'isolation test {ref} revision {revision} conserved under one calibration'))
    facilities = _pipeline_facilities(db, a['master_id'])
    use = {facilities[0]: read_a - null_start, facilities[1]: read_b - null_start}
    baselines = _period_baselines(db, a)
    if len(baselines) != len(facilities):
        reason = 'aligned service baseline missing; attribution cannot be certified'
        _set_shared_scope(db, a, 'isolation_test', e['id'], reason)
        return 'shared_scope'
    over = [f for f in facilities if use[f] - baselines[f]['expected_use'] > 0]
    if len(over) != 1:
        reason = 'isolation test implicates both branches; shared main remains under investigation'
        _set_shared_scope(db, a, 'isolation_test', e['id'], reason)
        return 'shared_scope'
    target = over[0]
    reason = (f'isolation test {ref} revision {revision}: branch use {use[facilities[0]]}/{use[facilities[1]]} '
              f'{a["unit"]} attributes excess to {target}')
    _set_attribution(db, a, target, 'isolation_test', e['id'], e['at'], reason)
    return 'attributed'


# --- per-facility evidence coverage output ----------------------------------

def coverage(db):
    rows = []
    for a in all_rows(db, 'SELECT * FROM water_anomaly ORDER BY id'):
        facilities = _pipeline_facilities(db, a['master_id'])
        per = {}
        for f in facilities:
            read = one(db, 'SELECT * FROM water_reading WHERE anomaly_id=? AND facility_id=?', (a['id'], f))
            per[f] = {
                'facility_id': f,
                'branch_reading': None if not read else {
                    'reading_id': read['id'], 'value': read['value'], 'unit': read['unit'],
                    'baseline': read['baseline'], 'excess': read['excess'],
                    'calibration_id': read['calibration_id'], 'at': read['at'],
                },
                'witnesses': [
                    {'method': w['method'], 'attribution': w['attribution'], 'state': w['state'],
                     'reason': w['reason'], 'event_id': w['event_id'], 'superseded': bool(w['superseded'])}
                    for w in all_rows(db, 'SELECT * FROM water_witness WHERE anomaly_id=? AND attribution=? ORDER BY id',
                                      (a['id'], f))],
                'signed_fault': None,
            }
        for fault in all_rows(db, 'SELECT * FROM water_fault WHERE anomaly_id=? ORDER BY id', (a['id'],)):
            if fault['facility_id'] in per:
                per[fault['facility_id']]['signed_fault'] = {
                    'fault_id': fault['id'], 'state': fault['state'], 'reason': fault['reason'],
                    'defect_id': fault['defect_id'], 'event_id': fault['event_id'],
                    'affected_shifts': [s['shift_id'] for s in all_rows(
                        db, 'SELECT shift_id FROM water_fault_shift WHERE fault_id=? ORDER BY shift_id', (fault['id'],))],
                }
        rows.append({
            'anomaly_id': a['id'], 'master_id': a['master_id'],
            'period': {'from': a['period_from'], 'to': a['period_to']},
            'unit': a['unit'], 'master_reading': a['master_reading'],
            'expected_total': a['expected_total'], 'excess': a['excess'],
            'state': a['state'], 'attribution': a['attribution'], 'reason': a['reason'],
            'valid_tests': [dict(t) for t in all_rows(
                db, "SELECT event_id,test_ref,stage_revision,calibration_id,null_start,read_a,read_b,null_end,"
                "null_offset,state,reason,superseded FROM water_test WHERE anomaly_id=? ORDER BY id", (a['id'],))],
            'facilities': list(per.values()),
        })
    return rows
