"""The reason to copy ("SRP #<number>") and the Paid column: a request counts as paid when a wallet booking
names its number. All of it is read from yagas_finance and must never get in the way of the inbox."""
import datetime
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from yagas_srp.models import SrpRequest
from yagas_srp.tests.test_list_actions import make_user

CORP_ID = 91_000_004  # nothing real can have this id


def row(html, srp):
    """The <tr> of one request out of a rendered list (the live test database holds real requests too, so
    assertions look at their own row, not at the whole page)."""
    marker = f'data-copy="SRP #{srp.pk}"'
    for part in html.split("<tr>"):
        if marker in part:
            return part
    raise AssertionError(f"request #{srp.pk} is not on the page")


class ReasonTextTests(TestCase):
    def setUp(self):
        self.viewer = make_user("srp_reason_viewer", 6001, "view_requests")
        self.srp = SrpRequest.objects.create(
            mail_id=3001, victim_name="Pilot", ship_type_name="Vulture", total_value=Decimal("1000"),
            received_at=timezone.now(), status=SrpRequest.Status.APPROVED, approved_amount=Decimal("1000"))

    def test_the_reason_names_the_inbox_number_and_nothing_else(self):
        self.assertEqual(self.srp.reason_text, f"SRP #{self.srp.pk}")

    def test_every_tab_offers_the_reason_to_copy(self):
        self.client.force_login(self.viewer)
        for status in ("pending", "approved", "denied", "invalid", "archived"):
            html = self.client.get(reverse("yagas_srp:request_list"), {"status": status}).content.decode()
            on_page = min(50, SrpRequest.objects.filter(status=status).count())   # 50 rows per page
            self.assertEqual(html.count('data-copy="SRP #'), on_page, status)
        html = self.client.get(reverse("yagas_srp:request_list"), {"status": "approved"}).content.decode()
        self.assertIn(f'data-copy="SRP #{self.srp.pk}"', html)

    def test_the_detail_page_shows_it_for_an_approved_request(self):
        self.client.force_login(self.viewer)
        html = self.client.get(reverse("yagas_srp:request_detail", args=[self.srp.pk])).content.decode()
        self.assertIn(f"SRP #{self.srp.pk}", html)
        self.assertIn("Wallet booking", html)


