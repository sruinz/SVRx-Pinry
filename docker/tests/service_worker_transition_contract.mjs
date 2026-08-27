import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '../..');
const workerPath = path.join(root, 'pinry-spa/src/service-worker.js');
const transitionPath = path.join(root, 'docker/sw-transition/index.html');

function loadWorker({ cacheNames = [], navigateResult = Promise.resolve() } = {}) {
  const listeners = new Map();
  const deleted = [];
  const fetched = [];
  const navigated = [];
  const state = { claimed: false, skipped: false };

  class FakeRequest {
    constructor(input, init = {}) {
      this.url = typeof input === 'string' ? input : input.url;
      this.mode = typeof input === 'string' ? init.mode : input.mode;
      this.method = typeof input === 'string' ? (init.method || 'GET') : input.method;
      this.cache = init.cache;
    }
  }

  const self = {
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    skipWaiting() {
      state.skipped = true;
      return Promise.resolve();
    },
    clients: {
      claim() {
        state.claimed = true;
        return Promise.resolve();
      },
      matchAll(options) {
        assert.equal(options.type, 'window');
        assert.equal(options.includeUncontrolled, true);
        assert.deepEqual(Object.keys(options).sort(), ['includeUncontrolled', 'type']);
        return Promise.resolve([
          {
            url: 'https://pinry.example/',
            navigate(url) {
              navigated.push(url);
              return navigateResult;
            },
          },
        ]);
      },
    },
  };
  const context = vm.createContext({
    self,
    caches: {
      keys: async () => [...cacheNames],
      delete: async (name) => {
        deleted.push(name);
        return true;
      },
    },
    Request: FakeRequest,
    fetch: async (request) => {
      fetched.push(request);
      return { ok: true, url: request.url };
    },
    Promise,
  });
  vm.runInContext(fs.readFileSync(workerPath, 'utf8'), context, { filename: workerPath });
  return {
    deleted, fetched, listeners, navigated, state,
  };
}

async function dispatchExtendable(listener, event = {}) {
  let pending;
  listener({
    ...event,
    waitUntil(value) {
      pending = Promise.resolve(value);
    },
  });
  await pending;
}

test('install/activate가 즉시 전환하고 기존 Pinry SPA 캐시만 삭제한다', async () => {
  // 이 테스트는 skipWaiting, claim 또는 접두사 필터가 빠지면 실패한다.
  const worker = loadWorker({
    cacheNames: ['pinry-spa-precache-v1', 'unrelated-cache', 'pinry-spa-runtime-v2'],
  });

  await dispatchExtendable(worker.listeners.get('install'));
  await dispatchExtendable(worker.listeners.get('activate'));

  assert.equal(worker.state.skipped, true);
  assert.equal(worker.state.claimed, true);
  assert.deepEqual(worker.deleted, ['pinry-spa-precache-v1', 'pinry-spa-runtime-v2']);
  assert.deepEqual(worker.navigated, ['https://pinry.example/']);
});

test('activate는 창 탐색 완료를 기다리며 교착하지 않는다', async () => {
  // client.navigate는 새 워커 활성화 완료를 기다릴 수 있으므로 activate가
  // 그 Promise를 다시 기다리면 상호 대기가 발생한다.
  const worker = loadWorker({ navigateResult: new Promise(() => {}) });
  const outcome = await Promise.race([
    dispatchExtendable(worker.listeners.get('activate')).then(() => 'activated'),
    new Promise((resolve) => setTimeout(() => resolve('timeout'), 30)),
  ]);

  assert.equal(outcome, 'activated');
  assert.deepEqual(worker.navigated, ['https://pinry.example/']);
});

