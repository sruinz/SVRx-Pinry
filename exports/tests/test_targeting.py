import re
from datetime import timedelta

import mock
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from exports.models import ExportJob
from exports.services.targeting import (
    ExportRequestError,
    TargetingService,
    iter_chunks,
)

from .helpers import (
    ExportStorageMixin,
    create_export_board,
    create_export_pin,
    create_export_user,
)


class ChunkingTests(TransactionTestCase):
    def test_iter_chunks_never_emits_more_than_400_values(self):
        values = list(range(50000))

        chunks = tuple(iter_chunks(values))

        self.assertEqual(len(chunks), 125)
        self.assertTrue(all(len(chunk) == 400 for chunk in chunks))
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[-1][-1], 49999)


class TargetingServiceTests(ExportStorageMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super(TargetingServiceTests, self).setUp()
        self.owner = create_export_user("target-owner")
        self.other = create_export_user("target-other")
        self.now = timezone.now()

    def test_selected_pins_preserve_request_order_and_include_owned_private(self):
        first = create_export_pin(self.owner, private=True, color="blue")
        second = create_export_pin(self.other, private=False, color="green")
        service = TargetingService(size_observer=lambda identity: 7)

        preview = service.preview(
            self.owner,
            {"scope": "pins", "pin_ids": [second.pk, first.pk]},
            self.now,
        )

        self.assertEqual(preview.pin_ids, (second.pk, first.pk))
        self.assertEqual(preview.requested_total, 2)
        self.assertEqual(preview.eligible_total, 2)
        self.assertEqual(preview.excluded_total, 0)
        self.assertEqual(preview.owned_private_total, 1)
        self.assertEqual(preview.estimated_original_files, 2)
        self.assertEqual(preview.estimated_original_bytes, 14)

    def test_hidden_and_missing_selected_pin_have_the_same_not_found_error(self):
        hidden = create_export_pin(self.other, private=True)
        service = TargetingService(size_observer=lambda identity: 1)

        for pin_id in (hidden.pk, hidden.pk + 999999):
            with self.subTest(pin_id=pin_id):
                with self.assertRaises(ExportRequestError) as raised:
                    service.preview(
                        self.owner,
                        {"scope": "pins", "pin_ids": [pin_id]},
                        self.now,
                    )
                self.assertEqual(
                    (raised.exception.code, raised.exception.status_code),
                    ("invalid_target", 404),
                )

    def test_board_is_owner_only_and_excludes_foreign_private_without_identity(self):
        owned = create_export_pin(self.owner, private=True, color="blue")
        visible = create_export_pin(self.other, private=False, color="green")
        hidden = create_export_pin(self.other, private=True, color="yellow")
        board = create_export_board(
            self.owner,
            pins=(hidden, visible, owned),
        )
        observed = []
        service = TargetingService(
            size_observer=lambda identity: observed.append(identity) or 5
        )

        preview = service.preview(
            self.owner,
            {"scope": "board", "board_id": board.pk},
            self.now,
        )

        self.assertEqual(preview.pin_ids, tuple(sorted((owned.pk, visible.pk))))
        self.assertEqual(preview.requested_total, 3)
        self.assertEqual(preview.eligible_total, 2)
        self.assertEqual(preview.excluded_total, 1)
        self.assertEqual(preview.owned_private_total, 1)
        self.assertNotIn(hidden.pk, [identity.pin_id for identity in observed])
        with self.assertRaises(ExportRequestError) as raised:
            service.preview(
                self.other,
                {"scope": "board", "board_id": board.pk},
                self.now,
            )
        self.assertEqual(
            (raised.exception.code, raised.exception.status_code),
            ("invalid_target", 404),
        )

    def test_preview_is_read_only_and_size_observation_failure_is_best_effort(self):
        pin = create_export_pin(self.owner)
        calls = []

        def unavailable(identity):
            calls.append((connection.in_atomic_block, identity.storage_name))
            raise OSError("secret storage path")

        preview = TargetingService(size_observer=unavailable).preview(
            self.owner,
            {"scope": "pins", "pin_ids": [pin.pk]},
            self.now,
        )

        self.assertEqual(calls[0][0], False)
        self.assertEqual(ExportJob.objects.count(), 0)
        self.assertEqual(preview.eligible_total, 1)
        self.assertGreaterEqual(preview.estimated_original_bytes, 0)
        self.assertGreaterEqual(
            preview.estimated_zip_bytes,
            preview.estimated_original_bytes,
        )

    def test_capture_locked_checkpoints_every_query_chunk(self):
        pins = [
            create_export_pin(self.owner, color="blue", filename="{}.png".format(index))
            for index in range(401)
        ]
        checkpoints = []
        service = TargetingService(size_observer=lambda identity: 1)

        with CaptureQueriesContext(connection) as queries:
            with transaction.atomic():
                captured = service.capture_locked(
                    self.owner,
                    {"scope": "pins", "pin_ids": [pin.pk for pin in pins]},
                    self.now,
                    lambda: checkpoints.append(True),
                )

        self.assertEqual(captured.pin_ids, tuple(pin.pk for pin in pins))
        self.assertGreaterEqual(len(checkpoints), 2)
        for query in queries:
            for values in re.findall(r"\bIN \(([^)]*)\)", query["sql"]):
                self.assertLessEqual(values.count(",") + 1, 400)

    def test_preview_as_of_is_the_supplied_utc_time(self):
        pin = create_export_pin(self.owner)
        requested = self.now - timedelta(seconds=3)

        preview = TargetingService(size_observer=lambda identity: 1).preview(
            self.owner,
            {"scope": "pins", "pin_ids": [pin.pk]},
            requested,
        )

        self.assertEqual(preview.as_of, requested)

    def test_default_size_observation_reuses_one_verified_media_root(self):
        first = create_export_pin(self.owner, color="blue", filename="a.png")
        second = create_export_pin(self.owner, color="green", filename="b.png")
        from django_images.file_ops import open_verified_media_root

        with mock.patch(
            "exports.services.targeting.open_verified_media_root",
            wraps=open_verified_media_root,
        ) as opener:
            preview = TargetingService().preview(
                self.owner,
                {"scope": "pins", "pin_ids": [first.pk, second.pk]},
                self.now,
            )

        self.assertEqual(preview.estimated_original_files, 2)
        self.assertEqual(opener.call_count, 1)

    def test_metadata_estimate_scales_past_long_description_and_tags(self):
        pin = create_export_pin(self.owner)
        pin.description = "한" * 100000
        pin.save(update_fields=("description",))
        pin.tags.add("tag-{}".format("x" * 90))

        preview = TargetingService(size_observer=lambda identity: 1).preview(
            self.owner,
            {"scope": "pins", "pin_ids": [pin.pk]},
            self.now,
        )

        metadata_bytes = (
            preview.estimated_zip_bytes - preview.estimated_original_bytes
        )
        self.assertGreater(metadata_bytes, 600000)
