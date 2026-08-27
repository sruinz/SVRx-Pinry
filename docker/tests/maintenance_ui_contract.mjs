import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";


const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT_PATH = path.resolve(HERE, "../migration/migration.js");
const HTML_PATH = path.resolve(HERE, "../migration/index.html");
const CSS_PATH = path.resolve(HERE, "../migration/migration.css");
assert.ok(existsSync(SCRIPT_PATH), "migration.js 구현이 필요합니다.");
const require = createRequire(import.meta.url);
const ui = require(SCRIPT_PATH);
const html = readFileSync(HTML_PATH, "utf8");
const css = readFileSync(CSS_PATH, "utf8");


function status(overrides = {}) {
  return {
    schema_version: 1,
    state: "migrating",
    phase: "copying",
    phase_label: "이미지 파일 이전",
    run_id: "20260827T010203Z-12345678-1234-4678-9234-567812345678",
    attempt: 1,
    resume_count: 0,
    started_at: "2026-08-27T01:02:03Z",
    phase_started_at: "2026-08-27T01:03:00Z",
    heartbeat_at: "2026-08-27T01:04:55Z",
    progress_at: "2026-08-27T01:04:50Z",
    last_committed_batch: 2,
    images_done: 12,
    images_total: 100,
    files_done: 36,
    files_total: 300,
    backfill_done: 0,
    backfill_total: 100,
    phase_percent: 12,
    overall_percent: 6,
    error_class: null,
    error_code: null,
    ...overrides,
  };
}


function response(payload, options = {}) {
  return {
    ok: options.ok ?? true,
    status: options.status ?? 200,
    json: async () => {
      if (options.jsonError) {
        throw new SyntaxError("invalid json");
      }
      return payload;
    },
  };
}


function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}


function controllerHarness(fetchFn, overrides = {}) {
  const renders = [];
  const staticErrors = [];
  const openDestinations = [];
  const scheduled = [];
  const location = {
    pathname: "/pins/42",
    search: "?x=1",
    hash: "#y",
    replacements: [],
    replace(destination) {
      this.replacements.push(destination);
    },
    assign() {},
  };
  const documentRef = {
    visibilityState: "visible",
    listeners: {},
    addEventListener(name, listener) {
      this.listeners[name] = listener;
    },
    removeEventListener(name) {
      delete this.listeners[name];
    },
  };
  const adapter = {
    renderStatus(payload, now) {
      renders.push({ payload, now });
    },
    renderStaticError(failures) {
      staticErrors.push(failures);
    },
    showOpen(destination) {
      openDestinations.push(destination);
    },
    bindActions() {},
  };
  const controller = new ui.MigrationController({
    fetchFn,
    adapter,
    documentRef,
    locationRef: location,
    nowFn: () => Date.parse("2026-08-27T01:05:00Z"),
    setTimeoutFn(callback, delay) {
      scheduled.push({ callback, delay });
      return scheduled.length;
    },
    clearTimeoutFn() {},
    ...overrides,
  });
  return {
    adapter,
    controller,
    documentRef,
    location,
    openDestinations,
    renders,
    scheduled,
    staticErrors,
  };
}


function fakeDocument() {
  const ids = [
    "page-title", "state-badge", "phase-name", "overall-progress",
    "overall-progress-value", "phase-progress", "phase-progress-value",
    "images-count", "files-count", "backfill-count", "heartbeat-age",
    "progress-age", "resume-count", "last-batch", "notice-title",
    "notice-text", "error-panel", "error-title", "operator-hint",
    "error-class", "error-code", "refresh-button", "copy-error-button",
    "copy-diagnostics-button", "open-button", "copy-result", "live-status",
  ];
  const nodes = Object.fromEntries(ids.map((id) => [id, {
    id,
    hidden: false,
    textContent: "",
    value: null,
    removed: [],
    attributes: {},
    listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    removeAttribute(name) { this.removed.push(name); },
    setAttribute(name, value) { this.attributes[name] = value; },
  }]));
  return {
    nodes,
    visibilityState: "visible",
    listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    removeEventListener(name) { delete this.listeners[name]; },
    getElementById(id) { return nodes[id] || null; },
  };
}


