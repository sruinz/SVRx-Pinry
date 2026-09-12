(function universalModule(root, factory) {
  "use strict";

  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
    return;
  }

  root.SVRxMigration = api;
  if (root.document) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", function startWhenReady() {
        api.boot(root.document, root);
      }, { once: true });
    } else {
      api.boot(root.document, root);
    }
  }
}(typeof globalThis !== "undefined" ? globalThis : this, function migrationFactory() {
  "use strict";

  var PUBLIC_DIAGNOSTIC_FIELDS = Object.freeze([
    "schema_version",
    "state",
    "phase",
    "phase_label",
    "run_id",
    "attempt",
    "resume_count",
    "started_at",
    "phase_started_at",
    "heartbeat_at",
    "progress_at",
    "last_committed_batch",
    "images_done",
    "images_total",
    "files_done",
    "files_total",
    "backfill_done",
    "backfill_total",
    "phase_percent",
    "overall_percent",
    "error_class",
    "error_code",
  ]);
  var STATUS_STATES = Object.freeze([
    "starting",
    "recovering",
    "migrating",
    "starting_service",
    "ready",
    "failed",
  ]);
  var PHASES = Object.freeze([
    "preparing",
    "recovery",
    "upgrade_v2",
    "snapshot",
    "planning",
    "copying",
    "database",
    "backfill_planning",
    "backfill_registering",
    "archive",
    "finalizing",
    "complete",
  ]);
  var ERROR_CLASSES = Object.freeze([
    "retryable",
    "operator_action_required",
    "fatal",
  ]);
  var INTEGER_FIELDS = Object.freeze([
    "attempt",
    "resume_count",
    "last_committed_batch",
    "images_done",
    "files_done",
    "backfill_done",
  ]);
  var TOTAL_FIELDS = Object.freeze([
    "images_total",
    "files_total",
    "backfill_total",
  ]);
  var TIMESTAMP_FIELDS = Object.freeze([
    "started_at",
    "phase_started_at",
    "heartbeat_at",
    "progress_at",
  ]);
  var STATE_LABELS = Object.freeze({
    starting: "시작 준비 중",
    recovering: "이전 상태 확인 중",
    migrating: "데이터 이전 중",
    starting_service: "서비스 시작 중",
    ready: "준비 완료",
    failed: "이전 실패",
  });

  function isPlainObject(value) {
    return Object.prototype.toString.call(value) === "[object Object]";
  }

  function isNonnegativeInteger(value) {
    return Number.isInteger(value) && value >= 0;
  }

  function isPercent(value) {
    return value === null
      || (typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100);
  }

  function isTimestamp(value) {
    if (typeof value !== "string"
        || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value)) {
      return false;
    }
    var parsed = Date.parse(value);
    return Number.isFinite(parsed)
      && new Date(parsed).toISOString().replace(".000Z", "Z") === value;
  }

  function schemaError(field) {
    throw new TypeError("status schema: " + field);
  }

  function validateStatus(payload) {
    if (!isPlainObject(payload)) {
      return schemaError("object");
    }

    var actualKeys = Object.keys(payload).sort();
    var expectedKeys = PUBLIC_DIAGNOSTIC_FIELDS.slice().sort();
    if (actualKeys.length !== expectedKeys.length
        || actualKeys.some(function differs(key, index) { return key !== expectedKeys[index]; })) {
      return schemaError("fields");
    }
    if (payload.schema_version !== 1) {
      return schemaError("schema_version");
    }
    if (STATUS_STATES.indexOf(payload.state) < 0) {
      return schemaError("state");
    }
    if (PHASES.indexOf(payload.phase) < 0) {
      return schemaError("phase");
    }
    if (typeof payload.phase_label !== "string"
        || payload.phase_label.length < 1
        || payload.phase_label.length > 160) {
      return schemaError("phase_label");
    }
    if (payload.run_id !== null
        && (typeof payload.run_id !== "string"
          || payload.run_id.length < 1
          || payload.run_id.length > 128)) {
      return schemaError("run_id");
    }

    INTEGER_FIELDS.forEach(function validateInteger(field) {
      if (!isNonnegativeInteger(payload[field])) {
        schemaError(field);
      }
    });
    TOTAL_FIELDS.forEach(function validateTotal(field) {
      if (payload[field] !== null && !isNonnegativeInteger(payload[field])) {
        schemaError(field);
      }
    });
    TIMESTAMP_FIELDS.forEach(function validateTimestamp(field) {
      if (payload[field] !== null && !isTimestamp(payload[field])) {
        schemaError(field);
      }
    });
    if (!isTimestamp(payload.phase_started_at) || !isTimestamp(payload.heartbeat_at)) {
      return schemaError("required timestamps");
    }
    if (!isPercent(payload.phase_percent)) {
      return schemaError("phase_percent");
    }
    if (!isPercent(payload.overall_percent)) {
      return schemaError("overall_percent");
    }
    if (payload.error_class !== null && ERROR_CLASSES.indexOf(payload.error_class) < 0) {
      return schemaError("error_class");
    }
    if (payload.error_code !== null
        && (typeof payload.error_code !== "string"
          || !/^[a-z0-9_]{1,128}$/.test(payload.error_code))) {
      return schemaError("error_code");
    }
    if (payload.state === "failed"
        && (payload.error_class === null || payload.error_code === null)) {
      return schemaError("error_class and error_code");
    }
    if (payload.state !== "failed"
        && (payload.error_class !== null || payload.error_code !== null)) {
      return schemaError("error_class and error_code");
    }

    [["images_done", "images_total"], ["files_done", "files_total"], ["backfill_done", "backfill_total"]]
      .forEach(function validateCounter(pair) {
        if (payload[pair[1]] !== null && payload[pair[0]] > payload[pair[1]]) {
          schemaError(pair[0]);
        }
      });
    return payload;
  }

  function formatCounter(done, total) {
    return String(done) + " / " + (total === null ? "확인 중" : String(total));
  }

  function formatPercent(value) {
    return value === null ? "확인 중" : String(value) + "%";
  }

  function applyProgress(element, value) {
    if (value === null) {
      element.removeAttribute("value");
      return;
    }
    element.value = value;
  }

  function backoffDelay(failures) {
    return Math.min(30000, 2000 * 2 ** failures);
  }

  function successDelay(payload) {
    return payload.state === "starting_service" ? 5000 : 2000;
  }

  function readyDestination(pathname, search, hash) {
    var safePathname = "/" + String(pathname || "/").replace(/^[\\/]+/, "");
    if (safePathname === "/migration/"
        || safePathname === "/migration/index.html") {
      return "/";
    }
    return safePathname + (search || "") + (hash || "");
  }

  function publicDiagnosticProjection(payload) {
    var projected = {};
    PUBLIC_DIAGNOSTIC_FIELDS.forEach(function copyPublic(field) {
      projected[field] = payload[field];
    });
    return projected;
  }

  function timestampAge(timestamp, now) {
    if (timestamp === null) {
      return "아직 없음";
    }
    var ageSeconds = Math.max(0, Math.floor((now - Date.parse(timestamp)) / 1000));
    if (ageSeconds < 60) {
      return String(ageSeconds) + "초 전";
    }
    if (ageSeconds < 3600) {
      return String(Math.floor(ageSeconds / 60)) + "분 전";
    }
    return String(Math.floor(ageSeconds / 3600)) + "시간 전";
  }

  function errorNotice(errorClass, errorCode) {
    if (errorCode === "local_settings_encoding_invalid") {
      return "local_settings.py의 인코딩을 안전하게 자동 복구할 수 없습니다. 원본을 백업하고 설정값을 확인한 뒤 UTF-8로 저장하세요.";
    }
    if (errorCode === "local_settings_syntax_invalid") {
      return "local_settings.py에 Python 구문 오류가 있습니다. 원본을 백업하고 설정 파일의 구문을 확인하세요. 데이터 이전은 시작하지 않았습니다.";
    }
    if (errorClass === "retryable") {
      return "컨테이너를 다시 시작하면 마지막 확정 배치부터 재개합니다.";
    }
    return "DSM Container Manager의 컨테이너 로그를 확인하세요.";
  }

  function isServiceStarting(payload) {
    return payload.state === "starting_service"
      || (payload.state === "migrating" && payload.phase === "complete");
  }

  function deriveViewModel(payload, now) {
    var failed = payload.state === "failed";
    var preparing = payload.state === "starting";
    var ready = payload.state === "ready";
    var serviceStarting = isServiceStarting(payload);
    var noMediaWork = payload.phase === "complete"
      && payload.run_id === null
      && payload.images_done === 0
      && payload.images_total === null
      && payload.files_done === 0
      && payload.files_total === null
      && payload.backfill_done === 0
      && payload.backfill_total === null;
    var notice = "중지해야 한다면 DSM Container Manager의 정상 중지를 사용하세요. 재시작하면 마지막 확정 배치부터 이어집니다.";
    if (failed) {
      notice = errorNotice(payload.error_class, payload.error_code);
    } else {
      var heartbeatAge = now - Date.parse(payload.heartbeat_at);
      var progressAge = payload.progress_at === null ? 0 : now - Date.parse(payload.progress_at);
      if (heartbeatAge > 15000) {
        notice = "상태 확인이 지연되고 있습니다.";
      } else if (serviceStarting) {
        notice = "앱 서버 준비 상태를 확인한 뒤 자동으로 전환합니다.";
      } else if (ready) {
        notice = "잠시 후 SVRx Pinry로 자동 이동합니다.";
      } else if (preparing) {
        notice = "설정과 데이터 상태를 확인한 뒤 필요한 작업을 자동으로 진행합니다.";
      } else if (payload.progress_at !== null && progressAge > 60000) {
        notice = "큰 파일을 처리 중이거나 저장소 응답을 기다리고 있습니다.";
      }
    }
    return {
      title: failed
        ? "데이터 이전을 완료하지 못했습니다."
        : (ready
          ? "SVRx Pinry로 이동합니다."
          : (serviceStarting
            ? "SVRx Pinry 서비스 시작을 확인하고 있습니다."
            : (preparing
              ? "SVRx Pinry를 준비하고 있습니다."
              : "기존 Pinry 데이터를 이전하고 있습니다."))),
      stateText: serviceStarting
        ? "서비스 시작 확인 중"
        : STATE_LABELS[payload.state],
      noticeTitle: failed
        ? "데이터를 보존한 상태로 멈춰 있습니다."
        : (ready
          ? "서비스 준비가 완료되었습니다."
          : (serviceStarting
            ? "서비스를 안전하게 시작하고 있습니다."
            : (preparing
              ? "실행 환경을 확인하고 있습니다."
              : "안전하게 이전하는 중입니다."))),
      notice: notice,
      heartbeatAge: timestampAge(payload.heartbeat_at, now),
      progressAge: timestampAge(payload.progress_at, now),
      overallPercentText: formatPercent(payload.overall_percent),
      phasePercentText: formatPercent(payload.phase_percent),
      imagesText: noMediaWork
        ? "해당 없음"
        : formatCounter(payload.images_done, payload.images_total),
      filesText: noMediaWork
        ? "해당 없음"
        : formatCounter(payload.files_done, payload.files_total),
      backfillText: noMediaWork
        ? "해당 없음"
        : formatCounter(payload.backfill_done, payload.backfill_total),
      errorClassText: ({
        retryable: "재시작 후 자동 재개 가능",
        operator_action_required: "사용자 확인 필요",
        fatal: "안전 상태 확인 필요",
      })[payload.error_class] || "없음",
      errorCodeText: payload.error_code || "없음",
      operatorHint: failed ? errorNotice(payload.error_class, payload.error_code) : "",
    };
  }

  function liveSignature(payload) {
    return [
      payload.state,
      payload.phase,
      payload.images_done,
      payload.files_done,
      payload.backfill_done,
      payload.error_code || "",
    ].join("|");
  }

  function liveText(payload) {
    var stateText = isServiceStarting(payload)
      ? "서비스 시작 확인 중"
      : STATE_LABELS[payload.state];
    var text = stateText + ". " + payload.phase_label
      + ". 이미지 " + String(payload.images_done)
      + ", 파일 " + String(payload.files_done)
      + ", 미디어 자산 " + String(payload.backfill_done) + ".";
    if (payload.error_code) {
      text += " 오류 " + payload.error_code + ".";
    }
    return text;
  }

  function updateLiveRegion(element, previousSignature, payload) {
    var signature = liveSignature(payload);
    if (signature !== previousSignature) {
      element.textContent = liveText(payload);
    }
    return signature;
  }

  async function writeClipboard(text, navigatorRef, documentRef) {
    if (navigatorRef && navigatorRef.clipboard
        && typeof navigatorRef.clipboard.writeText === "function") {
      try {
        await navigatorRef.clipboard.writeText(text);
        return true;
      } catch (error) {
        // 보안 컨텍스트가 아니면 아래의 선택 복사 방식으로 이어간다.
      }
    }

    var textarea = documentRef.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    textarea.style.pointerEvents = "none";
    documentRef.body.appendChild(textarea);
    try {
      textarea.focus();
      textarea.select();
      textarea.setSelectionRange(0, text.length);
      return documentRef.execCommand("copy") === true;
    } catch (error) {
      return false;
    } finally {
      documentRef.body.removeChild(textarea);
    }
  }

  function fallbackAbortController() {
    return {
      signal: { aborted: false },
      abort: function abort() { this.signal.aborted = true; },
    };
  }

  function validateRecovery(payload) {
    var fields = ["schema_version", "available", "reason", "remaining_attempts",
      "retry_after_seconds", "generation", "token"];
    if (!payload || Object.keys(payload).length !== fields.length
        || !fields.every(function hasField(field) { return Object.prototype.hasOwnProperty.call(payload, field); })
        || payload.schema_version !== 1 || typeof payload.available !== "boolean"
        || !["available", "cooldown", "exhausted", "unavailable", "unsafe_state", "accepted", "invalid_token"].includes(payload.reason)
        || !Number.isInteger(payload.remaining_attempts) || payload.remaining_attempts < 0 || payload.remaining_attempts > 3
        || !Number.isInteger(payload.retry_after_seconds) || payload.retry_after_seconds < 0
        || ![payload.generation, payload.token].every(function validCredential(value) {
          return value === null || (typeof value === "string" && /^[A-Za-z0-9_-]{1,256}$/.test(value));
        })
        || (payload.available && (payload.reason !== "available" || !payload.token || !payload.generation
          || payload.retry_after_seconds !== 0 || payload.remaining_attempts === 0))) {
      throw new Error("recovery schema");
    }
    return payload;
  }

  function MigrationController(options) {
    this.fetchFn = options.fetchFn;
    this.adapter = options.adapter;
    this.documentRef = options.documentRef;
    this.locationRef = options.locationRef;
    this.nowFn = options.nowFn || Date.now;
    this.setTimeoutFn = options.setTimeoutFn;
    this.clearTimeoutFn = options.clearTimeoutFn;
    this.AbortControllerCtor = options.AbortControllerCtor
      || (typeof AbortController === "function" ? AbortController : null);
    this.failureCount = 0;
    this.sequence = 0;
    this.timer = null;
    this.pendingController = null;
    this.lastStatus = null;
    this.recovery = null;
    this.restarting = false;
    this.submittedGeneration = null;
    this.originalDestination = readyDestination(
      this.locationRef.pathname,
      this.locationRef.search,
      this.locationRef.hash,
    );
    this.stopped = false;
    this.visibilityListener = this.handleVisibilityChange.bind(this);
  }

  MigrationController.prototype._abortPending = function abortPending() {
    if (this.pendingController) {
      this.pendingController.abort();
      this.pendingController = null;
    }
  };

  MigrationController.prototype._clearTimer = function clearTimer() {
    if (this.timer !== null) {
      this.clearTimeoutFn(this.timer);
      this.timer = null;
    }
  };

  MigrationController.prototype._schedule = function schedule(delay) {
    var self = this;
    if (this.stopped) {
      return;
    }
    this.timer = this.setTimeoutFn(function scheduledPoll() {
      self.timer = null;
      self.pollNow();
    }, delay);
  };

  MigrationController.prototype.start = function start() {
    this.stopped = false;
    this.documentRef.addEventListener("visibilitychange", this.visibilityListener);
    return this.pollNow();
  };

  MigrationController.prototype.stop = function stop() {
    this.stopped = true;
    this.sequence += 1;
    this._renderRecovery(null, false);
    this._clearTimer();
    this._abortPending();
    this.documentRef.removeEventListener("visibilitychange", this.visibilityListener);
  };

  MigrationController.prototype.handleVisibilityChange = function handleVisibilityChange() {
    var document = this.documentRef;
    if (document.visibilityState === "visible") {
      this.refreshNow();
    }
  };

  MigrationController.prototype.refreshNow = function refreshNow() {
    this._clearTimer();
    this._abortPending();
    return this.pollNow();
  };

  MigrationController.prototype.pollNow = async function pollNow() {
    var self = this;
    this._clearTimer();
    this._abortPending();
    var requestSequence = this.sequence + 1;
    this.sequence = requestSequence;
    this._renderRecovery(null, false);
    var requestController = this.AbortControllerCtor
      ? new this.AbortControllerCtor()
      : fallbackAbortController();
    this.pendingController = requestController;

    try {
      var response = await this.fetchFn("/migration-status.json", {
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: requestController.signal,
      });
      if (requestSequence !== this.sequence || this.stopped) {
        return;
      }
      if (!response || !response.ok) {
        throw new Error("status http error");
      }
      var payload = validateStatus(await response.json());
      if (requestSequence !== this.sequence || this.stopped) {
        return;
      }

      this.failureCount = 0;
      this.lastStatus = payload;
      this.adapter.renderStatus(payload, this.nowFn());
      if (payload.state === "failed") {
        await this._loadRecovery(requestSequence);
        if (requestSequence !== this.sequence || this.stopped) return;
      }
      if (payload.state === "ready") {
        var destination = this.originalDestination;
        var location = this.locationRef;
        try {
          location.replace(destination);
        } catch (error) {
          this.adapter.showOpen(destination);
        }
        return;
      }
      this._schedule(successDelay(payload));
    } catch (error) {
      if (requestSequence !== this.sequence || this.stopped
          || (error && error.name === "AbortError")) {
        return;
      }
      this.failureCount += 1;
      if (this.failureCount === 3) {
        this.adapter.renderStaticError(this.failureCount);
      }
      this._schedule(backoffDelay(this.failureCount));
    } finally {
      if (this.pendingController === requestController) {
        this.pendingController = null;
      }
    }
  };

  MigrationController.prototype._renderRecovery = function renderRecovery(snapshot, visible, message) {
    this.recovery = snapshot;
    var accepted = snapshot && snapshot.generation === this.submittedGeneration;
    var text = message || "DSM Container Manager의 컨테이너 로그를 확인하세요.";
    if (!message && snapshot) {
      if (accepted) text = "재시동 요청을 접수했습니다.";
      else if (snapshot.reason === "exhausted") text = "재시도 한도에 도달했습니다. DSM에서 컨테이너 로그를 확인하세요.";
      else if (snapshot.reason === "cooldown") text = String(snapshot.retry_after_seconds) + "초 후 다시 시도할 수 있습니다. 남은 재시도 " + String(snapshot.remaining_attempts) + "회";
      else if (snapshot.available) text = "남은 재시도 " + String(snapshot.remaining_attempts) + "회";
    }
    this.adapter.renderRecovery({visible: visible, enabled: !!(snapshot && snapshot.available && !this.restarting && !accepted), message: text});
  };

  MigrationController.prototype._loadRecovery = async function loadRecovery(sequence) {
    try {
      var response = await this.fetchFn("/migration/recovery", {
        cache: "no-store", credentials: "same-origin", headers: {Accept: "application/json"},
      });
      if (!response || !response.ok) throw new Error("recovery http");
      var snapshot = validateRecovery(await response.json());
      if (this.stopped || sequence !== this.sequence || !this.lastStatus || this.lastStatus.state !== "failed") return;
      this._renderRecovery(snapshot, true);
    } catch (error) {
      if (!this.stopped && sequence === this.sequence) this._renderRecovery(null, true);
    }
  };

  MigrationController.prototype.restart = async function restart() {
    var snapshot = this.recovery;
    if (this.stopped || this.restarting || !snapshot || !snapshot.available
        || snapshot.generation === this.submittedGeneration
        || !this.lastStatus || this.lastStatus.state !== "failed") return;
    this.restarting = true;
    var sequence = this.sequence;
    this._renderRecovery(null, true, "재시동 요청을 보내고 있습니다.");
    try {
      var response = await this.fetchFn("/migration/restart", {
        method: "POST", cache: "no-store", credentials: "same-origin",
        headers: {"X-SVRX-Recovery-Token": snapshot.token, "X-SVRX-Recovery-Generation": snapshot.generation},
      });
      if (response && response.status === 202) {
        this.submittedGeneration = snapshot.generation;
        if (!this.stopped && sequence === this.sequence && this.lastStatus.state === "failed") {
          this._renderRecovery(null, true, "재시동 요청을 접수했습니다.");
        }
        return;
      }
    } catch (error) {
      // 응답 유실은 접수 여부가 불명확하므로 POST를 재전송하지 않는다.
    } finally {
      this.restarting = false;
    }
    if (!this.stopped && sequence === this.sequence) await this.refreshNow();
  };

  function requiredElement(documentRef, id) {
    var element = documentRef.getElementById(id);
    if (!element) {
      throw new Error("maintenance element missing: " + id);
    }
    return element;
  }

  function createDomAdapter(documentRef) {
    var elements = {};
    [
      "page-title", "state-badge", "phase-name", "overall-progress",
      "overall-progress-value", "phase-progress", "phase-progress-value",
      "images-count", "files-count", "backfill-count", "heartbeat-age",
      "progress-age", "resume-count", "last-batch", "notice-title",
      "notice-text", "error-panel", "error-title", "operator-hint",
      "error-class", "error-code", "refresh-button", "copy-error-button",
      "copy-diagnostics-button", "open-button", "copy-result", "live-status",
      "recovery-panel", "restart-button", "recovery-status",
    ].forEach(function loadElement(id) {
      elements[id] = requiredElement(documentRef, id);
    });
    elements["error-panel"].hidden = true;
    elements["open-button"].hidden = true;
    elements["recovery-panel"].hidden = true;
    elements["restart-button"].disabled = true;

    var previousLiveSignature = null;
    var openDestination = "/";
    return {
      elements: elements,
      renderRecovery: function renderRecovery(view) {
        elements["recovery-panel"].hidden = !view.visible;
        elements["restart-button"].disabled = !view.enabled;
        elements["recovery-status"].textContent = view.message;
      },
      renderStatus: function renderStatus(payload, now) {
        var view = deriveViewModel(payload, now);
        elements["page-title"].textContent = view.title;
        elements["state-badge"].textContent = view.stateText;
        elements["state-badge"].setAttribute("data-state", payload.state);
        elements["phase-name"].textContent = payload.phase_label;
        applyProgress(elements["overall-progress"], payload.overall_percent);
        applyProgress(elements["phase-progress"], payload.phase_percent);
        elements["overall-progress-value"].textContent = view.overallPercentText;
        elements["phase-progress-value"].textContent = view.phasePercentText;
        elements["images-count"].textContent = view.imagesText;
        elements["files-count"].textContent = view.filesText;
        elements["backfill-count"].textContent = view.backfillText;
        elements["heartbeat-age"].textContent = view.heartbeatAge;
        elements["progress-age"].textContent = view.progressAge;
        elements["resume-count"].textContent = String(payload.resume_count) + "회";
        elements["last-batch"].textContent = String(payload.last_committed_batch);
        elements["notice-title"].textContent = view.noticeTitle;
        elements["notice-text"].textContent = view.notice;
        elements["error-panel"].hidden = payload.state !== "failed";
        elements["error-title"].textContent = payload.state === "failed"
          ? "데이터 이전을 완료하지 못했습니다."
          : "";
        elements["operator-hint"].textContent = view.operatorHint;
        elements["error-class"].textContent = view.errorClassText;
        elements["error-code"].textContent = view.errorCodeText;
        previousLiveSignature = updateLiveRegion(
          elements["live-status"], previousLiveSignature, payload,
        );
      },
      renderStaticError: function renderStaticError() {
        elements["page-title"].textContent = "상태 정보를 읽을 수 없습니다.";
        elements["state-badge"].textContent = "상태 확인 필요";
        elements["state-badge"].setAttribute("data-state", "failed");
        elements["error-panel"].hidden = false;
        elements["error-title"].textContent = "상태 정보를 읽을 수 없습니다.";
        elements["operator-hint"].textContent = "Container Manager 로그를 확인하세요.";
        elements["error-class"].textContent = "상태 조회 실패";
        elements["error-code"].textContent = "확인 중";
        elements["live-status"].textContent = "상태 정보를 읽을 수 없습니다. Container Manager 로그를 확인하세요.";
      },
      showOpen: function showOpen(destination) {
        openDestination = destination;
        elements["open-button"].hidden = false;
        elements["copy-result"].textContent = "자동 전환하지 못했습니다. SVRx Pinry 열기를 눌러 이동하세요.";
      },
      getOpenDestination: function getOpenDestination() {
        return openDestination;
      },
      setOpenDestination: function setOpenDestination(destination) {
        openDestination = destination;
      },
      setCopyResult: function setCopyResult(message) {
        elements["copy-result"].textContent = message;
      },
      bindActions: function bindActions(actions) {
        elements["restart-button"].addEventListener("click", actions.restart);
        elements["refresh-button"].addEventListener("click", actions.refresh);
        elements["copy-error-button"].addEventListener("click", actions.copyError);
        elements["copy-diagnostics-button"].addEventListener("click", actions.copyDiagnostics);
        elements["open-button"].addEventListener("click", actions.open);
      },
    };
  }

  function boot(documentRef, windowRef) {
    var adapter = createDomAdapter(documentRef);
    if (!windowRef || typeof windowRef.fetch !== "function") {
      adapter.renderStaticError();
      return null;
    }
    var destination = readyDestination(
      windowRef.location.pathname,
      windowRef.location.search,
      windowRef.location.hash,
    );
    adapter.setOpenDestination(destination);
    var controller = new MigrationController({
      fetchFn: windowRef.fetch.bind(windowRef),
      adapter: adapter,
      documentRef: documentRef,
      locationRef: windowRef.location,
      nowFn: Date.now,
      setTimeoutFn: windowRef.setTimeout.bind(windowRef),
      clearTimeoutFn: windowRef.clearTimeout.bind(windowRef),
      AbortControllerCtor: windowRef.AbortController,
    });

    async function copyText(text, successMessage) {
      var copied = await writeClipboard(text, windowRef.navigator, documentRef);
      adapter.setCopyResult(copied ? successMessage : "복사하지 못했습니다. 텍스트를 선택해 직접 복사하세요.");
    }

    adapter.bindActions({
      restart: function restart() { controller.restart(); },
      refresh: function refresh() {
        controller.refreshNow();
      },
      copyError: function copyError() {
        var code = controller.lastStatus && controller.lastStatus.error_code
          ? controller.lastStatus.error_code
          : "오류 코드 없음";
        copyText(code, "오류 코드를 복사했습니다.");
      },
      copyDiagnostics: function copyDiagnostics() {
        var diagnostics = controller.lastStatus
          ? publicDiagnosticProjection(controller.lastStatus)
          : {};
        copyText(JSON.stringify(diagnostics, null, 2), "진단 정보를 복사했습니다.");
      },
      open: function open() {
        windowRef.location.assign(adapter.getOpenDestination());
      },
    });
    controller.start();
    return controller;
  }

  return Object.freeze({
    PUBLIC_DIAGNOSTIC_FIELDS: PUBLIC_DIAGNOSTIC_FIELDS,
    MigrationController: MigrationController,
    applyProgress: applyProgress,
    backoffDelay: backoffDelay,
    boot: boot,
    createDomAdapter: createDomAdapter,
    deriveViewModel: deriveViewModel,
    formatCounter: formatCounter,
    publicDiagnosticProjection: publicDiagnosticProjection,
    readyDestination: readyDestination,
    successDelay: successDelay,
    updateLiveRegion: updateLiveRegion,
    validateStatus: validateStatus,
    writeClipboard: writeClipboard,
  });
}));
