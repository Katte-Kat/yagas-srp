"""Tags: what the rules give a request, who may see which requests, and the hand edit. These run on the real database of the instance inside a transaction that is rolled back; the tags
and rules the migration seeded are removed inside that transaction first, so every test starts from nothing."""
import importlib
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from yagas_srp import tagging
from yagas_srp.auth_hooks import SrpMenuItem
from yagas_srp.models import SrpRequest, SrpTag, SrpTagRule
from yagas_srp.tests.test_list_actions import make_user


def tag(name, **kw):
    return SrpTag.objects.create(name=name, slug=name.lower().replace(" ", "-"), **kw)


def rule(kind, keyword, *tags, **kw):
    r = SrpTagRule.objects.create(kind=kind, keyword=keyword, **kw)
    r.tags.add(*tags)
    return r


def request(mail_id, subject="", body="", ship_type_id=None, status=SrpRequest.Status.PENDING, **kw):
    return SrpRequest.objects.create(mail_id=mail_id, subject=subject, body=body, ship_type_id=ship_type_id, ship_type_name=kw.pop("ship", "Rifter"), status=status,
                                     received_at=timezone.now(), total_value=kw.pop("total", Decimal("1000")), **kw)


class Base(TestCase):
    def setUp(self):
        SrpTagRule.objects.all().delete()
        SrpTag.objects.all().delete()
        self.blops = tag("BLOPS SRP", order=1, color="#7b2cbf")
        self.sanction = tag("SANCTION SRP", order=2, color="#e67e22")


