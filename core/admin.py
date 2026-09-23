from django.contrib import admin
from django.contrib import messages
from django.contrib.admin import helpers
from django.db.models import Count
from django.db import transaction
from django.template.response import TemplateResponse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy

from taggit.models import Tag

from .models import Pin, Board
from .services.tag_management import TagMergeError, merge_tags
from users.models import User


class PinAdmin(admin.ModelAdmin):
    pass


class BoardAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        if change:
            return super(BoardAdmin, self).save_model(
                request,
                obj,
                form,
                change,
            )
        with transaction.atomic():
            obj.submitter = (
                User.objects.select_for_update().get(pk=obj.submitter_id)
            )
            return super(BoardAdmin, self).save_model(
                request,
                obj,
                form,
                change,
            )


try:
    admin.site.unregister(Tag)
except admin.sites.NotRegistered:
    pass


@admin.register(Tag)
class SVRxTagAdmin(admin.ModelAdmin):
    actions = ("merge_selected_tags",)
    list_display = ("name", "pin_usage_count", "slug")
    ordering = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)

    def get_queryset(self, request):
        return super(SVRxTagAdmin, self).get_queryset(request).annotate(
            pin_usage_total=Count("taggit_taggeditem_items"),
        )

    def pin_usage_count(self, obj):
        return obj.pin_usage_total

    pin_usage_count.admin_order_field = "pin_usage_total"
    pin_usage_count.short_description = gettext_lazy("핀 수")

    def get_actions(self, request):
        actions = super(SVRxTagAdmin, self).get_actions(request)
        if not (
            self.has_change_permission(request)
            and self.has_delete_permission(request)
        ):
            actions.pop("merge_selected_tags", None)
        return actions

    def merge_selected_tags(self, request, queryset):
        selected_tags = list(queryset.order_by("name", "pk"))
        if len(selected_tags) < 2:
            self.message_user(
                request,
                gettext("최소 두 개의 태그를 선택하세요."),
                level=messages.ERROR,
            )
            return None

        if "apply" not in request.POST:
            context = {
                **self.admin_site.each_context(request),
                "title": gettext("태그 병합"),
                "opts": self.model._meta,
                "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
                "action_name": "merge_selected_tags",
                "selected_tags": selected_tags,
            }
            return TemplateResponse(
                request,
                "admin/taggit/tag/merge_confirmation.html",
                context,
            )

        try:
            target_tag_id = int(request.POST.get("target_tag_id", ""))
        except (TypeError, ValueError):
            self.message_user(
                request,
                gettext("대표 태그를 선택하세요."),
                level=messages.ERROR,
            )
            return None

        try:
            result = merge_tags(
                target_tag_id,
                [tag.pk for tag in selected_tags],
            )
        except TagMergeError as error:
            self.message_user(request, str(error), level=messages.ERROR)
            return None

        self.message_user(
            request,
            gettext("{}개 태그를 {} 태그로 병합했습니다.").format(
                result.selected_count,
                result.target_name,
            ),
            level=messages.SUCCESS,
        )
        return None

    merge_selected_tags.allowed_permissions = ("change",)
    merge_selected_tags.short_description = gettext_lazy("선택한 태그 병합")


admin.site.register(Pin, PinAdmin)
admin.site.register(Board, BoardAdmin)
