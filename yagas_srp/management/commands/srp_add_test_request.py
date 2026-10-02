from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from yagas_srp.services import create_request_from_mail


class Command(BaseCommand):
    help = (
        "Run one fake inbound mail through the exact pipeline the mailbox poll uses "
        "(killmail lookup, valuation, validation) -- for testing without a real mail."
    )

    def add_arguments(self, parser):
        parser.add_argument("--sender-id", type=int, required=True, help="Character id the mail is 'from'")
        parser.add_argument("--body", required=True,
                            help='Mail body, e.g. \'<a href="killReport:ID:HASH">link</a>\'')
        parser.add_argument("--subject", default="SRP request")

    def handle(self, *args, **options):
        try:
            request = create_request_from_mail(
                receiver=None, mail_id=None, sender_id=options["sender_id"], subject=options["subject"],
                body_html=options["body"], received_at=timezone.now())
        except Exception as exc:
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc
        self.stdout.write(
            f"#{request.pk} status={request.status} ship={request.ship_type_name!r} "
            f"total={request.total_value} flags={request.validation_flags}")