class PaidColumnTests(TestCase):
    def setUp(self):
        try:
            from allianceauth.eveonline.models import EveCorporationInfo
            from yagas_finance.models import Profile, ProfileCorpLink, WalletJournalEntry
        except ImportError:
            self.skipTest("yagas_finance is not installed here")
        from yagas_finance.models import Category, CategoryRule
        self.Entry = WalletJournalEntry
        self.corp = EveCorporationInfo.objects.create(
            corporation_id=CORP_ID, corporation_name="Pay Corp", corporation_ticker="PAYC", member_count=1)
        self.profile = Profile.objects.create(name="Pay Profile")
        ProfileCorpLink.objects.create(
            profile=self.profile, corporation=self.corp, wallet_divisions=[4], hangar_divisions=[])
        self.viewer = make_user("srp_paid_viewer", 6002, "view_requests")
        self.received = timezone.now() - datetime.timedelta(minutes=5)   # newest, so it is on page 1
        # the SRP mark in finance (what `manage.py setup_srp_category` creates), started a week ago
        self.mark = CategoryRule.objects.create(
            category=Category.objects.create(profile=self.profile, name="SRP"),
            match_type=CategoryRule.MatchType.NOTES_WORD, match_value="srp", priority=4,
            valid_from=timezone.now() - datetime.timedelta(days=7))
        self.srp = self.approved(4001, "150000000")
        self.n = 0
        self.client.force_login(self.viewer)

    def approved(self, mail_id, amount, reviewed_at=None):
        return SrpRequest.objects.create(
            mail_id=mail_id, victim_name=f"Pilot {mail_id}", ship_type_name="Vulture", total_value=Decimal(amount),
            approved_amount=Decimal(amount), received_at=self.received, status=SrpRequest.Status.APPROVED,
            reviewed_at=reviewed_at or timezone.now())   # approved just now, i.e. after the mark began

    def book(self, reason, amount="-150000000", when=None, **kwargs):
        self.n += 1
        defaults = dict(corporation=self.corp, division=4, entry_id=self.n, date=when or timezone.now(),
                        ref_type="corporation_account_withdrawal", amount=Decimal(amount), reason=reason,
                        first_party_id=CORP_ID, second_party_id=555)
        defaults.update(kwargs)
        return self.Entry.objects.create(**defaults)

    def approved_tab(self):
        return self.client.get(reverse("yagas_srp:request_list"), {"status": "approved"}).content.decode()

    def test_approved_but_not_booked_is_open(self):
        html = self.approved_tab()
        self.assertIn("not paid yet", row(html, self.srp))
        self.assertIn(">Paid<", html)

    def test_a_booking_with_the_exact_amount_is_paid(self):
        self.book(f"SRP #{self.srp.pk}")
        mine = row(self.approved_tab(), self.srp)
        self.assertIn("text-bg-success", mine)
        self.assertNotIn("not paid yet", mine)

    def test_another_amount_is_flagged_with_both_numbers(self):
        self.book(f"SRP #{self.srp.pk}", amount="-100000000")
        mine = row(self.approved_tab(), self.srp)
        self.assertIn("text-bg-warning", mine)
        self.assertIn("100,000,000 booked", mine)
        self.assertIn("150,000,000", mine)   # what was approved, in the tooltip

    def test_two_partial_bookings_add_up(self):
        self.book(f"SRP #{self.srp.pk}", amount="-100000000")
        self.book(f"SRP #{self.srp.pk} rest", amount="-50000000")
        mine = row(self.approved_tab(), self.srp)
        self.assertIn("text-bg-success", mine)
        self.assertNotIn("text-bg-warning", mine)

    def test_a_booking_that_pays_several_requests_says_so(self):
        other = self.approved(4002, "50000000")
        self.book(f"SRP #{self.srp.pk}, #{other.pk}", amount="-200000000")
        html = self.approved_tab()
        self.assertIn(f"paid together with #{other.pk}", row(html, self.srp))
        self.assertIn(f"paid together with #{self.srp.pk}", row(html, other))

    def test_bookings_that_cannot_be_a_payment_are_ignored(self):
        self.book(f"SRP #{self.srp.pk}", when=self.received - datetime.timedelta(days=1))   # before the request existed
        self.book(f"SRP #{self.srp.pk}", amount="150000000")                                  # money in
        self.book(f"SRP #{self.srp.pk}", division=2)                                          # a wallet nobody covers
        self.book(f"SRP #{self.srp.pk}", is_internal_transfer=True)
        self.book("SRP")                                                                      # marked, names no request
        self.assertIn("not paid yet", row(self.approved_tab(), self.srp))

    def test_nothing_is_shown_until_the_srp_mark_is_set_up(self):
        self.book(f"SRP #{self.srp.pk}")
        with mock.patch("yagas_finance.srp_link.starts", return_value={}):   # (the live test database may have marks)
            html = self.approved_tab()
            detail = self.client.get(reverse("yagas_srp:request_detail", args=[self.srp.pk])).content.decode()
        self.assertNotIn("not paid yet", row(html, self.srp))
        self.assertNotIn("text-bg-success", row(html, self.srp))
        self.assertNotIn("not paid yet", detail)

    def test_requests_approved_before_the_mark_are_not_called_unpaid(self):
        old = self.approved(4200, "1000", reviewed_at=timezone.now() - datetime.timedelta(days=30))
        html = self.approved_tab()
        self.assertNotIn("not paid yet", row(html, old))     # paid before anybody typed the number, says nothing
        self.assertIn("not paid yet", row(html, self.srp))    # approved after the start: this one is to do
        self.book(f"SRP #{old.pk}", amount="-1000")           # ... but if the number is used, it is shown
        self.assertIn("text-bg-success", row(self.approved_tab(), old))

    def test_bookings_from_before_the_mark_started_do_not_count(self):
        self.mark.valid_from = timezone.now() + datetime.timedelta(hours=1)   # starts later than the booking
        self.mark.save()
        self.book(f"SRP #{self.srp.pk}")
        self.assertIn("not paid yet", row(self.approved_tab(), self.srp))

    def test_other_tabs_do_not_read_the_wallet_at_all(self):
        self.book(f"SRP #{self.srp.pk}")
        with CaptureQueriesContext(connection) as ctx:
            html = self.client.get(reverse("yagas_srp:request_list"), {"status": "pending"}).content.decode()
        self.assertNotIn("yagas_finance_walletjournalentry", " ".join(q["sql"] for q in ctx.captured_queries))
        self.assertNotIn(">Paid<", html)

    def test_the_approved_tab_reads_the_wallet_once_for_the_whole_page(self):
        for i in range(10):
            self.approved(4100 + i, "1000")
        self.book(f"SRP #{self.srp.pk}")
        with CaptureQueriesContext(connection) as ctx:
            self.approved_tab()
        reads = [q for q in ctx.captured_queries if "yagas_finance_walletjournalentry" in q["sql"]]
        self.assertEqual(len(reads), 1)

    def test_a_failing_finance_read_never_breaks_the_inbox(self):
        with mock.patch("yagas_finance.srp_link.payments_for_requests", side_effect=RuntimeError("boom")):
            response = self.client.get(reverse("yagas_srp:request_list"), {"status": "approved"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("not paid yet", row(response.content.decode(), self.srp))

    def test_the_detail_page_shows_the_booking(self):
        self.book(f"SRP #{self.srp.pk}")
        html = self.client.get(reverse("yagas_srp:request_detail", args=[self.srp.pk])).content.decode()
        self.assertIn("text-bg-success", html)
        self.assertIn(f"SRP #{self.srp.pk}", html)


class WithoutFinanceTests(TestCase):
    def test_no_finance_app_means_no_badges_and_no_errors(self):
        viewer = make_user("srp_nofinance_viewer", 6003, "view_requests")
        srp = SrpRequest.objects.create(
            mail_id=5001, victim_name="Pilot", ship_type_name="Vulture", total_value=Decimal("10"),
            approved_amount=Decimal("10"), received_at=timezone.now(), status=SrpRequest.Status.APPROVED)
        self.client.force_login(viewer)
        with mock.patch.dict("sys.modules", {"yagas_finance.srp_link": None}):
            response = self.client.get(reverse("yagas_srp:request_list"), {"status": "approved"})
            detail = self.client.get(reverse("yagas_srp:request_detail", args=[srp.pk]))
        self.assertEqual((response.status_code, detail.status_code), (200, 200))
        self.assertNotIn("not paid yet", row(response.content.decode(), srp))
