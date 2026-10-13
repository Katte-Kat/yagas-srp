"""Tags for SRP requests: what the rules give a request, and who may see what.

A rule matches a whole word (or phrase) in the mail subject or text, case-insensitively, or the ship group of the loss. A request gets the tags of every rule that matches; tags are only
ever added here. Access: a member of a viewer group of an active tag sees the requests carrying it, SRP managers (the `view_requests` permission) see everything."""
import re

from .models import SrpRequest, SrpTag, SrpTagRule


def _word(text):
    return re.compile(r"(?<!\w)" + re.escape(" ".join((text or "").lower().split())) + r"(?!\w)")


def ship_group(ship_type_id):
    """The ship group name of a type id from the game's static data (None when it cannot be told)."""
    if not ship_type_id:
        return None
    try:
        from eve_sde.models import ItemType

        item = ItemType.objects.filter(pk=ship_type_id).select_related("group").first()
        return (item.group.name or None) if item is not None and item.group_id else None
    except Exception:                                                  # no static data: the hull rules simply do not match
        return None


def matching_tags(subject, body, group, rules=None):
    """The tags (SrpTag objects, active ones only) that the active rules give to a mail with this subject and text lost in a ship of this group."""
    subject, body, group = (" ".join((text or "").lower().split()) for text in (subject, body, group))
    found = {}
    rules = rules if rules is not None else SrpTagRule.objects.filter(is_active=True).prefetch_related("tags")
    for rule in rules:
        if not rule.is_active or not rule.keyword.strip():
            continue
        if rule.kind == SrpTagRule.Kind.SUBJECT:
            hit = bool(subject) and bool(_word(rule.keyword).search(subject))
        elif rule.kind == SrpTagRule.Kind.BODY:
            hit = bool(body) and bool(_word(rule.keyword).search(body))
        else:
            hit = bool(group) and group == " ".join(rule.keyword.lower().split())
        if hit:
            for tag in rule.tags.all():
                if tag.is_active:
                    found[tag.pk] = tag
    return sorted(found.values(), key=lambda t: (t.order, t.name))


def apply_rules(srp, rules=None, group=None):
    """Give the request the tags its subject, text and ship call for. Returns the tags that were new to it. A request whose tags somebody edited by hand is left alone."""
    if srp.tags_edited:
        return []
    group = group if group is not None else ship_group(srp.ship_type_id)
    wanted = matching_tags(srp.subject, srp.body, group, rules)
    have = set(srp.tags.values_list("pk", flat=True))
    new = [tag for tag in wanted if tag.pk not in have]
    if new:
        srp.tags.add(*new)
    return new


def retag_all(force=False, dry_run=False):
    """Apply the rules to every request. Returns (requests that got something, tags given). `force` also touches the requests whose tags were edited by hand."""
    rules = list(SrpTagRule.objects.filter(is_active=True).prefetch_related("tags"))
    touched = given = 0
    groups = {}
    queryset = SrpRequest.objects.all() if force else SrpRequest.objects.filter(tags_edited=False)
    for srp in queryset.iterator():
        if srp.ship_type_id not in groups:
            groups[srp.ship_type_id] = ship_group(srp.ship_type_id) or ""
        wanted = matching_tags(srp.subject, srp.body, groups[srp.ship_type_id], rules)
        have = set(srp.tags.values_list("pk", flat=True))
        new = [tag for tag in wanted if tag.pk not in have]
        if new:
            touched += 1
            given += len(new)
            if not dry_run:
                srp.tags.add(*new)
    return touched, given


# ---- who sees what

def viewer_tag_ids(user):
    """The ids of the active tags this user may look at through a viewer group (empty for anonymous users)."""
    if not getattr(user, "is_authenticated", False):
        return []
    return list(SrpTag.objects.filter(is_active=True, viewer_groups__in=user.groups.all()).values_list("pk", flat=True).distinct())


def is_full_viewer(user):
    return user.has_perm("yagas_srp.view_requests")


def visible_requests(user):
    """The requests this user may look at: all of them for the SRP managers and directors, those with one of their tags for a viewer group member, else none."""
    if is_full_viewer(user):
        return SrpRequest.objects.all()
    ids = viewer_tag_ids(user)
    if not ids:
        return SrpRequest.objects.none()
    return SrpRequest.objects.filter(pk__in=SrpRequest.objects.filter(tags__in=ids).values("pk"))        # a subquery: a request with two of the tags is still one row


def may_open_inbox(user):
    return getattr(user, "is_authenticated", False) and (is_full_viewer(user) or bool(viewer_tag_ids(user)))


def tags_for_filter(user):
    """The tags this user may filter by: every active tag for a full viewer, the viewer-group ones for the others."""
    if is_full_viewer(user):
        return list(SrpTag.objects.filter(is_active=True))
    return list(SrpTag.objects.filter(pk__in=viewer_tag_ids(user)))
