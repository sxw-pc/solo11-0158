import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const page = pathToFileURL(path.join(root, 'mock', 'notice.html')).href;

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (const item of argv.slice(2)) {
    if (item.startsWith('--')) {
      const [key, value] = item.slice(2).split('=');
      flags[key] = value ?? 'true';
    } else positional.push(item);
  }
  return {output: path.resolve(positional[0] ?? fs.mkdtempSync(path.join(os.tmpdir(), 'toilet-notice-'))), flags};
}
const {output, flags} = parseArgs(process.argv);
fs.mkdirSync(output, {recursive: true});

const mode = flags.mode ?? 'observer'; // observer | fixed
const sourcePath = path.resolve(flags.source ?? path.join(root, 'mock', 'notice-source.json'));
const source = JSON.parse(fs.readFileSync(sourcePath, 'utf8'));
const declaredFirst = Number(source.declared_epoch_first);
const declaredLast = Number(source.declared_epoch_last);
const intendedVersion = `v${Number(source.opening_version)}`;
// Optional allow-list of epochs for which intact bundles may be persisted.
// The in-page observer still records every transition, so missed epochs
// show up as gaps instead of being silently hidden.
const schedule = flags.schedule
  ? new Set(flags.schedule.split(',').map(value => Number(value.trim())).filter(Number.isInteger))
  : null;
const pollMs = Number(process.env.CAPTURE_POLL_MS ?? 25);
const settleMs = Number(process.env.CAPTURE_SETTLE_MS ?? 40);
const hardDeadlineMs = Number(source.observation_end_ms) + 1500;

const edge = process.env.EDGE_BIN ?? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'toilet-edge-'));
const browserArgs = ['--headless=new', '--disable-gpu', '--no-first-run',
  '--no-default-browser-check', '--disable-background-networking',
  '--remote-debugging-port=0', '--remote-allow-origins=*',
  `--user-data-dir=${profile}`, '--window-size=800,600', 'about:blank'];
// Unprivileged Linux containers cannot use the setuid/user namespace sandbox.
if (process.platform !== 'win32' && process.env.CAPTURE_NO_SANDBOX !== '0') browserArgs.push('--no-sandbox');
const browser = spawn(edge, browserArgs, {stdio: 'ignore', windowsHide: true});
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

// Installed before any page script runs. It waits for #notice (document_start
// precedes DOM body) and then records every data-epoch/data-version mutation,
// giving an authoritative monotonic transition log even when the host-side
// sampler is too busy to poll at that instant.
const recorderSource = `(() => {
  window.__noticeRecord = {installedAt: performance.now(), initial: null, transitions: [], monotonicViolations: []};
  const snapshot = notice => ({
    version: notice.dataset.version || '',
    epoch: notice.dataset.epoch || '',
    at: Number(performance.now().toFixed(3))
  });
  const install = () => {
    const notice = document.querySelector('#notice');
    if (!notice) return false;
    window.__noticeRecord.initial = snapshot(notice);
    new MutationObserver(() => {
      const current = snapshot(notice);
      const log = window.__noticeRecord.transitions;
      const previous = log.length ? log[log.length - 1] : window.__noticeRecord.initial;
      if (previous && (current.epoch !== previous.epoch || current.version !== previous.version)) {
        const previousEpoch = Number(previous.epoch);
        if (Number.isInteger(previousEpoch) && Number.isInteger(Number(current.epoch))
            && Number(current.epoch) <= previousEpoch) {
          window.__noticeRecord.monotonicViolations.push({from: previous, to: current, at: current.at});
        }
        log.push(current);
      }
    }).observe(notice, {attributes: true, attributeFilter: ['data-epoch', 'data-version']});
    return true;
  };
  if (!install()) {
    const watcher = new MutationObserver(() => { if (install()) watcher.disconnect(); });
    watcher.observe(document, {childList: true, subtree: true});
  }
})();`;

const stateExpression = `JSON.stringify({
  state: (() => { const n = document.querySelector('#notice');
    return n ? {version: n.dataset.version, epoch: n.dataset.epoch} : null; })(),
  visual: document.querySelector('#visual-state')?.textContent ?? '',
  clock: Number(performance.now().toFixed(3)),
  html: document.documentElement.outerHTML,
  record: window.__noticeRecord ?? null
})`;

