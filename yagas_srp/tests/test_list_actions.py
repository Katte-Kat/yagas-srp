from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allianceauth.eveonline.models import EveCharacter

from yagas_srp.models import SrpRequest


def make_user(name, character_id, *codenames):
    user = User.objects.create_user(name, password="x")
    user.user_permissions.set(Permission.objects.filter(
        content_type__app_label="yagas_srp", codename__in=codenames))
    # Auth bounces users without a main character away from app pages
    user.profile.main_character = EveCharacter.objects.create(
        character_id=character_id, character_name=name, corporation_id=1, corporation_name="Test Corp")
    user.profile.save()
    return User.objects.get(pk=user.pk)


class ListActionTests(TestCase):
    def setUp(self):
        self.approver = make_user("srp_list_approver", 5001, "view_requests", "approve_requests")
        self.viewer = make_user("srp_list_viewer", 5002, "view_requests")
        self.clean = SrpRequest.objects.create(
            mail_id=2001, victim_name="Lost Pilot", sender_name="Sender", ship_type_name="Vulture",
            total_value=Decimal("287190880"), received_at=timezone.now(), status=SrpRequest.Status.PENDING)
        self.flagged = SrpRequest.objects.create(
            mail_id=2002, victim_name="", sender_name="Mailer", ship_type_name="Rifter",
            total_value=Decimal("1000"), validation_flags=["Kill is older than 14 days", "Capsule loss"],
            received_at=timezone.now(), status=SrpRequest.Status.PENDING)
        self.list_url = reverse("yagas_srp:request_list")

    def _html(self, user, **params):
        self.client.force_login(user)
        return self.client.get(self.list_url, params).content.decode()

    def test_decision_buttons_only_for_approvers(self):
        approve_url = reverse("yagas_srp:review_request", args=[self.clean.pk])
        self.assertIn(approve_url, self._html(self.approver))
        html = self._html(self.viewer)
        self.assertNotIn(approve_url, html)
        self.assertNotIn('value="approve"', html)

    def test_copy_buttons_carry_plain_values(self):
        html = self._html(self.approver)
        self.assertIn('data-copy="Lost Pilot"', html)
        self.assertIn('data-copy="287190880"', html)      # digits only, no thousands separators
        self.assertIn('data-copy="Mailer"', html)          # no victim -> the mail sender is copied
        # viewers can copy too: copying is no decision
        self.assertIn('data-copy="287190880"', self._html(self.viewer))

    def test_flagged_request_asks_before_approving(self):
        html = self._html(self.approver)
        self.assertIn("Kill is older than 14 days | Capsule loss", html)
        self.assertIn("Approve anyway?", html)

    def test_approve_from_the_list_returns_to_the_list(self):
        self.client.force_login(self.approver)
        back = self.list_url + "?status=pending&sort=total&dir=desc"
        response = self.client.post(
            reverse("yagas_srp:review_request", args=[self.clean.pk]), {"action": "approve", "next": back})
        self.assertEqual((response.status_code, response["Location"]), (302, back))
        self.clean.refresh_from_db()
        self.assertEqual((self.clean.status, self.clean.approved_amount), ("approved", Decimal("287190880")))
        self.assertEqual(self.clean.reviewed_by, self.approver)

    def test_deny_from_the_list(self):
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse("yagas_srp:review_request", args=[self.flagged.pk]), {"action": "deny", "next": self.list_url})
        self.assertEqual(response["Location"], self.list_url)
        self.flagged.refresh_from_db()
        self.assertEqual(self.flagged.status, "denied")

    def test_a_foreign_next_url_is_ignored(self):
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse("yagas_srp:review_request", args=[self.clean.pk]),
            {"action": "deny", "next": "https://evil.example/phish"})
        self.assertEqual(response["Location"], reverse("yagas_srp:request_detail", args=[self.clean.pk]))

    def test_viewer_cannot_decide(self):
        self.client.force_login(self.viewer)
        self.client.post(reverse("yagas_srp:review_request", args=[self.clean.pk]), {"action": "approve"})
        self.clean.refresh_from_db()
        self.assertEqual(self.clean.status, "pending")

    def test_approved_tab_shows_and_copies_the_approved_amount(self):
        SrpRequest.objects.filter(pk=self.clean.pk).update(
            status=SrpRequest.Status.APPROVED, approved_amount=Decimal("5000000"))
        html = self._html(self.approver, status="approved")
        self.assertIn('data-copy="5000000"', html)
        self.assertNotIn('value="approve"', html)          # no decision buttons outside the pending tab


class TabTotalTests(TestCase):
    """Every tab shows what its requests are worth together."""

    def setUp(self):
        self.user = make_user("srp_total_viewer", 5101, "view_requests")
        self.client.force_login(self.user)
        self.url = reverse("yagas_srp:request_list")

    def _tab(self, status):
        page = self.client.get(self.url, {"status": status})
        return page, next(t for t in page.context["tabs"] if t["value"] == status)

    def _make(self, mail_id, status, total, approved=None):
        return SrpRequest.objects.create(
            mail_id=mail_id, status=status, total_value=Decimal(total), approved_amount=approved,
            received_at=timezone.now(), ship_type_name="Vulture")

    def test_totals_per_tab_use_the_approved_amount_for_approved_requests(self):
        before = {s: self._tab(s)[1] for s in ("pending", "approved", "denied")}
        self._make(3001, SrpRequest.Status.PENDING, 100)
        self._make(3002, SrpRequest.Status.PENDING, 250)
        self._make(3003, SrpRequest.Status.APPROVED, 900, approved=Decimal("700"))   # paid less than calculated
        self._make(3004, SrpRequest.Status.DENIED, 40)
        after = {s: self._tab(s)[1] for s in ("pending", "approved", "denied")}
        self.assertEqual(after["pending"]["total"] - before["pending"]["total"], 350)
        self.assertEqual(after["approved"]["total"] - before["approved"]["total"], 700)   # not 900
        self.assertEqual(after["denied"]["total"] - before["denied"]["total"], 40)
        self.assertEqual(after["pending"]["count"] - before["pending"]["count"], 2)

    def test_summary_line_shows_the_total_of_the_selected_tab_with_a_copy_button(self):
        self._make(3011, SrpRequest.Status.DENIED, 1234567)
        page, tab = self._tab("denied")
        html = page.content.decode()
        self.assertIn('id="tab-total"', html)
        self.assertIn(f'data-copy="{int(tab["total"])}"', html)          # digits only
        self.assertIn("ISK</strong>", html)

    def test_invalid_tab_has_no_total_line(self):
        page, _tab = self._tab("invalid")
        self.assertNotIn('id="tab-total"', page.content.decode())
