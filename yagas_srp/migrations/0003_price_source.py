from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("yagas_srp", "0002_whole_isk_amounts"),
    ]

    operations = [
        migrations.AddField("srprequest", "ship_price_source", models.CharField(blank=True, default="", max_length=8)),
        migrations.AddField("srprequest", "priced_at", models.DateTimeField(blank=True, null=True)),
        migrations.AddField("srprequestitem", "price_source", models.CharField(blank=True, default="", max_length=8)),
    ]
