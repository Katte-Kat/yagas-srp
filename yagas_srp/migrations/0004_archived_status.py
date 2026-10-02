from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("yagas_srp", "0003_price_source"),
    ]

    operations = [
        migrations.AlterField(
            model_name="srprequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"), ("approved", "Approved"), ("denied", "Denied"),
                    ("invalid", "Invalid"), ("archived", "Archived"),
                ],
                db_index=True, default="pending", max_length=10),
        ),
    ]
