from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mailops", "0009_address_book_contact"),
    ]

    operations = [
        migrations.CreateModel(
            name="MailMessageTranslation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_email", models.EmailField(db_index=True, max_length=254)),
                ("folder", models.CharField(max_length=255)),
                ("uid", models.CharField(max_length=64)),
                ("message_id", models.CharField(blank=True, default="", max_length=512)),
                ("target_language", models.CharField(db_index=True, max_length=16)),
                ("source_hash", models.CharField(max_length=64)),
                ("source_language", models.CharField(blank=True, default="", max_length=32)),
                ("translated_subject", models.TextField(blank=True, default="")),
                ("translated_text", models.TextField(blank=True, default="")),
                ("model", models.CharField(blank=True, default="", max_length=128)),
                ("truncated", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mail_message_translations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Mail message translation",
                "verbose_name_plural": "Mail message translations",
                "ordering": ["-updated_at", "-id"],
            },
        ),
        migrations.AddConstraint(
            model_name="mailmessagetranslation",
            constraint=models.UniqueConstraint(
                fields=("user", "account_email", "folder", "uid", "target_language", "source_hash"),
                name="uniq_mail_translation_user_message_language_hash",
            ),
        ),
        migrations.AddIndex(
            model_name="mailmessagetranslation",
            index=models.Index(fields=["user", "account_email"], name="mailtrans_user_account_idx"),
        ),
        migrations.AddIndex(
            model_name="mailmessagetranslation",
            index=models.Index(fields=["account_email", "folder", "uid"], name="mailtrans_message_idx"),
        ),
    ]
