from django.contrib import admin

from django.contrib import messages

from .models import MailReceiver, SrpRequest, SrpRequestItem, SrpTag, SrpTagRule


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
    list_filter = ("status", "tags")
    search_fields = ("sender_name", "victim_name", "ship_type_name", "subject")
    filter_horizontal = ("tags",)
    inlines = [SrpRequestItemInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if change and "tags" in form.changed_data:                                  # tags changed by hand stay: the rules do not touch this request again
            SrpRequest.objects.filter(pk=form.instance.pk).update(tags_edited=True)


@admin.register(SrpTag)
class SrpTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "color", "is_active", "order", "viewer_group_names", "request_count")
    list_editable = ("is_active", "order")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("viewer_groups",)

    @admin.display(description="Viewer groups")
    def viewer_group_names(self, obj):
        return ", ".join(g.name for g in obj.viewer_groups.all()) or "-"

    @admin.display(description="Requests")
    def request_count(self, obj):
        return obj.requests.count()


@admin.register(SrpTagRule)
class SrpTagRuleAdmin(admin.ModelAdmin):
    list_display = ("kind", "keyword", "tag_names", "is_active", "note")
    list_filter = ("kind", "is_active")
    search_fields = ("keyword", "note")
    filter_horizontal = ("tags",)
    actions = ["apply_to_all_requests"]

    @admin.display(description="Gives the tags")
    def tag_names(self, obj):
        return ", ".join(t.name for t in obj.tags.all())

    @admin.action(description="Apply all active rules to every request now (tags are only added)")
    def apply_to_all_requests(self, request, queryset):
        from .tagging import retag_all

        touched, given = retag_all()
        self.message_user(request, f"{touched} request(s) got {given} new tag(s).", messages.SUCCESS)


@admin.register(MailReceiver)
class MailReceiverAdmin(admin.ModelAdmin):
    list_display = ("character_name", "is_active", "last_polled_at", "last_error")
    readonly_fields = ("character_id", "character_name", "last_polled_at", "last_error", "added_by")
