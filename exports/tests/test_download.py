from datetime import datetime, timedelta, timezone as datetime_timezone
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import unquote
import uuid

import mock
from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from core.models import Image, MediaAsset, Pin
from core.services.database_fence import DatabaseFenceBusy
from exports.models import ExportBlob, ExportItem, ExportJob
from exports.services import download as download_services

from .helpers import (
    ExportStorageMixin,
    create_export_pin,
    create_export_user,
)


UTC = datetime_timezone.utc
SUCCESS_HEADERS = (
    "X-Accel-Redirect",
    "Cache-Control",
    "Content-Disposition",
)


class ExportDownloadAPITests(ExportStorageMixin, TransactionTestCase):
    def setUp(self):
        super(ExportDownloadAPITests, self).setUp()
        self.client = APIClient()
        self.owner = create_export_user("download-owner")
        self.other = create_export_user("download-other")
        self.now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
        self.ready_directory = Path(self._export_directory.name, "ready")
        self.ready_directory.mkdir(mode=0o700)
        os.chmod(str(self.ready_directory), 0o700)

    def _complete_job(self, content=b"valid zip bytes", **overrides):
        job_id = overrides.pop("id", uuid.uuid4())
        path = self.ready_directory / "{}.zip".format(job_id)
        path.write_bytes(content)
        os.chmod(str(path), 0o600)
        current = path.stat()
        completed_at = overrides.pop(
            "completed_at", self.now - timedelta(hours=1),
        )
        values = {
            "id": job_id,
            "owner": self.owner,
            "scope": "pins",
            "state": "complete",
            "completed_at": completed_at,
            "expires_at": completed_at + timedelta(hours=24),
            "ready_cleanup_state": "retained",
            "ready_relative_path": "ready/{}.zip".format(job_id),
            "ready_display_name": "선택-Pin-내보내기-20260831T110000Z.zip",
            "ready_size": current.st_size,
            "ready_sha256": hashlib.sha256(content).hexdigest(),
            "ready_dev": current.st_dev,
            "ready_ino": current.st_ino,
            "ready_uid": current.st_uid,
            "ready_gid": current.st_gid,
            "ready_mode": 0o600,
            "ready_nlink": current.st_nlink,
            "ready_mtime_ns": current.st_mtime_ns,
            "ready_ctime_ns": current.st_ctime_ns,
        }
        values.update(overrides)
        return ExportJob.objects.create(**values)

    def _get(self, job):
        with mock.patch("exports.views.timezone.now", return_value=self.now):
            return self.client.get(
                "/api/v2/exports/{}/download/".format(job.pk),
            )

    def _get_with_clock(self, job, clock):
        with mock.patch(
            "exports.views.timezone.now",
            side_effect=clock,
        ):
            return self.client.get(
                "/api/v2/exports/{}/download/".format(job.pk),
            )

    @staticmethod
    def _ready_update(path, content):
        current = path.stat()
        return {
            "ready_size": current.st_size,
            "ready_sha256": hashlib.sha256(content).hexdigest(),
            "ready_dev": current.st_dev,
            "ready_ino": current.st_ino,
            "ready_uid": current.st_uid,
            "ready_gid": current.st_gid,
            "ready_mode": 0o600,
            "ready_nlink": current.st_nlink,
            "ready_mtime_ns": current.st_mtime_ns,
            "ready_ctime_ns": current.st_ctime_ns,
        }

    def _attach_item(self, job, pin, position=0, blob=None):
        if blob is None:
            blob = ExportBlob.objects.create(
                job=job,
                snapshot_generation=uuid.uuid4(),
                source_media_asset_id=pin.image.media_asset.pk,
                source_image_id=pin.image_id,
                source_relative_path=pin.image.image.name,
            )
        return ExportItem.objects.create(
            job=job,
            target_position=position,
            snapshot_generation=blob.snapshot_generation,
            blob=blob,
            pin_id=pin.pk,
            pin_owner_id=pin.submitter_id,
            owner_username=pin.submitter.username,
            is_public=not pin.private,
            published_at=pin.published,
            original_filename=pin.image.original_filename,
        )

    @staticmethod
    def _delete_pin_row(pin_id):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_pin WHERE id = %s", [pin_id])

    def _foreign_item_job(self, count, content):
        image = Image.objects.create(
            image="image/original/download-shared.png",
            original_filename="download-shared.png",
            width=1,
            height=1,
        )
        MediaAsset.objects.create(
            submitter=self.other,
            image=image,
            content_sha256="a" * 64,
        )
        for start in range(0, count, 400):
            batch_size = min(400, count - start)
            Pin.objects.bulk_create([
                Pin(submitter=self.other, image=image)
                for unused in range(batch_size)
            ], batch_size=400)
        pin_rows = list(Pin.objects.filter(
            submitter=self.other,
            image=image,
        ).order_by("pk").values_list("pk", "published"))
        self.assertEqual(len(pin_rows), count)
        job = self._complete_job(
            content=content,
            requested_total=count,
            target_total=count,
            snapshot_done=count,
            archive_total=count,
            archive_done=count,
            included_total=count,
        )
        blob = ExportBlob.objects.create(
            job=job,
            snapshot_generation=uuid.uuid4(),
            source_media_asset_id=image.media_asset.pk,
            source_image_id=image.pk,
            source_relative_path=image.image.name,
        )
        for start in range(0, count, 400):
            chunk = pin_rows[start:start + 400]
            ExportItem.objects.bulk_create([
                ExportItem(
                    job=job,
                    target_position=start + offset,
                    snapshot_generation=blob.snapshot_generation,
                    blob=blob,
                    pin_id=pin_id,
                    pin_owner_id=self.other.pk,
                    owner_username=self.other.username,
                    is_public=True,
                    published_at=published_at,
                    original_filename="download-shared.png",
                )
                for offset, (pin_id, published_at) in enumerate(chunk)
            ], batch_size=400)
        return job

    def assert_no_success_headers(self, response):
        for header in SUCCESS_HEADERS:
            self.assertNotIn(header, response)

    def test_owner_download_success_is_empty_uuid_only_internal_redirect(self):
        job = self._complete_job()
        self.client.force_authenticate(self.owner)

        response = self._get(job)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(
            response["X-Accel-Redirect"],
            "/__protected_exports/{}.zip".format(job.pk),
        )
        self.assertNotIn(job.ready_relative_path, response["X-Accel-Redirect"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        disposition = response["Content-Disposition"]
        self.assertIn(
            'attachment; filename="svrx-pinry-export-{}.zip"'.format(
                job.pk,
            ),
            disposition,
        )
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)

    def test_content_disposition_bounds_and_percent_encodes_db_name(self):
        job = self._complete_job()
        suffix = "-내보내기-20260831T110000Z.zip"
        malformed = "보고서\r\n" + ("가" * 100) + suffix
        ExportJob.objects.filter(pk=job.pk).update(
            ready_display_name=malformed,
        )
        self.client.force_authenticate(self.owner)

        response = self._get(job)

        self.assertEqual(response.status_code, 200, response.content)
        disposition = response["Content-Disposition"]
        self.assertIn(
            'filename="svrx-pinry-export-{}.zip"'.format(job.pk),
            disposition,
        )
        encoded = disposition.split("filename*=UTF-8''", 1)[1]
        display_name = unquote(encoded)
        self.assertIn("%0D%0A", encoded)
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertLessEqual(len(display_name.encode("utf-8")), 180)
        self.assertTrue(display_name.endswith(suffix))

    def test_anonymous_is_401_and_other_owner_is_hidden_as_404(self):
        job = self._complete_job()

        anonymous = self._get(job)
        self.client.force_authenticate(self.other)
        hidden = self._get(job)

        self.assertEqual(anonymous.status_code, 401)
        self.assertEqual(hidden.status_code, 404)
        self.assert_no_success_headers(anonymous)
        self.assert_no_success_headers(hidden)

    def test_every_non_complete_state_is_export_not_ready(self):
        self.client.force_authenticate(self.owner)
        cases = (
            ("queued", {}),
            ("snapshotting", {}),
            ("archiving", {}),
            ("verifying", {}),
            (
                "failed",
                {
                    "error_code": "archive_failed",
                    "error_class": "retryable",
                    "error_retryable": True,
                },
            ),
        )

        for state, extra in cases:
            with self.subTest(state=state):
                job = ExportJob.objects.create(
                    owner=self.owner,
                    scope="pins",
                    state=state,
                    **extra
                )
                response = self._get(job)
                self.assertEqual(
                    (response.status_code, response.json()),
                    (409, {"code": "export_not_ready"}),
                )
                self.assert_no_success_headers(response)

    def test_expired_state_is_gone_without_success_headers(self):
        job = self._complete_job()
        ExportJob.objects.filter(pk=job.pk).update(
            state="expired",
            ready_cleanup_state="pending",
        )
        self.client.force_authenticate(self.owner)

        response = self._get(job)

        self.assertEqual(
            (response.status_code, response.json()),
            (410, {"code": "export_expired"}),
        )
        self.assert_no_success_headers(response)

    def test_clock_expiry_commits_logical_pending_before_returning_gone(self):
        job = self._complete_job(expires_at=self.now)
        path = self.ready_directory / "{}.zip".format(job.pk)
        self.client.force_authenticate(self.owner)

        response = self._get(job)

        self.assertEqual(
            (response.status_code, response.json()),
            (410, {"code": "export_expired"}),
        )
        self.assert_no_success_headers(response)
        job.refresh_from_db()
        self.assertEqual(job.state, "expired")
        self.assertEqual(job.ready_cleanup_state, "pending")
        self.assertTrue(path.exists())

    def test_each_physical_receipt_field_mismatch_blocks_without_unlink(self):
        self.client.force_authenticate(self.owner)
        fields = (
            "ready_dev",
            "ready_ino",
            "ready_uid",
            "ready_gid",
            "ready_mode",
            "ready_nlink",
            "ready_size",
            "ready_mtime_ns",
            "ready_ctime_ns",
        )

        for field in fields:
            with self.subTest(field=field):
                content = "receipt-{}".format(field).encode("ascii")
                job = self._complete_job(content=content)
                path = self.ready_directory / "{}.zip".format(job.pk)
                before = path.stat()
                ExportJob.objects.filter(pk=job.pk).update(
                    **{field: getattr(job, field) + 1}
                )

                response = self._get(job)

                self.assertEqual(
                    (response.status_code, response.json()),
                    (410, {"code": "export_expired"}),
                )
                self.assert_no_success_headers(response)
                job.refresh_from_db()
                self.assertEqual(job.state, "expired")
                self.assertEqual(job.ready_cleanup_state, "blocked")
                after = path.stat()
                self.assertEqual((after.st_dev, after.st_ino), (
                    before.st_dev, before.st_ino,
                ))
                self.assertEqual(path.read_bytes(), content)

    def test_missing_file_and_noncanonical_db_path_expire_blocked(self):
        self.client.force_authenticate(self.owner)
        missing = self._complete_job(content=b"missing")
        missing_path = self.ready_directory / "{}.zip".format(missing.pk)
        missing_path.unlink()

        missing_response = self._get(missing)

        self.assertEqual(missing_response.status_code, 410)
        missing.refresh_from_db()
        self.assertEqual(
            (missing.state, missing.ready_cleanup_state),
            ("expired", "blocked"),
        )

        noncanonical = self._complete_job(content=b"canonical remains")
        canonical_path = (
            self.ready_directory / "{}.zip".format(noncanonical.pk)
        )
        ExportJob.objects.filter(pk=noncanonical.pk).update(
            ready_relative_path="ready/not-the-job-uuid.zip",
        )

        path_response = self._get(noncanonical)

        self.assertEqual(path_response.status_code, 410)
        noncanonical.refresh_from_db()
        self.assertEqual(
            (noncanonical.state, noncanonical.ready_cleanup_state),
            ("expired", "blocked"),
        )
        self.assertEqual(canonical_path.read_bytes(), b"canonical remains")

    def test_mismatch_cas_preserves_concurrent_expired_pending(self):
        job = self._complete_job(content=b"worker cleanup race")
        path = self.ready_directory / "{}.zip".format(job.pk)
        path.unlink()
        original = download_services._observe_ready_file

        def expire_before_cas(expectation):
            try:
                return original(expectation)
            except download_services._ReceiptMismatch:
                ExportJob.objects.filter(pk=job.pk).update(
                    state="expired",
                    ready_cleanup_state="pending",
                )
                raise

        self.client.force_authenticate(self.owner)
        with mock.patch.object(
            download_services,
            "_observe_ready_file",
            side_effect=expire_before_cas,
        ):
            response = self._get(job)

        self.assertEqual(response.status_code, 410)
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("expired", "pending"),
        )

    def test_mismatch_cas_restarts_when_receipt_generation_changes(self):
        job = self._complete_job(content=b"same physical file")
        correct_size = job.ready_size
        ExportJob.objects.filter(pk=job.pk).update(
            ready_size=correct_size + 1,
        )
        original = download_services._observe_ready_file
        calls = []

        def repair_generation(expectation):
            calls.append(expectation.ready_values)
            try:
                return original(expectation)
            except download_services._ReceiptMismatch:
                if len(calls) == 1:
                    ExportJob.objects.filter(pk=job.pk).update(
                        ready_size=correct_size,
                    )
                raise

        self.client.force_authenticate(self.owner)
        with mock.patch.object(
            download_services,
            "_observe_ready_file",
            side_effect=repair_generation,
        ):
            response = self._get(job)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(calls), 2)
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("complete", "retained"),
        )

    def test_final_fence_restarts_after_ready_receipt_replacement(self):
        job = self._complete_job(content=b"first generation")
        path = self.ready_directory / "{}.zip".format(job.pk)
        replacement = b"second generation"
        original = download_services._observe_ready_file
        calls = []

        def replace_after_observation(expectation):
            observed = original(expectation)
            calls.append(expectation.ready_values)
            if len(calls) == 1:
                path.unlink()
                path.write_bytes(replacement)
                os.chmod(str(path), 0o600)
                ExportJob.objects.filter(pk=job.pk).update(
                    **self._ready_update(path, replacement)
                )
            return observed

        self.client.force_authenticate(self.owner)
        with mock.patch.object(
            download_services,
            "_observe_ready_file",
            side_effect=replace_after_observation,
        ):
            response = self._get(job)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(calls), 2)
        self.assertEqual(path.read_bytes(), replacement)

    def test_owner_deleted_or_changed_between_read_and_fence_is_404(self):
        self.client.force_authenticate(self.owner)
        for action in ("changed", "deleted"):
            with self.subTest(action=action):
                job = self._complete_job(content=action.encode("ascii"))
                original = download_services._observe_ready_file

                def mutate_owner(expectation):
                    observed = original(expectation)
                    if action == "changed":
                        ExportJob.objects.filter(pk=job.pk).update(
                            owner_id=self.other.pk,
                        )
                    else:
                        self.owner.delete()
                    return observed

                with mock.patch.object(
                    download_services,
                    "_observe_ready_file",
                    side_effect=mutate_owner,
                ):
                    response = self._get(job)
                self.assertEqual(response.status_code, 404)
                self.assert_no_success_headers(response)
                if action == "deleted":
                    self.owner = create_export_user(
                        "download-owner-recreated",
                    )
                    self.client.force_authenticate(self.owner)

    def test_download_does_not_rehash_ready_archive(self):
        job = self._complete_job(content=b"already hashed")
        self.client.force_authenticate(self.owner)

        with mock.patch(
            "hashlib.sha256",
            side_effect=AssertionError("download must not rehash"),
        ):
            response = self._get(job)

        self.assertEqual(response.status_code, 200, response.content)

    def test_all_file_syscalls_finish_before_final_database_fence(self):
        job = self._complete_job(content=b"db only final fence")
        self.client.force_authenticate(self.owner)
        original_observe = download_services._observe_ready_file
        observed_atomic_states = []
        forbidden_atomic_calls = []
        originals = {
            name: getattr(download_services.os, name)
            for name in (
                "open", "fstat", "stat", "unlink", "rename", "replace",
                "fsync",
            )
        }
        patchers = []

        def guarded(name):
            def call(*args, **kwargs):
                if connection.in_atomic_block:
                    forbidden_atomic_calls.append(name)
                return originals[name](*args, **kwargs)
            return call

        def observe_then_guard(expectation):
            observed_atomic_states.append(connection.in_atomic_block)
            observed = original_observe(expectation)
            for name in originals:
                patcher = mock.patch.object(
                    download_services.os,
                    name,
                    new=guarded(name),
                )
                patcher.start()
                patchers.append(patcher)
            return observed

        try:
            with mock.patch.object(
                download_services,
                "_observe_ready_file",
                side_effect=observe_then_guard,
            ):
                response = self._get(job)
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(observed_atomic_states, [False])
        self.assertEqual(forbidden_atomic_calls, [])

    def test_canonical_leaf_is_opened_nonblocking_before_regular_check(self):
        job = self._complete_job(content=b"nonblocking")
        self.client.force_authenticate(self.owner)
        original = download_services.os.open
        original_open_ready = download_services._open_ready_directory
        leaf_flags = []

        def record_open(path, flags, *args, **kwargs):
            if path == "{}.zip".format(job.pk):
                leaf_flags.append(flags)
            return original(path, flags, *args, **kwargs)

        patcher = mock.patch.object(
            download_services.os,
            "open",
            new=record_open,
        )

        def open_ready_then_spy(root):
            ready = original_open_ready(root)
            patcher.start()
            return ready

        try:
            with mock.patch.object(
                download_services,
                "_open_ready_directory",
                side_effect=open_ready_then_spy,
            ):
                response = self._get(job)
        finally:
            patcher.stop()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(leaf_flags), 1)
        self.assertTrue(leaf_flags[0] & os.O_NONBLOCK)

    def test_foreign_private_or_deleted_pin_expires_pending_without_unlink(self):
        self.client.force_authenticate(self.owner)
        for action in ("private", "deleted"):
            with self.subTest(action=action):
                pin = create_export_pin(
                    self.other,
                    filename="revoked-{}.png".format(action),
                )
                job = self._complete_job(
                    content=action.encode("ascii"),
                    requested_total=1,
                    target_total=1,
                    snapshot_done=1,
                    archive_total=1,
                    archive_done=1,
                    included_total=1,
                )
                self._attach_item(job, pin)
                if action == "private":
                    Pin.objects.filter(pk=pin.pk).update(private=True)
                else:
                    self._delete_pin_row(pin.pk)
                path = self.ready_directory / "{}.zip".format(job.pk)

                response = self._get(job)

                self.assertEqual(
                    (response.status_code, response.json()),
                    (410, {"code": "export_expired"}),
                )
                self.assert_no_success_headers(response)
                job.refresh_from_db()
                self.assertEqual(
                    (job.state, job.ready_cleanup_state),
                    ("expired", "pending"),
                )
                self.assertTrue(path.exists())

    def test_permission_change_only_blocks_requests_started_after_change(self):
        pin = create_export_pin(
            self.other,
            filename="stream-boundary.png",
        )
        job = self._complete_job(
            requested_total=1,
            target_total=1,
            snapshot_done=1,
            archive_total=1,
            archive_done=1,
            included_total=1,
        )
        self._attach_item(job, pin)
        self.client.force_authenticate(self.owner)

        approved = self._get(job)
        Pin.objects.filter(pk=pin.pk).update(private=True)
        denied = self._get(job)

        self.assertEqual(approved.status_code, 200, approved.content)
        self.assertIn("X-Accel-Redirect", approved)
        self.assertEqual(denied.status_code, 410)
        self.assert_no_success_headers(denied)

    def test_fifty_thousand_foreign_items_are_scanned_in_400_chunks_before_fresh_decision(self):
        count = 50000
        job = self._foreign_item_job(count, b"fifty thousand")
        boundary = job.expires_at
        clock_values = iter((
            boundary - timedelta(seconds=1),
            boundary + timedelta(seconds=1),
        ))
        events = []

        def clock():
            value = next(clock_values)
            events.append(("clock", value))
            return value

        def observe(execute, sql, params, many, context):
            if "core_pin" in sql:
                widths = [
                    body.count("%s") + body.count("?")
                    for body in re.findall(
                        r"\bIN\s*\(([^()]*)\)", sql, re.I,
                    )
                ]
                widths = [width for width in widths if width]
                events.append(("pin_query", max(widths)))
            if self.owner._meta.db_table in sql and "SELECT" in sql.upper():
                events.append(("owner_query", None))
            return execute(sql, params, many, context)

        self.client.force_authenticate(self.owner)
        with connection.execute_wrapper(observe):
            response = self._get_with_clock(job, clock)

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json(), {"code": "export_expired"})
        pin_events = [event for event in events if event[0] == "pin_query"]
        self.assertEqual(len(pin_events), 125)
        self.assertTrue(all(event[1] <= 400 for event in pin_events))
        clock_indices = [
            index for index, event in enumerate(events)
            if event[0] == "clock"
        ]
        pin_indices = [
            index for index, event in enumerate(events)
            if event[0] == "pin_query"
        ]
        owner_indices = [
            index for index, event in enumerate(events)
            if event[0] == "owner_query"
        ]
        self.assertEqual(len(clock_indices), 2)
        self.assertLess(clock_indices[0], pin_indices[0])
        self.assertGreater(clock_indices[1], pin_indices[-1])
        self.assertGreater(clock_indices[1], owner_indices[-1])
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("expired", "pending"),
        )

    def test_already_expired_fifty_thousand_items_skip_permission_scan(self):
        job = self._foreign_item_job(50000, b"already expired")
        boundary = job.expires_at
        expired_now = boundary + timedelta(seconds=1)
        clock_calls = []
        pin_queries = []

        def clock():
            clock_calls.append(expired_now)
            return expired_now

        def observe(execute, sql, params, many, context):
            if "core_pin" in sql:
                pin_queries.append(sql)
            return execute(sql, params, many, context)

        monotonic_values = iter((0.0, 1.0))
        self.client.force_authenticate(self.owner)
        with connection.execute_wrapper(observe), mock.patch.object(
            download_services,
            "_monotonic",
            side_effect=lambda: next(monotonic_values),
        ):
            response = self._get_with_clock(job, clock)

        self.assertEqual(
            (response.status_code, response.json()),
            (410, {"code": "export_expired"}),
        )
        self.assertEqual(clock_calls, [expired_now])
        self.assertEqual(pin_queries, [])
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("expired", "pending"),
        )

    def test_pre_scan_expiry_deadline_rolls_back_and_returns_503(self):
        boundary = self.now + timedelta(hours=1)
        job = self._complete_job(expires_at=boundary)
        self.client.force_authenticate(self.owner)
        monotonic_values = iter((0.0, 6.0))

        with mock.patch.object(
            download_services,
            "_monotonic",
            side_effect=lambda: next(monotonic_values),
        ):
            response = self._get_with_clock(
                job,
                lambda: boundary + timedelta(seconds=1),
            )

        self.assertEqual(
            (response.status_code, response.json()),
            (
                503,
                {"code": "export_temporarily_unavailable"},
            ),
        )
        self.assert_no_success_headers(response)
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("complete", "retained"),
        )

    def test_one_second_before_expiry_is_still_downloadable(self):
        boundary = self.now + timedelta(hours=1)
        job = self._complete_job(expires_at=boundary)
        clock_values = iter((
            boundary - timedelta(seconds=1),
            boundary - timedelta(seconds=1),
        ))
        self.client.force_authenticate(self.owner)

        response = self._get_with_clock(job, lambda: next(clock_values))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("X-Accel-Redirect", response)

    def test_final_decision_clock_follows_owner_recheck(self):
        boundary = self.now + timedelta(hours=1)
        before = boundary - timedelta(seconds=1)
        after = boundary + timedelta(seconds=1)
        job = self._complete_job(expires_at=boundary)
        wall_time = [before]
        events = []
        owner_query_count = [0]

        def clock():
            events.append(("clock", wall_time[0]))
            return wall_time[0]

        def observe(execute, sql, params, many, context):
            if self.owner._meta.db_table in sql and "SELECT" in sql.upper():
                owner_query_count[0] += 1
                events.append(("owner_query", owner_query_count[0]))
                if owner_query_count[0] == 2:
                    wall_time[0] = after
            return execute(sql, params, many, context)

        self.client.force_authenticate(self.owner)
        with connection.execute_wrapper(observe):
            response = self._get_with_clock(job, clock)

        self.assertEqual(
            (response.status_code, response.json()),
            (410, {"code": "export_expired"}),
        )
        self.assertEqual(owner_query_count, [2])
        self.assertEqual(
            [event for event in events if event[0] == "clock"],
            [("clock", before), ("clock", after)],
        )
        self.assertGreater(
            events.index(("clock", after)),
            events.index(("owner_query", 2)),
        )
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("expired", "pending"),
        )

    def test_busy_commit_retry_uses_fresh_authorization_clock(self):
        boundary = self.now + timedelta(hours=1)
        job = self._complete_job(expires_at=boundary)
        self.client.force_authenticate(self.owner)
        original_fence = download_services.database_write_fence
        fence_attempts = []

        @contextmanager
        def busy_first_commit(*args, **kwargs):
            with original_fence(*args, **kwargs) as database_connection:
                yield database_connection
                fence_attempts.append(True)
                if len(fence_attempts) == 1:
                    raise DatabaseFenceBusy()

        clock_values = iter((
            boundary - timedelta(seconds=1),
            boundary - timedelta(seconds=1),
            boundary + timedelta(seconds=1),
            boundary + timedelta(seconds=1),
        ))
        clock_calls = []

        def clock():
            value = next(clock_values)
            clock_calls.append(value)
            return value

        self.client.raise_request_exception = False
        with mock.patch.object(
            download_services,
            "database_write_fence",
            side_effect=busy_first_commit,
        ):
            response = self._get_with_clock(job, clock)

        self.assertEqual(
            (response.status_code, response.json()),
            (410, {"code": "export_expired"}),
        )
        self.assertEqual(len(fence_attempts), 2)
        self.assertEqual(clock_calls, [
            boundary - timedelta(seconds=1),
            boundary - timedelta(seconds=1),
            boundary + timedelta(seconds=1),
        ])
        job.refresh_from_db()
        self.assertEqual(
            (job.state, job.ready_cleanup_state),
            ("expired", "pending"),
        )

    def test_final_and_mismatch_busy_deadline_are_503_without_mutation(self):
        self.client.force_authenticate(self.owner)

        @contextmanager
        def always_busy(*args, **kwargs):
            del args, kwargs
            raise DatabaseFenceBusy()
            yield  # pragma: no cover

        for stage in ("final", "mismatch"):
            with self.subTest(stage=stage):
                job = self._complete_job(content=stage.encode("ascii"))
                path = self.ready_directory / "{}.zip".format(job.pk)
                if stage == "mismatch":
                    path.unlink()
                self.client.raise_request_exception = False
                monotonic_values = iter((0.0, 6.0))
                with mock.patch.object(
                    download_services,
                    "database_write_fence",
                    side_effect=always_busy,
                ), mock.patch.object(
                    download_services,
                    "_monotonic",
                    side_effect=lambda: next(monotonic_values),
                    create=True,
                ):
                    response = self._get(job)

                self.assertEqual(
                    (response.status_code, response.json()),
                    (
                        503,
                        {"code": "export_temporarily_unavailable"},
                    ),
                )
                self.assert_no_success_headers(response)
                job.refresh_from_db()
                self.assertEqual(
                    (job.state, job.ready_cleanup_state),
                    ("complete", "retained"),
                )
