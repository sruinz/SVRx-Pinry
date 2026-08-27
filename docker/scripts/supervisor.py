#!/usr/bin/env python
import errno
import http.client
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from docker.scripts.migration_status import (
    ERROR_CODE_CLASSES,
    MigrationStatusStore,
    StatusError,
)


_MAX_FRAME_BYTES = 4096
_HEARTBEAT_SECONDS = 5.0
_POLL_SECONDS = 1.0
_READINESS_SECONDS = 60.0
_READINESS_INTERVAL_SECONDS = 0.5
_QUIESCE_SECONDS = 5.0
_SHUTDOWN_SECONDS = 15.0
_READINESS_URL = "http://127.0.0.1:8000/api/v2/version/"
_SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40}")

PROJECT_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
NGINX_COMMAND = ("/usr/sbin/nginx", "-g", "daemon off;")
WORKER_PATH = os.path.join(
    PROJECT_ROOT, "docker", "scripts", "migration_worker.py"
)
GUNICORN_PATH = os.path.join(
    PROJECT_ROOT, "docker", "scripts", "_start_gunicorn.sh"
)

_WORKER_SHUTDOWN = -2
_NGINX_EXITED = -3


class SupervisorError(Exception):
    def __init__(self, code):
        super(SupervisorError, self).__init__(code)
        self.code = code


class _ShutdownRequested(Exception):
    pass


def _safe_error_code(error, fallback="runtime_supervisor_failed"):
    code = getattr(error, "code", None)
    if code in ERROR_CODE_CLASSES:
        return code
    if (
        isinstance(error, StatusError)
        and len(error.args) == 1
        and error.args[0] in ERROR_CODE_CLASSES
    ):
        return error.args[0]
    return fallback


def _parse_proc_stat(payload):
    if not isinstance(payload, str):
        raise ValueError("invalid proc stat")
    closing = payload.rfind(")")
    opening = payload.find("(")
    if opening <= 0 or closing <= opening:
        raise ValueError("invalid proc stat")
    try:
        pid = int(payload[:opening].strip(), 10)
        fields = payload[closing + 1:].strip().split()
        state = fields[0]
        pgid = int(fields[2], 10)
        session = int(fields[3], 10)
        starttime = fields[19]
    except (IndexError, TypeError, ValueError):
        raise ValueError("invalid proc stat")
    if len(state) != 1 or not starttime.isdigit():
        raise ValueError("invalid proc stat")
    return {
        "pid": pid,
        "state": state,
        "pgid": pgid,
        "session": session,
        "starttime": starttime,
    }


def _read_proc_identity(pid):
    if type(pid) is not int or pid <= 0:
        raise OSError(errno.ESRCH, "invalid process id")
    path = "/proc/{}/stat".format(pid)
    with open(path, "r", encoding="ascii") as source:
        identity = _parse_proc_stat(source.read(8192))
    if identity["pid"] != pid:
        raise OSError(errno.ESRCH, "process identity changed")
    return identity


def _read_process_group(pgid):
    members = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            identity = _read_proc_identity(int(name, 10))
        except (OSError, ValueError):
            continue
        if identity["pgid"] == pgid:
            members[identity["pid"]] = identity
    return members


def _valid_readiness_payload(status, content_type, payload):
    if status != 200:
        return False
    if not isinstance(content_type, str):
        return False
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "source_commit", "display_version",
    }:
        return False
    source_commit = payload["source_commit"]
    display_version = payload["display_version"]
    if not isinstance(source_commit, str) or not isinstance(
        display_version, str
    ):
        return False
    if source_commit == "development":
        return display_version == "development"
    return (
        _SOURCE_COMMIT_RE.fullmatch(source_commit) is not None
        and display_version == source_commit[:12]
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message,
                         headers, new_url):
        del request, file_pointer, code, message, headers, new_url
        return None


