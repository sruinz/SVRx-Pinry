from django.db.models import Count, Q
from rest_framework.filters import BaseFilterBackend


class AllTagsFilter(BaseFilterBackend):
    parameter_name = "tags__name"

    def filter_queryset(self, request, queryset, view):
        del view
        tag_names = list(dict.fromkeys(
            tag_name
            for tag_name in request.query_params.getlist(self.parameter_name)
            if tag_name != ""
        ))
        if tag_names:
            return queryset.annotate(
                _matched_tag_count=Count(
                    "tags",
                    filter=Q(tags__name__in=tag_names),
                    distinct=True,
                )
            ).filter(_matched_tag_count=len(tag_names))
        return queryset
