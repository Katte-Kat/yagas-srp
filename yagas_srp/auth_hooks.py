from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


class SrpMenuItem(MenuItemHook):
    """Only users who may view the SRP inbox see the entry; badge = requests waiting for a decision."""

    def __init__(self):
        MenuItemHook.__init__(
            self, _("SRP Inbox"), "fa-solid fa-life-ring fa-fw", "yagas_srp:request_list",
            navactive=["yagas_srp:"])

    def render(self, request):
        if not request.user.has_perm("yagas_srp.view_requests"):
            return ""
        from .models import SrpRequest

        self.count = SrpRequest.objects.filter(status=SrpRequest.Status.PENDING).count() or None
        return MenuItemHook.render(self, request)


@hooks.register("menu_item_hook")
def register_menu():
    return SrpMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "yagas_srp", r"^srp-inbox/")