function contrastRatio(foreground, background) {
  function luminance(hex) {
    const channels = [1, 3, 5].map((offset) => (
      Number.parseInt(hex.slice(offset, offset + 2), 16) / 255
    )).map((value) => (
      value <= 0.04045
        ? value / 12.92
        : ((value + 0.055) / 1.055) ** 2.4
    ));
    return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2]);
  }
  const foregroundLuminance = luminance(foreground);
  const backgroundLuminance = luminance(background);
  return (Math.max(foregroundLuminance, backgroundLuminance) + 0.05)
    / (Math.min(foregroundLuminance, backgroundLuminance) + 0.05);
}


function cssToken(block, name) {
  const match = block.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, "i"));
  assert.ok(match, `${name} 색상 token이 필요합니다.`);
  return match[1];
}


test("22개 공개 필드의 정확한 스키마만 받아들인다", () => {
  const valid = status();
  assert.equal(Object.keys(valid).length, 22);
  assert.deepEqual(ui.validateStatus(valid), valid);

  assert.throws(() => ui.validateStatus({ ...valid, extra: 1 }), /schema/);
  const missing = { ...valid };
  delete missing.heartbeat_at;
  assert.throws(() => ui.validateStatus(missing), /schema/);
  assert.throws(
    () => ui.validateStatus({ ...valid, images_done: -1 }),
    /images_done/,
  );
  assert.throws(
    () => ui.validateStatus({ ...valid, overall_percent: 101 }),
    /overall_percent/,
  );
  assert.throws(
    () => ui.validateStatus({ ...valid, heartbeat_at: "now" }),
    /heartbeat_at/,
  );
  assert.throws(
    () => ui.validateStatus({ ...valid, heartbeat_at: "2026-02-30T01:02:03Z" }),
    /heartbeat_at/,
  );
  assert.throws(
    () => ui.validateStatus({
      ...valid,
      state: "failed",
      error_class: null,
      error_code: null,
    }),
    /error_class/,
  );
});


test("한국어 fallback과 접근 가능한 진행률·키보드 동작 순서를 유지한다", () => {
  for (const text of [
    "기존 Pinry 데이터를 이전하고 있습니다.",
    "브라우저를 닫아도 작업은 계속됩니다.",
    "컨테이너를 중지하지 마세요.",
    "데이터 폴더를 삭제하거나 수정하지 마세요.",
    "Container Manager 로그를 확인하세요.",
  ]) {
    assert.match(html, new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
  assert.match(html, /<html\s+lang="ko">/);
  assert.match(html, /<img[^>]+alt="SVRx Pinry"/);
  assert.match(
    html,
    /<progress\s+id="overall-progress"[^>]+aria-labelledby="overall-progress-label"/,
  );
  assert.match(
    html,
    /<progress\s+id="phase-progress"[^>]+aria-labelledby="phase-progress-label"/,
  );
  assert.match(html, /id="live-status"[^>]+aria-live="polite"/);

  const actionLabels = [
    "상태 다시 확인",
    "오류 코드 복사",
    "진단 정보 복사",
    "SVRx Pinry 열기",
  ];
  const positions = actionLabels.map((label) => html.indexOf(label));
  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual(positions, positions.slice().sort((a, b) => a - b));
  assert.equal((html.match(/<button\b/g) || []).length, 4);
  assert.doesNotMatch(html, /<script(?![^>]+src=)[^>]*>/);
  assert.doesNotMatch(html, /<style\b/);
});


test("모바일·200% 확대·reduced motion CSS 계약과 고정 색상 대비를 지킨다", () => {
  for (const contract of [
    /\.migration-card\s*\{[^}]*width:\s*min\(100%,\s*880px\)/s,
    /\.migration-card\s*\{[^}]*max-width:\s*880px/s,
    /min-width:\s*0/,
    /overflow-wrap:\s*anywhere/,
    /\.action-row\s*\{[^}]*flex-wrap:\s*wrap/s,
    /grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)/,
    /@media\s*\(max-width:\s*640px\)[\s\S]*grid-template-columns:\s*1fr/,
    /@media\s*\(max-width:\s*640px\)[\s\S]*button\s*\{[^}]*width:\s*100%/,
    /:focus-visible\s*\{[^}]*outline:\s*3px\s+solid/s,
    /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*transition-duration:\s*0\.01ms\s*!important/,
    /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*animation-duration:\s*0\.01ms\s*!important/,
  ]) {
    assert.match(css, contract);
  }

  const lightBlock = css.match(/:root\s*\{([\s\S]*?)\}/)[1];
  const darkBlock = css.match(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([\s\S]*?)\}/)[1];
  const textPairs = [
    [cssToken(lightBlock, "text"), cssToken(lightBlock, "card")],
    [cssToken(lightBlock, "muted"), cssToken(lightBlock, "card")],
    [cssToken(lightBlock, "accent-strong"), cssToken(lightBlock, "card")],
    [cssToken(lightBlock, "danger"), cssToken(lightBlock, "card")],
    [cssToken(lightBlock, "warning"), cssToken(lightBlock, "warning-soft")],
    [cssToken(darkBlock, "text"), cssToken(darkBlock, "card")],
    [cssToken(darkBlock, "muted"), cssToken(darkBlock, "card")],
    [cssToken(darkBlock, "accent-strong"), cssToken(darkBlock, "card")],
    [cssToken(darkBlock, "danger"), cssToken(darkBlock, "danger-soft")],
    [cssToken(darkBlock, "warning"), cssToken(darkBlock, "warning-soft")],
  ];
  for (const [foreground, background] of textPairs) {
    assert.ok(contrastRatio(foreground, background) >= 4.5);
  }
  assert.ok(contrastRatio(
    cssToken(lightBlock, "accent"),
    cssToken(lightBlock, "border"),
  ) >= 3);
  assert.ok(contrastRatio(
    cssToken(darkBlock, "accent"),
    cssToken(darkBlock, "border"),
  ) >= 3);
});