class _ChildRecord(object):
    def __init__(self, role, process, identity):
        self.role = role
        self.process = process
        self.pid = process.pid
        self.pgid = process.pid
        self.session = process.pid
        self.starttime = None
        self.returncode = None
        self.master_reaped = False
        if identity is not None:
            self.pgid = identity["pgid"]
            self.session = identity["session"]
            self.starttime = identity["starttime"]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    del value
    raise ValueError("non-finite JSON number")


_PROTOCOL_VALIDATOR = object.__new__(MigrationStatusStore)


def _validate_event(event):
    try:
        MigrationStatusStore._validate_event(_PROTOCOL_VALIDATOR, event)
    except StatusError:
        raise SupervisorError("worker_protocol_invalid")


class ProgressFrameDecoder(object):
    def __init__(self, event_validator=_validate_event):
        self._buffer = b""
        self._terminal = False
        self._event_validator = event_validator

    @staticmethod
    def _decode(line):
        if not line.endswith(b"\n") or len(line) > _MAX_FRAME_BYTES:
            raise SupervisorError("worker_protocol_invalid")
        encoded = line[:-1]
        try:
            text = encoded.decode("utf-8")
            event = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            canonical = json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (UnicodeError, ValueError):
            raise SupervisorError("worker_protocol_invalid")
        if not isinstance(event, dict):
            raise SupervisorError("worker_protocol_invalid")
        if encoded != canonical:
            raise SupervisorError("worker_protocol_invalid")
        return event

    def feed(self, payload):
        if not isinstance(payload, bytes) or not payload:
            return []
        if self._terminal:
            raise SupervisorError("worker_protocol_invalid")
        self._buffer += payload
        events = []
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            event = self._decode(raw + b"\n")
            try:
                self._event_validator(event)
            except SupervisorError:
                raise
            except Exception:
                raise SupervisorError("worker_protocol_invalid")
            events.append(event)
            if event.get("phase") in ("complete", "error"):
                self._terminal = True
                if self._buffer:
                    raise SupervisorError("worker_protocol_invalid")
                break
        if len(self._buffer) >= _MAX_FRAME_BYTES:
            raise SupervisorError("worker_protocol_invalid")
        return events

    def finish(self):
        if self._buffer or not self._terminal:
            raise SupervisorError("worker_protocol_invalid")


