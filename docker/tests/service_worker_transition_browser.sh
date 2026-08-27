#!/bin/sh
set -eu

CHROME_BIN=${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}

if [ ! -x "$CHROME_BIN" ]; then
    echo "SKIP: Chromium 브라우저를 찾을 수 없습니다."
    exit 77
fi

CHROME_BIN="$CHROME_BIN" node --input-type=module <<'NODE'
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';

const root = process.cwd();
const worker = fs.readFileSync(path.join(root, 'pinry-spa/src/service-worker.js'), 'utf8');
const transition = fs.readFileSync(path.join(root, 'docker/sw-transition/index.html'), 'utf8');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'svrx-pinry-sw-profile-'));
let mode = 'old';

const oldPage = `<!doctype html><html lang="ko"><body><main>구 SVRx Pinry 앱</main><script>
navigator.serviceWorker.register('/service-worker.js');
</script></body></html>`;
const oldWorker = `'use strict';
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open('pinry-spa-old').then((cache) => cache.add('/index.html')));
});
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate') {
    event.respondWith(caches.match('/index.html').then((response) => response || fetch(event.request)));
  }
});`;

function htmlForMode() {
  if (mode === 'old') return oldPage;
  if (mode === 'transition') return transition;
  if (mode === 'migration') {
    return '<!doctype html><html lang="ko"><body><main>SVRx Pinry 데이터 이전 중</main></body></html>';
  }
  return '<!doctype html><html lang="ko"><body><main>SVRx Pinry 서비스 준비 완료</main></body></html>';
}

const server = http.createServer((request, response) => {
  if (request.url === '/service-worker.js') {
    response.writeHead(200, {
      'Cache-Control': 'no-store',
      'Content-Type': 'application/javascript',
      'Service-Worker-Allowed': '/',
      'X-Content-Type-Options': 'nosniff',
    });
    response.end(mode === 'old' ? oldWorker : worker);
    return;
  }
  if (request.url === '/' || request.url === '/index.html') {
    response.writeHead(200, {
      'Cache-Control': 'no-store',
      'Content-Type': 'text/html; charset=utf-8',
    });
    response.end(htmlForMode());
    return;
  }
  response.writeHead(404);
  response.end('not found');
});

await new Promise((resolve, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', resolve);
});
const address = server.address();
const origin = `http://127.0.0.1:${address.port}`;

const chrome = spawn(process.env.CHROME_BIN, [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--remote-debugging-port=0',
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: 'ignore' });

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForFile(filename, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (fs.existsSync(filename)) return;
    if (chrome.exitCode !== null) throw new Error(`Chromium이 조기 종료됨: ${chrome.exitCode}`);
    await sleep(50);
  }
  throw new Error(`${filename} 대기 시간 초과`);
}

class CdpSession {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve, reject, timeout } = this.pending.get(message.id);
      this.pending.delete(message.id);
      clearTimeout(timeout);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    });
    socket.addEventListener('close', () => {
      for (const { reject, timeout } of this.pending.values()) {
        clearTimeout(timeout);
        reject(new Error('CDP 연결이 탐색 중 종료되었습니다.'));
      }
      this.pending.clear();
    });
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP 명령 시간 초과: ${method}`));
      }, 3000);
      this.pending.set(id, { resolve, reject, timeout });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

async function connect(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  return new CdpSession(socket);
}

let cdp;
try {
  const activePort = path.join(profile, 'DevToolsActivePort');
  await waitForFile(activePort);
  const [debugPort] = fs.readFileSync(activePort, 'utf8').trim().split('\n');
  const targetResponse = await fetch(
    `http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(origin)}`,
    { method: 'PUT' },
  );
  assert.equal(targetResponse.ok, true, 'CDP target 생성 실패');
  const target = await targetResponse.json();
  cdp = await connect(target.webSocketDebuggerUrl);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('ServiceWorker.enable');

  async function evaluate(expression) {
    const result = await cdp.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || 'Runtime.evaluate 실패');
    }
    return result.result.value;
  }

  async function navigate(url = origin) {
    await cdp.send('Page.navigate', { url });
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      try {
        if (await evaluate('document.readyState === "complete"')) return;
      } catch (_) {
        // 탐색 중의 일시적 context 교체는 재시도한다.
      }
      await sleep(50);
    }
    throw new Error(`탐색 시간 초과: ${url}`);
  }

  async function waitUntil(expression, description, timeout = 10000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try {
        if (await evaluate(expression)) return;
      } catch (_) {
        // 서비스 워커가 창을 다시 여는 동안 실행 context가 교체될 수 있다.
      }
      await sleep(100);
    }
    throw new Error(`${description} 대기 시간 초과`);
  }

  await navigate();
  await waitUntil('navigator.serviceWorker.ready.then(() => true)', '기존 워커 ready');
  await navigate();
  await waitUntil('Boolean(navigator.serviceWorker.controller)', '기존 controller');
  assert.match(await evaluate('document.body.textContent'), /구 SVRx Pinry 앱/);
  assert.equal(
    await evaluate('caches.keys().then((names) => names.filter((name) => name.startsWith("pinry-spa-")).length)'),
    1,
  );

  mode = 'migration';
  await navigate();
  assert.match(await evaluate('document.body.textContent'), /구 SVRx Pinry 앱/);
  console.log('NEGATIVE_PROOF_OK');

  mode = 'transition';
  await navigate();
  assert.match(await evaluate('document.body.textContent'), /구 SVRx Pinry 앱/);
  console.log('FIRST_TRANSITION_NAVIGATION_STALE_OK');

  // 첫 응답은 기존 cache-first 워커가 가릴 수 있다. 이후 수동 update나
  // 추가 Page.navigate 없이 브라우저의 자동 갱신과 제품 워커가 현재 창을
  // 전환해 완료 화면을 표시해야 한다.
  await waitUntil(
    'document.getElementById("status")?.textContent === "전환 준비 완료"',
    '한 번의 사용자 탐색 후 전환 준비 완료 화면',
  );
  assert.equal(
    await evaluate('caches.keys().then((names) => names.filter((name) => name.startsWith("pinry-spa-")).length)'),
    0,
  );
  console.log('TRANSITION_READY_OK');

  mode = 'migration';
  await navigate();
  assert.match(await evaluate('document.body.textContent'), /데이터 이전 중/);
  console.log('FIRST_MIGRATION_NAVIGATION_OK');

  mode = 'app';
  await navigate();
  assert.match(await evaluate('document.body.textContent'), /서비스 준비 완료/);
  console.log('MARKER_REMOVED_APP_OK');
  console.log('SERVICE_WORKER_TRANSITION_BROWSER_OK');
} finally {
  if (cdp?.socket?.readyState === WebSocket.OPEN) cdp.socket.close();
  chrome.kill('SIGTERM');
  await Promise.race([
    new Promise((resolve) => chrome.once('exit', resolve)),
    sleep(3000),
  ]);
  server.close();
  fs.rmSync(profile, { recursive: true, force: true });
}
NODE
