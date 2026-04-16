from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("authtoken", "0004_alter_tokenproxy_options"),
        ("mailops", "0002_push_notifications"),
    ]

    operations = [
        migrations.CreateModel(
            name="MailboxTokenCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("mailbox_email", models.EmailField(db_index=True, max_length=254, unique=True)),
                ("mailbox_password", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "token",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mailbox_credential",
                        to="authtoken.token",
                    ),
                ),
            ],
            options={
                "verbose_name": "Mailbox token credential",
                "verbose_name_plural": "Mailbox token credentials",
                "ordering": ["mailbox_email"],
            },
        ),
    ]
