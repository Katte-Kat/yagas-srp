from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


class SrpMenuItem(MenuItemHook):
    """Only users who may view the SRP inbox (or the requests of a tag their group may look at) see the entry; badge = their requests waiting for a decision."""

    def __init__(self):
        MenuItemHook.__init__(
            self, _("SRP Inbox"), "fa-solid fa-life-ring fa-fw", "yagas_srp:request_list",
            navactive=["yagas_srp:"])

    def render(self, request):
        from . import tagging

        if not tagging.may_open_inbox(request.user):
            return ""
        from .models import SrpRequest

        self.count = tagging.visible_requests(request.user).filter(status=SrpRequest.Status.PENDING).count() or None
        return MenuItemHook.render(self, request)


@hooks.register("menu_item_hook")
def register_menu():
    return SrpMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "yagas_srp", r"^srp-inbox/")