test("알 수 없는 전체량 null과 정확한 0을 구분한다", () => {
  assert.equal(ui.formatCounter(0, null), "0 / 확인 중");
  assert.equal(ui.formatCounter(0, 0), "0 / 0");

  const progress = {
    value: 99,
    removed: [],
    removeAttribute(name) {
      this.removed.push(name);
    },
  };
  ui.applyProgress(progress, null);
  assert.deepEqual(progress.removed, ["value"]);
  ui.applyProgress(progress, 0);
  assert.equal(progress.value, 0);
});


test("실패 후 재시도는 30초를 넘지 않고 성공 polling은 2~5초다", () => {
  assert.equal(ui.backoffDelay(0), 2000);
  assert.equal(ui.backoffDelay(1), 4000);
  assert.equal(ui.backoffDelay(10), 30000);
  assert.equal(ui.successDelay(status()), 2000);
  assert.equal(ui.successDelay(status({ state: "starting_service" })), 5000);
});


test("ready 전환은 원래 SPA URL을 보존하고 진행 경로는 root로 돌린다", () => {
  assert.equal(ui.readyDestination("/migration/", "?ignored=1", "#x"), "/");
  assert.equal(
    ui.readyDestination("/migration/index.html", "", ""),
    "/",
  );
  assert.equal(
    ui.readyDestination("/pins/42", "?x=1", "#y"),
    "/pins/42?x=1#y",
  );
  assert.equal(
    ui.readyDestination("//evil.example/path", "?x=1", "#y"),
    "/evil.example/path?x=1#y",
  );
  assert.equal(
    ui.readyDestination("///evil.example/path", "", ""),
    "/evil.example/path",
  );
});


test("fetch가 없어도 정적 오류 안내를 숨기지 않는다", () => {
  const documentRef = fakeDocument();
  const windowRef = {
    location: {
      pathname: "/pins/42",
      search: "",
      hash: "",
      assign() {},
      replace() {},
    },
    navigator: {},
    setTimeout() { return 1; },
    clearTimeout() {},
    AbortController: undefined,
  };

  assert.doesNotThrow(() => ui.boot(documentRef, windowRef));
  assert.equal(documentRef.nodes["error-panel"].hidden, false);
  assert.equal(
    documentRef.nodes["page-title"].textContent,
    "상태 정보를 읽을 수 없습니다.",
  );
  assert.match(
    documentRef.nodes["operator-hint"].textContent,
    /Container Manager/,
  );
});


test("진단 복사는 정확히 22개 공개 필드만 투영한다", () => {
  const payload = status();
  payload.privateValue = "secret";
  const projected = ui.publicDiagnosticProjection(payload);
  assert.equal(Object.keys(projected).length, 22);
  assert.equal("privateValue" in projected, false);
  assert.deepEqual(Object.keys(projected), ui.PUBLIC_DIAGNOSTIC_FIELDS);
});


