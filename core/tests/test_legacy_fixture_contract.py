import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_SCRIPT = (
    REPOSITORY_ROOT / "docker/tests/fixtures/create_legacy_fixture.py"
)


class LegacyFixtureContractTests(unittest.TestCase):
    def run_fixture(self, *arguments):
        return subprocess.run(
            [sys.executable, str(FIXTURE_SCRIPT)] + list(arguments),
            cwd=str(REPOSITORY_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    def load_fixture_module():
        spec = importlib.util.spec_from_file_location(
            "svrx_pinry_legacy_fixture", str(FIXTURE_SCRIPT)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_host_linear_commit_recording_does_not_require_repository_checkout(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            isolated_root = Path(temporary)
            fixture_script = isolated_root / "create_legacy_fixture.py"
            fixture_script.write_bytes(FIXTURE_SCRIPT.read_bytes())
            data_root = isolated_root / "data"
            run_path = data_root / "migration-backups" / "fixture-run"
            run_path.mkdir(parents=True)
            payload = {
                "event": "batch_commit",
                "batch_id": "fixture-batch-1",
            }
            canonical = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            frame = {
                "checksum": hashlib.sha256(canonical).hexdigest(),
                "payload": payload,
            }
            journal = run_path / "linear-migration-v1.jsonl"
            journal.write_text(
                json.dumps(
                    frame,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n",
                encoding="ascii",
            )
            output = data_root / "linear-commit-observation.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(fixture_script),
                    "record-linear-commit",
                    "--data-root",
                    str(data_root),
                    "--output",
                    str(output),
                    "--timeout",
                    "1",
                ],
                cwd=str(isolated_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", "replace"),
            )
            self.assertEqual(
                completed.stdout,
                b"FIXTURE_LINEAR_COMMIT_RECORDED\n",
            )
            self.assertTrue(output.is_file())

    def test_repository_fixture_bootstraps_project_imports_outside_cwd(self):
        source = "\n".join((
            "import importlib.util",
            "spec = importlib.util.spec_from_file_location(",
            "    'isolated_fixture', {!r})".format(str(FIXTURE_SCRIPT)),
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "from django_images.services import migration_batch_log",
            "assert migration_batch_log._canonical_json({",
            "    'second': 2, 'first': 1",
            "}) == b'{\"first\":1,\"second\":2}'",
            "print('FIXTURE_PROJECT_IMPORT_OK')",
        ))
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", source],
                cwd=temporary,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        self.assertEqual(
            completed.stdout,
            b"FIXTURE_PROJECT_IMPORT_OK\n",
        )

    def test_copying_observation_uses_linear_commits_and_legacy_fallback(
        self,
    ):
        fixture = self.load_fixture_module()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            backup_root = data_root / "legacy-backup"
            run_path = backup_root / "fixture-run"
            run_path.mkdir(parents=True)
            backup_root.chmod(0o700)
            run_path.chmod(0o700)
            state_path = run_path / "migration-state.json"
            state_path.write_text(
                json.dumps({
                    "run_id": "fixture-run",
                    "phase": "copying",
                }),
                encoding="ascii",
            )
            state_path.chmod(0o600)
            media_path = run_path / "media-migration.jsonl"
            media_path.write_bytes(
                b'{"event":"planned_skeleton"}\n'
                b'{"event":"plan_complete"}\n'
            )
            (run_path / "production.db.before-migration").write_bytes(
                b"fixture-snapshot"
            )

            with mock.patch.object(
                fixture.pwd, "getpwnam", side_effect=KeyError
            ):
                self.assertIsNone(fixture._copying_observation(str(data_root)))

                payload = {
                    "event": "batch_commit",
                    "batch_id": "fixture-batch-1",
                }
                canonical = json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                frame = {
                    "checksum": hashlib.sha256(canonical).hexdigest(),
                    "payload": payload,
                }
                linear_path = run_path / "linear-migration-v1.jsonl"
                linear_path.write_text(
                    json.dumps(
                        frame,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n",
                    encoding="ascii",
                )

                observation_path = data_root / "resume-observation.json"
                with mock.patch.object(
                    fixture.time,
                    "sleep",
                    side_effect=AssertionError("unexpected polling"),
                ):
                    fixture._record_resume(
                        str(data_root),
                        str(observation_path),
                        0.01,
                    )
                observation, _raw, observation_stat = (
                    fixture._load_json_file(observation_path)
                )
                self.assertEqual(
                    observation_stat.st_mode & 0o777,
                    0o600,
                )
                fixture._validate_resume_observation(observation)
                fixture._verify_recorded_copying(
                    str(data_root), observation
                )
                self.assertIsNotNone(observation)
                self.assertEqual(observation["run_id"], "fixture-run")

                state_path.write_text(
                    json.dumps({
                        "run_id": "fixture-run",
                        "phase": "complete",
                    }),
                    encoding="ascii",
                )
                self.assertIsNone(
                    fixture._copying_observation(str(data_root))
                )

                linear_path.unlink()
                state_path.write_text(
                    json.dumps({
                        "run_id": "fixture-run",
                        "phase": "copying",
                    }),
                    encoding="ascii",
                )
                media_path.write_bytes(
                    b'{"event":"planned_skeleton"}\n'
                    b'{"event":"published","image_id":1}\n'
                )
                self.assertIsNotNone(
                    fixture._copying_observation(str(data_root))
                )

    def test_receipt_privacy_keeps_relative_paths_but_rejects_private_values(
        self,
    ):
        fixture = self.load_fixture_module()

        fixture._validate_receipt_value({
            "legacy_original_path": "a/fixture-original.png",
            "file_key": "original:1",
        })

        unsafe_values = (
            "/data/production.db",
            "https://private.example/media/image.png",
            "//private-host/share/image.png",
        )
        for value in unsafe_values:
            with self.subTest(value=value), self.assertRaises(
                fixture.FixtureError
            ) as raised:
                fixture._validate_receipt_value({
                    "prior_image_media_url": value,
                })
            self.assertEqual(
                str(raised.exception), "receipt_contains_private_data"
            )

        with self.assertRaises(fixture.FixtureError) as raised:
            fixture._validate_receipt_value({"api_token": "private"})
        self.assertEqual(
            str(raised.exception), "receipt_contains_private_data"
        )

    def test_completed_linear_summary_matches_fixture_receipt(self):
        fixture = self.load_fixture_module()
        receipt = {
            "kind": "legacy-md5",
            "items": [{"derivatives": [{}, {}, {}]}],
        }
        media_summary = types.SimpleNamespace(
            image_count=1,
            md5_legacy=1,
            fixed_slot=0,
            named_canonical=0,
        )
        backfill_summary = types.SimpleNamespace(
            scanned=1,
            eligible=1,
            registered=1,
            already_registered=0,
            skipped=0,
            reason_counts={},
        )

        fixture._assert_completed_linear_summaries(
            receipt,
            media_summary,
            backfill_summary,
            {
                "images_total": 1,
                "files_total": 4,
                "backfill_total": 1,
            },
        )

        media_summary.md5_legacy = 0
        with self.assertRaisesRegex(
            fixture.FixtureError, "manifest_incomplete"
        ):
            fixture._assert_completed_linear_summaries(
                receipt,
                media_summary,
                backfill_summary,
                {
                    "images_total": 1,
                    "files_total": 4,
                    "backfill_total": 1,
                },
            )

    def test_api_failure_code_exposes_only_mode_and_http_status(self):
        fixture = self.load_fixture_module()

        self.assertEqual(
            fixture._api_failure_code(
                "api_local_upload_failed", "new", 503
            ),
            "api_local_upload_failed_new_503",
        )
        self.assertEqual(
            fixture._api_failure_code(
                "api_local_upload_failed", "migrated", "private"
            ),
            "api_local_upload_failed_migrated_unknown",
        )

    def test_completed_manifest_accepts_linear_authority(self):
        fixture = self.load_fixture_module()
        receipt = {
            "kind": "legacy-md5",
            "items": [{"image_id": 1, "derivatives": [{}, {}, {}]}],
        }
        summaries = (
            types.SimpleNamespace(
                image_count=1,
                md5_legacy=1,
                fixed_slot=0,
                named_canonical=0,
            ),
            types.SimpleNamespace(
                scanned=1,
                registered=1,
                already_registered=0,
                skipped=0,
                reason_counts={},
            ),
            {
                "images_total": 1,
                "files_total": 4,
                "backfill_total": 1,
            },
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            run_path = data_root / "migration-backups" / "fixture-run"
            run_path.mkdir(parents=True)
            media_raw = b'{"event":"planned_skeleton"}\n'
            backfill_raw = b'{"event":"planned_skeleton"}\n'
            artifacts = {
                "media-migration.jsonl": media_raw,
                "media-asset-backfill.jsonl": backfill_raw,
                "linear-migration-v1.jsonl": b"linear-authority\n",
                "migration-summary.json": json.dumps({
                    "phase": "complete",
                    "media_manifest_sha256": hashlib.sha256(
                        media_raw
                    ).hexdigest(),
                    "backfill_manifest_sha256": hashlib.sha256(
                        backfill_raw
                    ).hexdigest(),
                }).encode("ascii"),
            }
            for filename, payload in artifacts.items():
                path = run_path / filename
                path.write_bytes(payload)
                path.chmod(0o600)
            summary_path = run_path / "migration-summary.json"
            os.chown(summary_path, os.geteuid(), os.getegid())
            summary_path.chmod(0o644)

            with mock.patch.object(
                fixture,
                "_load_completed_linear_summaries",
                return_value=summaries,
            ):
                fixture._assert_completed_manifests(
                    str(data_root), str(run_path), receipt
                )

    @staticmethod
    def write_auth_database(data_root, users=(), tokens=()):
        database = data_root / "production.db"
        connection = sqlite3.connect(str(database))
        try:
            connection.execute(
                "CREATE TABLE auth_user ("
                "id INTEGER PRIMARY KEY, is_active INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE authtoken_token ("
                "key TEXT PRIMARY KEY, user_id INTEGER NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO auth_user (id, is_active) VALUES (?, ?)",
                users,
            )
            connection.executemany(
                "INSERT INTO authtoken_token (key, user_id) VALUES (?, ?)",
                tokens,
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def api_response(status_code, payload):
        response = mock.Mock(status_code=status_code)
        response.json.return_value = payload
        return response

    @staticmethod
    def linear_journal_frame(payload, compact=True):
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        frame = {
            "checksum": hashlib.sha256(canonical).hexdigest(),
            "payload": payload,
        }
        if compact:
            serialized = json.dumps(
                frame,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            serialized = json.dumps(
                frame,
                ensure_ascii=True,
                sort_keys=False,
            )
        return serialized.encode("ascii") + b"\n"

    @staticmethod
    def maintenance_status(state="migrating", **overrides):
        phases = {
            "starting": ("preparing", "이전 준비"),
            "recovering": ("recovery", "이전 상태 확인"),
            "migrating": ("copying", "이미지 파일 이전"),
            "starting_service": ("complete", "이전 완료"),
            "ready": ("complete", "준비 완료"),
            "failed": ("copying", "이전 실패"),
        }
        phase, phase_label = phases[state]
        terminal = state in ("starting_service", "ready")
        failed = state == "failed"
        status = {
            "schema_version": 1,
            "state": state,
            "phase": phase,
            "phase_label": phase_label,
            "run_id": "fixture-run",
            "attempt": 1,
            "resume_count": 1,
            "started_at": "2026-08-28T00:00:00Z",
            "phase_started_at": "2026-08-28T00:00:00Z",
            "heartbeat_at": "2026-08-28T00:00:01Z" if terminal else (
                "2026-08-28T00:00:00Z"
            ),
            "progress_at": "2026-08-28T00:00:01Z" if terminal else (
                "2026-08-28T00:00:00Z"
            ),
            "last_committed_batch": 5 if terminal else 4,
            "images_done": 346 if terminal else 300,
            "images_total": 346,
            "files_done": 1384 if terminal else 1200,
            "files_total": 1384,
            "backfill_done": 346 if terminal else 300,
            "backfill_total": 346,
            "phase_percent": 100.0 if terminal else 80.0,
            "overall_percent": 100.0 if terminal else 70.0,
            "error_class": "fatal" if failed else None,
            "error_code": "fixture_failed" if failed else None,
        }
        status.update(overrides)
        return status

    def run_maintenance_statuses(
        self, statuses, timeout="0.5", expected_state="migrating",
        require_terminal=False,
    ):
        class Handler(BaseHTTPRequestHandler):
            status_reads = 0

            def do_GET(self):
                if self.path == "/migration/":
                    body = (
                        "기존 Pinry 데이터를 이전하고 있습니다."
                    ).encode()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "text/html; charset=utf-8"
                    )
                elif self.path == "/migration-status.json":
                    index = min(
                        type(self).status_reads,
                        len(statuses) - 1,
                    )
                    type(self).status_reads += 1
                    body = json.dumps(statuses[index]).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                elif self.path in (
                    "/api/v2/version/", "/media/private"
                ):
                    body = b"blocked"
                    self.send_response(503)
                    self.send_header("Retry-After", "5")
                else:
                    body = b"not found"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            arguments = [
                "assert-maintenance-http",
                "--base-url",
                "http://127.0.0.1:{}".format(server.server_port),
                "--expected-state",
                expected_state,
                "--timeout",
                timeout,
                "--expected-images",
                "346",
                "--expected-files",
                "1384",
            ]
            if require_terminal:
                arguments.append("--require-terminal")
            completed = self.run_fixture(*arguments)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        return completed, Handler.status_reads

    def record_first_linear_commit(self, data_root, journal_payload):
        journal_path = data_root / "linear-migration-v1.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(journal_payload)
        observation_path = data_root / "first-commit-observation.json"
        completed = self.run_fixture(
            "record-linear-commit",
            "--data-root", str(data_root),
            "--output", str(observation_path),
            "--timeout", "1",
        )
        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        return journal_path, observation_path

    @staticmethod
    def write_tail_repair_observation(
        data_root, last_valid_offset=1024, torn_size=1200,
        device=10, inode=111111,
    ):
        observation_path = data_root / "tail-repair-observation.json"
        observation_path.write_text(json.dumps({
            "schema_version": 1,
            "fault": "journal_commit_partial_write",
            "journal_identity": {"device": device, "inode": inode},
            "torn_size": torn_size,
            "last_valid_offset": last_valid_offset,
            "valid_prefix_sha256": "a" * 64,
            "torn_journal_sha256": "b" * 64,
        }), encoding="ascii")
        observation_path.chmod(0o600)
        return observation_path

    def test_api_authentication_uses_existing_clone_token_when_private(self):
        module = self.load_fixture_module()
        token = "a" * 40

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            self.write_auth_database(
                data_root,
                users=((1, 1),),
                tokens=((token, 1),),
            )

            session = mock.Mock()

            def get_profile(_url, **kwargs):
                if kwargs.get("headers") == {
                    "Authorization": "Token {}".format(token)
                }:
                    return self.api_response(200, [{
                        "username": "legacy-owner",
                        "token": token,
                    }])
                return self.api_response(403, {})

            session.get.side_effect = get_profile

            headers = module._ensure_fixture_authentication(
                session,
                "http://app",
                "new",
                str(data_root),
                require_existing=True,
            )

        self.assertEqual(
            headers,
            {"Authorization": "Token {}".format(token)},
        )
        session.post.assert_not_called()

    def test_api_authentication_falls_back_to_registration_without_clone_token(self):
        module = self.load_fixture_module()
        token = "b" * 40

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            self.write_auth_database(data_root)
            session = mock.Mock()

            def get_profile(_url, **kwargs):
                if session.post.called:
                    return self.api_response(200, [{
                        "username": "fixture-reviewer",
                        "token": token,
                    }])
                return self.api_response(403, {})

            session.get.side_effect = get_profile
            session.post.return_value = self.api_response(201, {})

            headers = module._ensure_fixture_authentication(
                session,
                "http://app",
                "new",
                str(data_root),
            )

        self.assertEqual(
            headers,
            {"Authorization": "Token {}".format(token)},
        )
        session.post.assert_called_once()

    def test_api_authentication_requires_existing_token_when_requested(self):
        module = self.load_fixture_module()

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            self.write_auth_database(data_root)
            session = mock.Mock()
            session.get.return_value = self.api_response(403, {})

            with self.assertRaises(module.FixtureError) as raised:
                module._ensure_fixture_authentication(
                    session,
                    "http://app",
                    "new",
                    str(data_root),
                    require_existing=True,
                )

        self.assertEqual(str(raised.exception), "api_auth_probe_failed")
        session.post.assert_not_called()

    def test_api_authentication_required_mode_does_not_use_basic_auth(self):
        module = self.load_fixture_module()
        fixture_token = "f" * 40

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            self.write_auth_database(data_root)
            session = mock.Mock()
            session.get.return_value = self.api_response(200, [{
                "username": "fixture-reviewer",
                "token": fixture_token,
            }])

            with self.assertRaises(module.FixtureError) as raised:
                module._ensure_fixture_authentication(
                    session,
                    "http://app",
                    "new",
                    str(data_root),
                    require_existing=True,
                )

        self.assertEqual(str(raised.exception), "api_auth_probe_failed")
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_api_authentication_rejects_wrong_basic_identity(self):
        module = self.load_fixture_module()
        fixture_token = "c" * 40

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            self.write_auth_database(data_root)
            session = mock.Mock()

            def get_profile(_url, **kwargs):
                username = (
                    "fixture-reviewer" if session.post.called else "other-user"
                )
                return self.api_response(200, [{
                    "username": username,
                    "token": fixture_token,
                }])

            session.get.side_effect = get_profile
            session.post.return_value = self.api_response(201, {})

            headers = module._ensure_fixture_authentication(
                session,
                "http://app",
                "new",
                str(data_root),
            )

        self.assertEqual(
            headers,
            {"Authorization": "Token {}".format(fixture_token)},
        )
        session.post.assert_called_once()

    def test_api_authentication_fails_closed_without_leaking_clone_token(self):
        module = self.load_fixture_module()
        token = "d" * 40

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            self.write_auth_database(
                data_root,
                users=((1, 1),),
                tokens=((token, 1),),
            )
            session = mock.Mock()

            def get_profile(_url, **kwargs):
                if kwargs.get("headers") is not None:
                    return self.api_response(500, {"token": token})
                return self.api_response(403, {})

            session.get.side_effect = get_profile

            with self.assertRaises(module.FixtureError) as raised:
                module._ensure_fixture_authentication(
                    session,
                    "http://app",
                    "migrated",
                    str(data_root),
                )

        self.assertEqual(str(raised.exception), "api_auth_probe_failed")
        self.assertNotIn(token, str(raised.exception))
        session.post.assert_not_called()

    def test_configure_settings_preserves_existing_private_configuration(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            data_root.mkdir()
            settings_path = data_root / "local_settings.py"
            original = (
                b"# -*- coding: cp949 -*-\n"
                b"SECRET_KEY = 'legacy-secret'\n"
                b"PUBLIC = False\n"
                b"# " + "레거시 설정".encode("cp949") + b"\n"
                b"PINRY_FETCH_PRIVATE_ALLOWLIST = []"
            )
            settings_path.write_bytes(original)

            completed = self.run_fixture(
                "configure-settings",
                "--data-root", str(data_root),
                "--allow-host", "image-source",
                "--preserve-existing",
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            self.assertEqual(
                completed.stdout.decode("ascii").strip(),
                "FIXTURE_SETTINGS_OK",
            )
            repeated = self.run_fixture(
                "configure-settings",
                "--data-root", str(data_root),
                "--allow-host", "image-source",
                "--preserve-existing",
            )
            self.assertEqual(
                repeated.returncode, 0, repeated.stderr.decode("utf-8")
            )
            configured = settings_path.read_bytes()
            self.assertTrue(configured.startswith(original))
            self.assertIn(b"PUBLIC = False", configured)
            self.assertIn(b"SECRET_KEY = 'legacy-secret'", configured)
            self.assertTrue(configured.endswith(
                b"PINRY_FETCH_PRIVATE_ALLOWLIST = ['image-source']\n"
            ))
            self.assertEqual(configured.count(
                b"# SVRx Pinry NAS acceptance override\n"
            ), 1)
            self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)
            namespace = {}
            exec(compile(configured, str(settings_path), "exec"), namespace)
            self.assertIs(namespace["PUBLIC"], False)
            self.assertEqual(namespace["SECRET_KEY"], "legacy-secret")
            self.assertEqual(
                namespace["PINRY_FETCH_PRIVATE_ALLOWLIST"],
                ["image-source"],
            )

    def test_configure_settings_preserve_mode_rejects_invalid_existing_file(self):
        settings_prefix = (
            b"SECRET_KEY = 'legacy-secret'\nPUBLIC = False\n#"
        )
        expanded_oversized = settings_prefix + b"x" * (
            1024 * 1024 - len(settings_prefix)
        )
        cases = (
            ("missing", None),
            ("empty", b""),
            ("placeholder", b"SECRET_KEY = 'secret_key_place_holder'\n"),
            ("oversized", b"x" * (1024 * 1024 + 1)),
            ("expanded_oversized", expanded_oversized),
            ("symlink", b"PUBLIC = False\n"),
            ("hardlink", b"PUBLIC = False\n"),
        )

        for name, payload in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                dir="/private/tmp"
            ) as temporary:
                data_root = Path(temporary, "data")
                data_root.mkdir()
                settings_path = data_root / "local_settings.py"
                backing_path = data_root / "backing.py"
                if payload is not None:
                    if name in ("symlink", "hardlink"):
                        backing_path.write_bytes(payload)
                        if name == "symlink":
                            settings_path.symlink_to(backing_path)
                        else:
                            os.link(str(backing_path), str(settings_path))
                    else:
                        settings_path.write_bytes(payload)

                completed = self.run_fixture(
                    "configure-settings",
                    "--data-root", str(data_root),
                    "--allow-host", "image-source",
                    "--preserve-existing",
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    completed.stderr.decode("ascii").strip(),
                    "FIXTURE_ERROR:existing_settings_invalid",
                )
                if payload is None:
                    self.assertFalse(settings_path.exists())
                elif name in ("symlink", "hardlink"):
                    self.assertEqual(backing_path.read_bytes(), payload)
                else:
                    self.assertEqual(settings_path.read_bytes(), payload)

    def test_linear_fixture_records_exact_workload_distribution(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            completed = self.run_fixture(
                "create-linear",
                "--data-root",
                str(data_root),
                "--images",
                "50",
                "--thumbnails",
                "3",
                "--receipt",
                str(receipt_path),
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            receipt = json.loads(receipt_path.read_text("ascii"))
            self.assertEqual(receipt["images_total"], 50)
            self.assertEqual(receipt["files_total"], 200)
            self.assertEqual(receipt["formats"], {"JPEG": 100, "PNG": 100})
            self.assertEqual(len(receipt["expected_files"]), 200)
            self.assertTrue(all(
                set(item) == {
                    "file_key",
                    "format",
                    "relative_path",
                    "size",
                    "source_identity",
                }
                for item in receipt["expected_files"]
            ))
            media_root = data_root / "static/media"
            disguised_formats = set()
            for item in receipt["expected_files"]:
                payload = (media_root / item["relative_path"]).read_bytes()
                self.assertEqual(len(payload), item["size"])
                self.assertEqual(
                    (media_root / item["relative_path"]).stat().st_ino,
                    item["source_identity"]["inode"],
                )
                if item["format"] == "JPEG":
                    self.assertTrue(payload.startswith(b"\xff\xd8\xff"))
                    if item["relative_path"].endswith(".png"):
                        disguised_formats.add("JPEG")
                else:
                    self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
                    if item["relative_path"].endswith(".jpg"):
                        disguised_formats.add("PNG")
            self.assertEqual(disguised_formats, {"JPEG", "PNG"})

    def test_linear_fixture_rejects_unsupported_workload_size(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            completed = self.run_fixture(
                "create-linear",
                "--data-root",
                str(data_root),
                "--images",
                "51",
                "--thumbnails",
                "3",
                "--receipt",
                str(data_root / "linear-receipt.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:create_linear_arguments_invalid",
            )

    def test_linear_fixture_supports_only_documented_workload_sizes(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            for images in (50, 350, 1000):
                data_root = Path(temporary, "data-{}".format(images))
                receipt_path = data_root / "linear-receipt.json"
                completed = self.run_fixture(
                    "create-linear",
                    "--data-root",
                    str(data_root),
                    "--images",
                    str(images),
                    "--thumbnails",
                    "3",
                    "--receipt",
                    str(receipt_path),
                )

                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                receipt = json.loads(receipt_path.read_text("ascii"))
                self.assertEqual(receipt["images_total"], images)
                self.assertEqual(receipt["files_total"], images * 4)
                self.assertEqual(
                    receipt["formats"],
                    {"JPEG": images * 2, "PNG": images * 2},
                )

    def test_verify_linear_metrics_enforces_exact_file_passes(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear",
                "--data-root",
                str(data_root),
                "--images",
                "50",
                "--thumbnails",
                "3",
                "--receipt",
                str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            receipt = json.loads(receipt_path.read_text("ascii"))
            sizes = {
                item["file_key"]: item["size"]
                for item in receipt["expected_files"]
            }
            ones = {key: 1 for key in sizes}
            status_path = data_root / "linear-status.json"
            status = {
                "state": "ready",
                "images": 50,
                "images_total": 50,
                "files_total": 200,
                "images_done": 50,
                "files_done": 200,
                "seconds": 1.0,
                "batch_seconds_p50": 0.1,
                "batch_seconds_p95": 0.2,
                "batch_seconds_max": 0.3,
                "batch_sample_count": 2,
                "max_heartbeat_gap_seconds": 0.4,
                "seconds_source": "fixture_coordinator",
                "batch_timing_source": "generator_yield_lifetime",
                "heartbeat_source": "nginx_public_status",
                "supervisor_seconds": 1.1,
                "supervisor_status_samples": 3,
                "supervisor_heartbeat_updates": 1,
                "queries": 10,
                "source_bytes": sizes,
                "source_full_reads": ones,
                "resume_rehash_reads": {},
                "pillow_decodes": ones,
                "old_manifest_replays": 2,
                "linear_journal_replays": 1,
                "process_starts": 1,
                "cpu_average_percent": 1.0,
                "cpu_max_percent": 2.0,
                "disk_read_bytes": 1,
                "disk_write_bytes": 1,
                "max_rss_bytes": 1,
                "ratio": 1.0,
            }
            status_path.write_text(json.dumps(status), encoding="ascii")

            completed = self.run_fixture(
                "verify-linear-metrics",
                "--receipt",
                str(receipt_path),
                "--status",
                str(status_path),
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )

            status["source_full_reads"][next(iter(sizes))] = 2
            status_path.write_text(json.dumps(status), encoding="ascii")
            rejected = self.run_fixture(
                "verify-linear-metrics",
                "--receipt",
                str(receipt_path),
                "--status",
                str(status_path),
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(
                rejected.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_metrics_invalid",
            )

            status["source_full_reads"] = ones
            status["batch_seconds_p50"] = 0.3
            status["batch_seconds_p95"] = 0.2
            status_path.write_text(json.dumps(status), encoding="ascii")
            rejected = self.run_fixture(
                "verify-linear-metrics",
                "--receipt",
                str(receipt_path),
                "--status",
                str(status_path),
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(
                rejected.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_metrics_invalid",
            )

    def test_batch_timing_wraps_the_complete_consumer_lifetime(self):
        fixture = self.load_fixture_module()
        durations = []

        class Owner(object):
            def batches(self):
                yield "first"
                yield "second"

        with mock.patch.object(
            fixture.time,
            "monotonic",
            side_effect=(10.0, 14.0, 20.0, 29.0),
        ):
            with fixture._time_yielded_items(
                Owner, "batches", durations
            ):
                self.assertEqual(
                    list(Owner().batches()), ["first", "second"]
                )

        self.assertEqual(durations, [4.0, 9.0])

    def test_assert_maintenance_http_observes_progress_and_blocking(self):
        class Handler(BaseHTTPRequestHandler):
            status_reads = 0

            def do_GET(self):
                if self.path == "/migration/":
                    body = "기존 Pinry 데이터를 이전하고 있습니다.".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                elif self.path == "/migration-status.json":
                    type(self).status_reads += 1
                    recovering = type(self).status_reads == 1
                    changed = type(self).status_reads > 2
                    body = json.dumps(
                        LegacyFixtureContractTests.maintenance_status(
                            "recovering" if recovering else "migrating",
                            images_done=1 if changed else 0,
                            files_done=4 if changed else 0,
                            backfill_done=1 if changed else 0,
                            heartbeat_at=(
                                "2026-08-28T00:00:01Z" if changed else
                                "2026-08-28T00:00:00Z"
                            ),
                            progress_at=(
                                "2026-08-28T00:00:01Z" if changed else
                                "2026-08-28T00:00:00Z"
                            ),
                            last_committed_batch=1 if changed else 0,
                        )
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                elif self.path in ("/api/v2/version/", "/media/private"):
                    body = b"blocked"
                    self.send_response(503)
                    self.send_header("Retry-After", "5")
                else:
                    body = b"not found"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            completed = self.run_fixture(
                "assert-maintenance-http",
                "--base-url",
                "http://127.0.0.1:{}".format(server.server_port),
                "--expected-state",
                "migrating",
                "--timeout",
                "2",
                "--min-resume-count",
                "1",
            )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertGreaterEqual(Handler.status_reads, 2)

    def test_assert_maintenance_http_accepts_phase_or_single_field_progress(
        self,
    ):
        cases = (
            (
                self.maintenance_status(
                    "migrating", phase_percent=100.0
                ),
                self.maintenance_status(
                    "migrating",
                    phase="database",
                    phase_label="데이터베이스 반영",
                    phase_percent=0.0,
                ),
            ),
            (
                self.maintenance_status("migrating"),
                self.maintenance_status(
                    "migrating", files_done=1201
                ),
            ),
        )

        for baseline, progressed in cases:
            with self.subTest(progressed=progressed):
                completed, _status_reads = self.run_maintenance_statuses((
                    baseline,
                    progressed,
                ))
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )

    def test_assert_maintenance_http_accepts_actual_phase_cycles(self):
        statuses = (
            self.maintenance_status(
                "migrating", phase="planning", phase_label="이전 계획"
            ),
            self.maintenance_status(
                "migrating", phase="recovery", phase_label="이전 복구"
            ),
            self.maintenance_status(
                "migrating", phase="copying", phase_label="파일 복사"
            ),
            self.maintenance_status(
                "migrating", phase="database", phase_label="DB 반영"
            ),
            self.maintenance_status(
                "migrating", phase="copying", phase_label="파일 복사",
                images_done=301, files_done=1204,
                last_committed_batch=5,
            ),
            self.maintenance_status("ready"),
        )

        completed, status_reads = self.run_maintenance_statuses(
            statuses, require_terminal=True
        )

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )
        self.assertGreaterEqual(status_reads, len(statuses))

    def test_assert_maintenance_http_rejects_invalid_phase_reversal(self):
        completed, _status_reads = self.run_maintenance_statuses((
            self.maintenance_status(
                "migrating", phase="finalizing", phase_label="최종 검증"
            ),
            self.maintenance_status(
                "migrating", phase="copying", phase_label="파일 복사"
            ),
        ), require_terminal=True)

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "FIXTURE_ERROR:maintenance_progress_regressed",
        )

    def test_assert_maintenance_http_terminal_mode_checks_after_progress(self):
        malformed = self.maintenance_status("ready")
        del malformed["phase_label"]
        completed, status_reads = self.run_maintenance_statuses((
            self.maintenance_status("migrating"),
            self.maintenance_status(
                "migrating", phase="database", phase_label="DB 반영"
            ),
            malformed,
        ), require_terminal=True)

        self.assertEqual(completed.returncode, 1)
        self.assertGreaterEqual(status_reads, 3)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "FIXTURE_ERROR:maintenance_status_invalid",
        )

    def test_assert_maintenance_http_all_states_require_full_schema(self):
        for expected_state in ("recovering", "failed"):
            with self.subTest(expected_state=expected_state):
                invalid = self.maintenance_status(expected_state)
                del invalid["last_committed_batch"]
                completed, _status_reads = self.run_maintenance_statuses(
                    (invalid,), expected_state=expected_state
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "FIXTURE_ERROR:maintenance_status_invalid",
                )

    def test_assert_maintenance_http_accepts_fast_completed_migration(self):
        for completed_state in ("starting_service", "ready"):
            with self.subTest(completed_state=completed_state):
                completed, status_reads = self.run_maintenance_statuses((
                    self.maintenance_status("migrating"),
                    self.maintenance_status(completed_state),
                ))
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                self.assertGreaterEqual(status_reads, 2)

    def test_assert_maintenance_http_accepts_completion_when_migration_was_fast(self):
        cases = (
            (
                self.maintenance_status("starting"),
                self.maintenance_status("ready"),
            ),
            (
                self.maintenance_status("recovering"),
                self.maintenance_status("starting_service"),
            ),
            (self.maintenance_status("ready"),),
        )

        for statuses in cases:
            with self.subTest(first_state=statuses[0]["state"]):
                completed, _status_reads = self.run_maintenance_statuses(
                    statuses
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )

    def test_assert_maintenance_http_rejects_invalid_terminal_schema(self):
        missing = self.maintenance_status("ready")
        del missing["phase_label"]
        cases = (
            missing,
            self.maintenance_status("ready", schema_version=True),
            self.maintenance_status(
                "starting_service", images_done="346"
            ),
            self.maintenance_status("ready", phase="finalizing"),
            self.maintenance_status("ready", phase_percent=99.0),
            self.maintenance_status("ready", overall_percent=99.0),
            self.maintenance_status(
                "ready", images_done=345, images_total=345
            ),
            self.maintenance_status(
                "ready", files_done=1383, files_total=1383
            ),
            self.maintenance_status("ready", backfill_done=345),
            self.maintenance_status(
                "ready", backfill_done=347, backfill_total=346
            ),
        )

        for terminal in cases:
            with self.subTest(terminal=terminal):
                completed, _status_reads = self.run_maintenance_statuses((
                    self.maintenance_status("migrating"),
                    terminal,
                ))
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "FIXTURE_ERROR:maintenance_status_invalid",
                )

    def test_assert_maintenance_http_rejects_terminal_progress_regression(self):
        cases = {
            "images_done": self.maintenance_status(
                "migrating", images_done=350, images_total=400
            ),
            "files_done": self.maintenance_status(
                "migrating", files_done=1400, files_total=1500
            ),
            "last_committed_batch": self.maintenance_status(
                "migrating", last_committed_batch=6
            ),
            "backfill_done": self.maintenance_status(
                "migrating", backfill_done=350, backfill_total=400
            ),
            "heartbeat_at": self.maintenance_status(
                "migrating",
                heartbeat_at="2026-08-28T00:00:02Z",
                progress_at="2026-08-28T00:00:02Z",
            ),
            "progress_at": self.maintenance_status(
                "migrating",
                progress_at="2026-08-28T00:00:02Z",
            ),
            "attempt": self.maintenance_status(
                "migrating", attempt=2
            ),
            "resume_count": self.maintenance_status(
                "migrating", resume_count=2
            ),
        }

        for field, migrating in cases.items():
            with self.subTest(field=field):
                completed, _status_reads = self.run_maintenance_statuses((
                    migrating,
                    self.maintenance_status("ready"),
                ))
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "FIXTURE_ERROR:maintenance_progress_regressed",
                )

    def test_assert_maintenance_http_rejects_private_completed_status(self):
        terminal = self.maintenance_status("ready")
        terminal["private_path"] = "/private/source"
        completed, _status_reads = self.run_maintenance_statuses((
            self.maintenance_status("migrating"),
            terminal,
        ))

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "FIXTURE_ERROR:maintenance_status_private",
        )

    def test_assert_maintenance_http_retries_until_listener_ready(self):
        failed_status = self.maintenance_status("failed")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/migration/":
                    body = "기존 Pinry 데이터를 이전하고 있습니다.".encode()
                    self.send_response(200)
                elif self.path == "/migration-status.json":
                    body = json.dumps(failed_status).encode()
                    self.send_response(200)
                    self.send_header("Cache-Control", "no-store")
                elif self.path in ("/api/v2/version/", "/media/private"):
                    body = b"blocked"
                    self.send_response(503)
                    self.send_header("Retry-After", "5")
                else:
                    body = b"not found"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        server = ThreadingHTTPServer(
            ("127.0.0.1", port), Handler, bind_and_activate=False
        )
        started = threading.Event()

        def delayed_serve():
            threading.Event().wait(0.2)
            server.server_bind()
            server.server_activate()
            started.set()
            server.serve_forever()

        thread = threading.Thread(target=delayed_serve)
        thread.start()
        try:
            completed = self.run_fixture(
                "assert-maintenance-http",
                "--base-url",
                "http://127.0.0.1:{}".format(port),
                "--expected-state",
                "failed",
                "--timeout",
                "2",
            )
        finally:
            started.wait(1)
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(
            completed.returncode, 0, completed.stderr.decode("utf-8")
        )

    def test_assert_maintenance_http_reports_request_stage(self):
        module = self.load_fixture_module()
        import requests
        migrating_status = self.maintenance_status("migrating")

        class Response:
            def __init__(self, status_code):
                self.status_code = status_code
                self.text = (
                    "기존 Pinry 데이터를 이전하고 있습니다."
                )
                self.headers = {
                    "Cache-Control": "no-store",
                    "Retry-After": "5",
                }

            def json(self):
                return migrating_status

        cases = (
            ((), "maintenance_page_unavailable"),
            ((Response(200),), "maintenance_blocking_unavailable"),
            (
                (Response(200), Response(503), Response(503)),
                "maintenance_status_unavailable",
            ),
        )
        for initial_responses, expected_error in cases:
            responses = iter(initial_responses)

            def fail_after_responses(*_args, **_kwargs):
                try:
                    return next(responses)
                except StopIteration:
                    raise requests.ConnectionError()

            with self.subTest(expected_error=expected_error), mock.patch(
                "requests.Session.get", side_effect=fail_after_responses
            ), mock.patch("time.sleep", return_value=None):
                with self.assertRaises(module.FixtureError) as raised:
                    module._assert_maintenance_http(
                        "http://127.0.0.1:8000",
                        "migrating",
                        0.01,
                        expected_images=346,
                        expected_files=1384,
                    )
                self.assertEqual(str(raised.exception), expected_error)

    def test_assert_maintenance_http_preserves_failed_terminal_code(self):
        module = self.load_fixture_module()
        failed = self.maintenance_status(
            "failed", error_code="legacy_startup_failed"
        )

        class Response:
            status_code = 200
            text = "기존 Pinry 데이터를 이전하고 있습니다."
            headers = {"Cache-Control": "no-store", "Retry-After": "5"}

            def __init__(self, status=None, status_code=None):
                self.status = status
                if status_code is not None:
                    self.status_code = status_code

            def json(self):
                return self.status

        responses = iter((
            Response(),
            Response(status_code=503),
            Response(status_code=503),
        ))

        def response_sequence(*_args, **_kwargs):
            try:
                return next(responses)
            except StopIteration:
                return Response(failed)

        session = mock.Mock()
        session.get.side_effect = response_sequence
        requests_module = types.SimpleNamespace(
            Session=mock.Mock(return_value=session),
            RequestException=Exception,
        )
        with mock.patch.dict(
            sys.modules, {"requests": requests_module}
        ), mock.patch("time.sleep", return_value=None):
            with self.assertRaises(module.FixtureError) as raised:
                module._assert_maintenance_http(
                    "http://127.0.0.1:8000",
                    "migrating",
                    0.01,
                    expected_images=346,
                    expected_files=1384,
                )

        self.assertEqual(
            str(raised.exception),
            "maintenance_failed:legacy_startup_failed",
        )

    def test_maintenance_status_uses_allowlist_and_rejects_locations(self):
        class Handler(BaseHTTPRequestHandler):
            status = {"state": "failed"}

            def do_GET(self):
                if self.path == "/migration/":
                    body = "기존 Pinry 데이터를 이전하고 있습니다.".encode()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", "text/html; charset=utf-8"
                    )
                elif self.path == "/migration-status.json":
                    body = json.dumps(type(self).status).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                elif self.path in ("/api/v2/version/", "/media/private"):
                    body = b"blocked"
                    self.send_response(503)
                    self.send_header("Retry-After", "5")
                else:
                    body = b"not found"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        invalid_statuses = (
            {"state": "failed", "message": "not-public"},
            {"state": "failed", "sha256": "a" * 64},
            {"state": "failed", "digest": "b" * 64},
            {"state": "failed", "error_code": "c" * 64},
            {"state": "failed", "error_code": "/etc/passwd"},
            {"state": "failed", "error_code": r"C:\\private\\file"},
            {"state": "failed", "error_code": "file:///etc/passwd"},
            {"state": "failed", "error_code": "https://private.invalid"},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for status in invalid_statuses:
                Handler.status = status
                completed = self.run_fixture(
                    "assert-maintenance-http",
                    "--base-url",
                    "http://127.0.0.1:{}".format(server.server_port),
                    "--expected-state",
                    "failed",
                    "--timeout",
                    "2",
                )
                self.assertEqual(completed.returncode, 1, status)
                self.assertEqual(
                    completed.stderr.decode("utf-8").strip(),
                    "FIXTURE_ERROR:maintenance_status_private",
                )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_public_maintenance_status_allows_utc_timestamp_fields(self):
        fixture = self.load_fixture_module()
        original_urlsplit = fixture.urlsplit

        status = self.maintenance_status("migrating")
        timestamps = {
            status[field]
            for field in fixture._MAINTENANCE_TIMESTAMP_FIELDS
            if status[field] is not None
        }

        def legacy_urlsplit(value):
            parsed = original_urlsplit(value)
            if value in timestamps:
                return parsed._replace(scheme="2026-08-28t00")
            return parsed

        with mock.patch.object(
            fixture, "urlsplit", side_effect=legacy_urlsplit
        ):
            fixture._assert_public_status(status, "migrating")

    def test_observe_maintenance_service_records_public_heartbeat(self):
        class Handler(BaseHTTPRequestHandler):
            status_reads = 0
            statuses = (
                {
                    "state": "starting",
                    "heartbeat_at": "2026-08-28T00:00:00Z",
                },
                {
                    "state": "migrating",
                    "heartbeat_at": "2026-08-28T00:00:01Z",
                },
                {
                    "state": "starting_service",
                    "heartbeat_at": "2026-08-28T00:00:02Z",
                },
                {
                    "state": "ready",
                    "heartbeat_at": "2026-08-28T00:00:03Z",
                    "images_done": 50,
                    "images_total": 50,
                    "files_done": 200,
                    "files_total": 200,
                },
            )

            def do_GET(self):
                if self.path != "/migration-status.json":
                    body = b"not found"
                    self.send_response(404)
                else:
                    index = min(
                        type(self).status_reads,
                        len(type(self).statuses) - 1,
                    )
                    type(self).status_reads += 1
                    body = json.dumps(
                        type(self).statuses[index]
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(
                dir="/private/tmp"
            ) as temporary:
                observation_path = Path(temporary, "supervisor.json")
                completed = self.run_fixture(
                    "observe-maintenance-service",
                    "--base-url",
                    "http://127.0.0.1:{}".format(server.server_port),
                    "--output",
                    str(observation_path),
                    "--started-at-epoch-ns",
                    str(time.time_ns()),
                    "--images",
                    "50",
                    "--files",
                    "200",
                    "--timeout",
                    "2",
                    "--poll-interval",
                    "0.01",
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8"),
                )
                observation = json.loads(
                    observation_path.read_text("ascii")
                )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(observation["source"], "nginx_public_status")
        self.assertEqual(observation["state"], "ready")
        self.assertEqual(observation["images_total"], 50)
        self.assertEqual(observation["files_total"], 200)
        self.assertGreaterEqual(observation["supervisor_status_samples"], 4)
        self.assertEqual(observation["supervisor_heartbeat_updates"], 3)
        self.assertGreater(observation["supervisor_seconds"], 0)
        self.assertGreaterEqual(
            observation["max_heartbeat_gap_seconds"], 0
        )

    def test_observe_maintenance_service_rejects_constant_heartbeat(self):
        class Handler(BaseHTTPRequestHandler):
            status_reads = 0
            statuses = (
                {
                    "state": "starting",
                    "heartbeat_at": "2026-08-28T00:00:00Z",
                },
                {
                    "state": "migrating",
                    "heartbeat_at": "2026-08-28T00:00:00Z",
                },
                {
                    "state": "ready",
                    "heartbeat_at": "2026-08-28T00:00:00Z",
                    "images_done": 50,
                    "images_total": 50,
                    "files_done": 200,
                    "files_total": 200,
                },
            )

            def do_GET(self):
                index = min(
                    type(self).status_reads,
                    len(type(self).statuses) - 1,
                )
                type(self).status_reads += 1
                body = json.dumps(
                    type(self).statuses[index]
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(
                dir="/private/tmp"
            ) as temporary:
                completed = self.run_fixture(
                    "observe-maintenance-service",
                    "--base-url",
                    "http://127.0.0.1:{}".format(server.server_port),
                    "--output",
                    str(Path(temporary, "supervisor.json")),
                    "--started-at-epoch-ns",
                    str(time.time_ns()),
                    "--images",
                    "50",
                    "--files",
                    "200",
                    "--timeout",
                    "2",
                    "--poll-interval",
                    "0.01",
                )
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stderr.decode("utf-8").strip(),
            "FIXTURE_ERROR:supervisor_heartbeat_not_observed",
        )

    def test_container_metrics_are_merged_from_docker_stats(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            status_path = root / "metrics.json"
            stats_path = root / "stats.tsv"
            baseline_path = root / "baseline.json"
            supervisor_path = root / "supervisor.json"
            status_path.write_text(
                json.dumps({
                    "seconds": 6.0,
                    "images_total": 50,
                    "files_total": 200,
                    "seconds_source": "fixture_coordinator",
                    "batch_timing_source": "generator_yield_lifetime",
                    "heartbeat_source": "not_measured",
                    "max_heartbeat_gap_seconds": 0.0,
                }),
                encoding="ascii",
            )
            baseline_path.write_text(
                json.dumps({"seconds": 2.0}), encoding="ascii"
            )
            supervisor_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "source": "nginx_public_status",
                    "state": "ready",
                    "images_total": 50,
                    "files_total": 200,
                    "supervisor_seconds": 7.0,
                    "supervisor_status_samples": 8,
                    "supervisor_heartbeat_updates": 2,
                    "max_heartbeat_gap_seconds": 5.0,
                }),
                encoding="ascii",
            )
            container_id = "a" * 64
            cgroup_digest = "b" * 64
            stats_path.write_text(
                "\t".join((
                    "sample_kind", "epoch_ns", "container_id", "stats_id",
                    "proc_pid", "proc_start_ticks", "cgroup_sha256",
                    "cpu_percent", "memory_used", "block_read",
                    "block_write",
                )) + "\n" + "\t".join((
                    "periodic", "1", container_id, container_id[:12], "1",
                    "123", cgroup_digest, "10.00", "10MiB", "1MB",
                    "2MB",
                )) + "\n" + "\t".join((
                    "final", "2", container_id, container_id[:12], "1",
                    "123", cgroup_digest, "30.00", "20MiB", "3MB",
                    "4MB",
                )) + "\n",
                encoding="ascii",
            )

            completed = self.run_fixture(
                "merge-container-metrics",
                "--status",
                str(status_path),
                "--stats",
                str(stats_path),
                "--baseline",
                str(baseline_path),
                "--supervisor",
                str(supervisor_path),
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            merged = json.loads(status_path.read_text("ascii"))
            self.assertEqual(merged["cpu_average_percent"], 20.0)
            self.assertEqual(merged["cpu_max_percent"], 30.0)
            self.assertEqual(merged["max_rss_bytes"], 20 * 1024 * 1024)
            self.assertEqual(merged["disk_read_bytes"], 3 * 1000 * 1000)
            self.assertEqual(merged["disk_write_bytes"], 4 * 1000 * 1000)
            self.assertEqual(merged["ratio"], 3.0)
            self.assertEqual(merged["container_id"], container_id)
            self.assertEqual(merged["container_samples"], 2)
            self.assertTrue(merged["final_sample_preserved"])
            self.assertEqual(merged["supervisor_seconds"], 7.0)
            self.assertEqual(merged["max_heartbeat_gap_seconds"], 5.0)
            self.assertEqual(merged["supervisor_status_samples"], 8)
            self.assertEqual(merged["supervisor_heartbeat_updates"], 2)
            self.assertEqual(
                merged["heartbeat_source"], "nginx_public_status"
            )

            supervisor = json.loads(supervisor_path.read_text("ascii"))
            supervisor["images_total"] = 350
            supervisor["files_total"] = 1400
            supervisor_path.write_text(
                json.dumps(supervisor), encoding="ascii"
            )
            rejected = self.run_fixture(
                "merge-container-metrics",
                "--status", str(status_path),
                "--stats", str(stats_path),
                "--supervisor", str(supervisor_path),
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(
                rejected.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:container_metrics_invalid",
            )

    def test_audit_linear_io_parses_actual_trace_records(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear",
                "--data-root",
                str(data_root),
                "--images",
                "50",
                "--thumbnails",
                "3",
                "--receipt",
                str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            receipt = json.loads(receipt_path.read_text("ascii"))
            expected = receipt["expected_files"][0]
            payload = {
                "event": "batch_intent",
                "batch": 1,
                "entries": ["x" * 70000],
            }
            canonical = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            frame = json.dumps({
                "checksum": hashlib.sha256(canonical).hexdigest(),
                "payload": payload,
            }, ensure_ascii=True, sort_keys=True,
                separators=(",", ":")).encode("ascii") + b"\n"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete",
                "phase": "paths",
                "summary": {},
            })
            source_path = data_root / "static/media" / expected["relative_path"]
            journal_path = data_root / "linear-migration-v1.jsonl"
            staging_path = data_root / "static/media/.staging"
            originals_path = data_root / "static/media/originals"
            checkpoint_dir = data_root / "legacy-backup/run-1"
            checkpoint_temporary = (
                checkpoint_dir
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint_file = (
                checkpoint_dir / "linear-migration-checkpoint-v1.json"
            )
            batch_path = data_root / "static/media/.staging/batch.tmp"
            staging_path.mkdir(parents=True, exist_ok=True)
            originals_path.mkdir(parents=True, exist_ok=True)
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            batch_path.write_bytes(b"batch")
            checkpoint_temporary.write_bytes(b"checkpoint")
            staging_stat = staging_path.stat()
            originals_stat = originals_path.stat()
            checkpoint_stat = checkpoint_dir.stat()
            checkpoint_temporary_stat = checkpoint_temporary.stat()
            checkpoint_temporary.unlink()
            trace_root = data_root / "strace"
            trace_root.mkdir()
            trace = "\n".join((
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = "
                "3<{}>".format(json.dumps(str(journal_path)), journal_path),
                "openat(AT_FDCWD, {}, O_RDONLY) = 4<{}>".format(
                    json.dumps(str(source_path)), source_path
                ),
                "fstat(4<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0".format(
                    source_path,
                    expected["source_identity"]["device"],
                    expected["source_identity"]["inode"],
                    expected["source_identity"]["size"],
                ),
                "read(4<{}>, \"...\", {}) = {}".format(
                    source_path, expected["size"], expected["size"]
                ),
                "close(4<{}>) = 0".format(source_path),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "5<{}>".format(json.dumps(str(staging_path)), staging_path),
                "fstat(5<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0".format(
                    staging_path, staging_stat.st_dev,
                    staging_stat.st_ino, staging_stat.st_size,
                ),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "6<{}>".format(json.dumps(str(originals_path)), originals_path),
                "fstat(6<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0".format(
                    originals_path, originals_stat.st_dev,
                    originals_stat.st_ino, originals_stat.st_size,
                ),
                "renameat2(5<{}>, \"file.tmp\", 6<{}>, "
                "\"original.png\", RENAME_NOREPLACE) = 0".format(
                    staging_path, originals_path,
                ),
                "fsync(5<{}/static/media/.staging>) = 0".format(data_root),
                "fsync(6<{}/static/media/originals>) = 0".format(data_root),
                "write(3<{}/linear-migration-v1.jsonl>, {}, {}) = {}".format(
                    data_root,
                    json.dumps(frame.decode("latin1")),
                    len(frame),
                    len(frame),
                ),
                "fsync(3<{}/linear-migration-v1.jsonl>) = 0".format(
                    data_root
                ),
                "write(3<{}/linear-migration-v1.jsonl>, {}, {}) = {}".format(
                    data_root,
                    json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame),
                    len(phase_frame),
                ),
                "fsync(3<{}/linear-migration-v1.jsonl>) = 0".format(
                    data_root
                ),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "7<{}>".format(
                    json.dumps(str(checkpoint_dir)), checkpoint_dir
                ),
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0".format(
                    checkpoint_dir, checkpoint_stat.st_dev,
                    checkpoint_stat.st_ino, checkpoint_stat.st_size,
                ),
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>".format(
                    checkpoint_dir,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary,
                ),
                "fstat(8<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0"
                .format(
                    checkpoint_temporary,
                    checkpoint_temporary_stat.st_dev,
                    checkpoint_temporary_stat.st_ino,
                    checkpoint_temporary_stat.st_size,
                ),
                "fsync(8<{}>) = 0".format(checkpoint_temporary),
                "renameat(7<{}>, {}, 7<{}>, {}) = 0".format(
                    checkpoint_dir,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_dir,
                    json.dumps(checkpoint_file.name),
                ),
                "fsync(7<{}>) = 0".format(checkpoint_dir),
                "openat(AT_FDCWD, {}, O_RDONLY) = 9<{}>".format(
                    json.dumps(str(batch_path)), batch_path
                ),
                "syncfs(9<{}>) = 0".format(batch_path),
                "close(3<{}>) = 0".format(journal_path),
                "",
            ))
            (trace_root / "io.1").write_text(trace, encoding="utf-8")
            output_path = data_root / "io-audit.json"

            completed = self.run_fixture(
                "audit-linear-io",
                "--data-root",
                str(data_root),
                "--output",
                str(output_path),
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            audit = json.loads(output_path.read_text("ascii"))
            self.assertEqual(audit["journal_intent_fsyncs"], 1)
            self.assertGreater(audit["max_journal_event_bytes"], 65535)
            self.assertEqual(
                audit["source_read_bytes"],
                {expected["file_key"]: expected["size"]},
            )
            self.assertEqual(
                audit["source_read_passes"], {expected["file_key"]: 1}
            )
            self.assertEqual(audit["publication_directory_fsyncs"], 2)
            expected_identities = sorted((
                [staging_stat.st_dev, staging_stat.st_ino],
                [originals_stat.st_dev, originals_stat.st_ino],
            ))
            self.assertEqual(
                audit["publication_mutation_windows"],
                [{
                    "event": "batch_intent",
                    "mutated_directory_identities": expected_identities,
                    "fsynced_directory_identities": expected_identities,
                }],
            )
            self.assertEqual(audit["checkpoint_file_fsyncs"], 1)
            self.assertEqual(audit["checkpoint_directory_fsyncs"], 1)
            self.assertEqual(
                audit["checkpoint_events"], ["phase_complete:paths"]
            )
            self.assertEqual(audit["batch_file_syncfs"], 1)

    def test_audit_linear_io_rejects_two_journal_events_per_fsync(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            frames = (
                self.linear_journal_frame({
                    "event": "batch_intent", "batch_id": "paths:1-50",
                })
                + self.linear_journal_frame({
                    "event": "batch_commit", "batch_id": "paths:1-50",
                })
            )
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "close(3<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal, journal,
                    json.dumps(frames.decode("latin1")),
                    len(frames), len(frames), journal, journal,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_journal_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_journal_fsync_without_truncate(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            observation = self.write_tail_repair_observation(data_root)
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_RDWR) = 3<{}>\n"
                "fstat(3<{}>, {{st_dev=10, st_ino=111111, "
                "st_size=1200}}) = 0\n"
                "fsync(3<{}>) = 0\n"
                "close(3<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal, journal,
                    journal, journal,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--mode", "tail-repair",
                "--trace-root", str(trace_root),
                "--observation", str(observation),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_journal_fsync_mismatch",
            )

    def test_audit_linear_io_accepts_truncate_before_tail_repair_fsync(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            observation = self.write_tail_repair_observation(data_root)
            trace_root = data_root / "strace-tail-resume"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_RDWR) = 3<{}>\n"
                "fstat(3<{}>, {{st_dev=10, st_ino=111111, "
                "st_size=1200}}) = 0\n"
                "ftruncate(3<{}>, 1024) = 0\n"
                "fsync(3<{}>) = 0\n"
                "close(3<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal, journal, journal,
                    journal, journal,
                ),
                encoding="utf-8",
            )
            output = data_root / "audit.json"

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--mode", "tail-repair",
                "--trace-root", str(trace_root),
                "--observation", str(observation),
                "--output", str(output),
            )

            self.assertEqual(
                completed.returncode, 0,
                completed.stderr.decode("utf-8"),
            )
            audit = json.loads(output.read_text("ascii"))
            self.assertEqual(audit["journal_tail_truncate_count"], 1)
            self.assertEqual(audit["journal_tail_truncate_offset"], 1024)
            self.assertEqual(audit["journal_tail_repair_fsyncs"], 1)

    def test_audit_linear_io_rejects_inexact_tail_truncate_offsets(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            observation = self.write_tail_repair_observation(data_root)
            trace_root = data_root / "strace"
            trace_root.mkdir()
            for offset in (0, 1200, 900, 1025):
                with self.subTest(offset=offset):
                    (trace_root / "io.1").write_text(
                        "openat(AT_FDCWD, {}, O_RDWR) = 3<{}>\n"
                        "fstat(3<{}>, {{st_dev=10, st_ino=111111, "
                        "st_size=1200}}) = 0\n"
                        "ftruncate(3<{}>, {}) = 0\n"
                        "fsync(3<{}>) = 0\n".format(
                            json.dumps(str(journal)), journal, journal,
                            journal, offset, journal,
                        ),
                        encoding="utf-8",
                    )
                    completed = self.run_fixture(
                        "audit-linear-io", "--data-root", str(data_root),
                        "--mode", "tail-repair",
                        "--observation", str(observation),
                        "--output", str(data_root / "audit.json"),
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(
                        completed.stderr.decode("utf-8").strip(),
                        "FIXTURE_ERROR:linear_strace_journal_fsync_mismatch",
                    )

    def test_audit_linear_io_binds_tail_repair_to_observed_journal(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            observation = self.write_tail_repair_observation(data_root)
            trace_root = data_root / "strace"
            trace_root.mkdir()
            for device, inode, size in (
                (11, 111111, 1200),
                (10, 222222, 1200),
                (10, 111111, 1199),
            ):
                with self.subTest(device=device, inode=inode, size=size):
                    (trace_root / "io.1").write_text(
                        "openat(AT_FDCWD, {}, O_RDWR) = 3<{}>\n"
                        "fstat(3<{}>, {{st_dev={}, st_ino={}, "
                        "st_size={}}}) = 0\n"
                        "ftruncate(3<{}>, 1024) = 0\n"
                        "fsync(3<{}>) = 0\n".format(
                            json.dumps(str(journal)), journal, journal,
                            device, inode, size, journal, journal,
                        ),
                        encoding="utf-8",
                    )
                    completed = self.run_fixture(
                        "audit-linear-io", "--data-root", str(data_root),
                        "--mode", "tail-repair",
                        "--observation", str(observation),
                        "--output", str(data_root / "audit.json"),
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(
                        completed.stderr.decode("utf-8").strip(),
                        "FIXTURE_ERROR:linear_strace_journal_fsync_mismatch",
                    )

    def test_audit_linear_io_rejects_write_after_tail_truncate(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            observation = self.write_tail_repair_observation(data_root)
            frame = self.linear_journal_frame({
                "event": "batch_commit", "batch_id": "paths:1-50",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_RDWR) = 3<{}>\n"
                "fstat(3<{}>, {{st_dev=10, st_ino=111111, "
                "st_size=1200}}) = 0\n"
                "ftruncate(3<{}>, 1024) = 0\n"
                "write(3<{}>, {}, {}) = {}\n".format(
                    json.dumps(str(journal)), journal, journal, journal,
                    journal,
                    json.dumps(frame.decode("latin1")),
                    len(frame), len(frame),
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--mode", "tail-repair",
                "--observation", str(observation),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_journal_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_unbound_checkpoint_directory_fsync(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            other_directory = data_root / "legacy-backup/other"
            run_directory.mkdir(parents=True)
            other_directory.mkdir()
            checkpoint_temporary = (
                run_directory
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            run_stat = run_directory.stat()
            other_stat = other_directory.stat()
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>\n"
                "fstat(8<{}>, {{st_dev=10, st_ino=111111, st_size=7}}) "
                "= 0\n"
                "fsync(8<{}>) = 0\n"
                "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 9<{}>\n"
                "fstat(9<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "fsync(9<{}>) = 0\n"
                "close(3<{}>) = 0\n"
                "close(7<{}>) = 0\n"
                "close(8<{}>) = 0\n"
                "close(9<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal,
                    journal, json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size,
                    run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary,
                    checkpoint_temporary,
                    checkpoint_temporary,
                    run_directory,
                    json.dumps(checkpoint_temporary.name),
                    run_directory, json.dumps(checkpoint.name),
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, other_stat.st_dev, other_stat.st_ino,
                    other_stat.st_size,
                    run_directory, journal, run_directory,
                    checkpoint_temporary, run_directory,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_checkpoint_recreated_after_fsync(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            run_directory.mkdir(parents=True)
            run_stat = run_directory.stat()
            checkpoint_temporary = (
                run_directory
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>\n"
                "fstat(8<{}>, {{st_dev=10, st_ino=111111, st_size=7}}) "
                "= 0\n"
                "fsync(8<{}>) = 0\n"
                "unlinkat(7<{}>, {}, 0) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "9<{}>\n"
                "fstat(9<{}>, {{st_dev=10, st_ino=222222, st_size=7}}) "
                "= 0\n"
                "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                "fsync(7<{}>) = 0\n"
                "close(3<{}>) = 0\n"
                "close(8<{}>) = 0\n"
                "close(9<{}>) = 0\n"
                "close(7<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal,
                    journal, json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size, run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary, checkpoint_temporary,
                    checkpoint_temporary, run_directory,
                    json.dumps(checkpoint_temporary.name), run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary, checkpoint_temporary,
                    run_directory, json.dumps(checkpoint_temporary.name),
                    run_directory, json.dumps(checkpoint.name),
                    run_directory, journal, checkpoint_temporary,
                    checkpoint_temporary, run_directory,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_checkpoint_write_after_fsync(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            run_directory.mkdir(parents=True)
            run_stat = run_directory.stat()
            checkpoint_temporary = (
                run_directory
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>\n"
                "fstat(8<{}>, {{st_dev=10, st_ino=111111, st_size=7}}) "
                "= 0\n"
                "fsync(8<{}>) = 0\n"
                "write(8<{}>, \"x\", 1) = 1\n"
                "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                "fsync(7<{}>) = 0\n"
                "close(3<{}>) = 0\n"
                "close(8<{}>) = 0\n"
                "close(7<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal,
                    journal, json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size, run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary, checkpoint_temporary,
                    checkpoint_temporary, checkpoint_temporary,
                    run_directory, json.dumps(checkpoint_temporary.name),
                    run_directory, json.dumps(checkpoint.name),
                    run_directory, journal, checkpoint_temporary,
                    run_directory,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_checkpoint_write_after_rename(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            run_directory.mkdir(parents=True)
            run_stat = run_directory.stat()
            checkpoint_temporary = (
                run_directory
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>\n"
                "fstat(8<{}>, {{st_dev=10, st_ino=111111, st_size=7}}) "
                "= 0\n"
                "fsync(8<{}>) = 0\n"
                "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                "write(8<{}>, \"x\", 1) = 1\n"
                "fsync(7<{}>) = 0\n"
                "close(3<{}>) = 0\n"
                "close(8<{}>) = 0\n"
                "close(7<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal,
                    journal, json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size, run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary, checkpoint_temporary,
                    checkpoint_temporary,
                    run_directory, json.dumps(checkpoint_temporary.name),
                    run_directory, json.dumps(checkpoint.name),
                    checkpoint_temporary, run_directory, journal,
                    checkpoint_temporary, run_directory,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_checkpoint_write_after_directory_fsync(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            run_directory.mkdir(parents=True)
            run_stat = run_directory.stat()
            checkpoint_temporary = (
                run_directory
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>\n"
                "fstat(8<{}>, {{st_dev=10, st_ino=111111, st_size=7}}) "
                "= 0\n"
                "fsync(8<{}>) = 0\n"
                "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                "fsync(7<{}>) = 0\n"
                "write(8<{}>, \"x\", 1) = 1\n"
                "close(3<{}>) = 0\n"
                "close(8<{}>) = 0\n"
                "close(7<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal,
                    journal, json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size, run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary, checkpoint_temporary,
                    checkpoint_temporary,
                    run_directory, json.dumps(checkpoint_temporary.name),
                    run_directory, json.dumps(checkpoint.name),
                    run_directory, checkpoint_temporary, journal,
                    checkpoint_temporary, run_directory,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_final_checkpoint_mutation_after_fsync(
        self,
    ):
        for mutation_kind in ("truncate", "unlink", "rename"):
            with self.subTest(mutation_kind=mutation_kind):
                with tempfile.TemporaryDirectory(
                    dir="/private/tmp"
                ) as temporary:
                    data_root = Path(temporary, "data")
                    receipt_path = data_root / "linear-receipt.json"
                    created = self.run_fixture(
                        "create-linear", "--data-root", str(data_root),
                        "--images", "50", "--thumbnails", "3",
                        "--receipt", str(receipt_path),
                    )
                    self.assertEqual(created.returncode, 0)
                    run_directory = data_root / "legacy-backup/run-1"
                    run_directory.mkdir(parents=True)
                    run_stat = run_directory.stat()
                    checkpoint_temporary = (
                        run_directory
                        / ".linear-migration-checkpoint-v1.json."
                        "0123456789abcdef0123456789abcdef.tmp"
                    )
                    checkpoint = (
                        run_directory
                        / "linear-migration-checkpoint-v1.json"
                    )
                    journal = data_root / "linear-migration-v1.jsonl"
                    phase_frame = self.linear_journal_frame({
                        "event": "phase_complete", "phase": "paths",
                    })
                    if mutation_kind == "truncate":
                        mutation = (
                            "openat(AT_FDCWD, {}, O_WRONLY|O_TRUNC) = "
                            "9<{}>\nclose(9<{}>) = 0\n"
                        ).format(
                            json.dumps(str(checkpoint)), checkpoint,
                            checkpoint,
                        )
                    elif mutation_kind == "unlink":
                        mutation = "unlinkat(7<{}>, {}, 0) = 0\n".format(
                            run_directory, json.dumps(checkpoint.name),
                        )
                    else:
                        mutation = (
                            "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                        ).format(
                            run_directory, json.dumps(checkpoint.name),
                            run_directory,
                            json.dumps("moved-checkpoint.json"),
                        )
                    trace_root = data_root / "strace"
                    trace_root.mkdir()
                    (trace_root / "io.1").write_text(
                        "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = "
                        "3<{}>\n"
                        "write(3<{}>, {}, {}) = {}\n"
                        "fsync(3<{}>) = 0\n"
                        "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                        "7<{}>\n"
                        "fstat(7<{}>, {{st_dev={}, st_ino={}, "
                        "st_size={}}}) = 0\n"
                        "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, "
                        "0600) = 8<{}>\n"
                        "fstat(8<{}>, {{st_dev=10, st_ino=111111, "
                        "st_size=7}}) = 0\n"
                        "fsync(8<{}>) = 0\n"
                        "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                        "fsync(7<{}>) = 0\n"
                        "close(8<{}>) = 0\n"
                        "{}"
                        "close(3<{}>) = 0\n"
                        "close(7<{}>) = 0\n".format(
                            json.dumps(str(journal)), journal,
                            journal,
                            json.dumps(phase_frame.decode("latin1")),
                            len(phase_frame), len(phase_frame), journal,
                            json.dumps(str(run_directory)), run_directory,
                            run_directory, run_stat.st_dev,
                            run_stat.st_ino, run_stat.st_size,
                            run_directory,
                            json.dumps(checkpoint_temporary.name),
                            checkpoint_temporary, checkpoint_temporary,
                            checkpoint_temporary,
                            run_directory,
                            json.dumps(checkpoint_temporary.name),
                            run_directory, json.dumps(checkpoint.name),
                            run_directory, checkpoint_temporary,
                            mutation, journal, run_directory,
                        ),
                        encoding="utf-8",
                    )

                    completed = self.run_fixture(
                        "audit-linear-io", "--data-root", str(data_root),
                        "--output", str(data_root / "audit.json"),
                    )

                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(
                        completed.stderr.decode("utf-8").strip(),
                        "FIXTURE_ERROR:"
                        "linear_strace_checkpoint_fsync_mismatch",
                    )

    def test_audit_linear_io_accepts_repeated_atomic_checkpoint_replace(
        self,
    ):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            run_directory.mkdir(parents=True)
            run_stat = run_directory.stat()
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frames = [
                self.linear_journal_frame({
                    "event": "phase_complete", "phase": phase,
                })
                for phase in ("paths", "backfill")
            ]
            temporary_names = (
                ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp",
                ".linear-migration-checkpoint-v1.json."
                "fedcba9876543210fedcba9876543210.tmp",
            )
            checkpoint_traces = []
            for index, temporary_name in enumerate(temporary_names):
                descriptor = 8 + index
                temporary_path = run_directory / temporary_name
                checkpoint_traces.append(
                    "write(3<{}>, {}, {}) = {}\n"
                    "fsync(3<{}>) = 0\n"
                    "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                    "{}<{}>\n"
                    "fstat({}<{}>, {{st_dev=10, st_ino={}, st_size=7}}) "
                    "= 0\n"
                    "fsync({}<{}>) = 0\n"
                    "renameat(7<{}>, {}, 7<{}>, {}) = 0\n"
                    "fsync(7<{}>) = 0\n"
                    "close({}<{}>) = 0\n".format(
                        journal,
                        json.dumps(
                            phase_frames[index].decode("latin1")
                        ),
                        len(phase_frames[index]),
                        len(phase_frames[index]), journal,
                        run_directory, json.dumps(temporary_name),
                        descriptor, temporary_path,
                        descriptor, temporary_path, 111111 + index,
                        descriptor, temporary_path,
                        run_directory, json.dumps(temporary_name),
                        run_directory, json.dumps(checkpoint.name),
                        run_directory, descriptor, temporary_path,
                    )
                )
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "{}"
                "{}"
                "close(3<{}>) = 0\n"
                "close(7<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size, checkpoint_traces[0],
                    checkpoint_traces[1], journal, run_directory,
                ),
                encoding="utf-8",
            )
            output_path = data_root / "audit.json"

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(output_path),
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            audit = json.loads(output_path.read_text("ascii"))
            self.assertEqual(audit["checkpoint_file_fsyncs"], 2)
            self.assertEqual(audit["checkpoint_directory_fsyncs"], 2)
            self.assertEqual(
                audit["checkpoint_events"],
                ["phase_complete:paths", "phase_complete:backfill"],
            )

    def test_audit_linear_io_rejects_checkpoint_namespace_mutation(self):
        for mutation_kind in ("unlink", "rename"):
            with self.subTest(mutation_kind=mutation_kind):
                with tempfile.TemporaryDirectory(
                    dir="/private/tmp"
                ) as temporary:
                    data_root = Path(temporary, "data")
                    receipt_path = data_root / "linear-receipt.json"
                    created = self.run_fixture(
                        "create-linear", "--data-root", str(data_root),
                        "--images", "50", "--thumbnails", "3",
                        "--receipt", str(receipt_path),
                    )
                    self.assertEqual(created.returncode, 0)
                    run_directory = data_root / "legacy-backup/run-1"
                    run_directory.mkdir(parents=True)
                    run_stat = run_directory.stat()
                    checkpoint_temporary = (
                        run_directory
                        / ".linear-migration-checkpoint-v1.json."
                        "0123456789abcdef0123456789abcdef.tmp"
                    )
                    checkpoint = (
                        run_directory
                        / "linear-migration-checkpoint-v1.json"
                    )
                    journal = data_root / "linear-migration-v1.jsonl"
                    phase_frame = self.linear_journal_frame({
                        "event": "phase_complete", "phase": "paths",
                    })
                    if mutation_kind == "unlink":
                        mutation = (
                            "unlinkat(7<{}>, {}, 0) = 0".format(
                                run_directory,
                                json.dumps(checkpoint.name),
                            )
                        )
                    else:
                        mutation = (
                            "renameat(7<{}>, {}, 7<{}>, {}) = 0".format(
                                run_directory,
                                json.dumps(checkpoint.name),
                                run_directory,
                                json.dumps("moved-checkpoint.json"),
                            )
                        )
                    trace_lines = (
                        "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) "
                        "= 3<{}>".format(
                            json.dumps(str(journal)), journal
                        ),
                        "write(3<{}>, {}, {}) = {}".format(
                            journal,
                            json.dumps(phase_frame.decode("latin1")),
                            len(phase_frame),
                            len(phase_frame),
                        ),
                        "fsync(3<{}>) = 0".format(journal),
                        "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) "
                        "= 7<{}>".format(
                            json.dumps(str(run_directory)), run_directory
                        ),
                        "fstat(7<{}>, {{st_dev={}, st_ino={}, "
                        "st_size={}}}) = 0".format(
                            run_directory, run_stat.st_dev,
                            run_stat.st_ino, run_stat.st_size,
                        ),
                        "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, "
                        "0600) = 8<{}>".format(
                            run_directory,
                            json.dumps(checkpoint_temporary.name),
                            checkpoint_temporary,
                        ),
                        "fstat(8<{}>, {{st_dev=10, st_ino=111111, "
                        "st_size=7}}) = 0".format(
                            checkpoint_temporary
                        ),
                        "fsync(8<{}>) = 0".format(checkpoint_temporary),
                        "renameat(7<{}>, {}, 7<{}>, {}) = 0".format(
                            run_directory,
                            json.dumps(checkpoint_temporary.name),
                            run_directory,
                            json.dumps(checkpoint.name),
                        ),
                        mutation,
                        "fsync(7<{}>) = 0".format(run_directory),
                        "close(3<{}>) = 0".format(journal),
                        "close(8<{}>) = 0".format(
                            checkpoint_temporary
                        ),
                        "close(7<{}>) = 0".format(run_directory),
                    )
                    trace_root = data_root / "strace"
                    trace_root.mkdir()
                    (trace_root / "io.1").write_text(
                        "\n".join(trace_lines) + "\n", encoding="utf-8"
                    )

                    completed = self.run_fixture(
                        "audit-linear-io", "--data-root", str(data_root),
                        "--output", str(data_root / "audit.json"),
                    )

                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(
                        completed.stderr.decode("utf-8").strip(),
                        "FIXTURE_ERROR:"
                        "linear_strace_checkpoint_fsync_mismatch",
                    )

    def test_audit_linear_io_rejects_phase_without_checkpoint(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "close(3<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal, journal,
                    json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal, journal,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_checkpoint_renameat2_flags(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            run_directory = data_root / "legacy-backup/run-1"
            run_directory.mkdir(parents=True)
            run_stat = run_directory.stat()
            checkpoint_temporary = (
                run_directory
                / ".linear-migration-checkpoint-v1.json."
                "0123456789abcdef0123456789abcdef.tmp"
            )
            checkpoint = (
                run_directory / "linear-migration-checkpoint-v1.json"
            )
            journal = data_root / "linear-migration-v1.jsonl"
            phase_frame = self.linear_journal_frame({
                "event": "phase_complete", "phase": "paths",
            })
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "write(3<{}>, {}, {}) = {}\n"
                "fsync(3<{}>) = 0\n"
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 7<{}>\n"
                "fstat(7<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "openat(7<{}>, {}, O_WRONLY|O_CREAT|O_EXCL, 0600) = "
                "8<{}>\n"
                "fstat(8<{}>, {{st_dev=10, st_ino=111111, st_size=7}}) "
                "= 0\n"
                "fsync(8<{}>) = 0\n"
                "renameat2(7<{}>, {}, 7<{}>, {}, RENAME_EXCHANGE) = 0\n"
                "close(3<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal, journal,
                    json.dumps(phase_frame.decode("latin1")),
                    len(phase_frame), len(phase_frame), journal,
                    json.dumps(str(run_directory)), run_directory,
                    run_directory, run_stat.st_dev, run_stat.st_ino,
                    run_stat.st_size, run_directory,
                    json.dumps(checkpoint_temporary.name),
                    checkpoint_temporary, checkpoint_temporary,
                    checkpoint_temporary,
                    run_directory, json.dumps(checkpoint_temporary.name),
                    run_directory, json.dumps(checkpoint.name), journal,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_checkpoint_fsync_mismatch",
            )

    def test_audit_linear_io_rejects_regular_media_file_fsync(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear",
                "--data-root",
                str(data_root),
                "--images",
                "50",
                "--thumbnails",
                "3",
                "--receipt",
                str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            media_file = (
                data_root / "static/media/originals/asset/original.png"
            )
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = "
                "5<{}>\nfsync(5<{}>) = 0\nclose(5<{}>) = 0\n".format(
                    json.dumps(str(media_file)),
                    media_file,
                    media_file,
                    media_file,
                ),
                encoding="utf-8",
            )

            completed = self.run_fixture(
                "audit-linear-io",
                "--data-root",
                str(data_root),
                "--output",
                str(data_root / "io-audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_strace_media_file_fsync",
            )

    def test_audit_linear_io_rejects_outside_source_suffix_collision(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            item = json.loads(receipt_path.read_text("ascii"))[
                "expected_files"
            ][0]
            outside = Path(temporary, "outside", item["relative_path"])
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_RDONLY) = 4<{}>\n"
                "read(4<{}>, \"x\", 1) = 1\n".format(
                    json.dumps(str(outside)), outside, outside
                ), encoding="utf-8",
            )
            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode().strip(),
                "FIXTURE_ERROR:linear_strace_source_invalid",
            )

    def test_audit_linear_io_accepts_traced_identity_after_source_moved(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            item = json.loads(receipt_path.read_text("ascii"))[
                "expected_files"
            ][0]
            source = data_root / "static/media" / item["relative_path"]
            source.unlink()
            identity = item["source_identity"]
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_RDONLY) = 4<{}>\n"
                "fstat(4<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "read(4<{}>, \"x\", 1) = 1\nclose(4<{}>) = 0\n".format(
                    json.dumps(str(source)), source, source,
                    identity["device"], identity["inode"],
                    identity["size"], source, source,
                ), encoding="utf-8",
            )
            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode()
            )

    def test_audit_linear_io_rejects_syncfs_on_non_staging_fd(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            journal = data_root / "linear-migration-v1.jsonl"
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = 3<{}>\n"
                "syncfs(3<{}>) = 0\n".format(
                    json.dumps(str(journal)), journal, journal
                ), encoding="utf-8",
            )
            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode().strip(),
                "FIXTURE_ERROR:linear_strace_syncfs_invalid",
            )

    def test_audit_linear_io_rejects_duplicate_directory_fsync(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            directory = data_root / "static/media/.staging"
            directory.mkdir(parents=True, exist_ok=True)
            directory_stat = directory.stat()
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = 3<{}>\n"
                "fstat(3<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0\n"
                "fsync(3<{}>) = 0\nfsync(3<{}>) = 0\n".format(
                    json.dumps(str(directory)), directory,
                    directory, directory_stat.st_dev,
                    directory_stat.st_ino, directory_stat.st_size,
                    directory, directory,
                ), encoding="utf-8",
            )
            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode().strip(),
                "FIXTURE_ERROR:linear_strace_directory_fsync_duplicate",
            )

    def test_audit_linear_io_rejects_fsync_before_mutation(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            staging = data_root / "static/media/.staging"
            originals = data_root / "static/media/originals"
            staging.mkdir(parents=True, exist_ok=True)
            originals.mkdir(parents=True, exist_ok=True)
            staging_stat = staging.stat()
            originals_stat = originals.stat()
            journal = data_root / "linear-migration-v1.jsonl"
            payload = {"event": "batch_intent", "batch": 1}
            canonical = json.dumps(
                payload, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            frame = json.dumps({
                "checksum": hashlib.sha256(canonical).hexdigest(),
                "payload": payload,
            }, ensure_ascii=True, sort_keys=True,
                separators=(",", ":")).encode("ascii") + b"\n"
            trace_root = data_root / "strace"
            trace_root.mkdir()
            trace = "\n".join((
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = "
                "3<{}>".format(json.dumps(str(journal)), journal),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "5<{}>".format(json.dumps(str(staging)), staging),
                "fstat(5<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0"
                .format(
                    staging, staging_stat.st_dev, staging_stat.st_ino,
                    staging_stat.st_size,
                ),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "6<{}>".format(json.dumps(str(originals)), originals),
                "fstat(6<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0"
                .format(
                    originals, originals_stat.st_dev,
                    originals_stat.st_ino, originals_stat.st_size,
                ),
                "fsync(5<{}>) = 0".format(staging),
                "fsync(6<{}>) = 0".format(originals),
                "renameat2(5<{}>, \"file.tmp\", 6<{}>, "
                "\"original.png\", RENAME_NOREPLACE) = 0".format(
                    staging, originals,
                ),
                "write(3<{}>, {}, {}) = {}".format(
                    journal, json.dumps(frame.decode("latin1")),
                    len(frame), len(frame),
                ),
                "fsync(3<{}>) = 0".format(journal),
                "",
            ))
            (trace_root / "io.1").write_text(trace, encoding="utf-8")

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(data_root / "audit.json"),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode().strip(),
                "FIXTURE_ERROR:linear_strace_mutation_fsync_mismatch",
            )

    def test_audit_linear_io_tracks_all_at_dirfd_combinations(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            staging = data_root / "static/media/.staging"
            originals = data_root / "static/media/originals"
            staging.mkdir(parents=True, exist_ok=True)
            originals.mkdir(parents=True, exist_ok=True)
            staging_stat = staging.stat()
            originals_stat = originals.stat()
            journal = data_root / "linear-migration-v1.jsonl"
            trace_lines = [
                "openat(AT_FDCWD, {}, O_WRONLY|O_CREAT, 0600) = "
                "3<{}>".format(json.dumps(str(journal)), journal),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "5<{}>".format(json.dumps(str(staging)), staging),
                "fstat(5<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0"
                .format(
                    staging, staging_stat.st_dev, staging_stat.st_ino,
                    staging_stat.st_size,
                ),
                "openat(AT_FDCWD, {}, O_RDONLY|O_DIRECTORY) = "
                "6<{}>".format(json.dumps(str(originals)), originals),
                "fstat(6<{}>, {{st_dev={}, st_ino={}, st_size={}}}) = 0"
                .format(
                    originals, originals_stat.st_dev,
                    originals_stat.st_ino, originals_stat.st_size,
                ),
            ]

            def append_window(batch, syscall, synced_directories):
                payload = {"event": "batch_intent", "batch": batch}
                canonical = json.dumps(
                    payload, ensure_ascii=True, sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")
                frame = json.dumps({
                    "checksum": hashlib.sha256(canonical).hexdigest(),
                    "payload": payload,
                }, ensure_ascii=True, sort_keys=True,
                    separators=(",", ":")).encode("ascii") + b"\n"
                trace_lines.append(syscall)
                trace_lines.extend(
                    "fsync({}<{}>) = 0".format(descriptor, directory)
                    for descriptor, directory in synced_directories
                )
                trace_lines.append(
                    "write(3<{}>, {}, {}) = {}".format(
                        journal, json.dumps(frame.decode("latin1")),
                        len(frame), len(frame),
                    )
                )
                trace_lines.append("fsync(3<{}>) = 0".format(journal))

            append_window(
                1,
                "renameat2(AT_FDCWD, {}, 6<{}>, \"a\", "
                "RENAME_NOREPLACE) = 0".format(
                    json.dumps(str(staging / "a")), originals,
                ),
                ((5, staging), (6, originals)),
            )
            append_window(
                2,
                "renameat2(5<{}>, \"b\", AT_FDCWD<{}>, \"b\", "
                "RENAME_NOREPLACE) = 0".format(staging, originals),
                ((5, staging), (6, originals)),
            )
            append_window(
                3,
                "linkat(AT_FDCWD<{}>, \"c\", 6<{}>, \"c\", 0) = 0"
                .format(staging, originals),
                ((6, originals),),
            )
            append_window(
                4,
                "linkat(5<{}>, \"d\", AT_FDCWD, {}, 0) = 0".format(
                    staging, json.dumps(str(originals / "d")),
                ),
                ((6, originals),),
            )
            append_window(
                5,
                "unlinkat(AT_FDCWD<{}>, \"e\", 0) = 0".format(staging),
                ((5, staging),),
            )
            append_window(
                6,
                "unlinkat(5<{}>, \"f\", 0) = 0".format(staging),
                ((5, staging),),
            )
            trace_root = data_root / "strace"
            trace_root.mkdir()
            (trace_root / "io.1").write_text(
                "\n".join(trace_lines) + "\n", encoding="utf-8"
            )
            output = data_root / "audit.json"

            completed = self.run_fixture(
                "audit-linear-io", "--data-root", str(data_root),
                "--output", str(output),
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode()
            )
            audit = json.loads(output.read_text("ascii"))
            staging_identity = [
                staging_stat.st_dev, staging_stat.st_ino,
            ]
            originals_identity = [
                originals_stat.st_dev, originals_stat.st_ino,
            ]
            both = sorted((staging_identity, originals_identity))
            self.assertEqual(
                [
                    window["mutated_directory_identities"]
                    for window in audit["publication_mutation_windows"]
                ],
                [
                    both, both, [originals_identity],
                    [originals_identity], [staging_identity],
                    [staging_identity],
                ],
            )

    def test_audit_linear_io_rejects_rewinds_for_every_seek_mode(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "linear-receipt.json"
            created = self.run_fixture(
                "create-linear", "--data-root", str(data_root),
                "--images", "50", "--thumbnails", "3",
                "--receipt", str(receipt_path),
            )
            self.assertEqual(created.returncode, 0)
            item = json.loads(receipt_path.read_text("ascii"))[
                "expected_files"
            ][0]
            source = data_root / "static/media" / item["relative_path"]
            identity = item["source_identity"]
            trace_root = data_root / "strace"
            trace_root.mkdir()
            cases = (
                ("SEEK_SET", 0),
                ("SEEK_CUR", -1),
                ("SEEK_END", -identity["size"]),
            )
            for mode, requested in cases:
                with self.subTest(mode=mode):
                    trace = (
                        "openat(AT_FDCWD, {}, O_RDONLY) = 4<{}>\n"
                        "fstat(4<{}>, {{st_dev={}, st_ino={}, "
                        "st_size={}}}) = 0\n"
                        "read(4<{}>, \"x\", 1) = 1\n"
                        "lseek(4<{}>, {}, {}) = 0\n"
                        "read(4<{}>, \"x\", 1) = 1\n"
                        "close(4<{}>) = 0\n"
                    ).format(
                        json.dumps(str(source)), source, source,
                        identity["device"], identity["inode"],
                        identity["size"], source, source, requested,
                        mode, source, source,
                    )
                    (trace_root / "io.1").write_text(
                        trace, encoding="utf-8"
                    )
                    completed = self.run_fixture(
                        "audit-linear-io", "--data-root", str(data_root),
                        "--output", str(data_root / "audit.json"),
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(
                        completed.stderr.decode().strip(),
                        "FIXTURE_ERROR:linear_strace_source_reread",
                    )

    def test_record_linear_commit_captures_physical_prefix_without_path(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            intent = self.linear_journal_frame({
                "event": "batch_intent",
                "batch_id": "batch-1",
            })
            commit = self.linear_journal_frame({
                "event": "batch_commit",
                "batch_id": "batch-1",
            })
            journal_path, observation_path = self.record_first_linear_commit(
                data_root, intent + commit
            )

            observation = json.loads(observation_path.read_text("ascii"))
            journal_stat = journal_path.stat()
            self.assertEqual(observation["schema_version"], 2)
            self.assertEqual(
                observation["journal_identity"],
                {
                    "device": journal_stat.st_dev,
                    "inode": journal_stat.st_ino,
                },
            )
            self.assertEqual(
                observation["journal_prefix_size"], len(intent + commit)
            )
            self.assertEqual(
                observation["journal_prefix_sha256"],
                hashlib.sha256(intent + commit).hexdigest(),
            )
            self.assertNotIn("journal", observation)
            self.assertEqual(observation_path.stat().st_mode & 0o777, 0o600)

    def test_verify_linear_commit_rejects_in_place_prefix_rewrite(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            intent_payload = {
                "event": "batch_intent",
                "batch_id": "batch-1",
            }
            commit_payload = {
                "event": "batch_commit",
                "batch_id": "batch-1",
            }
            original = (
                self.linear_journal_frame(intent_payload)
                + self.linear_journal_frame(commit_payload)
            )
            journal_path, observation_path = self.record_first_linear_commit(
                data_root, original
            )
            original_inode = journal_path.stat().st_ino
            rewritten = (
                self.linear_journal_frame(intent_payload, compact=False)
                + self.linear_journal_frame(commit_payload, compact=False)
            )
            with journal_path.open("r+b") as journal_file:
                journal_file.write(rewritten)
                journal_file.truncate()
            self.assertEqual(journal_path.stat().st_ino, original_inode)

            completed = self.run_fixture(
                "verify-linear-commit",
                "--data-root", str(data_root),
                "--input", str(observation_path),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_commit_rewritten",
            )

    def test_verify_linear_commit_rejects_replaced_journal_inode(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            journal_payload = self.linear_journal_frame({
                "event": "batch_commit",
                "batch_id": "batch-1",
            })
            journal_path, observation_path = self.record_first_linear_commit(
                data_root, journal_payload
            )
            original_inode = journal_path.stat().st_ino
            replacement_path = data_root / "replacement.tmp"
            replacement_path.write_bytes(journal_payload)
            replacement_path.replace(journal_path)
            self.assertNotEqual(journal_path.stat().st_ino, original_inode)

            completed = self.run_fixture(
                "verify-linear-commit",
                "--data-root", str(data_root),
                "--input", str(observation_path),
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(
                completed.stderr.decode("utf-8").strip(),
                "FIXTURE_ERROR:linear_commit_rewritten",
            )

    def test_repair_commit_order_rejects_appended_commit(self):
        fixture = self.load_fixture_module()
        events = [
            {"event": "batch_commit", "batch_id": "paths:1-50"},
            {"event": "batch_repair", "batch_id": "paths:1-50"},
            {"event": "batch_commit", "batch_id": "paths:51-100"},
        ]

        self.assertFalse(fixture._commit_order_matches(
            ["paths:1-50"], events
        ))

    def test_checksum_failure_requires_exact_error_and_unchanged_journal(
        self,
    ):
        fixture = self.load_fixture_module()
        from django.conf import settings
        from django.db import connections

        original_settings_name = settings.DATABASES["default"]["NAME"]
        original_connection_name = connections["default"].settings_dict[
            "NAME"
        ]

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            connection, _database_path, _media_root = (
                fixture._configure_django(temporary)
            )
            self.addCleanup(connection.close)
            self.addCleanup(
                settings.DATABASES["default"].__setitem__,
                "NAME",
                original_settings_name,
            )
            self.addCleanup(
                connections["default"].settings_dict.__setitem__,
                "NAME",
                original_connection_name,
            )
            from django_images.services.legacy_startup import (
                LegacyStartupError,
            )
            from django_images.services.migration_batch_log import (
                MigrationBatchLogError,
            )
            journal = Path(temporary, "linear-migration-v1.jsonl")
            journal.write_bytes(b"corrupt-frame\n")
            expected_raw, expected_stat = fixture._read_regular_file(journal)

            with self.assertRaisesRegex(
                fixture.FixtureError,
                "linear_checksum_failure_invalid",
            ):
                fixture._verify_checksum_failure(
                    RuntimeError("unrelated"), journal,
                    expected_raw, expected_stat,
                )

            inner_error = MigrationBatchLogError(
                "linear_journal_invalid"
            )
            wrapped_error = LegacyStartupError("linear_journal_invalid")
            wrapped_error.__cause__ = inner_error
            fixture._verify_checksum_failure(
                wrapped_error, journal, expected_raw, expected_stat
            )

            journal.write_bytes(b"rewritten\n")
            with self.assertRaisesRegex(
                fixture.FixtureError,
                "linear_checksum_journal_changed",
            ):
                fixture._verify_checksum_failure(
                    wrapped_error,
                    journal, expected_raw, expected_stat,
                )

    def test_benchmark_runs_full_fault_matrix_and_real_startup(self):
        contents = (
            REPOSITORY_ROOT / "docker/tests/linear_migration_benchmark.sh"
        ).read_text("utf-8")
        fixture_contents = FIXTURE_SCRIPT.read_text("utf-8")
        fault_points = (
            "before_destination_rename",
            "after_destination_rename",
            "after_batch_intent",
            "after_database_commit",
            "after_batch_commit",
            "after_archive_rename",
            "journal_intent_partial_write",
            "journal_intent_full_write_before_fsync",
            "journal_commit_partial_write",
            "journal_commit_full_write_before_fsync",
            "journal_repair_partial_write",
            "journal_repair_full_write_before_fsync",
        )
        for fault_point in fault_points:
            self.assertIn(fault_point, contents)
        self.assertIn("exercise-linear-crash", contents)
        self.assertIn(
            "python /pinry/docker/scripts/startup.py --migrate-legacy",
            contents,
        )
        self.assertIn('exit_code="$(docker wait "${name}")"', contents)
        self.assertIn("merge-container-metrics", contents)
        self.assertIn("observe-maintenance-service", contents)
        self.assertIn("/pinry/docker/scripts/start.sh --migrate-legacy", contents)
        self.assertIn('seconds_source == "fixture_coordinator"', contents)
        self.assertIn(
            'heartbeat_source == "nginx_public_status"', contents
        )
        self.assertIn('r350["supervisor_seconds"] <= 2400', contents)
        self.assertIn('r1000["supervisor_seconds"] <= 7200', contents)
        self.assertIn("supervisor_status_samples", contents)
        self.assertIn("batch_sample_count", contents)
        self.assertIn("on_signal", contents)
        self.assertIn("exit 130", contents)
        self.assertIn("supervisor-receipt.json", contents)
        self.assertIn("checksum_invalid", contents)
        self.assertIn("prepare-linear-tail-repair", contents)
        self.assertIn("run-linear-tail-repair-only", contents)
        self.assertIn("--mode tail-repair", contents)
        self.assertIn("strace-tail-resume", contents)
        self.assertIn('"journal_tail_truncate_count"', fixture_contents)
        self.assertIn('"journal_tail_truncate_offset"', fixture_contents)
        self.assertIn('"tail_repairs"', fixture_contents)
        self.assertIn('"journal_prefix_preserved"', fixture_contents)
        self.assertIn(
            'assert result["tail_repairs"] == 0', contents
        )
        self.assertIn('"commit_ordinal_unchanged"', fixture_contents)
        self.assertIn(
            'if fault_point.startswith("journal_repair_"):',
            fixture_contents,
        )
        self.assertIn("_prepare_linear_repair(data_root)", fixture_contents)

    def test_crash_resume_contract_preserves_complete_journal_prefix(self):
        fixture = self.load_fixture_module()
        fault = "journal_intent_full_write_before_fsync"
        prefix = b'{"complete":1}\n'

        with self.assertRaisesRegex(
            fixture.FixtureError, "linear_crash_resume_invalid"
        ):
            fixture._verify_linear_crash_resume_contract(
                fault, prefix, b'{"replacement":1}\n', 0
            )
        with self.assertRaisesRegex(
            fixture.FixtureError, "linear_crash_resume_invalid"
        ):
            fixture._verify_linear_crash_resume_contract(
                fault, prefix, prefix + b'{"next":1}\n', 1
            )

        fixture._verify_linear_crash_resume_contract(
            fault, prefix, prefix + b'{"next":1}\n', 0
        )
        fixture._verify_linear_crash_resume_contract(
            "journal_intent_partial_write",
            prefix,
            prefix + b'{"next":1}\n',
            1,
        )

    def test_smoke_covers_corrupt_manifest_and_status_fallbacks(self):
        contents = (
            REPOSITORY_ROOT
            / "docker/tests/automatic_legacy_migration_smoke.sh"
        ).read_text("utf-8")
        self.assertIn("run_corrupt_manifest_mode", contents)
        self.assertIn("run_status_resilience_mode missing", contents)
        self.assertIn("run_status_resilience_mode corrupt", contents)
        self.assertIn("startup_lock_busy", contents)
        self.assertIn("assert_container_stable", contents)
        self.assertIn("record-linear-commit", contents)
        self.assertIn(
            'exit_code="$(docker wait "${container_name}")"',
            contents,
        )
        self.assertIn('[ "${exit_code}" = 0 ]', contents)
        self.assertIn(
            'http://app migrating 180 1 "" 512 2048', contents
        )
        self.assertIn(
            'http://app migrating 180 "" "" 512 2048', contents
        )

    def test_legacy_md5_fixture_covers_every_root_and_orphan_sentinel(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            data_root = Path(temporary, "data")
            receipt_path = data_root / "fixture-receipt.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(FIXTURE_SCRIPT),
                    "create",
                    "--kind",
                    "legacy-md5",
                    "--data-root",
                    str(data_root),
                    "--count",
                    "3",
                    "--receipt",
                    str(receipt_path),
                ],
                cwd=str(REPOSITORY_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(
                completed.returncode, 0, completed.stderr.decode("utf-8")
            )
            receipt = json.loads(receipt_path.read_text("ascii"))
            fixture = self.load_fixture_module()
            fixture._validate_receipt_value(receipt)
            self.assertTrue(all(
                "prior_image_media_url" not in item
                for item in receipt["items"]
            ))
            expected_roots = list("0123456789abcdef")
            self.assertEqual(receipt["expected_direct_roots"], expected_roots)
            self.assertEqual(
                sorted(receipt["archive_root_identity"]), expected_roots
            )
            referenced_roots = {
                item["legacy_original_path"].split("/", 1)[0]
                for item in receipt["items"]
            }
            referenced_roots.update(
                derivative["legacy_path"].split("/", 1)[0]
                for item in receipt["items"]
                for derivative in item["derivatives"]
            )
            orphan = receipt["orphan_sentinel"]
            orphan_root = orphan["path"].split("/", 1)[0]
            self.assertNotIn(orphan_root, referenced_roots)
            sentinel_path = data_root / "static/media" / orphan["path"]
            self.assertTrue(sentinel_path.is_file())
            self.assertEqual(
                hashlib.sha256(sentinel_path.read_bytes()).hexdigest(),
                orphan["sha256"],
            )
            sentinel_stat = sentinel_path.stat()
            self.assertEqual(
                (sentinel_stat.st_dev, sentinel_stat.st_ino),
                (
                    orphan["source_identity"]["device"],
                    orphan["source_identity"]["inode"],
                ),
            )


if __name__ == "__main__":
    unittest.main()
