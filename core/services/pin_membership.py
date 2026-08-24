from django.db import DEFAULT_DB_ALIAS, transaction

from core.models import Board, Pin
from core.services.board_cover import BoardCoverService
from users.models import User


class MembershipConflict(Exception):
    def __init__(self, code):
        super(MembershipConflict, self).__init__(code)
        self.code = code


class PinMembershipService(object):
    def _lock_user(self, user, using=DEFAULT_DB_ALIAS):
        return (
            User.objects.using(using).select_for_update().get(pk=user.pk)
        )

    def _lock_owned_boards(
        self,
        user,
        board_ids,
        using=DEFAULT_DB_ALIAS,
    ):
        expected_ids = sorted(set(board_ids))
        boards = list(
            Board.objects.using(using).select_for_update()
            .filter(submitter=user, pk__in=expected_ids)
            .order_by("pk")
        )
        if len(boards) != len(expected_ids):
            raise MembershipConflict("board_not_found")
        return boards

    def _lock_pins(self, pin_ids, using=DEFAULT_DB_ALIAS):
        return list(
            Pin.objects.using(using).select_for_update()
            .filter(pk__in=sorted(set(pin_ids)))
            .order_by("pk")
        )

    def add_owned_pins(self, user, board_id, pin_ids):
        using = DEFAULT_DB_ALIAS
        requested_ids = list(pin_ids)
        with transaction.atomic(using=using):
            board = self._lock_owned_boards(
                user,
                [board_id],
                using=using,
            )[0]
            pins = self._lock_pins(requested_ids, using=using)
            pins_by_id = {pin.pk: pin for pin in pins}
            if (
                len(pins_by_id) != len(set(requested_ids))
                or any(pin.submitter_id != user.pk for pin in pins)
            ):
                raise MembershipConflict("pin_not_found")
            existing_ids = set(
                board.pins.filter(pk__in=requested_ids)
                .values_list("pk", flat=True)
            )
            pins_to_add = [
                pins_by_id[pin_id]
                for pin_id in requested_ids
                if pin_id not in existing_ids
            ]
            if pins_to_add:
                board.pins.add(*pins_to_add)
            return [
                "unchanged" if pin_id in existing_ids else "added"
                for pin_id in requested_ids
            ]

    def move_pins(
        self,
        user,
        source_board_id,
        target_board_id,
        pin_ids,
    ):
        using = DEFAULT_DB_ALIAS
        requested_ids = list(pin_ids)
        with transaction.atomic(using=using):
            boards = self._lock_owned_boards(
                user,
                [source_board_id, target_board_id],
                using=using,
            )
            boards_by_id = {board.pk: board for board in boards}
            source = boards_by_id[source_board_id]
            target = boards_by_id[target_board_id]
            pins = self._lock_pins(requested_ids, using=using)
            pins_by_id = {pin.pk: pin for pin in pins}
            if (
                len(pins_by_id) != len(set(requested_ids))
                or any(
                    pin.private and pin.submitter_id != user.pk
                    for pin in pins
                )
            ):
                raise MembershipConflict("pin_not_found")

            source_ids = set(
                source.pins.filter(pk__in=requested_ids)
                .values_list("pk", flat=True)
            )
            target_ids = set(
                target.pins.filter(pk__in=requested_ids)
                .values_list("pk", flat=True)
            )
            if any(
                pin_id not in source_ids and pin_id not in target_ids
                for pin_id in requested_ids
            ):
                raise MembershipConflict("pin_membership_changed")

            pins_to_remove = [
                pins_by_id[pin_id]
                for pin_id in requested_ids
                if pin_id in source_ids
            ]
            pins_to_add = [
                pins_by_id[pin_id]
                for pin_id in requested_ids
                if pin_id not in target_ids
            ]
            BoardCoverService.clear_if_removed(
                source,
                [pin.pk for pin in pins_to_remove],
            )
            if pins_to_remove:
                source.pins.remove(*pins_to_remove)
            if pins_to_add:
                target.pins.add(*pins_to_add)
            return [
                "moved" if pin_id in source_ids else "unchanged"
                for pin_id in requested_ids
            ]

    def update_board_membership(
        self,
        user,
        board_id,
        pins_to_add,
        pins_to_remove,
    ):
        using = DEFAULT_DB_ALIAS
        add_ids = list(pins_to_add)
        remove_ids = list(pins_to_remove)
        requested_ids = add_ids + remove_ids
        with transaction.atomic(using=using):
            board = self._lock_owned_boards(
                user,
                [board_id],
                using=using,
            )[0]
            pins = self._lock_pins(requested_ids, using=using)
            visible_pins = {
                pin.pk: pin
                for pin in pins
                if not pin.private or pin.submitter_id == user.pk
            }
            existing_remove_ids = set(
                board.pins.filter(pk__in=remove_ids)
                .values_list("pk", flat=True)
            )
            additions = [
                visible_pins[pin_id]
                for pin_id in add_ids
                if pin_id in visible_pins
            ]
            removals = [
                visible_pins[pin_id]
                for pin_id in remove_ids
                if pin_id in visible_pins
            ]
            if additions:
                board.pins.add(*additions)
            BoardCoverService.clear_if_removed(
                board,
                [
                    pin.pk
                    for pin in removals
                    if pin.pk in existing_remove_ids
                ],
            )
            if removals:
                board.pins.remove(*removals)
            return board

    def add_new_pin_to_locked_boards(self, pin, boards):
        for board in boards:
            board.pins.add(pin)

    @staticmethod
    def lock_source_board_and_pin(source_board_id, pin_id, using):
        board = (
            Board.objects.using(using).select_for_update()
            .filter(pk=source_board_id)
            .first()
        )
        pin = (
            Pin.objects.using(using).select_for_update()
            .filter(pk=pin_id)
            .first()
        )
        return board, pin

    def delete_board(self, user, board_id):
        using = DEFAULT_DB_ALIAS
        through = Board.pins.through
        with transaction.atomic(using=using):
            locked_user = self._lock_user(user, using=using)
            board = self._lock_owned_boards(
                locked_user,
                [board_id],
                using=using,
            )[0]
            member_ids = list(
                through.objects.using(using)
                .filter(board_id=board.pk)
                .order_by("pin_id")
                .values_list("pin_id", flat=True)
            )
            self._lock_pins(member_ids, using=using)
            current_member_ids = list(
                through.objects.using(using)
                .filter(board_id=board.pk, pin_id__in=member_ids)
                .order_by("pin_id")
                .values_list("pin_id", flat=True)
            )
            through.objects.using(using).filter(
                board_id=board.pk,
                pin_id__in=current_member_ids,
            ).delete()
            board.delete(using=using)