test("실패 안내가 stale 안내보다 우선하고 진행 정체를 별도로 표시한다", () => {
  const now = Date.parse("2026-08-27T01:05:20Z");
  const failed = ui.deriveViewModel(status({
    state: "failed",
    heartbeat_at: "2026-08-27T00:00:00Z",
    progress_at: "2026-08-27T00:00:00Z",
    error_class: "fatal",
    error_code: "legacy_startup_failed",
  }), now);
  assert.equal(failed.title, "데이터 이전을 완료하지 못했습니다.");
  assert.doesNotMatch(failed.notice, /지연|큰 파일/);

  const stale = ui.deriveViewModel(status({
    heartbeat_at: "2026-08-27T01:04:59Z",
    progress_at: "2026-08-27T01:04:58Z",
  }), now);
  assert.equal(stale.notice, "상태 확인이 지연되고 있습니다.");

  const stalled = ui.deriveViewModel(status({
    heartbeat_at: "2026-08-27T01:05:18Z",
    progress_at: "2026-08-27T01:03:00Z",
  }), now);
  assert.equal(
    stalled.notice,
    "큰 파일을 처리 중이거나 저장소 응답을 기다리고 있습니다.",
  );
});


test("aria-live는 heartbeat만 바뀜 때는 갱신하지 않고 state와 error_code 변경은 알린다", () => {
  let assignments = 0;
  let value = "";
  const live = {
    set textContent(next) {
      assignments += 1;
      value = next;
    },
    get textContent() {
      return value;
    },
  };
  const first = status();
  let signature = ui.updateLiveRegion(live, null, first);
  assert.equal(assignments, 1);

  signature = ui.updateLiveRegion(
    live,
    signature,
    status({ heartbeat_at: "2026-08-27T01:05:00Z" }),
  );
  assert.equal(assignments, 1);

  ui.updateLiveRegion(live, signature, status({
    state: "failed",
    error_class: "retryable",
    error_code: "gunicorn_start_failed",
  }));
  assert.equal(assignments, 2);
  assert.match(value, /실패/);
  assert.match(value, /gunicorn_start_failed/);
});


test("DOM adapter는 실행 즉시 fallback을 숨기고 null·0 진행률을 textContent로 표시한다", () => {
  const documentRef = fakeDocument();
  const adapter = ui.createDomAdapter(documentRef);
  assert.equal(documentRef.nodes["error-panel"].hidden, true);
  assert.equal(documentRef.nodes["open-button"].hidden, true);

  adapter.renderStatus(status({
    phase_label: "<img src=x onerror=alert(1)>",
    images_total: null,
    files_done: 0,
    files_total: 0,
    phase_percent: null,
    overall_percent: 0,
  }), Date.parse("2026-08-27T01:05:00Z"));

  assert.equal(documentRef.nodes["images-count"].textContent, "12 / 확인 중");
  assert.equal(documentRef.nodes["files-count"].textContent, "0 / 0");
  assert.deepEqual(documentRef.nodes["phase-progress"].removed, ["value"]);
  assert.equal(documentRef.nodes["overall-progress"].value, 0);
  assert.equal(
    documentRef.nodes["phase-name"].textContent,
    "<img src=x onerror=alert(1)>",
  );
  assert.equal("innerHTML" in documentRef.nodes["phase-name"], false);

  adapter.showOpen("/pins/42?x=1#y");
  assert.equal(documentRef.nodes["open-button"].hidden, false);
  assert.equal(adapter.getOpenDestination(), "/pins/42?x=1#y");
});


test("Clipboard API 거부 시 readonly textarea와 execCommand로 복사한다", async () => {
  const calls = [];
  const textarea = {
    readOnly: false,
    style: {},
    value: "",
    focus() { calls.push("focus"); },
    select() { calls.push("select"); },
    setSelectionRange(start, end) { calls.push([start, end]); },
    setAttribute(name) {
      if (name === "readonly") this.readOnly = true;
    },
  };
  const documentRef = {
    body: {
      appendChild(node) { calls.push(["append", node]); },
      removeChild(node) { calls.push(["remove", node]); },
    },
    createElement(name) {
      assert.equal(name, "textarea");
      return textarea;
    },
    execCommand(name) {
      calls.push(["exec", name]);
      return true;
    },
  };
  const navigatorRef = {
    clipboard: {
      async writeText() {
        throw new Error("not allowed");
      },
    },
  };

  assert.equal(
    await ui.writeClipboard("safe diagnostics", navigatorRef, documentRef),
    true,
  );
  assert.equal(textarea.readOnly, true);
  assert.equal(textarea.value, "safe diagnostics");
  assert.ok(calls.some((call) => Array.isArray(call) && call[0] === "exec"));
  assert.ok(calls.some((call) => Array.isArray(call) && call[0] === "remove"));
});


