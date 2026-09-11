import hashlib
from datetime import timezone as datetime_timezone
import json
import os
from types import SimpleNamespace
import tempfile
import uuid
import zipfile

from django.test import SimpleTestCase
from django.utils import timezone

from exports.services.archive import (
    ArchiveValidationStopped,
    build_manifest,
    validate_archive,
)
from exports.services.file_ops import ExportStorageError


class ManifestTests(SimpleTestCase):
    def test_manifest_has_exact_schema_stable_order_and_no_private_user_data(self):
        exported_at = timezone.datetime(
            2026, 8, 30, 12, 1, 10, 123456,
            tzinfo=datetime_timezone.utc,
        )
        published_at = timezone.datetime(
            2026, 8, 1, 2, 3, 4, 123456,
            tzinfo=datetime_timezone.utc,
        )
        blob_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        job = SimpleNamespace(
            pk=uuid.UUID("6b269ff6-145e-44b9-a9c6-b17ddb6a67e5"),
            snapshot_at=timezone.datetime(
                2026, 8, 30, 12, 0, 3, tzinfo=datetime_timezone.utc,
            ),
            scope="board",
            board_id_snapshot=12,
            board_name_snapshot="여행",
            board_private_snapshot=True,
            board_owner_username_snapshot="sruin",
            requested_total=3,
            target_total=2,
            included_total=1,
            excluded_total=2,
            excluded_not_visible_total=1,
            excluded_permission_revoked_total=1,
        )
        blob = SimpleNamespace(
            pk=blob_id,
            mime_type="image/jpeg",
            size=7,
        )
        item = SimpleNamespace(
            blob=blob,
            blob_id=blob_id,
            pin_id=123,
            owner_username="sruin",
            is_public=False,
            published_at=published_at,
            description="여행 사진",
            tags_json='["바다", "여행"]',
            source_url="https://example.test/photo?keep=yes",
            source_url_redacted=True,
            referer_url=None,
            referer_url_redacted=False,
            original_filename="IMG_0001.JPG",
            archive_image_path="originals/IMG_0001.JPG",
            archive_xmp_path="originals/IMG_0001.JPG.xmp",
        )
        digest = "0123456789abcdef" * 4

        first = build_manifest(job, [item], {blob_id: digest}, exported_at)
        second = build_manifest(job, [item], {blob_id: digest}, exported_at)
        manifest = json.loads(first.decode("utf-8"))

        self.assertEqual(first, second)
        self.assertEqual(list(manifest), [
            "schema_version", "export_id", "exported_at", "snapshot_at",
            "scope", "counts", "exclusion_reasons", "items",
        ])
        self.assertEqual(manifest["scope"], {
            "type": "board",
            "board": {
                "id": 12,
                "name": "여행",
                "is_public": False,
                "owner_username": "sruin",
            },
        })
        self.assertEqual(manifest["counts"], {
            "requested_total": 3,
            "target_total": 2,
            "included_total": 1,
            "excluded_total": 2,
        })
        self.assertEqual(manifest["exclusion_reasons"], {
            "not_visible_at_request": 1,
            "permission_revoked": 1,
        })
        self.assertEqual(list(manifest["items"][0]), [
            "pin_id", "owner_username", "is_public", "published_at",
            "description", "tags", "source_url", "source_url_redacted",
            "referer_url", "referer_url_redacted", "original_filename",
            "archive_image_path", "archive_xmp_path", "mime_type", "size",
            "sha256",
        ])
        self.assertEqual(
            manifest["items"][0]["published_at"],
            "2026-08-01T02:03:04.123456Z",
        )
        serialized = first.decode("utf-8")
        self.assertNotIn("email", serialized)
        self.assertNotIn("token-value", serialized)

    def test_pin_scope_has_null_board(self):
        job = SimpleNamespace(
            pk=uuid.uuid4(),
            snapshot_at=timezone.now(),
            scope="pins",
            board_id_snapshot=None,
            board_name_snapshot=None,
            board_private_snapshot=None,
            board_owner_username_snapshot=None,
            requested_total=0,
            target_total=0,
            included_total=0,
            excluded_total=0,
            excluded_not_visible_total=0,
            excluded_permission_revoked_total=0,
        )

        manifest = json.loads(build_manifest(
            job, [], {}, timezone.now(),
        ).decode("utf-8"))

        self.assertEqual(manifest["scope"], {"type": "pins", "board": None})


