import re

from django.db.models import BooleanField, Case, Count, IntegerField, Q, Value, When

from core.models import Board, Pin


_POSITIVE_DECIMAL = re.compile(r"\A[0-9]+\Z")


class BulkOperationError(Exception):
    def __init__(self, code, status_code):
        super(BulkOperationError, self).__init__(code)
        self.code = code
        self.status_code = status_code


def _invalid_selection_query():
    return BulkOperationError("selection_invalid_request", 400)


def parse_selection_query(query_params):
    allowed_keys = {"board_id", "exclusive_owned"}
    if set(query_params.keys()) - allowed_keys:
        raise _invalid_selection_query()

    parsed = {"board_id": None, "exclusive_owned": False}
    board_values = query_params.getlist("board_id")
    if board_values:
        if (
            len(board_values) != 1
            or not _POSITIVE_DECIMAL.match(board_values[0])
            or int(board_values[0]) <= 0
        ):
            raise _invalid_selection_query()
        parsed["board_id"] = int(board_values[0])

    exclusive_values = query_params.getlist("exclusive_owned")
    if exclusive_values:
        if len(exclusive_values) != 1 or exclusive_values[0] != "true":
            raise _invalid_selection_query()
        parsed["exclusive_owned"] = True

    if parsed["exclusive_owned"] and parsed["board_id"] is None:
        raise _invalid_selection_query()
    return parsed


class BulkPinManagementService(object):
    MAX_SELECTION_IDS = 50000

    def selection_ids(self, user, board_id=None, exclusive_owned=False):
        board = self._owned_board(user, board_id) if board_id else None
        query = self._selection_query(user, board, exclusive_owned)
        rows = list(query.order_by("-id")[: self.MAX_SELECTION_IDS + 1])
        if len(rows) > self.MAX_SELECTION_IDS:
            raise BulkOperationError("selection_too_large", 409)
        return {"count": len(rows), "results": rows}

    def board_delete_preview(self, user, board_id):
        board = self._owned_board(user, board_id)
        query = self._board_query(user, board)
        counts = query.aggregate(
            exclusive_owned_count=Count(Case(
                When(
                    Q(submitter=user) & Q(board_count=1),
                    then=Value(1),
                ),
                output_field=IntegerField(),
            )),
            shared_owned_count=Count(Case(
                When(
                    Q(submitter=user) & Q(board_count__gt=1),
                    then=Value(1),
                ),
                output_field=IntegerField(),
            )),
            non_owned_count=Count(Case(
                When(~Q(submitter=user), then=Value(1)),
                output_field=IntegerField(),
            )),
        )
        return counts

    @staticmethod
    def _owned_board(user, board_id):
        board = Board.objects.filter(pk=board_id, submitter=user).first()
        if board is None:
            raise BulkOperationError("board_not_found", 404)
        return board

    def _selection_query(self, user, board, exclusive_owned):
        if board is None:
            query = Pin.objects.filter(submitter=user)
        else:
            query = self._board_query(user, board)

        if exclusive_owned:
            query = query.filter(submitter=user, board_count=1)
        return query.annotate(
            owned=Case(
                When(submitter=user, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        ).values("id", "owned")

    @staticmethod
    def _board_query(user, board):
        return (
            Pin.objects.annotate(board_count=Count("pins"))
            .filter(pins=board)
            .filter(Q(private=False) | Q(submitter=user))
        )
