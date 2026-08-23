from contextlib import contextmanager

from django.db import transaction
from django.db.models import Q

from core.models import Board, Pin


class BoardCoverError(Exception):
    def __init__(self, code):
        super(BoardCoverError, self).__init__(code)
        self.code = code


class BoardCoverService(object):
    def reconcile_locked_board(self, board):
        if board.cover_pin_id is None:
            return False
        cover = (
            Pin.objects.select_for_update()
            .filter(pk=board.cover_pin_id)
            .first()
        )
        valid = (
            cover is not None
            and board.pins.filter(pk=cover.pk).exists()
            and (board.private or not cover.private)
        )
        if valid:
            return False
        board.cover_pin = None
        board.save(update_fields=("cover_pin",))
        return True

    @contextmanager
    def pin_privacy_transition(self, user, pin_ids, target_private):
        ordered_ids = sorted(set(pin_ids))
        with transaction.atomic():
            if target_private:
                probed_board_ids = list(
                    Board.objects.filter(
                        private=False,
                        cover_pin_id__in=ordered_ids,
                    ).order_by("pk").values_list("pk", flat=True)
                )
            else:
                probed_board_ids = []
            list(
                Board.objects.select_for_update()
                .filter(pk__in=probed_board_ids)
                .order_by("pk")
            )
            pins = list(
                Pin.objects.select_for_update()
                .filter(pk__in=ordered_ids, submitter=user)
                .order_by("pk")
            )
            if len(pins) != len(ordered_ids):
                raise BoardCoverError("pin_not_found")
            if target_private:
                current_board_ids = list(
                    Board.objects.filter(
                        private=False,
                        cover_pin_id__in=ordered_ids,
                    ).order_by("pk").values_list("pk", flat=True)
                )
            else:
                current_board_ids = []
            if current_board_ids != probed_board_ids:
                raise BoardCoverError("board_cover_changed")
            yield {pin.pk: pin for pin in pins}
            if target_private and current_board_ids:
                Board.objects.filter(
                    pk__in=current_board_ids,
                    cover_pin_id__in=ordered_ids,
                    private=False,
                ).update(cover_pin=None)

    @staticmethod
    def clear_if_removed(board, removed_pin_ids):
        if board.cover_pin_id not in set(removed_pin_ids):
            return False
        board.cover_pin = None
        board.save(update_fields=("cover_pin",))
        return True

    @staticmethod
    def eligible_pins(board, request):
        query = board.pins.select_related("image", "submitter")
        if not board.private:
            return query.filter(private=False)
        user = request.user
        if user.is_authenticated:
            return query.filter(Q(private=False) | Q(submitter=user))
        return query.filter(private=False)

    def resolve(self, board, request):
        query = self.eligible_pins(board, request)
        if board.cover_pin_id is not None:
            manual = query.filter(pk=board.cover_pin_id).first()
            if manual is not None:
                return manual, manual.pk
        return query.order_by("pk").first(), None

    def set_cover(self, user, board_id, pin_id):
        with transaction.atomic():
            board = (
                Board.objects.select_for_update()
                .filter(pk=board_id, submitter=user)
                .first()
            )
            if board is None:
                raise BoardCoverError("board_not_found")
            if pin_id is None:
                board.cover_pin = None
                board.save(update_fields=("cover_pin",))
                return board
            pin = Pin.objects.select_for_update().filter(pk=pin_id).first()
            if (
                pin is None
                or not board.pins.filter(pk=pin_id).exists()
                or (pin.private and pin.submitter_id != user.pk)
            ):
                raise BoardCoverError("board_cover_invalid")
            if not board.private and pin.private:
                raise BoardCoverError("board_cover_private_pin")
            board.cover_pin = pin
            board.save(update_fields=("cover_pin",))
            return board
