from django.db import migrations

BLOPS, SANCTION = "BLOPS SRP", "SANCTION SRP"
VIEWER_GROUP = "BLOPS SRP"


def seed(apps, schema_editor):
    """The two tags and the rules the corporation asked for (idempotent; nothing that exists is changed): HUNT and TARGET in the subject of an SRP mail give both tags, a Black Ops loss gives
    BLOPS SRP, and the Auth group BLOPS SRP (empty until somebody is put in it) may look at the BLOPS SRP requests."""
    Group = apps.get_model("auth", "Group")
    SrpTag = apps.get_model("yagas_srp", "SrpTag")
    SrpTagRule = apps.get_model("yagas_srp", "SrpTagRule")
    group, _ = Group.objects.get_or_create(name=VIEWER_GROUP)
    blops, created = SrpTag.objects.get_or_create(
        slug="blops-srp", defaults={"name": BLOPS, "color": "#7b2cbf", "order": 10, "description": "Black Ops losses and hunts"})
    if created:
        blops.viewer_groups.add(group)
    sanction, _ = SrpTag.objects.get_or_create(
        slug="sanction-srp", defaults={"name": SANCTION, "color": "#e67e22", "order": 20, "description": "Sanctioned hunts and targets"})
    for kind, keyword, tags, note in (
        ("subject", "HUNT", (blops, sanction), "A hunter writes HUNT in the SRP mail"),
        ("subject", "TARGET", (blops, sanction), "A target hunt"),
        ("hull", "Black Ops", (blops,), "Every Black Ops loss"),
    ):
        if not SrpTagRule.objects.filter(kind=kind, keyword__iexact=keyword).exists():
            rule = SrpTagRule.objects.create(kind=kind, keyword=keyword, note=note)
            rule.tags.add(*tags)


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("yagas_srp", "0006_tags_and_rules"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
