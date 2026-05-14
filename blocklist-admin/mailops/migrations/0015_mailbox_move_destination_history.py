from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("mailops", "0014_receipt_ocr_log_ocr_excerpt"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MailboxMoveDestinationHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_email", models.EmailField(db_index=True, max_length=254)),
                ("target_folder", models.CharField(max_length=255)),
                ("last_used_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="mailbox_move_destinations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-last_used_at"],
                "verbose_name": "Mailbox move destination history",
                "verbose_name_plural": "Mailbox move destination histories",
            },
        ),
        migrations.AddIndex(
            model_name="mailboxmovedestinationhistory",
            index=models.Index(fields=["user", "account_email", "last_used_at"], name="mailmove_dst_usr_acc_used_idx"),
        ),
        migrations.AddConstraint(
            model_name="mailboxmovedestinationhistory",
            constraint=models.UniqueConstraint(
                fields=("user", "account_email", "target_folder"),
                name="uniq_mailbox_move_dest_user_account_folder",
            ),
        ),
    ]
