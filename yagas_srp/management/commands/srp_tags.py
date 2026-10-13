from django.core.management.base import BaseCommand

from yagas_srp import tagging


class Command(BaseCommand):
    help = (
        "Apply the SRP tag rules to the requests that exist (tags are only ever added). "
        "A request whose tags were edited by hand is left alone unless --force is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="only say what would be tagged")
        parser.add_argument("--force", action="store_true", help="also requests whose tags were edited by hand")

    def handle(self, *args, **options):
        touched, given = tagging.retag_all(force=options["force"], dry_run=options["dry_run"])
        verb = "would get" if options["dry_run"] else "got"
        self.stdout.write(f"{touched} request(s) {verb} {given} new tag(s).")