class RuleTests(Base):
    def test_a_subject_word_gives_every_tag_of_the_rule(self):
        rule("subject", "HUNT", self.blops, self.sanction)
        found = tagging.matching_tags("SRP hunt loss", "", None)
        self.assertEqual([t.name for t in found], ["BLOPS SRP", "SANCTION SRP"])
        self.assertEqual(tagging.matching_tags("HUNT", "", None), found)
        self.assertEqual(tagging.matching_tags("my hunters died", "", None), [])           # a whole word: hunters is not hunt
        self.assertEqual(tagging.matching_tags("shunt", "", None), [])
        self.assertEqual(tagging.matching_tags("", "hunt in the body", None), [])           # a subject rule only looks at the subject

    def test_a_phrase_and_the_body_and_the_hull(self):
        rule("subject", "target hunt", self.sanction)
        rule("body", "sanctioned", self.sanction)
        rule("hull", "Black Ops", self.blops)
        self.assertEqual([t.name for t in tagging.matching_tags("a TARGET   hunt", "", None)], ["SANCTION SRP"])
        self.assertEqual([t.name for t in tagging.matching_tags("", "this was sanctioned!", None)], ["SANCTION SRP"])
        self.assertEqual([t.name for t in tagging.matching_tags("", "", "black ops")], ["BLOPS SRP"])
        self.assertEqual(tagging.matching_tags("", "", "Black Ops Frigate"), [])             # the exact group, not a part of it

    def test_inactive_rules_and_inactive_tags_give_nothing(self):
        rule("subject", "HUNT", self.blops, is_active=False)
        self.assertEqual(tagging.matching_tags("HUNT", "", None), [])
        rule("subject", "TARGET", self.blops, self.sanction)
        self.sanction.is_active = False
        self.sanction.save()
        self.assertEqual([t.name for t in tagging.matching_tags("TARGET", "", None)], ["BLOPS SRP"])

    def test_apply_adds_only_and_a_hand_edited_request_is_left_alone(self):
        rule("subject", "HUNT", self.blops, self.sanction)
        srp = request(1, subject="HUNT")
        srp.tags.add(self.blops)
        added = tagging.apply_rules(srp)
        self.assertEqual([t.name for t in added], ["SANCTION SRP"])
        self.assertEqual(srp.tags.count(), 2)
        self.assertEqual(tagging.apply_rules(srp), [])
        edited = request(2, subject="HUNT", tags_edited=True)
        self.assertEqual(tagging.apply_rules(edited), [])
        self.assertEqual(edited.tags.count(), 0)

    def test_the_ship_group_comes_from_the_static_data(self):
        group = tagging.ship_group(22428)                                                  # Redeemer, a Black Ops hull
        self.assertEqual(group, "Black Ops")
        self.assertIsNone(tagging.ship_group(None))
        self.assertIsNone(tagging.ship_group(999999999))
        rule("hull", "Black Ops", self.blops)
        srp = request(3, ship_type_id=22428)
        self.assertEqual([t.name for t in tagging.apply_rules(srp)], ["BLOPS SRP"])

    def test_a_new_request_is_tagged_and_a_failing_rule_never_costs_the_request(self):
        from yagas_srp import services

        rule("subject", "HUNT", self.blops)
        srp = request(4, subject="HUNT")
        with mock.patch.object(services, "_create_request_from_mail", return_value=srp):
            self.assertIs(services.create_request_from_mail(receiver=None, mail_id=4, sender_id=1, subject="HUNT", body_html="", received_at=timezone.now()), srp)
        self.assertEqual([t.name for t in srp.tags.all()], ["BLOPS SRP"])
        other = request(5, subject="HUNT")
        with mock.patch.object(services, "_create_request_from_mail", return_value=other), mock.patch.object(tagging, "apply_rules", side_effect=RuntimeError("boom")):
            self.assertIs(services.create_request_from_mail(receiver=None, mail_id=5, sender_id=1, subject="HUNT", body_html="", received_at=timezone.now()), other)

    def test_retagging_and_the_command(self):
        rule("subject", "HUNT", self.blops, self.sanction)
        a, b = request(6, subject="HUNT one"), request(7, subject="nothing")
        c = request(8, subject="HUNT three", tags_edited=True)
        self.assertEqual(tagging.retag_all(dry_run=True), (1, 2))
        self.assertEqual(a.tags.count(), 0)                                                # a dry run writes nothing
        out = StringIO()
        call_command("srp_tags", stdout=out)
        self.assertIn("1 request(s) got 2 new tag(s)", out.getvalue())
        self.assertEqual((a.tags.count(), b.tags.count(), c.tags.count()), (2, 0, 0))
        call_command("srp_tags", "--force", stdout=StringIO())
        self.assertEqual(c.tags.count(), 2)


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.group = Group.objects.create(name="BLOPS viewers test")
        self.blops.viewer_groups.add(self.group)
        self.blops_viewer = make_user("srp_tag_blops_viewer", 6001)
        self.blops_viewer.groups.add(self.group)
        self.stranger = make_user("srp_tag_stranger", 6002)
        self.manager = make_user("srp_tag_manager", 6003, "view_requests", "approve_requests")
        self.tagged = request(10, subject="HUNT", ship="Redeemer", total=Decimal("500"))
        self.tagged.tags.add(self.blops)
        self.both = request(11, subject="TARGET", ship="Widow", total=Decimal("300"))
        self.both.tags.add(self.blops, self.sanction)
        self.sanction_only = request(12, subject="x", ship="Hulk", total=Decimal("200"))
        self.sanction_only.tags.add(self.sanction)
        self.plain = request(13, subject="plain", ship="Rifter", total=Decimal("100"))
        self.list_url = reverse("yagas_srp:request_list")

    def ids(self, user, **params):
        self.client.force_login(user)
        response = self.client.get(self.list_url, params)
        self.assertEqual(response.status_code, 200)
        return {srp.pk for srp in response.context["page"]}, response

    def test_a_viewer_group_member_sees_only_the_requests_of_the_tag(self):
        shown, response = self.ids(self.blops_viewer)
        self.assertEqual(shown, {self.tagged.pk, self.both.pk})
        self.assertEqual([c["tag"].name for c in response.context["tag_chips"]], ["BLOPS SRP"])
        self.assertFalse(response.context["full_viewer"])
        self.assertFalse(response.context["show_actions"])
        self.assertNotIn("review_request", response.content.decode().replace("yagas_srp:review", ""))

    def test_a_viewer_is_shown_only_the_tags_of_his_own_group_and_the_managers_all_of_them(self):
        shown, response = self.ids(self.blops_viewer)
        by_pk = {srp.pk: [t.name for t in srp.shown_tags] for srp in response.context["page"]}
        self.assertEqual(by_pk[self.both.pk], ["BLOPS SRP"])
        html = response.content.decode()
        self.assertNotIn("SANCTION SRP", html)
        detail = self.client.get(reverse("yagas_srp:request_detail", args=[self.both.pk])).content.decode()
        self.assertIn("BLOPS SRP", detail)
        self.assertNotIn("SANCTION SRP", detail)
        _, response = self.ids(self.manager)
        by_pk = {srp.pk: [t.name for t in srp.shown_tags] for srp in response.context["page"]}
        self.assertEqual(by_pk[self.both.pk], ["BLOPS SRP", "SANCTION SRP"])

    def test_the_tags_column_sits_between_system_and_total(self):
        _, response = self.ids(self.manager)
        html = response.content.decode()
        head = html[html.index("<thead>"):html.index("</thead>")]
        self.assertTrue(head.index("System") < head.index("Tags") < head.index("Total (ISK)"))

    def test_the_totals_do_not_count_a_request_twice(self):
        self.blops.viewer_groups.add(self.group)
        self.sanction.viewer_groups.add(self.group)                                       # a viewer of both tags: the request with both is still one request
        self.client.force_login(self.blops_viewer)
        tabs = {t["value"]: t for t in self.client.get(self.list_url).context["tabs"]}
        self.assertEqual((tabs["pending"]["count"], tabs["pending"]["total"]), (3, 1000))        # tagged 500 + both 300 + sanction-only 200

    def test_managers_see_everything_and_can_filter_by_tag(self):
        shown, response = self.ids(self.manager)
        pending = SrpRequest.objects.filter(status=SrpRequest.Status.PENDING).count()          # the live requests of the instance are in the list as well
        self.assertEqual(response.context["page"].paginator.count, pending)
        self.assertTrue({self.tagged.pk, self.both.pk, self.sanction_only.pk, self.plain.pk} <= set(tagging.visible_requests(self.manager).values_list("pk", flat=True)))
        self.assertTrue(response.context["full_viewer"])
        self.assertEqual([(c["tag"].name, c["count"]) for c in response.context["tag_chips"]], [("BLOPS SRP", 2), ("SANCTION SRP", 2)])
        shown, response = self.ids(self.manager, tag="sanction-srp")
        self.assertEqual(shown, {self.both.pk, self.sanction_only.pk})
        self.assertEqual(response.context["tag"].slug, "sanction-srp")
        _, response = self.ids(self.manager, tag="no-such-tag")
        self.assertEqual(response.context["page"].paginator.count, pending)                # an unknown tag is no filter

    def test_a_viewer_cannot_filter_by_a_tag_of_somebody_else(self):
        shown, response = self.ids(self.blops_viewer, tag="sanction-srp")
        self.assertEqual(shown, {self.tagged.pk, self.both.pk})
        self.assertIsNone(response.context["tag"])

    def test_nobody_else_gets_in(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.assertEqual(self.client.get(reverse("yagas_srp:request_detail", args=[self.tagged.pk])).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.list_url).status_code, 302)                  # to the login

    def test_the_detail_page_only_for_requests_the_viewer_may_see(self):
        self.client.force_login(self.blops_viewer)
        self.assertEqual(self.client.get(reverse("yagas_srp:request_detail", args=[self.tagged.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("yagas_srp:request_detail", args=[self.sanction_only.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("yagas_srp:request_detail", args=[self.plain.pk])).status_code, 404)

    def test_a_viewer_decides_nothing_and_edits_nothing(self):
        self.client.force_login(self.blops_viewer)
        self.assertEqual(self.client.post(reverse("yagas_srp:review_request", args=[self.tagged.pk]), {"action": "approve"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("yagas_srp:edit_tags", args=[self.tagged.pk]), {"action": "add", "tag": self.sanction.pk}).status_code, 302)
        self.tagged.refresh_from_db()
        self.assertEqual(self.tagged.status, "pending")
        self.assertEqual(self.tagged.tags.count(), 1)

    def test_an_inactive_tag_gives_nobody_access(self):
        self.blops.is_active = False
        self.blops.save()
        self.client.force_login(self.blops_viewer)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)

    def test_the_menu_entry_and_its_badge(self):
        def render(user):
            r = RequestFactory().get("/")
            r.user = user
            item = SrpMenuItem()
            return item.render(r), item

        html, item = render(self.blops_viewer)
        self.assertIn("SRP Inbox", html)
        self.assertEqual(item.count, 2)                                                    # his two pending requests, not all four
        self.assertEqual(render(self.stranger)[0], "")
        self.assertEqual(render(self.manager)[1].count, SrpRequest.objects.filter(status=SrpRequest.Status.PENDING).count())


class EditTests(Base):
    def setUp(self):
        super().setUp()
        self.manager = make_user("srp_tag_editor", 6101, "view_requests", "approve_requests")
        self.srp = request(20, subject="plain")
        self.client.force_login(self.manager)

    def test_a_manager_adds_and_removes_a_tag_and_the_rules_leave_it_alone_after_that(self):
        url = reverse("yagas_srp:edit_tags", args=[self.srp.pk])
        self.client.post(url, {"action": "add", "tag": self.blops.pk})
        self.srp.refresh_from_db()
        self.assertEqual([t.name for t in self.srp.tags.all()], ["BLOPS SRP"])
        self.assertTrue(self.srp.tags_edited)
        self.client.post(url, {"action": "remove", "tag": self.blops.pk})
        self.assertEqual(self.srp.tags.count(), 0)
        rule("subject", "plain", self.sanction)
        self.assertEqual(tagging.retag_all(), (0, 0))                                      # a hand edit is respected

    def test_bad_input_changes_nothing(self):
        url = reverse("yagas_srp:edit_tags", args=[self.srp.pk])
        self.client.post(url, {"action": "add", "tag": 999999})
        self.client.post(url, {"action": "dance", "tag": self.blops.pk})
        self.srp.refresh_from_db()
        self.assertEqual((self.srp.tags.count(), self.srp.tags_edited), (0, False))

    def test_the_detail_page_offers_the_tags_to_a_manager(self):
        self.srp.tags.add(self.blops)
        html = self.client.get(reverse("yagas_srp:request_detail", args=[self.srp.pk])).content.decode()
        self.assertIn("BLOPS SRP", html)
        self.assertIn('value="remove"', html)
        self.assertIn("SANCTION SRP", html)                                                # offered in the add list


class SeedAndAdminTests(Base):
    def test_the_seed_gives_the_two_tags_the_rules_and_the_viewer_group_once(self):
        seed = importlib.import_module("yagas_srp.migrations.0007_seed_tags").seed
        from django.apps import apps

        SrpTag.objects.all().delete()
        Group.objects.filter(name="BLOPS SRP").delete()
        seed(apps, None)
        seed(apps, None)                                                                   # again: nothing doubles
        self.assertEqual(sorted(SrpTag.objects.values_list("name", flat=True)), ["BLOPS SRP", "SANCTION SRP"])
        self.assertEqual(SrpTagRule.objects.count(), 3)
        blops = SrpTag.objects.get(slug="blops-srp")
        self.assertEqual([g.name for g in blops.viewer_groups.all()], ["BLOPS SRP"])
        hunt = SrpTagRule.objects.get(kind="subject", keyword="HUNT")
        self.assertEqual(sorted(t.name for t in hunt.tags.all()), ["BLOPS SRP", "SANCTION SRP"])
        self.assertEqual(sorted(t.name for t in SrpTagRule.objects.get(kind="subject", keyword="TARGET").tags.all()), ["BLOPS SRP", "SANCTION SRP"])
        self.assertEqual([t.name for t in SrpTagRule.objects.get(kind="hull").tags.all()], ["BLOPS SRP"])
        self.assertEqual([t.name for t in tagging.matching_tags("HUNT", "", None)], ["BLOPS SRP", "SANCTION SRP"])

    def test_hunt_target_and_hunter_say_what_kind_of_request_it_is(self):
        from django.apps import apps

        seed7 = importlib.import_module("yagas_srp.migrations.0007_seed_tags").seed
        seed8 = importlib.import_module("yagas_srp.migrations.0008_hunt_target_tags").seed
        SrpTag.objects.all().delete()
        Group.objects.filter(name="BLOPS SRP").delete()
        seed7(apps, None)
        seed8(apps, None)
        seed8(apps, None)                                                                  # again: nothing doubles
        self.assertEqual(sorted(SrpTag.objects.values_list("name", flat=True)), ["BLOPS SRP", "HUNT", "SANCTION SRP", "TARGET"])
        self.assertEqual(SrpTagRule.objects.count(), 4)
        names = lambda subject: [t.name for t in tagging.matching_tags(subject, "", None)]
        self.assertEqual(names("HUNT"), ["BLOPS SRP", "SANCTION SRP", "HUNT"])
        self.assertEqual(names("srp for a hunter"), ["BLOPS SRP", "SANCTION SRP", "HUNT"])        # HUNTER counts like HUNT
        self.assertEqual(names("TARGET"), ["BLOPS SRP", "SANCTION SRP", "TARGET"])
        self.assertEqual(names("hunters"), [])                                             # whole words only

    def test_the_admin_pages_and_the_apply_action(self):
        admin = make_user("srp_tag_admin", 6201)
        admin.is_superuser = admin.is_staff = True
        admin.save()
        self.client.force_login(admin)
        rule("subject", "HUNT", self.blops)
        request(30, subject="HUNT now")
        for name in ("srptag", "srptagrule", "srprequest"):
            self.assertEqual(self.client.get(reverse(f"admin:yagas_srp_{name}_changelist")).status_code, 200, name)
        response = self.client.post(reverse("admin:yagas_srp_srptagrule_changelist"), {"action": "apply_to_all_requests", "_selected_action": [SrpTagRule.objects.get().pk]}, follow=True)
        self.assertContains(response, "1 request(s) got 1 new tag(s)")
