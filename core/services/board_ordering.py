import hashlib

from django.db import transaction

from core.models import Board
from users.models import User


class BoardOrderChanged(Exception):
    code = "board_order_changed"


class BoardOrderService(object):
    @staticmethod
    def _version(user_id, board_ids):
        digest_input = (
            b"svrx-board-order-v1\0"
            + str(user_id).encode("ascii")
            + b"\0"
            + ",".join(str(board_id) for board_id in board_ids).encode(
                "ascii"
            )
        )
        return hashlib.sha256(digest_input).hexdigest()

    @staticmethod
    def _canonical_ids(boards):
        return [
            board.pk
            for board in sorted(
                boards,
                key=lambda board: (board.display_order, -board.pk),
            )
        ]

    @staticmethod
    def _ordered_ids(user):
        return list(
            Board.objects.filter(submitter=user)
            .order_by("display_order", "-id")
            .values_list("id", flat=True)
        )

    def snapshot(self, user):
        board_ids = self._ordered_ids(user)
        return {
            "version": self._version(user.pk, board_ids),
            "board_ids": board_ids,
        }

    def save(self, user, version, board_ids):
        with transaction.atomic():
            locked_user = (
                User.objects.select_for_update().get(pk=user.pk)
            )
            boards = list(
                Board.objects.select_for_update()
                .filter(submitter=locked_user)
                .order_by("pk")
            )
            current_ids = self._canonical_ids(boards)
            current_version = self._version(locked_user.pk, current_ids)
            if board_ids == current_ids:
                return {
                    "version": current_version,
                    "board_ids": current_ids,
                }
            if (
                version != current_version
                or len(board_ids) != len(current_ids)
                or set(board_ids) != set(current_ids)
            ):
                raise BoardOrderChanged()
            boards_by_id = {board.pk: board for board in boards}
            for position, board_id in enumerate(board_ids, 1):
                boards_by_id[board_id].display_order = position
            Board.objects.bulk_update(boards, ["display_order"])
            return {
                "version": self._version(locked_user.pk, board_ids),
                "board_ids": board_ids,
            }
