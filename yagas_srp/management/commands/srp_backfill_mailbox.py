import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone

from esi.exceptions import ESIBucketLimitException

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from yagas_srp.models import MailReceiver, SrpRequest
from yagas_srp.services import MAIL_SCOPE, create_request_from_mail, esi_provider, extract_killmail_ref
from yagas_srp.tasks import _addressed_to, _field, fetch_mail_headers

logger = logging.getLogger(__name__)

# ESI allows 600 mail requests per 15 minutes per character (bucket "char-social"). Pacing the
# body downloads at one per 1.7 s uses ~530 of them and leaves room for the regular poll.
PACE_SECONDS = 1.7


class Command(BaseCommand):
    help = (
        "Reads the receiver's OLDER mails (the regular poll only looks at the newest 200), newest first, and "
        "turns every personally addressed mail that links a killmail into a request. Mails older than "
        "--archive-older-than-days become \"archived\" requests (history), younger ones stay pending. Mails "
        "without a killmail link are skipped silently, not recorded as invalid. Paced to ESI's mail rate limit "
        "(about 500 mails per 15 minutes), so a big inbox takes an hour or more. Safe to repeat: mails already "
        "known are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Only mails from this date on (YYYY-MM-DD). Default: the whole inbox.")
        parser.add_argument("--receiver", type=int, help="Receiver character id (default: the first active receiver)")
        parser.add_argument("--dry-run", action="store_true", help="Only count what would be imported, per month")
        parser.add_argument("--limit", type=int, default=0, help="Import at most this many requests (0 = no limit)")
        parser.add_argument("--archive-older-than-days", type=int, default=14,
                            help="Requests from mails older than this are archived instead of pending (default 14)")

    def handle(self, *args, **options):
        from esi.models import Token

        receivers = MailReceiver.objects.filter(is_active=True)
        if options["receiver"]:
            receivers = receivers.filter(character_id=options["receiver"])
        receiver = receivers.first()
        if receiver is None:
            raise CommandError("No active mail receiver.")
        since = None
        if options["since"]:
            since = datetime.strptime(options["since"], "%Y-%m-%d").replace(tzinfo=dt_timezone.utc)
        token = Token.objects.filter(character_id=receiver.character_id).require_scopes(
            [MAIL_SCOPE]).require_valid().first()
        if token is None:
            raise CommandError(f"No valid {MAIL_SCOPE} token for {receiver.character_name}.")

        esi = esi_provider(["Mail"])
        headers = fetch_mail_headers(
            esi, receiver.character_id, token, max_pages=None, older_than=since, wait_on_rate_limit=True)
        known = set(SrpRequest.objects.filter(receiver=receiver).values_list("mail_id", flat=True))
        candidates = sorted(
            (h for h in headers
             if h.mail_id not in known
             and (since is None or h.timestamp >= since)
             and _field(h, "from_", "from") != receiver.character_id
             and _addressed_to(h, receiver.character_id)),
            key=lambda h: h.mail_id, reverse=True)   # newest first: the useful ones land first
        since_note = f" (since {options['since']})" if since else ""
        self.stdout.write(
            f"{len(headers)} mail headers read, {len(candidates)} personal mails not yet imported{since_note}.")

        archive_before = timezone.now() - timedelta(days=options["archive_older_than_days"])
        per_month, created, archived, skipped, failed = Counter(), 0, 0, 0, 0
        for number, header in enumerate(candidates, 1):
            try:
                mail = self._read_mail(esi, receiver, token, header.mail_id)
                time.sleep(PACE_SECONDS)
                if extract_killmail_ref(mail.body or "") is None:
                    skipped += 1
                    continue
                per_month[header.timestamp.strftime("%Y-%m")] += 1
                if options["dry_run"]:
                    continue
                if options["limit"] and created >= options["limit"]:
                    break
                srp = create_request_from_mail(
                    receiver=receiver, mail_id=header.mail_id, sender_id=_field(header, "from_", "from"),
                    subject=header.subject or "", body_html=mail.body or "", received_at=header.timestamp)
                created += 1
                if srp.status == SrpRequest.Status.PENDING and header.timestamp < archive_before:
                    SrpRequest.objects.filter(pk=srp.pk).update(status=SrpRequest.Status.ARCHIVED)
                    archived += 1
            except Exception:
                failed += 1
                logger.exception("yagas_srp: backfill could not process mail %s", header.mail_id)
            if number % 100 == 0:
                self.stdout.write(f"  ... {number}/{len(candidates)} mails read, {created} requests so far")

        self.stdout.write("Mails with a killmail link per month:")
        for month in sorted(per_month):
            self.stdout.write(f"  {month}: {per_month[month]}")
        verb = "would be imported" if options["dry_run"] else "imported"
        imported = sum(per_month.values()) if options["dry_run"] else created
        self.stdout.write(
            f"{sum(per_month.values())} mails with a killmail link ({verb}: {imported}, of which "
            f"{archived} archived), {skipped} without a link skipped, {failed} failed.")

    def _read_mail(self, esi, receiver, token, mail_id):
        """One mail body; when ESI's mail bucket is used up, wait for it to reset and try again."""
        for attempt in range(4):
            try:
                mail, _resp = esi.client.Mail.GetCharactersCharacterIdMailMailId(
                    character_id=receiver.character_id, mail_id=mail_id, token=token).result(
                    return_response=True, force_refresh=True)
                return mail
            except ESIBucketLimitException as exc:
                wait = float(getattr(exc, "reset", 0) or 900) + 5
                self.stdout.write(f"  ESI mail rate limit reached, waiting {int(wait)} s ...")
                time.sleep(wait)
        raise RuntimeError(f"ESI mail rate limit did not clear for mail {mail_id}")
