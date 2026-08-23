from types import SimpleNamespace

from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.test import TestCase, TransactionTestCase
import mock

from core.models import Board, Pin
from core.services.board_cover import BoardCoverService
from core.services.pin_membership import (
    MembershipConflict,
    PinMembershipService,
)
from core.tests.helpers import create_image, create_user
from django_images.test_helpers import TemporaryMediaMixin


class _RecordingQuerySet(object):
    def __init__(self, label, events, result):
        self.label = label
        self.events = events
        self.result = result

    def using(self, alias):
        self.events.append((self.label, "using", alias))
        return self

    def select_for_update(self):
        self.events.append((self.label, "select_for_update"))
        return self

    def filter(self, **filters):
        self.events.append((self.label, "filter", filters))
        return self

    def first(self):
        self.events.append((self.label, "first"))
        return self.result


class PinMembershipServiceTests(TestCase):
    MOVE_CASES = (
        (True, False, "moved", False, True),
        (True, True, "moved", False, True),
        (False, True, "unchanged", False, True),
    )

    def setUp(self):
        self.owner = create_user("membership-owner")
        self.other_user = create_user("membership-other")
        self.source = Board.objects.create(
            submitter=self.owner,
            name="membership-source",
        )
        self.target = Board.objects.create(
            submitter=self.owner,
            name="membership-target",
        )
        self.other_board = Board.objects.create(
            submitter=self.owner,
            name="membership-other-board",
        )
        self.pin = self._create_pin(self.owner)
        self.service = PinMembershipService()

    @staticmethod
    def _create_pin(user, private=False):
        return Pin.objects.create(
            submitter=user,
            image=create_image(),
            private=private,
        )

    def test_add_locks_board_before_pins(self):
        events = []
        lock_boards = self.service._lock_owned_boards
        lock_pins = self.service._lock_pins

        def record_boards(*args, **kwargs):
            events.append("boards")
            return lock_boards(*args, **kwargs)

        def record_pins(*args, **kwargs):
            events.append("pins")
            return lock_pins(*args, **kwargs)

        with mock.patch.object(
            self.service,
            "_lock_owned_boards",
            side_effect=record_boards,
        ), mock.patch.object(
            self.service,
            "_lock_pins",
            side_effect=record_pins,
        ):
            self.service.add_owned_pins(
                self.owner,
                self.target.pk,
                [self.pin.pk],
            )

        self.assertEqual(events, ["boards", "pins"])

    def test_add_preserves_existing_board_and_is_idempotent(self):
        self.other_board.pins.add(self.pin)

        first = self.service.add_owned_pins(
            self.owner,
            self.target.pk,
            [self.pin.pk],
        )
        second = self.service.add_owned_pins(
            self.owner,
            self.target.pk,
            [self.pin.pk],
        )

        self.assertEqual(first, ["added"])
        self.assertEqual(second, ["unchanged"])
        self.assertTrue(self.other_board.pins.filter(pk=self.pin.pk).exists())
        self.assertTrue(self.target.pins.filter(pk=self.pin.pk).exists())

    def test_add_rejects_any_non_owned_pin_before_all_changes(self):
        foreign = self._create_pin(self.other_user)

        with self.assertRaises(MembershipConflict) as caught:
            self.service.add_owned_pins(
                self.owner,
                self.target.pk,
                [self.pin.pk, foreign.pk],
            )

        self.assertEqual(caught.exception.code, "pin_not_found")
        self.assertFalse(self.target.pins.exists())

    def test_move_supports_all_existing_membership_states(self):
        for source_member, target_member, status, final_source, final_target in (
            self.MOVE_CASES
        ):
            with self.subTest(
                source_member=source_member,
                target_member=target_member,
            ):
                self.source.pins.clear()
                self.target.pins.clear()
                if source_member:
                    self.source.pins.add(self.pin)
                if target_member:
                    self.target.pins.add(self.pin)

                result = self.service.move_pins(
                    self.owner,
                    self.source.pk,
                    self.target.pk,
                    [self.pin.pk],
                )

                self.assertEqual(result, [status])
                self.assertEqual(
                    self.source.pins.filter(pk=self.pin.pk).exists(),
                    final_source,
                )
                self.assertEqual(
                    self.target.pins.filter(pk=self.pin.pk).exists(),
                    final_target,
                )

    def test_move_clears_source_cover_and_preserves_target_cover(self):
        source_fallback = self._create_pin(self.owner)
        target_cover = self._create_pin(self.owner)
        self.source.pins.add(self.pin, source_fallback)
        self.target.pins.add(target_cover)
        self.source.cover_pin = self.pin
        self.target.cover_pin = target_cover
        self.source.save(update_fields=("cover_pin",))
        self.target.save(update_fields=("cover_pin",))

        self.service.move_pins(
            self.owner,
            self.source.pk,
            self.target.pk,
            [self.pin.pk],
        )

        self.source.refresh_from_db()
        self.target.refresh_from_db()
        self.assertIsNone(self.source.cover_pin_id)
        self.assertEqual(self.target.cover_pin_id, target_cover.pk)
        fallback, manual_id = BoardCoverService().resolve(
            self.source,
            None,
        )
        self.assertEqual(fallback.pk, source_fallback.pk)
        self.assertIsNone(manual_id)

    def test_update_membership_clears_removed_manual_cover(self):
        fallback_pin = self._create_pin(self.owner)
        self.source.pins.add(self.pin, fallback_pin)
        self.source.cover_pin = self.pin
        self.source.save(update_fields=("cover_pin",))

        self.service.update_board_membership(
            self.owner,
            self.source.pk,
            [],
            [self.pin.pk],
        )

        self.source.refresh_from_db()
        self.assertIsNone(self.source.cover_pin_id)
        fallback, manual_id = BoardCoverService().resolve(
            self.source,
            None,
        )
        self.assertEqual(fallback.pk, fallback_pin.pk)
        self.assertIsNone(manual_id)

    def test_update_membership_preserves_cover_when_non_cover_is_removed(self):
        removed_pin = self._create_pin(self.owner)
        self.source.pins.add(self.pin, removed_pin)
        self.source.cover_pin = self.pin
        self.source.save(update_fields=("cover_pin",))

        self.service.update_board_membership(
            self.owner,
            self.source.pk,
            [],
            [removed_pin.pk],
        )

        self.source.refresh_from_db()
        self.assertEqual(self.source.cover_pin_id, self.pin.pk)

    def test_update_membership_preserves_cover_for_requested_non_member(self):
        self.source.cover_pin = self.pin
        self.source.save(update_fields=("cover_pin",))

        self.service.update_board_membership(
            self.owner,
            self.source.pk,
            [],
            [self.pin.pk],
        )

        self.source.refresh_from_db()
        self.assertEqual(self.source.cover_pin_id, self.pin.pk)

    def test_update_membership_removes_initial_non_member_in_overlap(self):
        self.service.update_board_membership(
            self.owner,
            self.source.pk,
            [self.pin.pk],
            [self.pin.pk],
        )

        self.assertFalse(
            self.source.pins.filter(pk=self.pin.pk).exists()
        )

    def test_move_rejects_pin_outside_both_boards_without_changes(self):
        with self.assertRaises(MembershipConflict) as caught:
            self.service.move_pins(
                self.owner,
                self.source.pk,
                self.target.pk,
                [self.pin.pk],
            )

        self.assertEqual(caught.exception.code, "pin_membership_changed")
        self.assertFalse(self.source.pins.filter(pk=self.pin.pk).exists())
        self.assertFalse(self.target.pins.filter(pk=self.pin.pk).exists())

    def test_move_validates_every_pin_before_changing_membership(self):
        valid_pin = self._create_pin(self.owner)
        invalid_pin = self._create_pin(self.owner)
        self.source.pins.add(valid_pin)

        with self.assertRaises(MembershipConflict) as caught:
            self.service.move_pins(
                self.owner,
                self.source.pk,
                self.target.pk,
                [valid_pin.pk, invalid_pin.pk],
            )

        self.assertEqual(caught.exception.code, "pin_membership_changed")
        self.assertTrue(self.source.pins.filter(pk=valid_pin.pk).exists())
        self.assertFalse(self.target.pins.filter(pk=valid_pin.pk).exists())

    def test_move_allows_public_foreign_pin(self):
        foreign = self._create_pin(self.other_user)
        self.source.pins.add(foreign)

        result = self.service.move_pins(
            self.owner,
            self.source.pk,
            self.target.pk,
            [foreign.pk],
        )

        self.assertEqual(result, ["moved"])
        self.assertFalse(self.source.pins.filter(pk=foreign.pk).exists())
        self.assertTrue(self.target.pins.filter(pk=foreign.pk).exists())

    def test_move_hides_private_foreign_and_missing_pin_with_same_error(self):
        foreign_private = self._create_pin(self.other_user, private=True)
        self.source.pins.add(foreign_private)

        for pin_id in (foreign_private.pk, foreign_private.pk + 100000):
            with self.subTest(pin_id=pin_id):
                with self.assertRaises(MembershipConflict) as caught:
                    self.service.move_pins(
                        self.owner,
                        self.source.pk,
                        self.target.pk,
                        [pin_id],
                    )
                self.assertEqual(caught.exception.code, "pin_not_found")

        self.assertTrue(
            self.source.pins.filter(pk=foreign_private.pk).exists()
        )
        self.assertFalse(
            self.target.pins.filter(pk=foreign_private.pk).exists()
        )

    def test_source_board_and_pin_helper_locks_in_order_without_atomic(self):
        events = []
        board = object()
        pin = object()
        fake_board = SimpleNamespace(objects=_RecordingQuerySet(
            "board", events, board
        ))
        fake_pin = SimpleNamespace(objects=_RecordingQuerySet(
            "pin", events, pin
        ))

        with mock.patch(
            "core.services.pin_membership.Board",
            fake_board,
        ), mock.patch(
            "core.services.pin_membership.Pin",
            fake_pin,
        ), mock.patch(
            "core.services.pin_membership.transaction.atomic",
            side_effect=AssertionError("helper must use the caller transaction"),
        ):
            result = self.service.lock_source_board_and_pin(
                self.source.pk,
                self.pin.pk,
                "membership-db",
            )

        self.assertEqual(result, (board, pin))
        self.assertEqual(
            [event[0] for event in events if event[1] == "select_for_update"],
            ["board", "pin"],
        )
        self.assertEqual(
            [event for event in events if event[1] == "using"],
            [
                ("board", "using", "membership-db"),
                ("pin", "using", "membership-db"),
            ],
        )

    def test_delete_board_locks_sorted_member_pins_and_preserves_pin_rows(self):
        later_pin = self._create_pin(self.owner)
        self.source.pins.add(later_pin, self.pin)
        locked_ids = []
        lock_pins = self.service._lock_pins

        def record_pin_ids(pin_ids, using=DEFAULT_DB_ALIAS):
            locked_ids.append(tuple(pin_ids))
            return lock_pins(pin_ids, using=using)

        with mock.patch.object(
            self.service,
            "_lock_pins",
            side_effect=record_pin_ids,
        ):
            self.service.delete_board(self.owner, self.source.pk)

        self.assertEqual(
            locked_ids,
            [tuple(sorted((self.pin.pk, later_pin.pk)))],
        )
        self.assertFalse(Board.objects.filter(pk=self.source.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=later_pin.pk).exists())
        through = Board.pins.through
        self.assertFalse(
            through.objects.filter(board_id=self.source.pk).exists()
        )


