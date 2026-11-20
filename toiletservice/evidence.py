"""Browser capture verification and contract adjudication.

A capture directory produced by scripts/capture-notice.mjs holds the original
screenshot bytes, rendered DOM, accessibility tree and a manifest. This module
never trusts the manifest on its own: every hash is recomputed from the files
on disk and every text claim is re-derived from the raw DOM / accessibility
tree. A single mismatch raises EvidenceError, so tampered or spliced evidence
can never reach a notice case.

Accessibility conclusions are limited to the text exposed in the browser
accessibility tree; nothing here claims what a screen reader actually speaks.
"""

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = 'capture.json'


class EvidenceError(ValueError):
    """The browser evidence is missing, tampered with or internally inconsistent."""


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_directory(capture_dir):
    candidate = Path(capture_dir)
    if not candidate.exists() and not candidate.is_absolute():
        candidate = ROOT / capture_dir
    manifest = candidate / MANIFEST_NAME
    if not manifest.is_file():
        raise EvidenceError(f'capture manifest not found: {capture_dir}')
    return candidate, manifest


class _NoticeDOM(HTMLParser):
    def __init__(self):
        super().__init__()
        self.notice = {}
        self._visual_parts = []
        self._in_visual = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get('id') == 'notice':
            self.notice = {
                'version': attributes.get('data-version', ''),
                'epoch': attributes.get('data-epoch', '')}
        if attributes.get('id') == 'visual-state':
            self._in_visual = True

    def handle_endtag(self, tag):
        self._in_visual = False

    def handle_data(self, data):
        if self._in_visual:
            self._visual_parts.append(data)

    @property
    def visual_text(self):
        return ''.join(self._visual_parts).strip()


def _derive_dom(dom_path):
    parser = _NoticeDOM()
    parser.feed(Path(dom_path).read_text(encoding='utf8'))
    return parser.notice, parser.visual_text


def _derive_ax_texts(ax_path):
    nodes = json.loads(Path(ax_path).read_text(encoding='utf8'))
    return [node.get('name', {}).get('value')
            for node in nodes
            if node.get('role', {}).get('value') == 'StaticText'
            and node.get('name', {}).get('value') is not None]


def load_capture(capture_dir):
    """Verify a capture directory and return its evidence in structured form."""
    directory, manifest_path = resolve_directory(capture_dir)
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise EvidenceError('capture manifest is not valid JSON') from exc
    if manifest.get('schema') != 'notice-capture/v2':
        raise EvidenceError('unsupported capture schema; re-run the capture script')

    intended_version = manifest['intended_version']
    intended_text = manifest['intended_text']
    first = int(manifest['declared_epoch_first'])
    last = int(manifest['declared_epoch_last'])

    if not manifest.get('monotonic', {}).get('ok'):
        raise EvidenceError('data-epoch was not monotonic; capture cannot testify for the page')

    source_claim = manifest['source_file']['sha256']
    source_actual = sha256_file(directory / manifest['source_file']['name'])
    if source_actual != source_claim:
        raise EvidenceError('notice-source.json hash does not match the manifest')

    frames = {}
    for frame in manifest['frames']:
        epoch = int(frame['epoch'])
        if not first <= epoch <= last:
            raise EvidenceError(f'epoch {epoch} is outside the declared range {first}-{last}')
        if epoch in frames:
            raise EvidenceError(f'duplicate bundle for epoch {epoch}; evidence sets must not be spliced')
        if frame['version'] != intended_version:
            raise EvidenceError(f'epoch {epoch} page version {frame["version"]} != declared {intended_version}')

        png, dom, ax = (frame['files']['screenshot'],
                        frame['files']['dom'], frame['files']['ax_tree'])
        actual = {
            'screenshot': sha256_file(directory / png),
            'dom': sha256_file(directory / dom),
            'ax_tree': sha256_file(directory / ax)}
        claimed = {'screenshot': frame['screenshot_sha256'],
                   'dom': frame['dom_sha256'],
                   'ax_tree': frame['ax_tree_sha256']}
        for kind in claimed:
            if actual[kind] != claimed[kind]:
                raise EvidenceError(f'epoch {epoch} {kind} hash mismatch; original bytes were altered')

        dom_state, visual = _derive_dom(directory / dom)
        if dom_state.get('epoch') != str(epoch) or dom_state.get('version') != intended_version:
            raise EvidenceError(f'epoch {epoch} DOM element state disagrees with its bundle label')
        if visual != frame['visual_text']:
            raise EvidenceError(f'epoch {epoch} visual text in manifest disagrees with rendered DOM')
        ax_texts = _derive_ax_texts(directory / ax)
        if ax_texts != frame['ax_static_text']:
            raise EvidenceError(f'epoch {epoch} accessibility tree text in manifest disagrees with raw AX tree')

        frames[epoch] = {
            'epoch': epoch, 'version': frame['version'], 'visual_text': visual,
            'ax_texts': ax_texts, 'browser_clock_ms': float(frame['browser_clock_ms']),
            'captured_at': frame['captured_at'],
            'screenshot_sha256': actual['screenshot'], 'dom_sha256': actual['dom'],
            'ax_tree_sha256': actual['ax_tree'],
            'screenshot_file': png, 'dom_file': dom, 'ax_tree_file': ax}

    captured = set(frames)
    gap_epochs = sorted(int(item['epoch']) for item in manifest.get('gaps', []))
    if sorted(gap_epochs) != sorted(set(range(first, last + 1)) - captured):
        raise EvidenceError('manifest gap list does not reconcile with the declared epochs and bundles')
    if manifest.get('torn'):
        # Torn bundles must already appear as gaps, never as frames.
        if any(int(item.get('epoch', -1)) in captured for item in manifest['torn'] if 'epoch' in item):
            raise EvidenceError('a torn bundle was persisted as evidence; cross-epoch splicing is forbidden')

    return {
        'capture_id': hashlib.sha256(manifest_bytes).hexdigest(),
        'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'facility_id': manifest['facility_id'],
        'opening_version': int(manifest['opening_version']),
        'intended_version': intended_version, 'intended_text': intended_text,
        'declared_first': first, 'declared_last': last,
        'browser_product': manifest['browser']['product'],
        'monotonic_ok': bool(manifest['monotonic']['ok']),
        'source_sha256': source_actual,
        'frames': frames, 'gaps': gap_epochs,
        'registered_at': manifest.get('captured_at')}


