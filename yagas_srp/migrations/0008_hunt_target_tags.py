from django.db import migrations


def seed(apps, schema_editor):
    """Tags that say what kind of request it is, shown in the list: HUNT and TARGET. The subject words HUNT and TARGET give them (besides the two tags they gave before), and HUNTER counts
    like HUNT. Idempotent; a rule that somebody deleted or a tag that somebody renamed is not brought back."""
    SrpTag = apps.get_model("yagas_srp", "SrpTag")
    SrpTagRule = apps.get_model("yagas_srp", "SrpTagRule")
    blops = SrpTag.objects.filter(slug="blops-srp").first()
    sanction = SrpTag.objects.filter(slug="sanction-srp").first()
    hunt, _ = SrpTag.objects.get_or_create(slug="hunt", defaults={"name": "HUNT", "color": "#2f9e44", "order": 30, "description": "A hunter's loss"})
    target, _ = SrpTag.objects.get_or_create(slug="target", defaults={"name": "TARGET", "color": "#e03131", "order": 40, "description": "A target hunt"})
    for keyword, kind_tag in (("HUNT", hunt), ("TARGET", target)):
        rule = SrpTagRule.objects.filter(kind="subject", keyword__iexact=keyword).first()
        if rule is not None:
            rule.tags.add(kind_tag)
    if not SrpTagRule.objects.filter(kind="subject", keyword__iexact="HUNTER").exists():
        rule = SrpTagRule.objects.create(kind="subject", keyword="HUNTER", note="A hunter writes HUNTER in the SRP mail (same as HUNT)")
        rule.tags.add(*[t for t in (blops, sanction, hunt) if t is not None])


class Migration(migrations.Migration):

    dependencies = [
        ("yagas_srp", "0007_seed_tags"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
