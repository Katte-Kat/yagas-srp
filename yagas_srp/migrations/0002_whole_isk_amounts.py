from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models

ISK = Decimal("1")


def _round_existing(apps, schema_editor):
    """Round stored amounts to whole ISK first so narrowing the columns never truncates."""
    SrpRequest = apps.get_model("yagas_srp", "SrpRequest")
    SrpRequestItem = apps.get_model("yagas_srp", "SrpRequestItem")

    def whole(value):
        return None if value is None else value.quantize(ISK, rounding=ROUND_HALF_UP)

    for model, fields in (
        (SrpRequest, ["ship_value", "fit_value", "cargo_value", "total_value", "approved_amount"]),
        (SrpRequestItem, ["unit_price", "value"]),
    ):
        batch = []
        for row in model.objects.all().iterator():
            for field in fields:
                setattr(row, field, whole(getattr(row, field)))
            batch.append(row)
            if len(batch) >= 500:
                model.objects.bulk_update(batch, fields)
                batch = []
        if batch:
            model.objects.bulk_update(batch, fields)


class Migration(migrations.Migration):

    dependencies = [
        ("yagas_srp", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(_round_existing, migrations.RunPython.noop),
        migrations.AlterField("srprequest", "ship_value", models.DecimalField(decimal_places=0, default=0, max_digits=20)),
        migrations.AlterField("srprequest", "fit_value", models.DecimalField(decimal_places=0, default=0, max_digits=20)),
        migrations.AlterField("srprequest", "cargo_value", models.DecimalField(decimal_places=0, default=0, max_digits=20)),
        migrations.AlterField("srprequest", "total_value", models.DecimalField(decimal_places=0, default=0, max_digits=20)),
        migrations.AlterField("srprequest", "approved_amount", models.DecimalField(blank=True, decimal_places=0, max_digits=20, null=True)),
        migrations.AlterField("srprequestitem", "unit_price", models.DecimalField(blank=True, decimal_places=0, max_digits=20, null=True)),
        migrations.AlterField("srprequestitem", "value", models.DecimalField(decimal_places=0, default=0, max_digits=20)),
    ]
