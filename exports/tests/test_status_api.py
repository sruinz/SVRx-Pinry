from datetime import datetime, timedelta, timezone as datetime_timezone
import uuid

import mock
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from exports.models import ExportJob, ExportWorkerLease

from .helpers import create_export_user


UTC = datetime_timezone.utc
STATUS_KEYS = {
    "schema_version", "id", "state", "phase_label", "scope",
    "phase_percent", "overall_percent", "counters", "created_at",
    "snapshot_at", "heartbeat_at", "completed_at", "expires_at",
    "resume_count", "error", "download_url",
}
COUNTER_KEYS = {
    "requested_total", "target_total", "snapshot_done", "archive_total",
    "archive_done", "included_total", "excluded_total", "bytes_total",
    "bytes_done",
}


class ExportStatusAPITests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = create_export_user("status-owner")
        self.other = create_export_user("status-other")
        self.now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def _job(self, **overrides):
        created_at = overrides.pop(
            "created_at", self.now - timedelta(minutes=5),
        )
        values = {
            "owner": self.owner,
            "scope": "pins",
            "requested_total": 100,
            "target_total": 100,
            "snapshot_done": 100,
            "archive_total": 100,
            "archive_done": 37,
            "included_total": 100,
            "excluded_total": 0,
            "bytes_total": 1000,
            "bytes_done": 370,
        }
        values.update(overrides)
        job = ExportJob.objects.create(**values)
        ExportJob.objects.filter(pk=job.pk).update(created_at=created_at)
        job.refresh_from_db()
        return job

    def _complete_job(self, **overrides):
        job_id = overrides.pop("id", uuid.uuid4())
        completed_at = overrides.pop(
            "completed_at", self.now - timedelta(hours=1),
        )
        values = {
            "id": job_id,
            "state": "complete",
            "completed_at": completed_at,
            "expires_at": completed_at + timedelta(hours=24),
            "ready_cleanup_state": "retained",
            "ready_relative_path": "ready/{}.zip".format(job_id),
            "ready_display_name": "내보내기.zip",
            "ready_size": 123,
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
        values.update(overrides)
        return self._job(**values)

    def _get_at(self, path):
        with mock.patch("exports.views.timezone.now", return_value=self.now):
            return self.client.get(path)

    def test_latest_and_retrieve_return_exact_status_envelopes(self):
        job = self._job(
            state="archiving",
            snapshot_at=self.now - timedelta(minutes=4),
            heartbeat_at=self.now - timedelta(seconds=2),
            resume_count=2,
        )
        self.client.force_authenticate(self.owner)

        latest = self._get_at("/api/v2/exports/latest/")
        retrieve = self._get_at(
            "/api/v2/exports/{}/".format(job.pk),
        )

        self.assertEqual(latest.status_code, 200, latest.content)
        self.assertEqual(set(latest.json()), {
            "schema_version", "latest_attempt", "downloadable_job",
        })
        self.assertIsNone(latest.json()["downloadable_job"])
        self.assertEqual(retrieve.status_code, 200, retrieve.content)
        for payload in (latest.json()["latest_attempt"], retrieve.json()):
            self.assertEqual(set(payload), STATUS_KEYS)
            self.assertEqual(set(payload["counters"]), COUNTER_KEYS)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["id"], str(job.pk))
            self.assertEqual(payload["state"], "archiving")
            self.assertEqual(payload["phase_label"], "압축 중")
            self.assertEqual(payload["scope"], "pins")
            self.assertEqual(payload["phase_percent"], 37.0)
            self.assertEqual(payload["overall_percent"], 47.8)
            self.assertEqual(payload["counters"], {
                "requested_total": 100,
                "target_total": 100,
                "snapshot_done": 100,
                "archive_total": 100,
                "archive_done": 37,
                "included_total": 100,
                "excluded_total": 0,
                "bytes_total": 1000,
                "bytes_done": 370,
            })
            self.assertEqual(payload["resume_count"], 2)
            self.assertIsNone(payload["error"])
            self.assertIsNone(payload["download_url"])
            self.assertNotIn("health_state", payload)
            self.assertNotIn("worker_error", payload)

    def test_latest_separates_new_failed_attempt_from_prior_download(self):
        completed = self._complete_job(
            created_at=self.now - timedelta(hours=2),
        )
        failed = self._job(
            state="failed",
            created_at=self.now - timedelta(minutes=1),
            error_code="archive_failed",
            error_class="retryable",
            error_retryable=True,
        )
        self.client.force_authenticate(self.owner)

        response = self._get_at("/api/v2/exports/latest/")

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["latest_attempt"]["id"], str(failed.pk))
        self.assertEqual(payload["latest_attempt"]["state"], "failed")
        self.assertEqual(payload["latest_attempt"]["error"], {
            "code": "archive_failed",
            "class": "retryable",
            "retryable": True,
            "message": "ZIP 파일을 만들지 못했습니다.",
        })
        self.assertEqual(
            payload["downloadable_job"]["id"], str(completed.pk),
        )
        self.assertEqual(payload["downloadable_job"]["state"], "complete")
        self.assertEqual(
            payload["downloadable_job"]["download_url"],
            "/api/v2/exports/{}/download/".format(completed.pk),
        )

    def test_complete_at_expiry_is_logically_expired_and_not_downloadable(self):
        job = self._complete_job(
            expires_at=self.now,
            created_at=self.now - timedelta(minutes=1),
        )
        self.client.force_authenticate(self.owner)

        latest = self._get_at("/api/v2/exports/latest/")
        retrieve = self._get_at(
            "/api/v2/exports/{}/".format(job.pk),
        )

        self.assertEqual(latest.status_code, 200, latest.content)
        self.assertEqual(
            latest.json()["latest_attempt"]["state"], "expired",
        )
        self.assertIsNone(latest.json()["latest_attempt"]["download_url"])
        self.assertIsNone(latest.json()["downloadable_job"])
        self.assertEqual(retrieve.status_code, 200, retrieve.content)
        self.assertEqual(retrieve.json()["state"], "expired")
        self.assertEqual(retrieve.json()["phase_label"], "만료됨")
        self.assertIsNone(retrieve.json()["download_url"])
        job.refresh_from_db()
        self.assertEqual(job.state, "complete")
        self.assertEqual(job.ready_cleanup_state, "retained")

    def test_status_progress_matrix_and_zero_denominators(self):
        jobs = (
            (
                self._job(
                    state="snapshotting",
                    snapshot_done=25,
                ),
                25.0,
                5.0,
            ),
            (
                self._job(
                    state="snapshotting",
                    requested_total=0,
                    target_total=0,
                    snapshot_done=0,
                    included_total=0,
                ),
                None,
                None,
            ),
            (
                self._job(state="verifying"),
                50.0,
                97.0,
            ),
            (
                self._complete_job(),
                100.0,
                100.0,
            ),
            (
                self._job(
                    state="archiving",
                    archive_total=0,
                    archive_done=0,
                ),
                None,
                None,
            ),
        )
        self.client.force_authenticate(self.owner)

        for job, phase_percent, overall_percent in jobs:
            with self.subTest(
                state=job.state,
                target_total=job.target_total,
                archive_total=job.archive_total,
            ):
                response = self._get_at(
                    "/api/v2/exports/{}/".format(job.pk),
                )
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(
                    response.json()["phase_percent"], phase_percent,
                )
                self.assertEqual(
                    response.json()["overall_percent"], overall_percent,
                )

    def test_latest_and_retrieve_are_owner_scoped(self):
        owner_job = self._job(created_at=self.now - timedelta(minutes=2))
        self._job(
            owner=self.other,
            created_at=self.now - timedelta(minutes=1),
        )
        self.client.force_authenticate(self.owner)

        latest = self._get_at("/api/v2/exports/latest/")
        hidden = self._get_at(
            "/api/v2/exports/{}/".format(
                ExportJob.objects.filter(owner=self.other).get().pk,
            ),
        )

        self.assertEqual(
            latest.json()["latest_attempt"]["id"], str(owner_job.pk),
        )
        self.assertEqual(hidden.status_code, 404)

    def test_anonymous_latest_and_retrieve_are_401(self):
        job = self._job()

        for path in (
            "/api/v2/exports/latest/",
            "/api/v2/exports/{}/".format(job.pk),
        ):
            with self.subTest(path=path):
                response = self._get_at(path)
                self.assertEqual(response.status_code, 401)

    def test_r28_only_fresh_ready_global_heartbeat_marks_worker_wait(self):
        job = self._job(heartbeat_at=self.now - timedelta(seconds=30))
        lease = ExportWorkerLease.objects.create(
            pk=1,
            health_state="ready",
            heartbeat_at=self.now - timedelta(seconds=14),
        )
        self.client.force_authenticate(self.owner)

        fresh = self._get_at("/api/v2/exports/latest/")
        self.assertEqual(
            fresh.json()["latest_attempt"]["phase_label"],
            "작업자 대기 중",
        )
        fresh_retrieve = self._get_at(
            "/api/v2/exports/{}/".format(job.pk),
        )
        self.assertEqual(
            fresh_retrieve.json()["phase_label"], "작업자 대기 중",
        )

        cases = (
            ("ready", self.now - timedelta(seconds=15)),
            ("starting", self.now - timedelta(seconds=1)),
            ("failed", self.now - timedelta(seconds=1)),
            ("stopped", self.now - timedelta(seconds=1)),
        )
        for health_state, heartbeat_at in cases:
            with self.subTest(
                health_state=health_state,
                heartbeat_at=heartbeat_at,
            ):
                lease.health_state = health_state
                lease.heartbeat_at = heartbeat_at
                lease.save(update_fields=("health_state", "heartbeat_at"))
                response = self._get_at("/api/v2/exports/latest/")
                self.assertEqual(
                    response.json()["latest_attempt"]["phase_label"],
                    "대기 중",
                )

        lease.health_state = "ready"
        lease.heartbeat_at = self.now - timedelta(seconds=1)
        lease.save(update_fields=("health_state", "heartbeat_at"))
        job.heartbeat_at = self.now
        job.save(update_fields=("heartbeat_at",))
        response = self._get_at("/api/v2/exports/latest/")
        self.assertEqual(
            response.json()["latest_attempt"]["phase_label"], "대기 중",
        )