def epoch_texts_align(frame, intended_text, intended_version):
    """Both texts must expose the intended closure notice under the same version."""
    return (frame['version'] == intended_version
            and frame['visual_text'] == intended_text
            and intended_text in frame['ax_texts'])


def adjudicate(contract_kind, capture):
    """Return (verdict, detail) for one contract kind against verified capture.

    verdict is one of:
      {'mismatch': True}  -> conclusive breach, confirm the linked complaint
      {'mismatch': False} -> conclusive alignment, reject the linked complaint
      {'mismatch': None}  -> evidence gaps only, the case stays pending review
    """
    frames = capture['frames']
    gaps = capture['gaps']
    intended_text = capture['intended_text']
    intended_version = capture['intended_version']
    wrong_epochs = sorted(epoch for epoch, frame in frames.items()
                          if not epoch_texts_align(frame, intended_text, intended_version))

    if contract_kind == 'final':
        # Only the last fully captured epoch at window close matters. Gaps in
        # earlier epochs cannot erase a captured wrong frame, but do not by
        # themselves block the final-state contract.
        if not frames:
            return {'mismatch': None, 'conclusive': False,
                    'wrong_epochs': [], 'missing_epochs': gaps}, \
                   'no intact epoch captured for the final-state contract'
        last_epoch = max(frames)
        if wrong_epochs and last_epoch in wrong_epochs:
            return {'mismatch': True, 'conclusive': True,
                    'wrong_epochs': wrong_epochs, 'missing_epochs': gaps}, \
                   f'last complete epoch {last_epoch} does not show the intended notice'
        return {'mismatch': False, 'conclusive': True,
                'wrong_epochs': wrong_epochs, 'missing_epochs': gaps}, \
               f'last complete epoch {last_epoch} matches the intended notice'

    if contract_kind == 'continuous':
        # Every declared epoch must have a same-version complete bundle and
        # both texts must read as the intended closure. A provably wrong
        # epoch confirms the breach; gaps alone keep the case open.
        if wrong_epochs:
            return {'mismatch': True, 'conclusive': True,
                    'wrong_epochs': wrong_epochs, 'missing_epochs': gaps}, \
                   f'captured epoch(s) {wrong_epochs} contradict the continuous contract'
        if gaps:
            return {'mismatch': None, 'conclusive': False,
                    'wrong_epochs': [], 'missing_epochs': gaps}, \
                   f'declared epoch(s) {gaps} lack intact same-version evidence'
        return {'mismatch': False, 'conclusive': True,
                'wrong_epochs': [], 'missing_epochs': []}, \
               'every declared epoch matches the intended notice'

    raise EvidenceError(f'unknown contract kind {contract_kind}')