test("404·잘못된 JSON·스키마 실패 3회 후 정적 오류를 보이고 유효 응답으로 회복한다", async () => {
  const queue = [
    response(null, { ok: false, status: 404 }),
    response(null, { jsonError: true }),
    response({ invalid: true }),
    response(status()),
  ];
  const harness = controllerHarness(async () => queue.shift());

  await harness.controller.pollNow();
  await harness.controller.pollNow();
  await harness.controller.pollNow();
  assert.deepEqual(harness.staticErrors, [3]);
  assert.equal(harness.controller.failureCount, 3);
  assert.equal(harness.scheduled.length, 3);

  await harness.controller.pollNow();
  assert.equal(harness.controller.failureCount, 0);
  assert.equal(harness.renders.length, 1);
  assert.equal(harness.renders[0].payload.state, "migrating");
});


test("나중에 도착한 오래된 fetch 응답은 새 ready 상태를 덮지 못한다", async () => {
  const older = deferred();
  const newer = deferred();
  const queue = [older.promise, newer.promise];
  const harness = controllerHarness(() => queue.shift());

  const olderPoll = harness.controller.pollNow();
  const newerPoll = harness.controller.pollNow();
  newer.resolve(response(status({ state: "ready", phase: "complete", phase_label: "이전 완료", overall_percent: 100, phase_percent: 100 })));
  await newerPoll;
  older.resolve(response(status()));
  await olderPoll;

  assert.equal(harness.renders.length, 1);
  assert.equal(harness.renders[0].payload.state, "ready");
  assert.deepEqual(harness.location.replacements, ["/pins/42?x=1#y"]);
});


test("탭이 visible로 돌아오면 pending fetch를 취소하고 즉시 다시 조회한다", async () => {
  const first = deferred();
  const second = deferred();
  const signals = [];
  const queue = [first.promise, second.promise];
  const harness = controllerHarness((url, options) => {
    signals.push(options.signal);
    return queue.shift();
  });

  const initial = harness.controller.start();
  assert.equal(typeof harness.documentRef.listeners.visibilitychange, "function");
  harness.documentRef.listeners.visibilitychange();
  assert.equal(signals.length, 2);
  assert.equal(signals[0].aborted, true);

  second.resolve(response(status()));
  await Promise.resolve();
  await Promise.resolve();
  first.resolve(response(status({ images_done: 1 })));
  await initial;
  await Promise.resolve();
  assert.equal(harness.renders.at(-1).payload.images_done, 12);
  harness.controller.stop();
});


test("ready replace 실패 시 SVRx Pinry 열기 대상을 표시한다", async () => {
  const harness = controllerHarness(async () => response(status({
    state: "ready",
    phase: "complete",
    phase_label: "이전 완료",
    phase_percent: 100,
    overall_percent: 100,
  })));
  harness.location.replace = () => {
    throw new Error("navigation blocked");
  };
  await harness.controller.pollNow();
  assert.deepEqual(harness.openDestinations, ["/pins/42?x=1#y"]);
});


test("ready는 polling 중 URL이 바뀌어도 첫 진입 URL로 전환한다", async () => {
  const pending = deferred();
  const harness = controllerHarness(() => pending.promise);
  const poll = harness.controller.pollNow();
  harness.location.pathname = "/changed";
  harness.location.search = "?later=1";
  harness.location.hash = "#later";
  pending.resolve(response(status({
    state: "ready",
    phase: "complete",
    phase_label: "이전 완료",
    phase_percent: 100,
    overall_percent: 100,
  })));
  await poll;
  assert.deepEqual(harness.location.replacements, ["/pins/42?x=1#y"]);
});


test("정적 오류 안내는 연속 실패의 세 번째에 한 번만 전환한다", async () => {
  const harness = controllerHarness(async () => response(null, {
    ok: false,
    status: 404,
  }));
  await harness.controller.pollNow();
  await harness.controller.pollNow();
  await harness.controller.pollNow();
  await harness.controller.pollNow();
  assert.deepEqual(harness.staticErrors, [3]);
});
