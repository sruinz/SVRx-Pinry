import json
from contextlib import contextmanager
from datetime import timedelta

import mock
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from exports.models import (
    ExportAttempt,
    ExportAttemptFile,
    ExportJob,
    ExportSlot,
    ExportTarget,
    ExportWorkerLease,
)
from exports.serializers import ExportRequestSerializer
from exports.services.jobs import (
    ExportRequestError,
    JobService,
    WorkerHealthService,
)

from .helpers import (
    ExportStorageMixin,
    create_export_board,
    create_export_pin,
    create_export_user,
)


class ExportRequestSerializerTests(TransactionTestCase):
    def test_strict_scope_xor_type_duplicate_and_limit_contract(self):
        invalid = (
            {},
            {"scope": "pins"},
            {"scope": "board"},
            {"scope": "pins", "pin_ids": [1], "board_id": 1},
            {"scope": "board", "board_id": 1, "pin_ids": [1]},
            {"scope": "pins", "pin_ids": []},
            {"scope": "pins", "pin_ids": [1, 1]},
            {"scope": "pins", "pin_ids": [True]},
            {"scope": "pins", "pin_ids": ["1"]},
            {"scope": "board", "board_id": True},
            {"scope": "board", "board_id": "1"},
            {"scope": "pins", "pin_ids": list(range(1, 50002))},
            {"scope": "pins", "pin_ids": [1], "unknown": 2},
        )

        for payload in invalid:
            with self.subTest(payload_keys=tuple(payload)):
                serializer = ExportRequestSerializer(data=payload)
                self.assertFalse(serializer.is_valid())
        serializer = ExportRequestSerializer(
            data={"scope": "pins", "pin_ids": list(range(1, 50001))}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data,
            {"scope": "pins", "pin_ids": list(range(1, 50001))},
        )


class WorkerHealthServiceTests(TransactionTestCase):
    def test_only_ready_heartbeat_newer_than_fifteen_seconds_is_available(self):
        now = timezone.now()
        lease = ExportWorkerLease.objects.create(
            pk=1,
            health_state="ready",
            heartbeat_at=now - timedelta(seconds=15),
        )

        with self.assertRaises(ExportRequestError) as stale:
            WorkerHealthService.require_available(now)
        self.assertEqual(stale.exception.code, "export_worker_unavailable")
        lease.heartbeat_at = now - timedelta(seconds=14, microseconds=999999)
        lease.save(update_fields=("heartbeat_at",))
        snapshot = WorkerHealthService.require_available(now)
        self.assertEqual(snapshot.health_state, "ready")

    def test_storage_unsafe_health_preserves_its_safe_error_code(self):
        now = timezone.now()
        ExportWorkerLease.objects.create(
            pk=1,
            health_state="failed",
            heartbeat_at=now,
            error_code="export_storage_unsafe",
        )

        with self.assertRaises(ExportRequestError) as raised:
            WorkerHealthService.require_available(now)

        self.assertEqual(raised.exception.code, "export_storage_unsafe")


