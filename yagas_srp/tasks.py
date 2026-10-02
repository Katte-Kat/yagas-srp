import logging

from celery import shared_task
from django.db import IntegrityError
from django.utils import timezone

logger = logging.getLogger(__name__)

# One poll handles at most this many new mails; the rest wait for the next run.
MAX_NEW_MAILS_PER_POLL = 25


def _field(obj, *names):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _addressed_to(header, character_id: int) -> bool:
    """Only mails sent to the receiver personally -- not corp/alliance/mailing-list spam."""
    for recipient in header.recipients or []:
        if _field(recipient, "recipient_type") == "character" and _field(recipient, "recipient_id") == character_id:
            return True
    return False


def _poll_receiver(receiver) -> int:
    from esi.models import Token

    from yagas_srp.models import SrpRequest
    from yagas_srp.services import MAIL_SCOPE, create_request_from_mail, esi_provider

    token = (
        Token.objects.filter(character_id=receiver.character_id)
        .require_scopes([MAIL_SCOPE])
        .require_valid()
        .first()
    )
    if token is None:
        raise RuntimeError(f"No valid ESI token with {MAIL_SCOPE} for {receiver.character_name}; link it again")

    esi = esi_provider(["Mail"])
    headers, _resp = esi.client.Mail.GetCharactersCharacterIdMail(
        character_id=receiver.character_id, token=token).result(return_response=True, force_refresh=True)

    known = set(SrpRequest.objects.filter(receiver=receiver).values_list("mail_id", flat=True))
    fresh = sorted(
        (h for h in headers
         if h.mail_id not in known
         and _field(h, "from_", "from") != receiver.character_id
         and _addressed_to(h, receiver.character_id)),
        key=lambda h: h.mail_id)

    created = 0
    for header in fresh[:MAX_NEW_MAILS_PER_POLL]:
        try:
            mail, _resp = esi.client.Mail.GetCharactersCharacterIdMailMailId(
                character_id=receiver.character_id, mail_id=header.mail_id, token=token).result(
                return_response=True, force_refresh=True)
            create_request_from_mail(
                receiver=receiver, mail_id=header.mail_id, sender_id=_field(header, "from_", "from"),
                subject=header.subject or "", body_html=mail.body or "", received_at=header.timestamp)
            created += 1
        except IntegrityError:
            continue  # a parallel poll got it first
        except Exception:
            # Left unrecorded on purpose: retried on the next poll (transient ESI error).
            logger.exception("yagas_srp: could not process mail %s", header.mail_id)
    return created


@shared_task
def poll_mailbox():
    """Pull new SRP mails from every active receiver character's inbox."""
    from yagas_srp.models import MailReceiver

    total = 0
    for receiver in MailReceiver.objects.filter(is_active=True):
        try:
            total += _poll_receiver(receiver)
            receiver.last_error = ""
        except Exception as exc:
            logger.exception("yagas_srp: polling %s failed", receiver)
            receiver.last_error = str(exc)[:1000]
        receiver.last_polled_at = timezone.now()
        receiver.save(update_fields=["last_polled_at", "last_error"])
    return total
