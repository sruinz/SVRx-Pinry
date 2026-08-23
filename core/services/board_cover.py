from django.db import transaction
from django.db.models import Q

from core.models import Board, Pin


class BoardCoverError(Exception):
    def __init__(self, code):
        super(BoardCoverError, self).__init__(code)
        self.code = code


class BoardCoverService(object):
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
