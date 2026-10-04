import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from esi.decorators import token_required

from django.contrib import messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth.models import Group, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.decorators import login_required, permission_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Case, Count, DecimalField, F, Func, IntegerField, Sum, Value, When
from django.db.models.functions import Coalesce, NullIf
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import MailReceiver, SrpRequest
from .roles import ROLE, ROLE_SUMMARY, display_name, ensure_role, find_account
from .services import MAIL_SCOPE, reprice_request
from .tasks import poll_mailbox

logger = logging.getLogger(__name__)

SLOT_ORDER = [
    ("high", "High slots"), ("mid", "Mid slots"), ("low", "Low slots"), ("rig", "Rigs"),
    ("subsystem", "Subsystems"), ("drone", "Drones"), ("fighter", "Fighters"),
]
NOT_COUNTED = [("cargo", "Cargo"), ("implant", "Implants"), ("other", "Other holds")]

# Sortable inbox columns: key -> (label, ordering expression, direction on first click, right-aligned)
INBOX_COLUMNS = [
    ("id", "#", F("id"), "desc", False),
    ("received", "Received", F("received_at"), "desc", False),
    # shown as the victim, or the mail sender when the killmail could not be read
    ("pilot", "Pilot", Coalesce(NullIf(F("victim_name"), Value("")), F("sender_name")), "asc", False),
    ("ship", "Ship", F("ship_type_name"), "asc", False),
    ("system", "System", F("solar_system_name"), "asc", False),
    ("total", "Total (ISK)", F("total_value"), "desc", True),
    ("flags", "Flags", Func(F("validation_flags"), function="JSON_LENGTH", output_field=IntegerField()), "desc", False),
]
DEFAULT_SORT = ("received", "desc")


def _inbox_sort(request):
    """(key, direction) from ?sort=&dir=, falling back to newest first for anything unknown."""
    by_key = {col[0]: col for col in INBOX_COLUMNS}
    key = request.GET.get("sort")
    if key not in by_key:
        return DEFAULT_SORT
    direction = request.GET.get("dir")
    if direction not in ("asc", "desc"):
        direction = by_key[key][3]
    return key, direction


def _can_poll(user):
    """Everyone who decides on requests (all Directors) may pull the mailbox; receiver
    managers may too. Which character is polled stays a manage_receiver decision."""
    return user.has_perm("yagas_srp.approve_requests") or user.has_perm("yagas_srp.manage_receiver")


@login_required
@permission_required("yagas_srp.view_requests")
def request_list(request):
    status = request.GET.get("status") or SrpRequest.Status.PENDING
    if status not in SrpRequest.Status.values:
        status = SrpRequest.Status.PENDING

    # One grouped query for every tab: how many requests and what they are worth together
    # (the approved amount for approved requests, the computed hull + fit value otherwise).
    worth = Case(
        When(status=SrpRequest.Status.APPROVED, approved_amount__isnull=False, then=F("approved_amount")),
        default=F("total_value"), output_field=DecimalField(max_digits=22, decimal_places=0))
    per_status = {
        row["status"]: row
        for row in SrpRequest.objects.values("status").annotate(n=Count("id"), total=Sum(worth))
    }
    tabs = [
        {"value": value, "label": label, "count": per_status.get(value, {}).get("n", 0),
         "total": per_status.get(value, {}).get("total") or 0, "active": value == status}
        for value, label in SrpRequest.Status.choices
    ]
    current_tab = next(tab for tab in tabs if tab["active"])
    sort, direction = _inbox_sort(request)
    ordering = next(col[2] for col in INBOX_COLUMNS if col[0] == sort)
    ordering = ordering.asc() if direction == "asc" else ordering.desc()
    # "-id" keeps the order of equal values stable between pages
    requests_qs = SrpRequest.objects.filter(status=status).order_by(ordering, "-id")
    page = Paginator(requests_qs, 50).get_page(request.GET.get("page"))
    columns = []
    for key, label, _expr, first_direction, right in INBOX_COLUMNS:
        active = key == sort
        next_direction = ("asc" if direction == "desc" else "desc") if active else first_direction
        columns.append({
            "label": label, "right": right, "active": active, "direction": direction,
            "query": f"status={status}&sort={key}&dir={next_direction}",
        })

    return render(request, "yagas_srp/request_list.html", {
        "tabs": tabs,
        "status": status,
        "page": page,
        "columns": columns,
        "current_tab": current_tab,
        "show_total": status != SrpRequest.Status.INVALID,   # invalid mails have no value
        "sort_query": f"sort={sort}&dir={direction}",
        "receivers": MailReceiver.objects.all(),
        "can_manage": request.user.has_perm("yagas_srp.manage_receiver"),
        "can_poll": _can_poll(request.user),
        "can_approve": request.user.has_perm("yagas_srp.approve_requests"),
        "can_assign_managers": request.user.has_perm("yagas_srp.assign_srp_managers"),
        "show_actions": status == SrpRequest.Status.PENDING and request.user.has_perm("yagas_srp.approve_requests"),
        "list_url": request.get_full_path(),
    })


