import re
import time

from django.conf import settings
from django.db import connection, OperationalError, transaction
from django.db.models import BooleanField, Case, Count, IntegerField, Q, Value, When

from core.models import Board, Pin
from core.services.board_cover import BoardCoverError, BoardCoverService
from core.services.pin_membership import (
    MembershipConflict,
    PinMembershipService,
)


_POSITIVE_DECIMAL = re.compile(r"\A[0-9]+\Z")
_SQLITE_BUSY_WORD = re.compile(r"\b(?:busy|locked)\b")
_POSTGRESQL_BUSY_SQLSTATES = frozenset(("40001", "40P01", "55P03"))


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


def normalize_bulk_exception(error):
    if isinstance(error, OperationalError):
        if (
            connection.vendor == "sqlite"
            and _SQLITE_BUSY_WORD.search(str(error).lower())
        ):
            return "database_busy", True
        if connection.vendor == "postgresql":
            candidates = (
                error,
                getattr(error, "__cause__", None),
                getattr(error, "__context__", None),
            )
            for candidate in candidates:
                sqlstate = (
                    getattr(candidate, "pgcode", None)
                    or getattr(candidate, "sqlstate", None)
                )
                if sqlstate in _POSTGRESQL_BUSY_SQLSTATES:
                    return "database_busy", True
    return "internal_error", False