class ExportAPITests(ExportStorageMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super(ExportAPITests, self).setUp()
        self.client = APIClient()
        self.owner = create_export_user("api-owner")
        self.other = create_export_user("api-other")
        self.pin = create_export_pin(self.owner, private=True)
        self.board = create_export_board(self.owner, pins=(self.pin,))
        self.now = timezone.now()

    def _login(self):
        self.client.force_authenticate(self.owner)

    def _ready_worker(self):
        ExportWorkerLease.objects.update_or_create(
            pk=1,
            defaults={
                "health_state": "ready",
                "heartbeat_at": timezone.now(),
                "error_code": None,
            },
        )

    def test_anonymous_requests_are_401(self):
        for path in ("/api/v2/exports/preview/", "/api/v2/exports/"):
            response = self.client.post(
                path,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                format="json",
            )
            self.assertEqual(response.status_code, 401)

    def test_parser_normalizes_invalid_json_media_type_unknown_key_and_oversize(self):
        self._login()
        cases = (
            (b"not-json", "application/json"),
            (b"{}", "text/plain"),
            (json.dumps({"scope": "pins", "pin_ids": [self.pin.pk], "x": 1}).encode(), "application/json"),
            (b"{}" + b" " * (1024 * 1024 - 1), "application/json"),
        )
        for body, content_type in cases:
            with self.subTest(content_type=content_type, size=len(body)):
                response = self.client.generic(
                    "POST",
                    "/api/v2/exports/preview/",
                    body,
                    content_type=content_type,
                )
                self.assertEqual(
                    (response.status_code, response.json()),
                    (400, {"code": "invalid_target"}),
                )

    def test_parser_accepts_an_exact_one_mib_valid_json_body(self):
        self._login()
        prefix = json.dumps(
            {"scope": "board", "board_id": self.board.pk},
            separators=(",", ":"),
        ).encode()
        body = prefix + b" " * (1024 * 1024 - len(prefix))

        response = self.client.generic(
            "POST",
            "/api/v2/exports/preview/",
            body,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)

    def test_preview_has_exact_keys_and_does_not_require_worker(self):
        self._login()

        response = self.client.post(
            "/api/v2/exports/preview/",
            {"scope": "pins", "pin_ids": [self.pin.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {
            "schema_version", "as_of", "scope", "requested_total",
            "eligible_total", "excluded_total", "owned_private_total",
            "estimated_original_files", "estimated_original_bytes",
            "estimated_zip_bytes",
        })
        self.assertEqual(ExportJob.objects.count(), 0)

    def test_empty_and_only_hidden_boards_preview_zero_but_create_is_400(self):
        hidden = create_export_pin(self.other, private=True, color="green")
        boards = (
            create_export_board(self.owner, name="empty"),
            create_export_board(self.owner, name="hidden", pins=(hidden,)),
        )
        self._login()
        self._ready_worker()

        for board in boards:
            preview = self.client.post(
                "/api/v2/exports/preview/",
                {"scope": "board", "board_id": board.pk},
                format="json",
            )
            created = self.client.post(
                "/api/v2/exports/",
                {"scope": "board", "board_id": board.pk},
                format="json",
            )
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()["eligible_total"], 0)
            self.assertEqual(
                (created.status_code, created.json()),
                (400, {"code": "invalid_target"}),
            )

    def test_hidden_and_missing_selected_pin_are_the_same_404(self):
        hidden = create_export_pin(self.other, private=True, color="green")
        self._login()
        self._ready_worker()

        for path in ("/api/v2/exports/preview/", "/api/v2/exports/"):
            for pin_id in (hidden.pk, hidden.pk + 999999):
                response = self.client.post(
                    path,
                    {"scope": "pins", "pin_ids": [pin_id]},
                    format="json",
                )
                self.assertEqual(
                    (response.status_code, response.json()),
                    (404, {"code": "invalid_target"}),
                )

    def test_create_returns_exact_202_and_persists_targets_and_slot(self):
        self._login()
        self._ready_worker()

        response = self.client.post(
            "/api/v2/exports/",
            {"scope": "pins", "pin_ids": [self.pin.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(set(response.json()), {
            "schema_version", "id", "state", "scope", "requested_total",
            "target_total", "excluded_total", "status_url",
        })
        job = ExportJob.objects.get(pk=response.json()["id"])
        self.assertEqual(list(job.targets.values_list("pin_id", flat=True)), [self.pin.pk])
        self.assertEqual(ExportSlot.objects.get(owner=self.owner).current_job_id, job.pk)

    def test_create_worker_failure_is_exact_503_without_rows(self):
        self._login()

        response = self.client.post(
            "/api/v2/exports/",
            {"scope": "pins", "pin_ids": [self.pin.pk]},
            format="json",
        )

        self.assertEqual(
            (response.status_code, response.json()),
            (503, {"code": "export_worker_unavailable"}),
        )
        self.assertEqual(ExportJob.objects.count(), 0)
        self.assertEqual(ExportTarget.objects.count(), 0)
        self.assertEqual(ExportSlot.objects.count(), 0)

    def test_worker_health_is_rechecked_inside_authoritative_fence(self):
        self._ready_worker()

        def stop_worker(identity):
            del identity
            ExportWorkerLease.objects.filter(pk=1).update(
                health_state="stopped"
            )
            return 1

        from exports.services.targeting import TargetingService
        service = JobService(
            targeting=TargetingService(size_observer=stop_worker),
            available_space_observer=lambda: 10 ** 12,
        )

        with self.assertRaises(ExportRequestError) as raised:
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )

        self.assertEqual(raised.exception.code, "export_worker_unavailable")
        self.assertEqual(ExportJob.objects.count(), 0)

    def test_insufficient_space_rolls_back_job_target_and_slot(self):
        self._ready_worker()
        service = JobService(
            available_space_observer=lambda: 0,
        )

        with self.assertRaises(ExportRequestError) as raised:
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )

        self.assertEqual(raised.exception.code, "insufficient_space")
        self.assertEqual(ExportJob.objects.count(), 0)
        self.assertEqual(ExportTarget.objects.count(), 0)
        self.assertEqual(ExportSlot.objects.count(), 0)

    def test_blocked_cleanup_or_three_owner_rows_fail_closed(self):
        self._ready_worker()
        service = JobService(available_space_observer=lambda: 10 ** 12)
        blocked = ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            staging_cleanup_state="blocked",
        )

        with self.assertRaises(ExportRequestError) as raised:
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )
        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertTrue(ExportJob.objects.filter(pk=blocked.pk).exists())

        blocked.staging_cleanup_state = "pending"
        blocked.save(update_fields=("staging_cleanup_state",))
        ExportJob.objects.create(owner=self.owner, scope="pins")
        ExportJob.objects.create(owner=self.owner, scope="pins")
        with self.assertRaises(ExportRequestError) as raised:
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )
        self.assertEqual(raised.exception.code, "export_storage_unsafe")
        self.assertEqual(ExportJob.objects.filter(owner=self.owner).count(), 3)

    def test_expired_pending_and_complete_rows_allow_one_new_active_row(self):
        self._ready_worker()
        ready = {
            "ready_relative_path": "ready/history.zip",
            "ready_display_name": "history.zip",
            "ready_size": 10,
            "ready_sha256": "a" * 64,
            "ready_dev": 1,
            "ready_ino": 2,
            "ready_uid": 3,
            "ready_gid": 4,
            "ready_mode": 0o600,
            "ready_nlink": 1,
            "ready_mtime_ns": 5,
            "ready_ctime_ns": 6,
        }
        ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="expired",
            ready_cleanup_state="pending",
            **ready
        )
        ready["ready_relative_path"] = "ready/current.zip"
        ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            state="complete",
            ready_cleanup_state="retained",
            completed_at=self.now,
            expires_at=self.now + timedelta(hours=1),
            **ready
        )

        job = JobService(
            available_space_observer=lambda: 10 ** 12
        ).create(
            self.owner,
            {"scope": "pins", "pin_ids": [self.pin.pk]},
            self.now,
        )

        self.assertEqual(job.state, "queued")
        self.assertEqual(ExportJob.objects.filter(owner=self.owner).count(), 3)

    def test_identity_change_is_reobserved_outside_fence_before_single_commit(self):
        self._ready_worker()
        observations = []
        size_observation_atomic_states = []

        def mutate_once(identity):
            size_observation_atomic_states.append(connection.in_atomic_block)
            observations.append(identity.storage_name)
            if len(observations) == 1:
                image = self.pin.image
                image.image.name = "image/original/changed.png"
                image.save(update_fields=("image",))
            return 11

        def observe_space():
            self.assertFalse(connection.in_atomic_block)
            return 10 ** 12

        from exports.services.targeting import TargetingService
        service = JobService(
            targeting=TargetingService(size_observer=mutate_once),
            available_space_observer=observe_space,
        )

        job = service.create(
            self.owner,
            {"scope": "pins", "pin_ids": [self.pin.pk]},
            self.now,
        )

        self.assertEqual(len(observations), 2)
        self.assertEqual(size_observation_atomic_states, [False, False])
        self.assertEqual(ExportJob.objects.filter(pk=job.pk).count(), 1)
        self.assertEqual(ExportTarget.objects.filter(job=job).count(), 1)

    def test_identity_retry_exhaustion_is_temporary_and_commits_no_rows(self):
        self._ready_worker()
        observations = []

        def change_every_time(identity):
            observations.append(identity.storage_name)
            image = self.pin.image
            image.image.name = "image/original/change-{}.png".format(
                len(observations)
            )
            image.save(update_fields=("image",))
            return 1

        from exports.services.targeting import TargetingService
        service = JobService(
            targeting=TargetingService(size_observer=change_every_time),
            available_space_observer=lambda: 10 ** 12,
        )

        with self.assertRaises(ExportRequestError) as raised:
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )

        self.assertEqual(
            (raised.exception.code, raised.exception.status_code),
            ("export_temporarily_unavailable", 503),
        )
        self.assertEqual(len(observations), 3)
        self.assertEqual(ExportJob.objects.count(), 0)
        self.assertEqual(ExportTarget.objects.count(), 0)
        self.assertEqual(ExportSlot.objects.count(), 0)

    def test_database_fence_failure_is_secret_free_temporary_503(self):
        self._login()
        self._ready_worker()
        from core.services.database_fence import DatabaseFenceDeadline

        with mock.patch(
            "exports.services.jobs.database_write_fence",
            side_effect=DatabaseFenceDeadline(lambda: 1, budget_seconds=1),
        ):
            response = self.client.post(
                "/api/v2/exports/",
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                format="json",
            )

        self.assertEqual(
            (response.status_code, response.json()),
            (503, {"code": "export_temporarily_unavailable"}),
        )
        self.assertNotIn("database", response.content.decode().lower())

    def test_database_busy_deadline_exhaustion_is_normalized(self):
        self._ready_worker()
        from core.services.database_fence import DatabaseFenceBusy

        clock_values = iter((0, 6))
        service = JobService(
            available_space_observer=lambda: 10 ** 12,
            monotonic=lambda: next(clock_values),
            sleeper=lambda seconds: None,
        )
        with mock.patch(
            "exports.services.jobs.database_write_fence",
            side_effect=DatabaseFenceBusy(),
        ):
            with self.assertRaises(ExportRequestError) as raised:
                service.create(
                    self.owner,
                    {"scope": "pins", "pin_ids": [self.pin.pk]},
                    self.now,
                )

        self.assertEqual(
            (raised.exception.code, raised.exception.status_code),
            ("export_temporarily_unavailable", 503),
        )

    def test_postgresql_fence_model_list_is_complete_and_excludes_worker_lease(self):
        self._ready_worker()
        captured = []

        @contextmanager
        def capture_fence(using, models):
            captured.append((using, tuple(models)))
            with transaction.atomic(using=using):
                yield

        service = JobService(available_space_observer=lambda: 10 ** 12)
        with mock.patch(
            "exports.services.jobs.database_write_fence",
            side_effect=capture_fence,
        ):
            service.create(
                self.owner,
                {"scope": "pins", "pin_ids": [self.pin.pk]},
                self.now,
            )

        self.assertEqual(captured[0][0], "default")
        tables = {model._meta.db_table for model in captured[0][1]}

        self.assertEqual(tables, {
            "auth_user", "core_board", "core_board_pins", "core_pin",
            "django_images_image", "core_mediaasset", "taggit_tag",
            "taggit_taggeditem", "exports_exportjob", "exports_exportslot",
            "exports_exporttarget",
        })
        self.assertNotIn(ExportWorkerLease._meta.db_table, tables)