let ws;
try {
  const portFile = path.join(profile, 'DevToolsActivePort');
  for (let retry = 0; retry < 100 && !fs.existsSync(portFile); retry++) await pause(100);
  if (!fs.existsSync(portFile)) throw new Error('browser debugger did not start');
  const port = fs.readFileSync(portFile, 'utf8').split(/\r?\n/)[0];
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const target = targets.find(item => item.type === 'page');
  if (!target) throw new Error('browser page target missing');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  let serial = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const reply = JSON.parse(event.data);
    if (!reply.id || !pending.has(reply.id)) return;
    const {resolve, reject} = pending.get(reply.id);
    pending.delete(reply.id);
    if (reply.error) reject(new Error(reply.error.message));
    else resolve(reply.result);
  };
  const call = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++serial;
    pending.set(id, {resolve, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
  await call('Page.enable');
  await call('Runtime.enable');
  await call('Accessibility.enable');
  // Key line: the recorder is committed before navigation, hence before
  // the page's own setTimeout script can touch data-epoch.
  await call('Page.addScriptToEvaluateOnNewDocument', {source: recorderSource});
  const browserVersion = await call('Browser.getVersion');
  await call('Page.navigate', {url: page});
  const started = Date.now();
  const iso = () => new Date().toISOString();

  const staticTexts = nodes => nodes
    .filter(node => node.role?.value === 'StaticText')
    .map(node => node.name?.value)
    .filter(value => value !== undefined);

  // One atomic bundle per observed epoch. Pre/post state must describe the
  // same epoch and page version, otherwise the bundle is torn and is never
  // spliced together: it is discarded and the epoch is recorded as a gap.
  async function captureBundle(prefix) {
    const pre = JSON.parse((await call('Runtime.evaluate',
      {expression: stateExpression, returnByValue: true})).result.value);
    const accessibility = await call('Accessibility.getFullAXTree');
    const screenshot = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    const post = JSON.parse((await call('Runtime.evaluate',
      {expression: stateExpression, returnByValue: true})).result.value);
    if (!pre.state || !post.state
      || pre.state.epoch !== post.state.epoch || pre.state.version !== post.state.version) {
      return {torn: true, pre: pre.state, post: post.state};
    }
    const png = Buffer.from(screenshot.data, 'base64');
    const html = Buffer.from(pre.html, 'utf8');
    const ax = Buffer.from(JSON.stringify(accessibility.nodes, null, 2), 'utf8');
    const pngFile = `${prefix}.png`, htmlFile = `${prefix}.html`, axFile = `${prefix}.ax.json`;
    fs.writeFileSync(path.join(output, pngFile), png);
    fs.writeFileSync(path.join(output, htmlFile), html);
    fs.writeFileSync(path.join(output, axFile), ax);
    return {
      epoch: Number(pre.state.epoch), version: pre.state.version,
      visual_text: pre.visual, ax_static_text: staticTexts(accessibility.nodes),
      browser_clock_ms: pre.clock, captured_at: iso(),
      elapsed_ms: Date.now() - started,
      files: {screenshot: pngFile, dom: htmlFile, ax_tree: axFile},
      screenshot_sha256: sha256(png), dom_sha256: sha256(html), ax_tree_sha256: sha256(ax)
    };
  }

  const frames = [];
  const gaps = [];
  const torn = [];
  const capturedEpochs = new Set();
  let lastRecord = null;

  if (mode === 'fixed') {
    // Legacy behaviour retained as the "machine busy" demonstration: four
    // shutters at fixed wall-clock moments, no epoch following. Gaps are
    // derived afterwards against the declared epoch range. The shutter grid
    // can be overridden (e.g. --shutter-ms=400,2100) to reproduce a busy
    // machine that only saw the first and the last epoch.
    const shutters = flags['shutter-ms']
      ? flags['shutter-ms'].split(',').map(value => Number(value.trim()))
      : [500, 1000, 1500, 2100];
    for (const [index, targetMs] of shutters.entries()) {
      await pause(Math.max(0, targetMs - (Date.now() - started)));
      const bundle = await captureBundle(`frame-${index}`);
      if (bundle.torn) {
        torn.push({shutter: index, epoch: bundle.pre ? Number(bundle.pre.epoch) : null,
          pre: bundle.pre, post: bundle.post,
          reason: 'epoch changed while the evidence bundle was being captured'});
        continue;
      }
      bundle.shutter = index;
      frames.push(bundle);
      capturedEpochs.add(bundle.epoch);
    }
    const evaluated = JSON.parse((await call('Runtime.evaluate',
      {expression: stateExpression, returnByValue: true})).result.value);
    lastRecord = evaluated.record;
  } else {
    // Observer mode: poll the page, capture each declared epoch the first
    // time it is observed, keep polling until the last declared epoch was
    // seen by the in-page recorder or the hard deadline passes.
    while (Date.now() - started < hardDeadlineMs) {
      const evaluated = JSON.parse((await call('Runtime.evaluate',
        {expression: stateExpression, returnByValue: true})).result.value);
      lastRecord = evaluated.record;
      const currentEpoch = evaluated.state ? Number(evaluated.state.epoch) : NaN;
      const observedSequence = [lastRecord?.initial, ...(lastRecord?.transitions ?? [])]
        .filter(Boolean).map(item => Number(item.epoch));
      const lastObserved = observedSequence.length ? Math.max(...observedSequence) : NaN;
      if (Number.isInteger(currentEpoch) && currentEpoch >= declaredFirst && currentEpoch <= declaredLast
          && !capturedEpochs.has(currentEpoch) && (!schedule || schedule.has(currentEpoch))) {
        const bundle = await captureBundle(`epoch-${currentEpoch}`);
        if (bundle.torn) {
          torn.push({epoch: currentEpoch, pre: bundle.pre, post: bundle.post,
            reason: 'epoch changed while the evidence bundle was being captured'});
        } else {
          frames.push(bundle);
          capturedEpochs.add(currentEpoch);
        }
      }
      const finished = Number.isInteger(lastObserved) && lastObserved >= declaredLast
        && (!schedule || [...schedule].every(epoch => capturedEpochs.has(epoch)
            || torn.some(item => item.epoch === epoch)))
        && capturedEpochs.size + torn.length >= (schedule ? schedule.size : declaredLast - declaredFirst + 1);
      if (finished) { await pause(settleMs); break; }
      await pause(pollMs);
    }
  }

  frames.sort((a, b) => a.epoch - b.epoch);
  const observedEpochs = new Set([lastRecord?.initial, ...(lastRecord?.transitions ?? [])]
    .filter(Boolean).map(item => Number(item.epoch)));
  for (let epoch = declaredFirst; epoch <= declaredLast; epoch += 1) {
    if (capturedEpochs.has(epoch)) continue;
    const tear = torn.find(item => item.epoch === epoch);
    if (tear) gaps.push({epoch, reason: tear.reason ?? 'torn bundle discarded'});
    else if (observedEpochs.has(epoch))
      gaps.push({epoch, reason: 'epoch transition observed in-page but no intact bundle was sampled'});
    else gaps.push({epoch, reason: 'epoch never observed before the capture window ended'});
  }
  const monotonicSequence = [lastRecord?.initial, ...(lastRecord?.transitions ?? [])]
    .filter(Boolean).map(item => Number(item.epoch));
  const sourceBytes = fs.readFileSync(sourcePath);
  // Self-contained evidence directory: keep a copy of the declaration file
  // so hashes can be re-verified without reaching back into mock/.
  fs.writeFileSync(path.join(output, 'notice-source.json'), sourceBytes);

  const manifest = {
    schema: 'notice-capture/v2',
    page,
    mode,
    facility_id: source.facility_id,
    opening_version: Number(source.opening_version),
    intended_text: source.intended_text,
    intended_version: intendedVersion,
    declared_epoch_first: declaredFirst,
    declared_epoch_last: declaredLast,
    observation_window_ms: {
      published_at: Number(source.published_at_ms),
      observation_end: Number(source.observation_end_ms)
    },
    captured_at: iso(),
    browser: {
      product: browserVersion.product,
      revision: browserVersion.revision,
      user_agent: browserVersion.userAgent,
      executable: edge
    },
    schedule: schedule ? [...schedule].sort((a, b) => a - b) : null,
    transitions: lastRecord?.transitions ?? [],
    initial: lastRecord?.initial ?? null,
    monotonic: {
      ok: (lastRecord?.monotonicViolations ?? []).length === 0,
      sequence: monotonicSequence,
      violations: lastRecord?.monotonicViolations ?? []
    },
    source_file: {name: 'notice-source.json', sha256: sha256(sourceBytes)},
    frames,
    gaps,
    torn
  };
  fs.writeFileSync(path.join(output, 'capture.json'), JSON.stringify(manifest, null, 2));
  process.stdout.write(JSON.stringify({...manifest, output}) + '\n');
} finally {
  if (ws) ws.close();
  browser.kill();
}
