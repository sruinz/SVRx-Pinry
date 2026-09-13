import re
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.filters import BaseFilterBackend


class PinSearchFilter(BaseFilterBackend):
    def filter_queryset(self, request, queryset, view):
        del view
        values = {}
        errors = {}
        choices = {
            "animation": ("static", "animated"),
            "aspect": ("landscape", "portrait", "square"),
        }
        for key in (*choices, "min_width", "min_height", "date_from", "date_to"):
            if key not in request.query_params:
                continue
            raw = request.query_params.getlist(key)
            if len(raw) != 1:
                errors[key] = "invalid"
                continue
            value = raw[0]
            try:
                if key in choices:
                    if value not in choices[key]:
                        raise ValueError
                elif key.startswith("min_"):
                    if not re.fullmatch(r"[0-9]{1,10}", value):
                        raise ValueError
                    value = int(value)
                    if not 1 <= value <= 2147483647:
                        raise ValueError
                else:
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                        raise ValueError
                    value = date.fromisoformat(value)
                    # UTC 변환 및 종료일 다음 날 계산의 경계 넘침을 피한다.
                    if not 2 <= value.year <= 9998:
                        raise ValueError
                values[key] = value
            except ValueError:
                errors[key] = "invalid"
        if values.get("date_from") and values.get("date_to"):
            if values["date_from"] > values["date_to"]:
                errors["date_to"] = "before_start"
        if errors:
            raise ValidationError({"code": "pin_search_invalid", "fields": errors})

        animation = values.get("animation")
        if animation == "animated":
            queryset = queryset.filter(image__animation_status__in=("gif", "webp"))
        elif animation == "static":
            # 기존 판별 규칙처럼 GIF·WebP 외 저장 파일은 파일을 열지 않고 정지로 취급한다.
            legacy_static = (
                Q(image__animation_status__isnull=True)
                & ~Q(image__image="")
                & ~Q(image__image__iendswith=".gif")
                & ~Q(image__image__iendswith=".webp")
            )
            queryset = queryset.filter(Q(image__animation_status="static") | legacy_static)
        aspect = values.get("aspect")
        if aspect:
            queryset = queryset.filter(image__width__gt=0, image__height__gt=0)
            lookup = {"landscape": "gt", "portrait": "lt", "square": "exact"}[aspect]
            queryset = queryset.filter(**{"image__width__" + lookup: F("image__height")})
        for key in ("min_width", "min_height"):
            if key in values:
                queryset = queryset.filter(**{"image__" + key[4:] + "__gte": values[key]})
        for key, lookup in (("date_from", "gte"), ("date_to", "lt")):
            if key not in values:
                continue
            day = values[key] + (timedelta(days=1) if key == "date_to" else timedelta())
            boundary = datetime.combine(day, time.min)
            if settings.USE_TZ:
                boundary = timezone.make_aware(boundary, timezone.get_default_timezone())
            queryset = queryset.filter(**{"published__" + lookup: boundary})
        return queryset