class BulkPinManagementService(object):
    MAX_SELECTION_IDS = 50000
    _MEMBERSHIP_ERROR_STATUS = {
        "board_not_found": 404,
        "pin_not_found": 404,
        "pin_membership_changed": 409,
    }
    _SUCCESS_STATUSES = frozenset(
        ("deleted", "updated", "moved", "unchanged")
    )
    _PRESERVED_CODES = frozenset((
        "shared_pin",
        "non_owned_pin",
        "source_membership_changed",
    ))

    def __init__(
        self,
        membership_service=None,
        clock=time.monotonic,
        board_cover_service=None,
    ):
        self.membership_service = (
            membership_service or PinMembershipService()
        )
        self.board_cover_service = (
            board_cover_service or BoardCoverService()
        )
        self.clock = clock

    def execute(self, user, request_data, started_at):
        operation = request_data["operation"]
        handlers = {
            "delete": self._delete,
            "add_to_board": self._add_to_board,
            "move_between_boards": self._move_between_boards,
            "update": self._update,
            "delete_if_exclusive_to_board": (
                self._delete_if_exclusive_to_board
            ),
        }
        try:
            return handlers[operation](user, request_data, started_at)
        except MembershipConflict as error:
            status_code = self._MEMBERSHIP_ERROR_STATUS.get(error.code)
            if status_code is None:
                raise BulkOperationError("internal_error", 500) from None
            raise BulkOperationError(error.code, status_code) from None

    def _delete(self, user, request_data, started_at):
        pin_ids = request_data["pin_ids"]
        pins = self._owned_pins(user, pin_ids)
        results = []
        deadline_expired = False
        deadline = started_at + settings.PINRY_FETCH_TOTAL_TIMEOUT
        for pin_id in pin_ids:
            if deadline_expired or self.clock() >= deadline:
                deadline_expired = True
                results.append(self._deadline_failure(pin_id))
                continue
            try:
                pins[pin_id].delete()
            except Exception as error:
                results.append(self._exception_failure(pin_id, error))
            else:
                results.append({"id": pin_id, "status": "deleted"})
        return self._summarize("delete", results)

    def _add_to_board(self, user, request_data, started_at):
        del started_at
        pin_ids = request_data["pin_ids"]
        with transaction.atomic():
            statuses = self.membership_service.add_owned_pins(
                user,
                request_data["board_id"],
                pin_ids,
            )
        results = [
            {
                "id": pin_id,
                "status": "updated" if item_status == "added"
                else item_status,
            }
            for pin_id, item_status in zip(pin_ids, statuses)
        ]
        return self._summarize("add_to_board", results)

    def _move_between_boards(self, user, request_data, started_at):
        del started_at
        pin_ids = request_data["pin_ids"]
        with transaction.atomic():
            statuses = self.membership_service.move_pins(
                user,
                request_data["source_board_id"],
                request_data["target_board_id"],
                pin_ids,
            )
        results = [
            {"id": pin_id, "status": item_status}
            for pin_id, item_status in zip(pin_ids, statuses)
        ]
        return self._summarize("move_between_boards", results)

    def _update(self, user, request_data, started_at):
        del started_at
        pin_ids = request_data["pin_ids"]
        changes = request_data["changes"]
        if "private" in changes:
            try:
                with self.board_cover_service.pin_privacy_transition(
                    user,
                    pin_ids,
                    changes["private"],
                ) as pins:
                    self._apply_locked_updates(
                        pins,
                        pin_ids,
                        changes,
                    )
            except BoardCoverError as error:
                if error.code == "board_cover_changed":
                    raise BulkOperationError(
                        "board_cover_changed",
                        409,
                    ) from None
                if error.code == "pin_not_found":
                    raise BulkOperationError("pin_not_found", 404) from None
                raise
        else:
            with transaction.atomic():
                pins = self._owned_pins(user, pin_ids, lock=True)
                self._apply_locked_updates(pins, pin_ids, changes)
        results = [
            {"id": pin_id, "status": "updated"}
            for pin_id in pin_ids
        ]
        return self._summarize("update", results)

    @staticmethod
    def _apply_locked_updates(pins, pin_ids, changes):
        for pin_id in pin_ids:
            pin = pins[pin_id]
            if "private" in changes:
                pin.private = changes["private"]
                pin.save(update_fields=("private",))
            tag_changes = changes.get("tags")
            if tag_changes is None:
                continue
            mode = tag_changes["mode"]
            values = tag_changes["values"]
            if mode == "add":
                pin.tags.add(*values)
            elif mode == "remove":
                pin.tags.remove(*values)
            else:
                pin.tags.set(values)

    def _delete_if_exclusive_to_board(
        self,
        user,
        request_data,
        started_at,
    ):
        source_board_id = request_data["source_board_id"]
        self._owned_board(user, source_board_id)
        existing_pins = {
            pin.pk: pin
            for pin in Pin.objects.filter(
                pk__in=request_data["pin_ids"],
            ).only("pk", "image_id").order_by("pk")
        }
        results = []
        deadline_expired = False
        deadline = started_at + settings.PINRY_FETCH_TOTAL_TIMEOUT
        for pin_id in request_data["pin_ids"]:
            if deadline_expired or self.clock() >= deadline:
                deadline_expired = True
                results.append(self._deadline_failure(pin_id))
                continue
            pin = existing_pins.get(pin_id)
            if pin is None:
                results.append({
                    "id": pin_id,
                    "status": "preserved",
                    "code": "source_membership_changed",
                })
                continue
            try:
                item_status, code = pin.delete_if_exclusive_to_board(
                    user.pk,
                    source_board_id,
                )
            except Exception as error:
                results.append(self._exception_failure(pin_id, error))
                continue
            if item_status == "deleted" and code is None:
                results.append({"id": pin_id, "status": "deleted"})
            elif (
                item_status == "preserved"
                and code in self._PRESERVED_CODES
            ):
                results.append({
                    "id": pin_id,
                    "status": "preserved",
                    "code": code,
                })
            else:
                results.append({
                    "id": pin_id,
                    "status": "failed",
                    "code": "internal_error",
                    "retryable": False,
                })
        return self._summarize(
            "delete_if_exclusive_to_board",
            results,
        )

    @staticmethod
    def _deadline_failure(pin_id):
        return {
            "id": pin_id,
            "status": "failed",
            "code": "bulk_deadline_exceeded",
            "retryable": True,
        }

    @staticmethod
    def _exception_failure(pin_id, error):
        code, retryable = normalize_bulk_exception(error)
        return {
            "id": pin_id,
            "status": "failed",
            "code": code,
            "retryable": retryable,
        }

    @classmethod
    def _summarize(cls, operation, results):
        return {
            "operation": operation,
            "succeeded": sum(
                item["status"] in cls._SUCCESS_STATUSES
                for item in results
            ),
            "preserved": sum(
                item["status"] == "preserved" for item in results
            ),
            "failed": sum(
                item["status"] == "failed" for item in results
            ),
            "results": results,
        }

    @staticmethod
    def _owned_pins(user, pin_ids, lock=False):
        query = Pin.objects.filter(pk__in=pin_ids).order_by("pk")
        if lock:
            query = query.select_for_update()
        pins = list(query)
        if (
            len(pins) != len(pin_ids)
            or any(pin.submitter_id != user.pk for pin in pins)
        ):
            raise BulkOperationError("pin_not_found", 404)
        return {pin.pk: pin for pin in pins}

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
