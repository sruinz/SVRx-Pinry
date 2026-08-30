import re
import uuid
from datetime import timedelta

from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from core.models import Image, MediaAsset, Pin
from exports.contracts import StopRequested
from exports.models import ExportBlob, ExportItem, ExportJob
from exports.services.permissions import (
    iter_visible_foreign_pin_ids,
    revoked_item_ids,
)

from .helpers import ExportStorageMixin, create_export_pin, create_export_user


class ExportPermissionTests(ExportStorageMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        super(ExportPermissionTests, self).setUp()
        self.owner = create_export_user("permission-owner")
        self.other = create_export_user("permission-other")

    def _job(self, count):
        return ExportJob.objects.create(
            owner=self.owner,
            scope="pins",
            requested_total=count,
            target_total=count,
            included_total=count,
        )

    def _blob(self, job, source_id=1):
        return ExportBlob.objects.create(
            job=job,
            snapshot_generation=uuid.uuid4(),
            source_media_asset_id=source_id,
            source_image_id=source_id,
            source_relative_path="image/original/{}.png".format(source_id),
        )

    def _item(self, job, blob, pin, position, state="included"):
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
            inclusion_state=state,
        )

    @staticmethod
    def _delete_pin_row(pin_id):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM core_pin WHERE id = %s", [pin_id])

    def test_foreign_items_require_exact_live_public_identity(self):
        pins = [
            create_export_pin(
                self.other,
                filename="foreign-{}.png".format(index),
            )
            for index in range(5)
        ]
        job = self._job(len(pins))
        blob = self._blob(job)
        items = [
            self._item(job, blob, pin, position)
            for position, pin in enumerate(pins)
        ]
        self._delete_pin_row(pins[1].pk)
        Pin.objects.filter(pk=pins[2].pk).update(private=True)
        Pin.objects.filter(pk=pins[3].pk).update(submitter=self.owner)
        Pin.objects.filter(pk=pins[4].pk).update(
            published=pins[4].published + timedelta(seconds=1),
        )

        revoked = revoked_item_ids(job)

        self.assertEqual(revoked, {item.pk for item in items[1:]})
        self.assertNotIn(items[0].pk, revoked)

    def test_owned_snapshot_survives_delete_private_and_new_pk_identity(self):
        deleted = create_export_pin(self.owner, filename="owned-deleted.png")
        private = create_export_pin(self.owner, filename="owned-private.png")
        reused = create_export_pin(self.owner, filename="owned-reused.png")
        job = self._job(3)
        blob = self._blob(job)
        items = [
            self._item(job, blob, pin, position)
            for position, pin in enumerate((deleted, private, reused))
        ]
        self._delete_pin_row(deleted.pk)
        Pin.objects.filter(pk=private.pk).update(private=True)
        Pin.objects.filter(pk=reused.pk).update(
            submitter=self.other,
            published=reused.published + timedelta(seconds=1),
            private=True,
        )

        self.assertEqual(revoked_item_ids(job), set())
        self.assertEqual(len(items), 3)

    def test_owned_same_published_ownership_transfer_is_revoked(self):
        pin = create_export_pin(self.owner, filename="owned-transfer.png")
        job = self._job(1)
        item = self._item(job, self._blob(job), pin, 0)
        Pin.objects.filter(pk=pin.pk).update(
            submitter=self.other,
            private=True,
        )

        self.assertEqual(revoked_item_ids(job), {item.pk})

    def test_excluded_items_are_not_requeried_or_returned(self):
        pin = create_export_pin(self.other, filename="already-excluded.png")
        job = self._job(1)
        item = self._item(job, self._blob(job), pin, 0, state="excluded")
        Pin.objects.filter(pk=pin.pk).update(private=True)

        with CaptureQueriesContext(connection) as queries:
            revoked = revoked_item_ids(job)

        self.assertEqual(revoked, set())
        self.assertFalse(any(
            "core_pin" in query["sql"] for query in queries.captured_queries
        ))
        self.assertTrue(ExportItem.objects.filter(pk=item.pk).exists())

    def test_one_thousand_one_foreign_items_use_bounded_queries_and_callbacks(self):
        image = Image.objects.create(
            image="image/original/permissions-shared.png",
            original_filename="permissions-shared.png",
            width=1,
            height=1,
        )
        MediaAsset.objects.create(
            submitter=self.other,
            image=image,
            content_sha256="a" * 64,
        )
        Pin.objects.bulk_create([
            Pin(submitter=self.other, image=image)
            for _index in range(1001)
        ], batch_size=400)
        pins = list(Pin.objects.filter(submitter=self.other).order_by("pk"))
        job = self._job(len(pins))
        blob = self._blob(job)
        ExportItem.objects.bulk_create([
            ExportItem(
                job=job,
                target_position=position,
                snapshot_generation=blob.snapshot_generation,
                blob=blob,
                pin_id=pin.pk,
                pin_owner_id=pin.submitter_id,
                owner_username=self.other.username,
                is_public=True,
                published_at=pin.published,
                original_filename="permissions-shared.png",
            )
            for position, pin in enumerate(pins)
        ], batch_size=400)
        heartbeat_calls = []
        stop_calls = []
        checkpoint_calls = []

        with CaptureQueriesContext(connection) as queries:
            revoked = revoked_item_ids(
                job,
                heartbeat=lambda: heartbeat_calls.append(True),
                stop_requested=lambda: stop_calls.append(True) and False,
                checkpoint=lambda: checkpoint_calls.append(True),
            )

        in_sizes = []
        for query in queries.captured_queries:
            if "core_pin" not in query["sql"]:
                continue
            for values in re.findall(r"\bIN \(([^)]*)\)", query["sql"]):
                in_sizes.append(values.count(",") + 1)
        self.assertEqual(revoked, set())
        self.assertTrue(in_sizes)
        self.assertLessEqual(max(in_sizes), 400)
        self.assertEqual(len(heartbeat_calls), 3)
        self.assertEqual(len(stop_calls), 3)
        self.assertEqual(len(checkpoint_calls), 3)

    def test_visible_foreign_iterator_yields_matching_ids_per_chunk(self):
        first = create_export_pin(self.other, filename="visible-first.png")
        second = create_export_pin(self.other, filename="visible-second.png")
        hidden = create_export_pin(
            self.other,
            private=True,
            filename="visible-hidden.png",
        )
        job = self._job(3)
        blob = self._blob(job)
        items = [
            self._item(job, blob, pin, position)
            for position, pin in enumerate((first, second, hidden))
        ]

        chunks = tuple(iter_visible_foreign_pin_ids(
            items,
            self.owner.pk,
            chunk_size=2,
        ))

        self.assertEqual(chunks, ({first.pk, second.pk}, set()))

    def test_stop_requested_carries_current_lease(self):
        pin = create_export_pin(self.other, filename="permission-stop.png")
        job = self._job(1)
        blob = self._blob(job)
        self._item(job, blob, pin, 0)
        lease = object()

        with self.assertRaises(StopRequested) as raised:
            revoked_item_ids(
                job,
                stop_requested=lambda: True,
                lease=lease,
            )

        self.assertIs(raised.exception.lease, lease)
