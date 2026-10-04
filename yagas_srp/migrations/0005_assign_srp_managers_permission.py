from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("yagas_srp", "0004_archived_status"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="srppermissions",
            options={
                "default_permissions": (),
                "managed": False,
                "permissions": (
                    ("view_requests", "Can view the SRP request inbox"),
                    ("approve_requests", "Can approve or deny SRP requests"),
                    ("manage_receiver", "Can link the SRP mail receiver character"),
                    ("assign_srp_managers", "Can hand out and take away the SRP Manager role"),
                ),
            },
        ),
    ]
