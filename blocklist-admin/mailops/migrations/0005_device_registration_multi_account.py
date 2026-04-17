from django.db import migrations, models


def normalize_device_registration_accounts(apps, schema_editor):
    DeviceRegistration = apps.get_model("mailops", "DeviceRegistration")
    for registration in DeviceRegistration.objects.all().iterator():
        normalized_email = (registration.account_email or "").strip().lower()
        if normalized_email == registration.account_email:
            continue
        registration.account_email = normalized_email
        registration.save(update_fields=["account_email"])


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0004_encrypt_mailbox_token_credentials"),
    ]

    operations = [
        migrations.RunPython(normalize_device_registration_accounts, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="deviceregistration",
            name="fcm_token",
            field=models.TextField(),
        ),
        migrations.AddConstraint(
            model_name="deviceregistration",
            constraint=models.UniqueConstraint(
                fields=("account_email", "fcm_token"),
                name="uniq_device_registration_account_fcm_token",
            ),
        ),
    ]
