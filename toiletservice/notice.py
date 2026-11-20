"""Manual notice review intake; browser evidence is not yet evaluated here."""

from .store import one, all_rows, log


def open_case(db, e):
    opening = one(db, 'SELECT id FROM opening WHERE facility_id=? AND version=?',
                  (e['facility_id'], int(e['opening_version'])))
    if not opening or e['contract_kind'] not in {'final', 'continuous'}:
        raise ValueError('notice case needs an opening version and signed contract kind')
    db.execute('INSERT INTO notice_case(id,facility_id,opening_version,contract_kind,state,reason) VALUES(?,?,?,?,?,?)',
               (e['id'], e['facility_id'], int(e['opening_version']),
                e['contract_kind'], 'pending_review', 'awaiting browser capture review'))
    log(db, 'notice', e['id'], 'pending_review', 'awaiting browser capture review', e['id'])
    return 'pending_review'


def review(db, e):
    case = one(db, 'SELECT * FROM notice_case WHERE id=?', (e['case_id'],))
    if not case or not e.get('inspector'):
        raise ValueError('notice case and independent reviewer required')
    complaints = all_rows(db, 'SELECT reporter,at FROM complaint WHERE notice_case_id=?', (e['case_id'],))
    if any(e['inspector'] == c['reporter'] or e['at'] < c['at'] for c in complaints):
        raise ValueError('reviewer must be independent and later than complaint')
    mismatch = bool(e['mismatch'])
    db.execute('INSERT INTO notice_review(id,case_id,inspector,at,mismatch) VALUES(?,?,?,?,?)',
               (e['id'], e['case_id'], e['inspector'], e['at'], int(mismatch)))
    state = 'mismatch' if mismatch else 'aligned'
    db.execute('UPDATE notice_case SET state=?,reason=? WHERE id=?',
               (state, 'manual independent notice review', e['case_id']))
    log(db, 'notice', e['case_id'], state, 'manual independent notice review', e['id'])
    from .complaint import reconcile_notice
    reconcile_notice(db, e['case_id'], e['id'])
    return state
