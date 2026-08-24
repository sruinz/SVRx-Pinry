import re
from collections import namedtuple

from django.db.models import BigIntegerField, ExpressionWrapper, F, Value
from django.db.models.functions import Cast
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter

from core.pin_sorting import MAX_RANDOM_SEED, PRIME, random_coefficients


BOARD_SORT_ERROR = {"code": "board_sort_invalid"}
BoardSort = namedtuple("BoardSort", ("mode", "random_seed"))
_SEED_PATTERN = re.compile(r"\A(?:0|[1-9][0-9]{0,9})\Z")


def _invalid():
    raise ValidationError(BOARD_SORT_ERROR)


def parse_board_sort(query_params):
    sorts = query_params.getlist("sort")
    seeds = query_params.getlist("random_seed")
    usernames = query_params.getlist("submitter__username")
    if len(sorts) > 1 or len(seeds) > 1 or len(usernames) > 1:
        _invalid()
    if not sorts:
        if seeds:
            _invalid()
        return None
    if query_params.getlist("ordering"):
        _invalid()
    if len(usernames) != 1 or not usernames[0]:
        _invalid()
    mode = sorts[0]
    if mode not in ("custom", "latest", "oldest", "random"):
        _invalid()
    if mode != "random":
        if seeds:
            _invalid()
        return BoardSort(mode, None)
    if len(seeds) != 1 or not _SEED_PATTERN.match(seeds[0]):
        _invalid()
    seed = int(seeds[0])
    if seed > MAX_RANDOM_SEED:
        _invalid()
    return BoardSort(mode, seed)


def apply_board_sort(queryset, board_sort):
    if board_sort.mode == "custom":
        return queryset.order_by("display_order", "-id")
    if board_sort.mode == "latest":
        return queryset.order_by("-published", "-id")
    if board_sort.mode == "oldest":
        return queryset.order_by("published", "id")
    multiplier, increment = random_coefficients(board_sort.random_seed)
    output = BigIntegerField()
    id_mod = ExpressionWrapper(
        Cast(F("id"), output_field=output) % Value(PRIME),
        output_field=output,
    )
    key = ExpressionWrapper(
        ((id_mod * Value(multiplier)) + Value(increment)) % Value(PRIME),
        output_field=output,
    )
    return queryset.annotate(_board_random_key=key).order_by(
        "_board_random_key", "id"
    )


class BoardSortFilter(OrderingFilter):
    def filter_queryset(self, request, queryset, view):
        board_sort = parse_board_sort(request.query_params)
        if board_sort is None:
            return super(BoardSortFilter, self).filter_queryset(
                request, queryset, view
            )
        return apply_board_sort(queryset, board_sort)