class RetiredRowSafetyTests(ExportStorageMixin, TransactionTestCase):
    def test_cleaned_attempt_with_unretired_file_keeps_the_terminal_job(self):
        owner = create_export_user("retired-owner")
        job = ExportJob.objects.create(
            owner=owner,
            scope="pins",
            state="failed",
            staging_cleanup_state="cleaned",
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
        )
        attempt = ExportAttempt.objects.create(
            job=job,
            attempt_generation=1,
            lease_uuid="11111111-1111-4111-8111-111111111111",
            state="cleaned",
            relative_path="attempts/1",
            dir_dev=1,
            dir_ino=2,
            dir_uid=3,
            dir_gid=4,
            dir_mode=0o700,
        )
        ExportAttemptFile.objects.create(
            attempt=attempt,
            kind="archive",
            state="writing",
            receipt_level="open",
            relative_path="attempts/1/archive.part",
            receipt_dev=1,
            receipt_ino=2,
            receipt_uid=3,
            receipt_gid=4,
            receipt_mode=0o600,
            receipt_nlink=1,
        )
        service = JobService(available_space_observer=lambda: 10 ** 12)

        with transaction.atomic():
            service._delete_retired_rows_if_cleanup_complete(
                owner,
                lambda: None,
            )

        self.assertTrue(ExportJob.objects.filter(pk=job.pk).exists())
