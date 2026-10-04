"""The SRP Manager role (a plain Auth group) and the helpers to hand it out.

"Look and use, change nothing": the role sees every request and approves or denies it, but cannot change
the mail receiver or hand out roles.
"""
from django.contrib.auth.models import Group, Permission, User

from allianceauth.authentication.models import CharacterOwnership

ROLE = "SRP Manager"
ROLE_SUMMARY = (
    "Sees every request in the SRP inbox and approves or denies it. Cannot change the mail receiver or "
    "hand out this role."
)
ROLE_PERMISSIONS = ["yagas_srp.view_requests", "yagas_srp.approve_requests"]


def ensure_role() -> Group:
    """The role's group, created when missing. Missing permissions are ADDED, none is ever removed and
    the members are not touched, so this is safe to call at any time."""
    group, _created = Group.objects.get_or_create(name=ROLE)
    have = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}
    for code in ROLE_PERMISSIONS:
        if code not in have:
            app, codename = code.split(".")
            permission = Permission.objects.filter(content_type__app_label=app, codename=codename).first()
            if permission is not None:
                group.permissions.add(permission)
    return group


def find_account(text):
    """The Auth account behind a typed character name (any registered character) or account name.
    Returns (user, error message)."""
    text = " ".join((text or "").split())
    if not text:
        return None, "Enter the name of a character."
    owned = CharacterOwnership.objects.filter(character__character_name__iexact=text).select_related("user").first()
    user = owned.user if owned else User.objects.filter(username__iexact=text).first()
    if user is None:
        return None, f'No Auth account has a character called "{text}". The pilot has to log in to Auth once first.'
    if not user.is_active:
        return None, f'The account behind "{text}" is disabled.'
    return user, None


def display_name(user):
    main = getattr(user.profile, "main_character", None)
    return main.character_name if main else user.username


def search_accounts(text, limit=10):
    """Registered characters whose name contains `text` (two letters minimum), as suggestions for the
    "add" box: [{"name": character, "detail": which account it belongs to}]."""
    text = " ".join((text or "").split())
    if len(text) < 2:
        return []
    owned = (
        CharacterOwnership.objects.filter(character__character_name__icontains=text, user__is_active=True)
        .select_related("character", "user__profile__main_character")
        .order_by("character__character_name")[:limit]
    )
    results = []
    for row in owned:
        main = getattr(row.user.profile, "main_character", None)
        if main is not None and main.character_id != row.character.character_id:
            detail = f"alt of {main.character_name}"
        else:
            detail = f"account {row.user.username}"
        results.append({"name": row.character.character_name, "detail": detail})
    return results
