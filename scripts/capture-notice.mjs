import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {fileURLToPath, pathToFileURL} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const page = pathToFileURL(path.join(root, 'mock', 'notice.html')).href;
const output = path.resolve(process.argv[2] ?? fs.mkdtempSync(path.join(os.tmpdir(), 'toilet-notice-')));
fs.mkdirSync(output, {recursive: true});
const edge = process.env.EDGE_BIN ?? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'toilet-edge-'));
const browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--no-default-browser-check', '--disable-background-networking',
  '--remote-debugging-port=0', '--remote-allow-origins=*',
  `--user-data-dir=${profile}`, '--window-size=800,600', 'about:blank'],
{stdio: 'ignore', windowsHide: true});
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
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
  await call('Page.navigate', {url: page});
  const started = Date.now();
  const frames = [];
  for (const [index, targetMs] of [500, 1000, 1500, 2100].entries()) {
    await pause(Math.max(0, targetMs - (Date.now() - started)));
    const evaluated = await call('Runtime.evaluate', {expression: `JSON.stringify({
      version: document.querySelector('#notice').dataset.version,
      epoch: document.querySelector('#notice').dataset.epoch,
      visual: document.querySelector('#visual-state').textContent,
      clock: performance.now(), html: document.documentElement.outerHTML
    })`, returnByValue: true});
    const {version, epoch, visual, clock, html} = JSON.parse(evaluated.result.value);
    const accessibility = await call('Accessibility.getFullAXTree');
    const screenshot = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
    const png = Buffer.from(screenshot.data, 'base64');
    fs.writeFileSync(path.join(output, `frame-${index}.png`), png);
    fs.writeFileSync(path.join(output, `frame-${index}.html`), html);
    fs.writeFileSync(path.join(output, `frame-${index}.ax.json`), JSON.stringify(accessibility.nodes, null, 2));
    frames.push({index, elapsed_ms: Date.now() - started, browser_clock_ms: clock,
      version, epoch, visual, ax_static_text: accessibility.nodes.filter(node =>
        node.role?.value === 'StaticText').map(node => node.name?.value),
      screenshot_sha256: crypto.createHash('sha256').update(png).digest('hex')});
  }
  const manifest = {page, frames};
  fs.writeFileSync(path.join(output, 'capture.json'), JSON.stringify(manifest, null, 2));
  process.stdout.write(JSON.stringify({...manifest, output}) + '\n');
} finally {
  if (ws) ws.close();
  browser.kill();
}