@login_required
@permission_required("yagas_srp.view_requests")
def request_detail(request, pk):
    srp = get_object_or_404(SrpRequest.objects.select_related("reviewed_by", "receiver"), pk=pk)
    items = list(srp.items.all())
    counted = [
        {"label": label, "items": [i for i in items if i.slot_group == key]}
        for key, label in SLOT_ORDER
    ]
    not_counted = [
        {"label": label, "items": [i for i in items if i.slot_group == key]}
        for key, label in NOT_COUNTED
    ]
    return render(request, "yagas_srp/request_detail.html", {
        "srp": srp,
        "counted_groups": [g for g in counted if g["items"]],
        "other_groups": [g for g in not_counted if g["items"]],
        "can_approve": request.user.has_perm("yagas_srp.approve_requests"),
    })


def _back(request, pk):
    """Back to where the decision was made (the inbox list sends its own URL), else the detail page."""
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect("yagas_srp:request_detail", pk=pk)


@login_required
@permission_required("yagas_srp.approve_requests")
@require_POST
def review_request(request, pk):
    srp = get_object_or_404(SrpRequest, pk=pk)
    action = request.POST.get("action")
    note = (request.POST.get("note") or "").strip()
    pending = SrpRequest.objects.filter(pk=pk, status=SrpRequest.Status.PENDING)
    review = {"reviewed_by": request.user, "reviewed_at": timezone.now(), "review_note": note}

    if action == "reopen":
        if srp.status in (SrpRequest.Status.APPROVED, SrpRequest.Status.DENIED, SrpRequest.Status.ARCHIVED):
            srp.status = SrpRequest.Status.PENDING
            srp.reviewed_by = None
            srp.reviewed_at = None
            srp.approved_amount = None
            srp.save(update_fields=["status", "reviewed_by", "reviewed_at", "approved_amount"])
            messages.success(request, f"Request #{pk} is pending again.")
    elif action == "approve":
        try:
            amount = Decimal(request.POST.get("amount") or srp.total_value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if amount < 0 or amount > Decimal("1e15"):
                raise InvalidOperation
        except InvalidOperation:
            messages.error(request, "The payout amount is not a valid number.")
            return redirect("yagas_srp:request_detail", pk=pk)
        # Conditional update: two directors clicking at once can't both decide.
        if pending.update(status=SrpRequest.Status.APPROVED, approved_amount=amount, **review):
            messages.success(request, f"Request #{pk} approved for {amount:,.0f} ISK.")
        else:
            messages.warning(request, f"Request #{pk} was already decided.")
    elif action == "deny":
        if pending.update(status=SrpRequest.Status.DENIED, approved_amount=None, **review):
            messages.success(request, f"Request #{pk} denied.")
        else:
            messages.warning(request, f"Request #{pk} was already decided.")
    return _back(request, pk)


@login_required
@permission_required("yagas_srp.approve_requests")
@require_POST
def reprice(request, pk):
    srp = get_object_or_404(SrpRequest, pk=pk)
    if srp.status != SrpRequest.Status.PENDING or not srp.killmail_id:
        messages.warning(request, "Only pending requests with a killmail can be repriced.")
        return redirect("yagas_srp:request_detail", pk=pk)
    try:
        reprice_request(srp)
    except Exception:
        logger.exception("yagas_srp: repricing #%s failed", pk)
        messages.error(request, "Could not reach the price source (Fuzzwork/ESI). Nothing was changed, try again later.")
    else:
        messages.success(request, f"Request #{pk} repriced at the current Jita split: {srp.total_value:,.0f} ISK.")
    return redirect("yagas_srp:request_detail", pk=pk)


@login_required
@permission_required("yagas_srp.view_requests")
@user_passes_test(_can_poll)
@require_POST
def poll_now(request):
    poll_mailbox.delay()
    messages.success(request, "Mailbox poll queued -- new requests appear within a few seconds.")
    return redirect("yagas_srp:request_list")


def _eligible_characters(user):
    """Characters that already authorized the mail scope in Auth and could become the receiver.

    A receiver's inbox gets read, so managers may only pick their own characters;
    superusers (who can see every stored token anyway) may pick any.
    """
    from esi.models import Token

    tokens = Token.objects.all() if user.is_superuser else Token.objects.filter(user=user)
    tokens = tokens.require_scopes([MAIL_SCOPE]).exclude(refresh_token__isnull=True)
    chosen = set(MailReceiver.objects.values_list("character_id", flat=True))
    found = {}
    for character_id, name, owner in tokens.values_list("character_id", "character_name", "user__username"):
        if character_id not in chosen:
            found.setdefault(character_id, {"character_id": character_id, "name": name, "owner": owner})
    return sorted(found.values(), key=lambda row: row["name"].lower())


@login_required
@permission_required("yagas_srp.manage_receiver")
def receivers(request):
    return render(request, "yagas_srp/receivers.html", {
        "receivers": MailReceiver.objects.select_related("added_by"),
        "candidates": _eligible_characters(request.user),
        "show_owner": request.user.is_superuser,
    })


@login_required
@permission_required("yagas_srp.manage_receiver")
@require_POST
def select_receiver(request):
    try:
        character_id = int(request.POST.get("character_id", ""))
    except ValueError:
        character_id = None
    # Never trust the posted id: it has to be one of the characters offered on the page.
    choice = next((c for c in _eligible_characters(request.user) if c["character_id"] == character_id), None)
    if choice is None:
        messages.error(request, "That character is not available as a receiver.")
    else:
        MailReceiver.objects.update_or_create(
            character_id=choice["character_id"],
            defaults={"character_name": choice["name"], "is_active": True, "added_by": request.user,
                      "last_error": ""},
        )
        messages.success(request, f"{choice['name']} is now an SRP mail receiver.")
    return redirect("yagas_srp:receivers")


@login_required
@permission_required("yagas_srp.manage_receiver")
@require_POST
def update_receiver(request, pk):
    receiver = get_object_or_404(MailReceiver, pk=pk)
    action = request.POST.get("action")
    if action == "remove":
        receiver.delete()  # requests already collected stay (FK is SET_NULL)
        messages.success(request, f"{receiver.character_name} removed; collected requests were kept.")
    elif action in ("activate", "deactivate"):
        receiver.is_active = action == "activate"
        receiver.save(update_fields=["is_active"])
        messages.success(
            request, f"{receiver.character_name} is now {'active' if receiver.is_active else 'inactive'}.")
    return redirect("yagas_srp:receivers")


@login_required
@permission_required("yagas_srp.manage_receiver")
@token_required(scopes=[MAIL_SCOPE], new=True)
def add_receiver(request, token):
    """The character logging in here becomes the mailbox that gets polled."""
    MailReceiver.objects.update_or_create(
        character_id=token.character_id,
        defaults={"character_name": token.character_name, "is_active": True, "added_by": request.user},
    )
    messages.success(request, f"{token.character_name} is now an SRP mail receiver.")
    return redirect("yagas_srp:receivers")


def _audit(request, group, message):
    """Role changes show up in the Django admin log of the group, with who did it."""
    LogEntry.objects.create(
        user=request.user, content_type=ContentType.objects.get_for_model(Group), object_id=str(group.pk),
        object_repr=str(group)[:200], action_flag=CHANGE, change_message=message)


@login_required
@permission_required("yagas_srp.assign_srp_managers")
def managers(request):
    group = Group.objects.filter(name=ROLE).first()
    members = []
    if group is not None:
        for user in group.user_set.filter(is_active=True).select_related("profile__main_character"):
            main = user.profile.main_character
            members.append({
                "user_id": user.pk, "name": display_name(user), "username": user.username,
                "character_id": main.character_id if main else None,
            })
        members.sort(key=lambda member: member["name"].lower())
    names = User.objects.filter(is_active=True, profile__main_character__isnull=False).values_list(
        "profile__main_character__character_name", flat=True).order_by("profile__main_character__character_name")
    return render(request, "yagas_srp/managers.html", {
        "role": ROLE, "summary": ROLE_SUMMARY, "members": members, "character_names": list(names),
    })


@login_required
@permission_required("yagas_srp.assign_srp_managers")
@require_POST
def manager_add(request):
    user, error = find_account(request.POST.get("character"))
    if error:
        messages.error(request, error)
    else:
        group = ensure_role()
        if group.user_set.filter(pk=user.pk).exists():
            messages.info(request, f"{display_name(user)} already is {ROLE}.")
        else:
            user.groups.add(group)
            _audit(request, group, f"Added {user.username} ({display_name(user)}) via the SRP inbox")
            messages.success(request, f"{display_name(user)} is now {ROLE}.")
    return redirect("yagas_srp:managers")


@login_required
@permission_required("yagas_srp.assign_srp_managers")
@require_POST
def manager_remove(request):
    group = Group.objects.filter(name=ROLE).first()
    user = User.objects.filter(pk=request.POST.get("user_id") or 0).first()
    if group is None or user is None or not group.user_set.filter(pk=user.pk).exists():
        messages.error(request, "That person does not have this role.")
    else:
        user.groups.remove(group)
        _audit(request, group, f"Removed {user.username} ({display_name(user)}) via the SRP inbox")
        messages.success(request, f"{display_name(user)} no longer is {ROLE}.")
    return redirect("yagas_srp:managers")
