from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

from yagas_srp.tests.test_list_actions import make_user


def pilot(name, character_id, active=True):
    """An Auth account whose main character is `name`."""
    user = User.objects.create_user(f"acct_{character_id}", password="x", is_active=active)
    character = EveCharacter.objects.create(
        character_id=character_id, character_name=name, corporation_id=1, corporation_name="Test Corp")
    CharacterOwnership.objects.create(character=character, user=user, owner_hash=f"hash{character_id}")
    user.profile.main_character = character
    user.profile.save()
    return User.objects.get(pk=user.pk)


class SrpManagerRoleTests(TestCase):
    def setUp(self):
        self.ceo = make_user("srp_ceo", 6001, "view_requests", "approve_requests", "assign_srp_managers")
        self.plain = make_user("srp_plain", 6002, "view_requests")
        self.target = pilot("Fresh Manager", 6003)
        self.client.force_login(self.ceo)
        self.page = reverse("yagas_srp:managers")
        self.add = reverse("yagas_srp:manager_add")
        self.remove = reverse("yagas_srp:manager_remove")

    def _group(self):
        return Group.objects.filter(name="SRP Manager").first()

    def test_button_and_page_only_for_the_assign_permission(self):
        list_url = reverse("yagas_srp:request_list")
        self.assertContains(self.client.get(list_url), self.page)
        self.client.force_login(self.plain)
        self.assertNotContains(self.client.get(list_url), self.page)
        self.assertEqual(self.client.get(self.page).status_code, 302)
        self.assertEqual(self.client.post(self.add, {"character": "Fresh Manager"}).status_code, 302)
        self.target.refresh_from_db()
        self.assertFalse(self.target.groups.filter(name="SRP Manager").exists())

    def test_adding_by_character_name_gives_the_role_and_it_works(self):
        self.client.post(self.add, {"character": "fresh MANAGER"})            # any capitalisation
        group = self._group()
        self.assertTrue(group.user_set.filter(pk=self.target.pk).exists())
        self.assertEqual(sorted(p.codename for p in group.permissions.all()), ["approve_requests", "view_requests"])
        entry = LogEntry.objects.filter(object_id=str(group.pk), content_type__model="group").first()
        self.assertIn("Added", entry.change_message)
        self.assertEqual(entry.user, self.ceo)
        # the new manager can use the inbox, but cannot hand the role on
        self.client.force_login(User.objects.get(pk=self.target.pk))
        self.assertEqual(self.client.get(reverse("yagas_srp:request_list")).status_code, 200)
        self.assertEqual(self.client.get(self.page).status_code, 302)

    def test_unknown_and_disabled_accounts_are_refused(self):
        self.client.post(self.add, {"character": "Nobody Atall"})
        pilot("Gone Pilot", 6004, active=False)
        self.client.post(self.add, {"character": "Gone Pilot"})
        group = self._group()
        self.assertTrue(group is None or group.user_set.count() == 0)

    def test_adding_twice_keeps_one_membership(self):
        self.client.post(self.add, {"character": "Fresh Manager"})
        self.client.post(self.add, {"character": "Fresh Manager"})
        self.assertEqual(self._group().user_set.filter(pk=self.target.pk).count(), 1)

    def test_remove_takes_the_role_away_and_only_for_members(self):
        self.client.post(self.add, {"character": "Fresh Manager"})
        self.client.post(self.remove, {"user_id": self.target.pk})
        self.assertFalse(self._group().user_set.filter(pk=self.target.pk).exists())
        before = LogEntry.objects.count()
        self.client.post(self.remove, {"user_id": self.plain.pk})           # not a member: nothing happens
        self.assertEqual(LogEntry.objects.count(), before)

    def test_other_groups_cannot_be_reached_through_this_page(self):
        director, _ = Group.objects.get_or_create(name="Director")
        self.client.post(self.add, {"character": "Fresh Manager", "role": "Director", "group": "Director"})
        self.assertFalse(director.user_set.filter(pk=self.target.pk).exists())
        self.client.post(self.remove, {"user_id": self.target.pk, "role": "Director"})
        self.assertEqual(Group.objects.filter(name="Director").count(), 1)

    def test_page_lists_members_and_character_suggestions(self):
        self.client.post(self.add, {"character": "Fresh Manager"})
        page = self.client.get(self.page)
        self.assertEqual([m["name"] for m in page.context["members"]], ["Fresh Manager"])
        self.assertIn("Fresh Manager", page.context["character_names"])