test('탐색 요청은 no-store 네트워크로만 응답한다', async () => {
  // 이 테스트는 navigation fetch가 캐시를 조회하거나 no-store를 잃으면 실패한다.
  const worker = loadWorker();
  let response;
  worker.listeners.get('fetch')({
    request: { url: 'https://pinry.example/board/1', mode: 'navigate', method: 'GET' },
    respondWith(value) {
      response = Promise.resolve(value);
    },
  });

  await response;
  assert.equal(worker.fetched.length, 1);
  assert.equal(worker.fetched[0].url, 'https://pinry.example/board/1');
  assert.equal(worker.fetched[0].cache, 'no-store');

  let nonNavigationIntercepted = false;
  worker.listeners.get('fetch')({
    request: { url: 'https://pinry.example/api/v2/pins/', mode: 'cors', method: 'GET' },
    respondWith() {
      nonNavigationIntercepted = true;
    },
  });
  assert.equal(nonNavigationIntercepted, false);
});

test('controller ping에 network-only-v1 ACK를 보낸다', () => {
  // 이 테스트는 메시지 유형과 버전이 서로 맞지 않으면 실패한다.
  const worker = loadWorker();
  const replies = [];
  worker.listeners.get('message')({
    data: { type: 'SVRX_PINRY_NETWORK_ONLY_PING' },
    source: { postMessage: (message) => replies.push(message) },
  });
  assert.equal(JSON.stringify(replies), JSON.stringify([{
    type: 'SVRX_PINRY_NETWORK_ONLY_ACK',
    version: 'network-only-v1',
  }]));
});

test('워커 소스에 Workbox·precache·cache match 우회가 없다', () => {
  // 이 테스트는 네트워크 전용 계약을 깨는 재도입을 막는다.
  const source = fs.readFileSync(workerPath, 'utf8');
  for (const forbidden of ['importScripts', 'precache', 'caches.match', 'workbox']) {
    assert.equal(source.toLowerCase().includes(forbidden.toLowerCase()), false, forbidden);
  }
});

test('전환 화면은 controller ACK와 레거시 캐시 0개를 모두 확인한 뒤 완료를 표시한다', async () => {
  // 이 테스트는 ACK 또는 캐시 확인 하나만으로 ready가 되면 실패한다.
  const html = fs.readFileSync(transitionPath, 'utf8');
  const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length > 0, '인라인 전환 스크립트가 필요합니다.');

  const elements = {
    status: { textContent: '' },
    detail: { textContent: '' },
  };
  const messageListeners = [];
  const controller = {
    postMessage(message) {
      assert.equal(message.type, 'SVRX_PINRY_NETWORK_ONLY_PING');
      queueMicrotask(() => {
        for (const listener of messageListeners) {
          listener({
            source: controller,
            data: { type: 'SVRX_PINRY_NETWORK_ONLY_ACK', version: 'network-only-v1' },
          });
        }
      });
    },
  };
  const navigator = {
    serviceWorker: {
      controller,
      ready: Promise.resolve({ active: controller }),
      register: async (url) => {
        assert.equal(url, '/service-worker.js');
        return { update: async () => undefined };
      },
      addEventListener(type, listener) {
        if (type === 'message') messageListeners.push(listener);
      },
    },
  };
  const context = vm.createContext({
    caches: { keys: async () => ['other-app-cache'] },
    document: { getElementById: (id) => elements[id] },
    navigator,
    self: { caches: true },
    queueMicrotask,
    setTimeout,
    clearTimeout,
    Promise,
  });
  vm.runInContext(scripts.at(-1)[1], context, { filename: transitionPath });

  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(elements.status.textContent, '전환 준비 완료');
  assert.match(elements.detail.textContent, /본 이전 이미지/);
});

test('기존 cache-first 워커를 건너뛰면 첫 탐색은 캐시된 구 앱일 수 있다', async () => {
  // 이 negative proof는 단일 이미지 교체로 첫 화면을 보장한다는 잘못된 가정을 막는다.
  const staleApp = { body: '구 SVRx Pinry 앱' };
  let networkCalls = 0;
  const cacheFirstNavigation = async () => staleApp || (() => {
    networkCalls += 1;
    return { body: '한국어 이전 화면' };
  })();

  const response = await cacheFirstNavigation();
  assert.equal(response.body, '구 SVRx Pinry 앱');
  assert.equal(networkCalls, 0);
});