class RuntimeSupervisor(object):
    def __init__(
        self,
        arguments,
        data_root,
        service_uid,
        service_gid,
        status_store,
        clock=time.monotonic,
        process_factory=subprocess.Popen,
        selector_factory=selectors.DefaultSelector,
        lock_acquirer=None,
        identity_reader=_read_proc_identity,
        group_reader=_read_process_group,
        group_signaler=os.killpg,
        waitpid=os.waitpid,
        readiness_probe=None,
        quiescence_probe=None,
        sleeper=time.sleep,
    ):
        self.arguments = list(arguments)
        self.data_root = data_root
        self.service_uid = service_uid
        self.service_gid = service_gid
        self.status_store = status_store
        self.clock = clock
        self.process_factory = process_factory
        self.selector_factory = selector_factory
        self.lock_acquirer = lock_acquirer
        self.identity_reader = identity_reader
        self.group_reader = group_reader
        self.group_signaler = group_signaler
        self.waitpid = waitpid
        self.readiness_probe = readiness_probe
        self.quiescence_probe = quiescence_probe
        self.sleeper = sleeper

        self.startup_lock = None
        self.progress_reader = None
        self.children = {}
        self.nginx = None
        self.worker = None
        self.gunicorn = None
        self.last_worker_error = "legacy_startup_failed"
        self._last_heartbeat = None
        self._shutdown_signal = None
        self._shutdown_deadline = None
        self._shutdown_signaled = set()
        self._worker_terminal = None

    @staticmethod
    def _log(message):
        try:
            print(message, flush=True)
        except (IOError, OSError):
            pass

    def _raise_if_shutdown(self):
        if self._shutdown_signal is not None:
            raise _ShutdownRequested()

    def _project_status(self, method, *arguments):
        try:
            method(*arguments)
            return True
        except StatusError as error:
            code = _safe_error_code(error)
            if code == "migration_status_write_failed":
                self._log("이전 상태 기록을 재시도합니다.")
                return False
            raise SupervisorError(code)
        except (IOError, OSError):
            self._log("이전 상태 기록을 재시도합니다.")
            return False

    def _status_tick(self):
        self._project_status(self.status_store.publish_pending)
        now = self.clock()
        if (
            self._last_heartbeat is None
            or now - self._last_heartbeat >= _HEARTBEAT_SECONDS
        ):
            self._project_status(self.status_store.heartbeat)
            self._last_heartbeat = now

    def _spawn(self, role, command, pass_fds=(), cwd=None):
        if self._shutdown_signal is not None:
            raise _ShutdownRequested()
        try:
            if cwd is None:
                process = self.process_factory(
                    list(command),
                    close_fds=True,
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                )
            else:
                process = self.process_factory(
                    list(command),
                    close_fds=True,
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                    cwd=cwd,
                )
        except (IOError, OSError):
            code = {
                "nginx": "nginx_start_failed",
                "gunicorn": "gunicorn_start_failed",
            }.get(role, "runtime_supervisor_failed")
            raise SupervisorError(code)
        identity = None
        try:
            identity = self.identity_reader(process.pid)
        except (IOError, OSError, ValueError):
            if process.poll() is None:
                self._terminate_unregistered(process)
                raise SupervisorError("runtime_supervisor_failed")
        if identity is not None and (
            identity["pgid"] != process.pid
            or identity["session"] != process.pid
        ):
            self._terminate_unregistered(process)
            raise SupervisorError("runtime_supervisor_failed")
        record = _ChildRecord(role, process, identity)
        self.children[role] = record
        self._log("{0} 프로세스를 시작했습니다.".format(role))
        return record

    @staticmethod
    def _terminate_unregistered(process):
        try:
            process.terminate()
        except (AttributeError, IOError, OSError):
            pass
        try:
            process.wait(timeout=1)
        except (AttributeError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except (AttributeError, IOError, OSError):
                pass
            try:
                process.wait()
            except (AttributeError, IOError, OSError):
                pass

    def _record_is_current(self, record):
        if record.process.poll() is not None:
            return False
        try:
            identity = self.identity_reader(record.pid)
        except (IOError, OSError, ValueError):
            return False
        return (
            identity["pid"] == record.pid
            and identity["pgid"] == record.pgid
            and identity["session"] == record.session
            and identity["starttime"] == record.starttime
        )

    def _signal_record(self, record, signum):
        if record is None or record.role not in self.children:
            return False
        if not self._record_is_current(record):
            if record.process.poll() is not None:
                self._reap_record(record)
            return False
        try:
            self.group_signaler(record.pgid, signum)
        except (IOError, OSError):
            if record.process.poll() is not None:
                self._reap_record(record)
                return False
            raise SupervisorError("runtime_supervisor_failed")
        return True

    def _reap_record(self, record, timeout=None):
        if record is None:
            return None
        if record.master_reaped:
            result = record.returncode
        else:
            try:
                result = record.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None
            except (IOError, OSError):
                result = record.process.poll()
            if result is not None:
                record.returncode = result
                record.master_reaped = True
        if result is None:
            return None
        if record.starttime is None:
            self.children.pop(record.role, None)
            return result
        try:
            group_alive = self._group_is_alive(record)
        except SupervisorError:
            group_alive = True
        if not group_alive:
            self.children.pop(record.role, None)
        return result

    def _verified_group_members(self, record):
        try:
            members = self.group_reader(record.pgid)
        except (IOError, OSError, TypeError, ValueError):
            raise SupervisorError("runtime_supervisor_failed")
        if not isinstance(members, dict):
            raise SupervisorError("runtime_supervisor_failed")
        try:
            master_starttime = int(record.starttime, 10)
        except (TypeError, ValueError):
            raise SupervisorError("runtime_supervisor_failed")
        for pid, identity in members.items():
            if type(pid) is not int or not isinstance(identity, dict):
                raise SupervisorError("runtime_supervisor_failed")
            try:
                starttime = int(identity["starttime"], 10)
                valid = (
                    identity["pid"] == pid
                    and identity["pgid"] == record.pgid
                    and identity["session"] == record.session
                    and starttime >= master_starttime
                )
                if pid == record.pid:
                    valid = valid and (
                        identity["starttime"] == record.starttime
                    )
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise SupervisorError("runtime_supervisor_failed")
        return members

    def _group_is_alive(self, record):
        return bool(self._verified_group_members(record))

    def _signal_verified_group(self, record, signum):
        if not self._group_is_alive(record):
            return False
        try:
            self.group_signaler(record.pgid, signum)
        except (IOError, OSError):
            raise SupervisorError("runtime_supervisor_failed")
        return True

    def _record_is_registered(self, record):
        return (
            record is not None
            and self.children.get(record.role) is record
        )

    def _send_term_record(self, record):
        if not self._record_is_registered(record):
            return
        if record.role in self._shutdown_signaled:
            return
        signaled = False
        try:
            signaled = self._signal_verified_group(
                record, signal.SIGTERM
            )
        except SupervisorError:
            signaled = False
        if not signaled and record.process.poll() is None:
            try:
                record.process.terminate()
                signaled = True
            except (AttributeError, IOError, OSError):
                pass
        if signaled:
            self._shutdown_signaled.add(record.role)

    def _refresh_records(self, records):
        active = []
        for record in records:
            if not self._record_is_registered(record):
                continue
            if record.master_reaped or record.process.poll() is not None:
                self._reap_record(record, timeout=0)
            if self._record_is_registered(record):
                active.append(record)
        return active

    def _kill_record(self, record):
        if not self._record_is_registered(record):
            return
        try:
            if self._signal_verified_group(record, signal.SIGKILL):
                return
        except SupervisorError:
            pass
        if record.process.poll() is None:
            try:
                record.process.kill()
            except (AttributeError, IOError, OSError):
                pass

    def _terminate_records(self, records, deadline=None):
        active = [
            record for record in records
            if self._record_is_registered(record)
        ]
        if not active:
            return
        if deadline is None:
            deadline = self.clock() + _SHUTDOWN_SECONDS
        for record in active:
            self._send_term_record(record)
        kill_at = max(self.clock(), deadline - _POLL_SECONDS)
        while active:
            now = self.clock()
            if now >= kill_at:
                break
            active = self._refresh_records(active)
            if active:
                self.sleeper(min(
                    0.05,
                    max(0, kill_at - now),
                ))
        active = self._refresh_records(active)
        for record in active:
            self._kill_record(record)
        active = self._refresh_records(active)
        observable = []
        for record in active:
            if record.master_reaped:
                try:
                    self._verified_group_members(record)
                except SupervisorError:
                    continue
            observable.append(record)
        active = observable
        while active:
            now = self.clock()
            if now >= deadline:
                break
            active = self._refresh_records(active)
            if active:
                self.sleeper(min(
                    0.05,
                    max(0, deadline - now),
                ))
        self._refresh_records(active)

    def _terminate_record(self, record, deadline=None):
        self._terminate_records((record,), deadline=deadline)

    def _spawn_nginx(self):
        return self._spawn("nginx", NGINX_COMMAND)

    def _spawn_worker(self, lock_fd):
        try:
            reader, writer = os.pipe()
        except (IOError, OSError):
            raise SupervisorError("runtime_supervisor_failed")
        try:
            os.set_inheritable(reader, False)
            os.set_inheritable(writer, False)
            command = [sys.executable, WORKER_PATH]
            command.extend(self.arguments)
            command.extend([
                "--progress-fd", str(writer),
                "--lock-fd", str(lock_fd),
            ])
            record = self._spawn(
                "migration", command, pass_fds=(writer, lock_fd)
            )
        except BaseException as error:
            for descriptor in (reader, writer):
                try:
                    os.close(descriptor)
                except (IOError, OSError):
                    pass
            if isinstance(error, (IOError, OSError)):
                raise SupervisorError("runtime_supervisor_failed")
            raise
        try:
            os.close(writer)
            os.set_blocking(reader, False)
        except (IOError, OSError):
            try:
                os.close(reader)
            except (IOError, OSError):
                pass
            self._terminate_record(record)
            raise SupervisorError("runtime_supervisor_failed")
        return record, reader

    def _spawn_gunicorn(self):
        return self._spawn(
            "gunicorn", (GUNICORN_PATH,), cwd=PROJECT_ROOT
        )

    def _child_survived_start(self, record):
        if record.process.poll() is not None:
            self._reap_record(record)
            self._terminate_record(record)
            return False
        self.sleeper(0.05)
        if record.process.poll() is not None:
            self._reap_record(record)
            self._terminate_record(record)
            return False
        return True

    def _acquire_lock(self):
        self._raise_if_shutdown()
        if self.lock_acquirer is None:
            from django_images.services import startup_lock
            acquire = startup_lock.acquire_startup_lock
        else:
            acquire = self.lock_acquirer
        return acquire(self.data_root, self.service_uid, self.service_gid)

    def _apply_worker_event(self, event):
        self._project_status(self.status_store.apply_worker_event, event)
        phase = event["phase"]
        if phase == "error":
            self.last_worker_error = event["error_code"]
        if phase in ("complete", "error"):
            self._worker_terminal = phase

    def _drive_worker_and_heartbeat(self):  # noqa: C901
        decoder = ProgressFrameDecoder()
        selector = self.selector_factory()
        eof = False
        try:
            selector.register(self.progress_reader, selectors.EVENT_READ)
            while not eof:
                self._status_tick()
                if self._shutdown_signal is not None:
                    return _WORKER_SHUTDOWN
                if self.nginx.process.poll() is not None:
                    self._reap_record(self.nginx)
                    self._terminate_records((self.nginx, self.worker))
                    return _NGINX_EXITED
                ready = selector.select(_POLL_SECONDS)
                for key, _mask in ready:
                    if key.fileobj != self.progress_reader:
                        continue
                    while True:
                        try:
                            payload = os.read(self.progress_reader, 65536)
                        except BlockingIOError:
                            break
                        except InterruptedError:
                            continue
                        if not payload:
                            eof = True
                            break
                        for event in decoder.feed(payload):
                            self._apply_worker_event(event)
                        if len(payload) < 65536:
                            break
            decoder.finish()
            result = self._reap_record(self.worker, timeout=_POLL_SECONDS)
            if result is None:
                raise SupervisorError("worker_protocol_invalid")
            if self._record_is_registered(self.worker):
                self._terminate_record(self.worker)
            if self._record_is_registered(self.worker):
                raise SupervisorError("runtime_supervisor_failed")
            if result == 0 and self._worker_terminal == "complete":
                return 0
            if result != 0 and self._worker_terminal == "error":
                return 1
            raise SupervisorError("worker_protocol_invalid")
        except SupervisorError as error:
            self.last_worker_error = _safe_error_code(
                error, "worker_protocol_invalid"
            )
            self._terminate_record(self.worker)
            return 1
        finally:
            try:
                selector.unregister(self.progress_reader)
            except Exception:
                pass
            selector.close()
            try:
                os.close(self.progress_reader)
            except (IOError, OSError):
                pass
            self.progress_reader = None

    def _default_readiness_probe(self):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        request = urllib.request.Request(_READINESS_URL, method="GET")
        try:
            response = opener.open(request, timeout=2)
            try:
                payload = response.read(65537)
                if len(payload) > 65536:
                    return False
                parsed = json.loads(
                    payload.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                )
                return _valid_readiness_payload(
                    response.getcode(),
                    response.headers.get("Content-Type", ""),
                    parsed,
                )
            finally:
                response.close()
        except (IOError, OSError, UnicodeError, ValueError,
                http.client.HTTPException,
                urllib.error.URLError):
            return False

    def _wait_for_readiness(self):
        deadline = self.clock() + _READINESS_SECONDS
        probe = self.readiness_probe or self._default_readiness_probe
        while self.clock() < deadline:
            self._status_tick()
            if self._shutdown_signal is not None:
                return _WORKER_SHUTDOWN
            if self.nginx.process.poll() is not None:
                return _NGINX_EXITED
            if self.gunicorn.process.poll() is not None:
                return 1
            if probe():
                return 0
            self.sleeper(_READINESS_INTERVAL_SECONDS)
        return 1

    def _default_quiescence_probe(self, record, deadline):
        master_stopped = False
        stable_members = None
        stable_count = 0
        flags = os.WUNTRACED | os.WNOHANG
        while self.clock() < deadline:
            try:
                pid, status = self.waitpid(record.pid, flags)
            except OSError as error:
                if error.errno == errno.EINTR:
                    continue
                raise SupervisorError("runtime_supervisor_failed")
            if pid == record.pid:
                if os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP:
                    master_stopped = True
                elif os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    raise SupervisorError("runtime_supervisor_failed")
                else:
                    raise SupervisorError("runtime_supervisor_failed")
            try:
                current = self.identity_reader(record.pid)
                members = self.group_reader(record.pgid)
            except (IOError, OSError, ValueError):
                self.sleeper(0.05)
                continue
            if (
                current["starttime"] != record.starttime
                or current["pgid"] != record.pgid
                or current["session"] != record.session
            ):
                raise SupervisorError("runtime_supervisor_failed")
            member_ids = frozenset(members)
            all_stopped = bool(member_ids) and all(
                member["state"] in ("T", "t")
                for member in members.values()
            )
            if master_stopped and all_stopped:
                if member_ids == stable_members:
                    stable_count += 1
                else:
                    stable_members = member_ids
                    stable_count = 1
                if stable_count >= 2:
                    return True
            else:
                stable_members = None
                stable_count = 0
            self.sleeper(0.05)
        return False

    def _marker_exists(self):
        try:
            return os.path.lexists(self.status_store.marker_path)
        except (AttributeError, IOError, OSError):
            return None

    def _confirm_gate(self):
        try:
            self.status_store.create_gate()
        except StatusError:
            return False
        if not hasattr(self.status_store, "marker_path"):
            return True
        return self._marker_exists() is True

    def _resume_static_nginx(self):
        try:
            return self._signal_record(self.nginx, signal.SIGCONT)
        except SupervisorError:
            return False

    def _gate_transition_state(self):
        if self._shutdown_signal is not None:
            return _WORKER_SHUTDOWN
        if self.nginx.process.poll() is not None:
            self._reap_record(self.nginx, timeout=0)
            self._terminate_record(self.nginx)
            return _NGINX_EXITED
        if self.gunicorn.process.poll() is not None:
            self._reap_record(self.gunicorn, timeout=0)
            self._terminate_record(self.gunicorn)
            return 1
        return 0

    def _guard_gate_transition(self, nginx_stopped=False):
        state = self._gate_transition_state()
        if state == 0:
            return None
        if not self._confirm_gate():
            self._terminate_records((self.gunicorn, self.nginx))
            return 1
        if state == _WORKER_SHUTDOWN:
            return 0
        self._terminate_record(self.gunicorn)
        if state == _NGINX_EXITED:
            return 1
        try:
            self._project_status(
                self.status_store.failed, "gunicorn_start_failed"
            )
        except SupervisorError:
            pass
        if nginx_stopped and not self._resume_static_nginx():
            self._terminate_record(self.nginx)
            return 1
        return self._hold_failed("gunicorn_start_failed")

    def _transition_gate(self):  # noqa: C901
        guarded = self._guard_gate_transition()
        if guarded is not None:
            return guarded
        if not self._project_until_durable(
            self.status_store.starting_service,
            self.clock() + _QUIESCE_SECONDS,
        ):
            return self._fail_gate_transition("migration_status_write_failed")
        guarded = self._guard_gate_transition()
        if guarded is not None:
            return guarded
        try:
            stopped = self._signal_record(self.nginx, signal.SIGSTOP)
        except SupervisorError:
            stopped = False
        if not stopped:
            try:
                self._project_status(
                    self.status_store.failed, "runtime_supervisor_failed"
                )
            except SupervisorError:
                pass
            self._terminate_records((self.gunicorn, self.nginx))
            return 1
        guarded = self._guard_gate_transition(nginx_stopped=True)
        if guarded is not None:
            return guarded
        deadline = self.clock() + _QUIESCE_SECONDS
        probe = self.quiescence_probe or self._default_quiescence_probe
        try:
            quiet = probe(self.nginx, deadline)
        except SupervisorError:
            try:
                self._project_status(
                    self.status_store.failed, "runtime_supervisor_failed"
                )
            except SupervisorError:
                pass
            self._terminate_records((self.gunicorn, self.nginx))
            return 1
        guarded = self._guard_gate_transition(nginx_stopped=True)
        if guarded is not None:
            return guarded
        if not quiet:
            self._terminate_record(self.gunicorn)
            self._project_status(
                self.status_store.failed, "runtime_gate_quiesce_failed"
            )
            if not self._confirm_gate() or not self._resume_static_nginx():
                self._terminate_record(self.nginx)
                return 1
            return self._hold_failed("runtime_gate_quiesce_failed")
        try:
            opened = self._project_until_durable(
                lambda: self.status_store.open_service(
                    readiness_confirmed=True
                ),
                self.clock() + _QUIESCE_SECONDS,
            )
        except SupervisorError as error:
            code = _safe_error_code(error)
            self._terminate_record(self.gunicorn)
            if (
                code == "runtime_gate_open_failed"
                and self._confirm_gate()
                and self._resume_static_nginx()
            ):
                return self._hold_failed(code)
            self._terminate_record(self.nginx)
            return 1
        guarded = self._guard_gate_transition(nginx_stopped=True)
        if guarded is not None:
            return guarded
        if not opened:
            self._terminate_record(self.gunicorn)
            self._project_status(
                self.status_store.failed, "migration_status_write_failed"
            )
            if not self._confirm_gate() or not self._resume_static_nginx():
                self._terminate_record(self.nginx)
                return 1
            return self._hold_failed("migration_status_write_failed")
        try:
            resumed = self._signal_record(self.nginx, signal.SIGCONT)
        except SupervisorError:
            resumed = False
        if not resumed:
            if not self._confirm_gate():
                self._terminate_records((self.gunicorn, self.nginx))
                return 1
            self._terminate_records((self.gunicorn, self.nginx))
            return 1
        return self._serve_application()

    def _project_until_durable(self, method, deadline):
        while self.clock() < deadline:
            self._raise_if_shutdown()
            if self._project_status(method):
                return True
            self._status_tick()
            self.sleeper(0.05)
        return False

    def _fail_gate_transition(self, code):
        self._terminate_record(self.gunicorn)
        self._project_status(self.status_store.failed, code)
        if not self._confirm_gate():
            self._terminate_record(self.nginx)
            return 1
        return self._hold_failed(code)

    def _start_application_and_serve(self):
        self._raise_if_shutdown()
        try:
            self.gunicorn = self._spawn_gunicorn()
        except SupervisorError:
            return self._hold_failed("gunicorn_start_failed")
        self._raise_if_shutdown()
        if not self._child_survived_start(self.gunicorn):
            return self._hold_failed("gunicorn_start_failed")
        ready = self._wait_for_readiness()
        if ready == _WORKER_SHUTDOWN:
            return 0
        if ready == _NGINX_EXITED:
            self._terminate_record(self.gunicorn)
            return 1
        if ready != 0:
            self._terminate_record(self.gunicorn)
            return self._hold_failed("gunicorn_start_failed")
        return self._transition_gate()

    def _serve_application(self):
        while self._shutdown_signal is None:
            self._status_tick()
            if self.nginx.process.poll() is not None:
                self._reap_record(self.nginx)
                self._terminate_records((self.nginx, self.gunicorn))
                return 1
            if self.gunicorn.process.poll() is not None:
                self._reap_record(self.gunicorn)
                self._terminate_record(self.gunicorn)
                try:
                    self.status_store.create_gate()
                except StatusError:
                    self._terminate_record(self.nginx)
                    return 1
                return self._hold_failed("gunicorn_start_failed")
            self.sleeper(_POLL_SECONDS)
        return 0

    def _hold_failed(self, code):
        try:
            self._project_status(self.status_store.failed, code)
        except SupervisorError:
            pass
        while self._shutdown_signal is None:
            self._status_tick()
            if self.nginx is None or self.nginx.process.poll() is not None:
                if self.nginx is not None:
                    self._reap_record(self.nginx)
                    self._terminate_record(self.nginx)
                return 1
            self.sleeper(_POLL_SECONDS)
        return 0

    def handle_signal(self, signum, frame):
        del frame
        if self._shutdown_signal is not None:
            return
        self._shutdown_signal = signum
        self._shutdown_deadline = self.clock() + _SHUTDOWN_SECONDS
        if "migration" in self.children:
            order = ("migration", "nginx")
        else:
            order = ("gunicorn", "nginx")
        for role in order:
            record = self.children.get(role)
            self._send_term_record(record)

    def _cleanup(self):
        deadline = self._shutdown_deadline
        if deadline is None:
            deadline = self.clock() + _SHUTDOWN_SECONDS
        self._terminate_records(
            [
                self.children.get(role)
                for role in ("migration", "gunicorn", "nginx")
            ],
            deadline=deadline,
        )
        if os.getpid() == 1:
            while True:
                try:
                    pid, _status = os.waitpid(-1, os.WNOHANG)
                except OSError as error:
                    if error.errno == errno.EINTR:
                        continue
                    break
                if pid <= 0:
                    break

    def run(self):
        result = 1
        try:
            self._raise_if_shutdown()
            self.status_store.prepare_runtime_gate()
            self._raise_if_shutdown()
            self.nginx = self._spawn_nginx()
            if not self._child_survived_start(self.nginx):
                return 1
            self._raise_if_shutdown()
            self._project_status(self.status_store.initialize)
            self._raise_if_shutdown()
            try:
                self.startup_lock = self._acquire_lock()
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                if isinstance(error, _ShutdownRequested):
                    raise
                return self._hold_failed(_safe_error_code(error))
            self._raise_if_shutdown()
            self.worker, self.progress_reader = self._spawn_worker(
                self.startup_lock.fileno()
            )
            worker_result = self._drive_worker_and_heartbeat()
            if worker_result == _WORKER_SHUTDOWN:
                return 0
            if worker_result == _NGINX_EXITED:
                return 1
            if worker_result != 0:
                return self._hold_failed(self.last_worker_error)
            self._raise_if_shutdown()
            result = self._start_application_and_serve()
            return result
        except _ShutdownRequested:
            return 0
        except SupervisorError as error:
            if self.nginx is not None and self.nginx.process.poll() is None:
                return self._hold_failed(_safe_error_code(error))
            return 1
        except StatusError as error:
            self._log(
                "유지보수 게이트를 준비하지 못했습니다: {}".format(
                    _safe_error_code(error)
                )
            )
            return 1
        finally:
            self._cleanup()
            if self.startup_lock is not None:
                try:
                    self.startup_lock.close()
                except (AttributeError, IOError, OSError):
                    pass