class PinConditionalDeletionTests(TemporaryMediaMixin, TransactionTestCase):
    def setUp(self):
        super(PinConditionalDeletionTests, self).setUp()
        self.owner = create_user("conditional-owner")
        self.other_user = create_user("conditional-other")
        self.source = Board.objects.create(
            submitter=self.owner,
            name="conditional-source",
        )
        self.pin = Pin.objects.create(
            submitter=self.owner,
            image=create_image(),
        )
        self.source.pins.add(self.pin)

    def test_conditional_delete_calls_common_board_pin_lock(self):
        calls = []
        source_id = self.source.pk
        pin_id = self.pin.pk
        real_lock = PinMembershipService.lock_source_board_and_pin

        def record_lock(source_board_id, pin_id, using):
            calls.append((
                source_board_id,
                pin_id,
                using,
                connection.in_atomic_block,
            ))
            return real_lock(source_board_id, pin_id, using)

        with mock.patch.object(
            PinMembershipService,
            "lock_source_board_and_pin",
            side_effect=record_lock,
        ):
            result = self.pin.delete_if_exclusive_to_board(
                self.owner.pk,
                self.source.pk,
            )

        self.assertEqual(result, ("deleted", None))
        self.assertEqual(
            calls,
            [(source_id, pin_id, "default", True)],
        )

    def test_conditional_delete_normalizes_missing_source_board(self):
        source_id = self.source.pk
        pin_id = self.pin.pk
        self.source.delete()

        result = self.pin.delete_if_exclusive_to_board(
            self.owner.pk,
            source_id,
        )

        self.assertEqual(result, ("preserved", "source_membership_changed"))
        self.assertTrue(Pin.objects.filter(pk=pin_id).exists())

    def test_conditional_delete_normalizes_missing_pin(self):
        source_id = self.source.pk
        pin_id = self.pin.pk
        Pin.objects.filter(pk=pin_id).delete()

        result = self.pin.delete_if_exclusive_to_board(
            self.owner.pk,
            source_id,
        )

        self.assertEqual(result, ("preserved", "source_membership_changed"))
        self.assertFalse(Pin.objects.filter(pk=pin_id).exists())

    def test_conditional_delete_normalizes_missing_source_membership(self):
        pin_id = self.pin.pk
        self.source.pins.remove(self.pin)

        result = self.pin.delete_if_exclusive_to_board(
            self.owner.pk,
            self.source.pk,
        )

        self.assertEqual(result, ("preserved", "source_membership_changed"))
        self.assertTrue(Pin.objects.filter(pk=pin_id).exists())

    def test_conditional_delete_preserves_foreign_pin_on_source(self):
        self.source.pins.remove(self.pin)
        foreign_pin = Pin.objects.create(
            submitter=self.other_user,
            image=create_image(),
        )
        self.source.pins.add(foreign_pin)

        result = foreign_pin.delete_if_exclusive_to_board(
            self.owner.pk,
            self.source.pk,
        )

        self.assertEqual(result, ("preserved", "non_owned_pin"))
        self.assertTrue(Pin.objects.filter(pk=foreign_pin.pk).exists())
        self.assertTrue(
            self.source.pins.filter(pk=foreign_pin.pk).exists()
        )

    def test_conditional_legacy_delete_requires_autocommit(self):
        pin_id = self.pin.pk

        with transaction.atomic():
            with self.assertRaisesMessage(
                RuntimeError,
                "registered_pin_delete_requires_autocommit",
            ):
                self.pin.delete_if_exclusive_to_board(
                    self.owner.pk,
                    self.source.pk,
                )

        self.assertTrue(Pin.objects.filter(pk=pin_id).exists())
