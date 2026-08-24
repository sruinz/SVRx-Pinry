from django.contrib import admin
from django.db import transaction

from .models import Pin, Board
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


admin.site.register(Pin, PinAdmin)
admin.site.register(Board, BoardAdmin)