class ArchiveValidationTests(SimpleTestCase):
    @staticmethod
    def _zip_file(entries):
        file_obj = tempfile.TemporaryFile()
        with zipfile.ZipFile(file_obj, "w", allowZip64=True) as archive:
            for name, compression, content in entries:
                info = zipfile.ZipInfo(name, (2026, 8, 30, 12, 0, 0))
                info.compress_type = compression
                archive.writestr(info, content)
        file_obj.flush()
        file_obj.seek(0)
        return file_obj

    def test_validation_checks_order_methods_crc_and_returns_file_digest(self):
        entries = (
            ("originals/photo.jpg", zipfile.ZIP_STORED, b"image"),
            ("originals/photo.jpg.xmp", zipfile.ZIP_DEFLATED, b"xmp"),
            ("manifest.json", zipfile.ZIP_DEFLATED, b"{}"),
        )
        heartbeat_calls = []
        with self._zip_file(entries) as file_obj:
            expected = os.pread(
                file_obj.fileno(),
                os.fstat(file_obj.fileno()).st_size,
                0,
            )
            size, digest = validate_archive(
                file_obj.fileno(),
                [entry[0] for entry in entries],
                lambda: heartbeat_calls.append(True),
                lambda: False,
            )

        self.assertEqual(size, len(expected))
        self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
        self.assertTrue(heartbeat_calls)

    def test_validation_rejects_duplicate_traversal_order_and_wrong_method(self):
        cases = (
            (
                (("same", zipfile.ZIP_STORED, b"a"),
                 ("same", zipfile.ZIP_STORED, b"b")),
                ("same", "same"),
            ),
            ((("../secret", zipfile.ZIP_STORED, b"a"),), ("../secret",)),
            ((("manifest.json", zipfile.ZIP_STORED, b"{}"),),
             ("manifest.json",)),
        )
        for entries, expected_names in cases:
            with self.subTest(entries=entries):
                with self._zip_file(entries) as file_obj:
                    with self.assertRaises(ExportStorageError) as raised:
                        validate_archive(
                            file_obj.fileno(), expected_names,
                            lambda: None, lambda: False,
                        )
                self.assertEqual(raised.exception.code, "archive_failed")

    def test_virtual_forty_five_second_validation_pulses_and_checks_stop_at_megabyte_boundaries(self):
        content = b"x" * (12 * 1024 * 1024 + 17)
        clock = [0.0]
        heartbeat_times = []
        stop_times = []
        stop_state = {"requested": False}

        def heartbeat():
            heartbeat_times.append(clock[0])
            clock[0] += 2.5
            if clock[0] >= 45.0:
                stop_state["requested"] = True

        def stop_requested():
            stop_times.append(clock[0])
            return stop_state["requested"]

        entries = (("originals/large.bin", zipfile.ZIP_STORED, content),)
        with self._zip_file(entries) as file_obj:
            archive_chunks = (
                os.fstat(file_obj.fileno()).st_size + 1024 * 1024 - 1
            ) // (1024 * 1024)
            with self.assertRaises(ArchiveValidationStopped):
                validate_archive(
                    file_obj.fileno(), ["originals/large.bin"],
                    heartbeat, stop_requested,
                )

        self.assertGreaterEqual(clock[0], 45.0)
        self.assertGreater(len(heartbeat_times), archive_chunks)
        self.assertEqual(len(stop_times), len(heartbeat_times) + 1)
        gaps = [
            later - earlier
            for earlier, later in zip(
                heartbeat_times, heartbeat_times[1:] + [clock[0]],
            )
        ]
        self.assertLessEqual(max(gaps), 5.0)
