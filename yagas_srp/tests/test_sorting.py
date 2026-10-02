from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from allianceauth.eveonline.models import EveCharacter

from yagas_srp.models import SrpRequest


class InboxSortingTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("srp_sort_viewer", password="x")
        user.user_permissions.set(Permission.objects.filter(
            content_type__app_label="yagas_srp", codename="view_requests"))
        # Auth bounces users without a main character away from app pages
        user.profile.main_character = EveCharacter.objects.create(
            character_id=4001, character_name="Sorter", corporation_id=1, corporation_name="Test Corp")
        user.profile.save()
        self.client.force_login(User.objects.get(pk=user.pk))
        self.url = reverse("yagas_srp:request_list")
        now = timezone.now()
        rows = [  # victim, sender, ship, system, total, flags, age in hours
            ("Zed", "s1", "Vulture", "Jita", 300, [], 3),
            ("", "Amy", "Rifter", "Amarr", 100, ["a", "b"], 1),     # no victim -> shown as the sender
            ("Bob", "s3", "Zealot", "Dodixie", 200, ["a"], 2),
        ]
        # The rows live in the (little used) "invalid" tab and are picked out by mail id, so the
        # tests also hold when they run against a database that already contains real requests.
        self.mail_ids = {1000 + i for i in range(len(rows))}
        for i, (victim, sender, ship, system, total, flags, hours) in enumerate(rows):
            SrpRequest.objects.create(
                mail_id=1000 + i, victim_name=victim, sender_name=sender, ship_type_name=ship,
                solar_system_name=system, total_value=Decimal(total), validation_flags=flags,
                received_at=now - timedelta(hours=hours), status=SrpRequest.Status.INVALID)

    def _ships(self, **params):
        page = self.client.get(self.url, {"status": "invalid", **params})
        self.assertEqual(page.status_code, 200)
        return [r.ship_type_name for r in page.context["page"] if r.mail_id in self.mail_ids]

    def test_default_is_newest_first(self):
        self.assertEqual(self._ships(), ["Rifter", "Zealot", "Vulture"])

    def test_sorts_by_each_column_in_both_directions(self):
        self.assertEqual(self._ships(sort="total", dir="asc"), ["Rifter", "Zealot", "Vulture"])
        self.assertEqual(self._ships(sort="total", dir="desc"), ["Vulture", "Zealot", "Rifter"])
        self.assertEqual(self._ships(sort="ship", dir="asc"), ["Rifter", "Vulture", "Zealot"])
        self.assertEqual(self._ships(sort="system", dir="asc"), ["Rifter", "Zealot", "Vulture"])  # Amarr, Dodixie, Jita
        self.assertEqual(self._ships(sort="received", dir="asc"), ["Vulture", "Zealot", "Rifter"])
        self.assertEqual(self._ships(sort="flags", dir="desc"), ["Rifter", "Zealot", "Vulture"])

    def test_pilot_sorts_by_the_name_that_is_displayed(self):
        # Amy (sender, victim unknown) < Bob < Zed
        self.assertEqual(self._ships(sort="pilot", dir="asc"), ["Rifter", "Zealot", "Vulture"])

    def test_first_click_direction_and_toggle(self):
        page = self.client.get(self.url, {"sort": "total"})
        self.assertEqual(page.context["sort_query"], "sort=total&dir=desc")  # numbers start descending
        total = next(c for c in page.context["columns"] if c["label"] == "Total (ISK)")
        self.assertTrue(total["active"])
        self.assertIn("dir=asc", total["query"])                              # next click flips it
        ship = next(c for c in page.context["columns"] if c["label"] == "Ship")
        self.assertIn("sort=ship&dir=asc", ship["query"])                     # text starts ascending

    def test_unknown_sort_values_fall_back_safely(self):
        self.assertEqual(self._ships(sort="victim_name; DROP TABLE", dir="sideways"), ["Rifter", "Zealot", "Vulture"])
        self.assertEqual(self._ships(sort="ship", dir="sideways"), ["Rifter", "Vulture", "Zealot"])

    def test_tabs_and_pagination_keep_the_sort(self):
        html = self.client.get(self.url, {"sort": "ship", "dir": "desc"}).content.decode()
        self.assertIn("status=approved&sort=ship&amp;dir=desc", html)   # (autoescaped &)
