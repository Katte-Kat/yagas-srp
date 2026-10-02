from django.contrib import admin

from .models import MailReceiver, SrpRequest, SrpRequestItem


class SrpRequestItemInline(admin.TabularInline):
    model = SrpRequestItem
    extra = 0
    can_delete = False
    readonly_fields = ("type_id", "type_name", "flag", "slot_group", "quantity_destroyed",
                       "quantity_dropped", "unit_price", "value", "counted")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SrpRequest)
class SrpRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "received_at", "sender_name", "ship_type_name", "total_value", "status", "reviewed_by")
    list_filter = ("status",)
    search_fields = ("sender_name", "victim_name", "ship_type_name", "subject")
    inlines = [SrpRequestItemInline]


@admin.register(MailReceiver)
class MailReceiverAdmin(admin.ModelAdmin):
    list_display = ("character_name", "is_active", "last_polled_at", "last_error")
    readonly_fields = ("character_id", "character_name", "last_polled_at", "last_error", "added_by")
