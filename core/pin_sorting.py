import re
from collections import namedtuple

from django.db.models import BigIntegerField, ExpressionWrapper, F, Value
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter


PIN_SORT_ERROR = {"code": "pin_sort_invalid"}
MAX_RANDOM_SEED = 2147483646
PRIME = 2147483647
PinSort = namedtuple("PinSort", ("mode", "random_seed"))
_SEED_PATTERN = re.compile(r"\A(?:0|[1-9][0-9]{0,9})\Z")


def _invalid():
    raise ValidationError(PIN_SORT_ERROR)


def parse_pin_sort(query_params):
    sorts = query_params.getlist("sort")
    seeds = query_params.getlist("random_seed")
    if len(sorts) > 1 or len(seeds) > 1:
        _invalid()
    if not sorts:
        if seeds:
            _invalid()
        return None
    if query_params.getlist("ordering"):
        _invalid()
    mode = sorts[0]
    if mode not in ("latest", "oldest", "random"):
        _invalid()
    if mode != "random":
        if seeds:
            _invalid()
        return PinSort(mode, None)
    if len(seeds) != 1 or not _SEED_PATTERN.match(seeds[0]):
        _invalid()
    seed = int(seeds[0])
    if seed > MAX_RANDOM_SEED:
        _invalid()
    return PinSort(mode, seed)


def random_coefficients(seed):
    multiplier = 1 + ((seed * 1103515245 + 12345) % (PRIME - 1))
    increment = (seed * 1664525 + 1013904223) % PRIME
    return multiplier, increment


def apply_pin_sort(queryset, pin_sort):
    if pin_sort.mode == "latest":
        return queryset.order_by("-published", "-id")
    if pin_sort.mode == "oldest":
        return queryset.order_by("published", "id")
    multiplier, increment = random_coefficients(pin_sort.random_seed)
    output = BigIntegerField()
    id_mod = ExpressionWrapper(F("id") % Value(PRIME), output_field=output)
    key = ExpressionWrapper(
        ((id_mod * Value(multiplier)) + Value(increment)) % Value(PRIME),
        output_field=output,
    )
    return queryset.annotate(_pin_random_key=key).order_by(
        "_pin_random_key", "id"
    )


class PinSortFilter(OrderingFilter):
    def filter_queryset(self, request, queryset, view):
        pin_sort = parse_pin_sort(request.query_params)
        if pin_sort is None:
            return super(PinSortFilter, self).filter_queryset(
                request, queryset, view
            )
        return apply_pin_sort(queryset, pin_sort)
