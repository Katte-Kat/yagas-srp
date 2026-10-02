import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from esi.decorators import token_required

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import MailReceiver, SrpRequest
from .services import MAIL_SCOPE, reprice_request
from .tasks import poll_mailbox

logger = logging.getLogger(__name__)

SLOT_ORDER = [
    ("high", "High slots"), ("mid", "Mid slots"), ("low", "Low slots"), ("rig", "Rigs"),
    ("subsystem", "Subsystems"), ("drone", "Drones"), ("fighter", "Fighters"),
]
NOT_COUNTED = [("cargo", "Cargo"), ("implant", "Implants"), ("other", "Other holds")]


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

    counts = dict(SrpRequest.objects.values_list("status").annotate(n=Count("id")))
    tabs = [
        {"value": value, "label": label, "count": counts.get(value, 0), "active": value == status}
        for value, label in SrpRequest.Status.choices
    ]
    page = Paginator(SrpRequest.objects.filter(status=status), 50).get_page(request.GET.get("page"))

    return render(request, "yagas_srp/request_list.html", {
        "tabs": tabs,
        "status": status,
        "page": page,
        "receivers": MailReceiver.objects.all(),
        "can_manage": request.user.has_perm("yagas_srp.manage_receiver"),
        "can_poll": _can_poll(request.user),
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
        if srp.status in (SrpRequest.Status.APPROVED, SrpRequest.Status.DENIED):
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
    return redirect("yagas_srp:request_detail", pk=pk)


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
