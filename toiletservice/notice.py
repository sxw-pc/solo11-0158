"""Notice case intake.

Two review paths feed the same complaint ledger:
  * review()            - the legacy manual independent review regression path;
  * register_evidence() + certify() - browser capture evidence verified from
    its original bytes and adjudicated per signed contract kind.
"""

import json

from .store import one, all_rows, log
from . import evidence as evidence_mod


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


def register_evidence(db, e):
    """Persist a verified browser capture against one notice case.

    The capture directory is verified first (recomputed hashes, raw DOM/AX
    re-derivation, monotonic epoch, declared-range reconciliation). Replaying
    the same capture is idempotent; a different capture for the same evidence
    event id is an immutable conflict.
    """
    case = one(db, 'SELECT * FROM notice_case WHERE id=?', (e['case_id'],))
    if not case:
        raise ValueError('unknown notice case')
    capture = evidence_mod.load_capture(e['capture_dir'])
    if capture['facility_id'] != case['facility_id'] \
            or capture['opening_version'] != case['opening_version']:
        raise evidence_mod.EvidenceError('capture does not belong to this notice case opening')

    existing = one(db, 'SELECT capture_id FROM notice_evidence WHERE id=?', (e['id'],))
    if existing:
        if existing['capture_id'] != capture['capture_id']:
            raise ValueError('immutable notice evidence id conflict')
        return 'evidence_replayed'

    db.execute('''INSERT INTO notice_evidence(id,case_id,capture_id,manifest_sha256,source_sha256,
        declared_first,declared_last,intended_version,intended_text,browser_product,
        monotonic_ok,gaps_json,registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (e['id'], e['case_id'], capture['capture_id'], capture['manifest_sha256'],
                capture['source_sha256'], capture['declared_first'], capture['declared_last'],
                capture['intended_version'], capture['intended_text'], capture['browser_product'],
                int(capture['monotonic_ok']), json.dumps(capture['gaps']), e['at']))
    for frame in capture['frames'].values():
        db.execute('''INSERT INTO notice_epoch_evidence(id,evidence_id,epoch,page_version,visual_text,
            ax_texts_json,browser_clock_ms,captured_at,screenshot_sha256,dom_sha256,ax_tree_sha256,
            screenshot_file,dom_file,ax_tree_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (f"{e['id']}-e{frame['epoch']}", e['id'], frame['epoch'], frame['version'],
                    frame['visual_text'], json.dumps(frame['ax_texts'], ensure_ascii=False),
                    frame['browser_clock_ms'], frame['captured_at'], frame['screenshot_sha256'],
                    frame['dom_sha256'], frame['ax_tree_sha256'], frame['screenshot_file'],
                    frame['dom_file'], frame['ax_tree_file']))
    db.execute('UPDATE notice_case SET reason=? WHERE id=?',
               (f"browser evidence {e['id']} registered; awaiting certification", e['case_id']))
    log(db, 'notice', e['case_id'], 'evidence_registered',
        f"{len(capture['frames'])} epoch bundles, gaps={capture['gaps']}", e['id'])
    return 'evidence_registered'


def certify(db, e):
    """Adjudicate a registered capture against the case's signed contract kind.

    A conclusive verdict writes a browser-derived certification and drives the
    linked complaint through complaint.reconcile_notice. Gaps alone write an
    inconclusive certification that keeps the complaint pending and lists the
    missing epochs; the final epoch can never be reported as a full-window pass.
    Re-importing the same (event, evidence, verdict) pair is idempotent.
    """
    case = one(db, 'SELECT * FROM notice_case WHERE id=?', (e['case_id'],))
    if not case:
        raise ValueError('unknown notice case')
    stored = one(db, 'SELECT * FROM notice_evidence WHERE id=?', (e['evidence_id'],))
    if not stored or stored['case_id'] != e['case_id']:
        raise ValueError('certification needs registered evidence for this case')

    frames = {}
    for row in all_rows(db, 'SELECT * FROM notice_epoch_evidence WHERE evidence_id=? ORDER BY epoch',
                        (e['evidence_id'],)):
        frames[row['epoch']] = {
            'epoch': row['epoch'], 'version': row['page_version'],
            'visual_text': row['visual_text'],
            'ax_texts': json.loads(row['ax_texts_json'])}
    capture = {
        'frames': frames, 'gaps': json.loads(stored['gaps_json']),
        'intended_text': stored['intended_text'],
        'intended_version': stored['intended_version']}
    verdict, detail = evidence_mod.adjudicate(case['contract_kind'], capture)

    previous = one(db, 'SELECT id FROM notice_certification WHERE id=?', (e['id'],))
    if previous:
        prior = one(db, 'SELECT evidence_id,mismatch,conclusive,missing_epochs_json,wrong_epochs_json FROM notice_certification WHERE id=?',
                    (e['id'],))
        current_signature = (e['evidence_id'], int(verdict['mismatch'] is True),
                             int(verdict['conclusive']),
                             json.dumps(verdict['missing_epochs']),
                             json.dumps(verdict['wrong_epochs']))
        prior_signature = (prior['evidence_id'], prior['mismatch'], prior['conclusive'],
                           prior['missing_epochs_json'], prior['wrong_epochs_json'])
        if current_signature != prior_signature:
            raise ValueError('immutable certification event id conflict')
        return 'certification_replayed'

    db.execute('''INSERT INTO notice_certification(id,evidence_id,case_id,basis,mismatch,
        conclusive,missing_epochs_json,wrong_epochs_json,review_at) VALUES(?,?,?,?,?,?,?,?,?)''',
               (e['id'], e['evidence_id'], e['case_id'],
                f"browser capture {stored['capture_id'][:12]}",
                int(verdict['mismatch'] is True), int(verdict['conclusive']),
                json.dumps(verdict['missing_epochs']),
                json.dumps(verdict['wrong_epochs']), e['at']))

    if not verdict['conclusive']:
        case_state, reason = 'pending_gap', detail
    elif verdict['mismatch']:
        case_state, reason = 'mismatch', detail
    else:
        case_state, reason = 'aligned', detail
    db.execute('UPDATE notice_case SET state=?,reason=? WHERE id=?',
               (case_state, reason, e['case_id']))
    log(db, 'notice', e['case_id'], case_state, reason, e['id'])

    from .complaint import reconcile_notice
    reconcile_notice(db, e['case_id'], e['id'])
    return case_state
