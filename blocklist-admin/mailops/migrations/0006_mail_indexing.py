from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mailops", "0005_device_registration_multi_account"),
    ]

    operations = [
        migrations.CreateModel(
            name="MailAccountIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_email", models.EmailField(db_index=True, max_length=254)),
                ("imap_host", models.CharField(blank=True, default="", max_length=255)),
                ("sent_folder", models.CharField(blank=True, default="", max_length=255)),
                (
                    "index_status",
                    models.CharField(
                        choices=[
                            ("empty", "Empty"),
                            ("syncing", "Syncing"),
                            ("ready", "Ready"),
                            ("partial", "Partial"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="empty",
                        max_length=16,
                    ),
                ),
                ("last_indexed_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_finished_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="mail_account_indexes", to=settings.AUTH_USER_MODEL),
                ),
            ],
            options={
                "verbose_name": "Mail account index",
                "verbose_name_plural": "Mail account indexes",
                "ordering": ["account_email"],
            },
        ),
        migrations.CreateModel(
            name="MailConversationIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("conversation_id", models.CharField(db_index=True, max_length=128)),
                ("thread_key", models.CharField(db_index=True, max_length=512)),
                ("normalized_subject", models.CharField(blank=True, default="", max_length=998)),
                ("latest_message_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("message_count", models.PositiveIntegerField(default=0)),
                ("has_unread", models.BooleanField(default=False)),
                ("has_attachments", models.BooleanField(default=False)),
                ("has_visible_attachments", models.BooleanField(default=False)),
                ("participants_json", models.JSONField(blank=True, default=list)),
                ("folders_json", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conversations", to="mailops.mailaccountindex"),
                ),
            ],
            options={
                "verbose_name": "Mail conversation index",
                "verbose_name_plural": "Mail conversation indexes",
                "ordering": ["-latest_message_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="MailFolderIndexState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("folder", models.CharField(max_length=255)),
                ("uidvalidity", models.CharField(blank=True, default="", max_length=64)),
                ("highest_indexed_uid", models.PositiveBigIntegerField(default=0)),
                ("last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="folder_states", to="mailops.mailaccountindex"),
                ),
            ],
            options={
                "verbose_name": "Mail folder index state",
                "verbose_name_plural": "Mail folder index states",
                "ordering": ["account", "folder"],
            },
        ),
        migrations.CreateModel(
            name="MailMessageIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("folder", models.CharField(db_index=True, max_length=255)),
                ("uid", models.PositiveBigIntegerField()),
                ("direction", models.CharField(choices=[("inbound", "Inbound"), ("outbound", "Outbound")], max_length=16)),
                ("message_id", models.CharField(blank=True, db_index=True, default="", max_length=998)),
                ("in_reply_to", models.CharField(blank=True, default="", max_length=998)),
                ("references_raw", models.TextField(blank=True, default="")),
                ("thread_key", models.CharField(db_index=True, max_length=512)),
                ("normalized_subject", models.CharField(blank=True, default="", max_length=998)),
                ("subject", models.CharField(blank=True, default="", max_length=998)),
                ("sender_name", models.CharField(blank=True, default="", max_length=255)),
                ("sender_email", models.EmailField(blank=True, default="", max_length=254)),
                ("sender_raw", models.CharField(blank=True, default="", max_length=998)),
                ("to_json", models.JSONField(blank=True, default=list)),
                ("cc_json", models.JSONField(blank=True, default=list)),
                ("sent_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("flags_json", models.JSONField(blank=True, default=list)),
                ("is_read", models.BooleanField(db_index=True, default=False)),
                ("size", models.PositiveBigIntegerField(default=0)),
                ("has_attachments", models.BooleanField(default=False)),
                ("has_visible_attachments", models.BooleanField(default=False)),
                ("dedupe_key", models.CharField(db_index=True, max_length=1024)),
                ("raw_headers_json", models.JSONField(blank=True, default=dict)),
                ("indexed_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="mailops.mailaccountindex"),
                ),
                (
                    "conversation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="messages",
                        to="mailops.mailconversationindex",
                    ),
                ),
            ],
            options={
                "verbose_name": "Mail message index",
                "verbose_name_plural": "Mail message indexes",
                "ordering": ["-sent_at", "-uid"],
            },
        ),
        migrations.AddConstraint(
            model_name="mailaccountindex",
            constraint=models.UniqueConstraint(fields=("user", "account_email"), name="uniq_mail_account_index_user_email"),
        ),
        migrations.AddConstraint(
            model_name="mailconversationindex",
            constraint=models.UniqueConstraint(fields=("account", "conversation_id"), name="uniq_mail_conversation_index_account_conversation"),
        ),
        migrations.AddIndex(
            model_name="mailconversationindex",
            index=models.Index(fields=["account", "latest_message_at"], name="mailconv_account_latest_idx"),
        ),
        migrations.AddIndex(
            model_name="mailconversationindex",
            index=models.Index(fields=["account", "thread_key"], name="mailconv_account_thread_idx"),
        ),
        migrations.AddConstraint(
            model_name="mailfolderindexstate",
            constraint=models.UniqueConstraint(fields=("account", "folder"), name="uniq_mail_folder_index_state_account_folder"),
        ),
        migrations.AddIndex(
            model_name="mailfolderindexstate",
            index=models.Index(fields=["account", "folder"], name="mailfolder_account_folder_idx"),
        ),
        migrations.AddConstraint(
            model_name="mailmessageindex",
            constraint=models.UniqueConstraint(fields=("account", "folder", "uid"), name="uniq_mail_message_index_account_folder_uid"),
        ),
        migrations.AddIndex(
            model_name="mailmessageindex",
            index=models.Index(fields=["account", "message_id"], name="mailmsg_account_msgid_idx"),
        ),
        migrations.AddIndex(
            model_name="mailmessageindex",
            index=models.Index(fields=["account", "thread_key"], name="mailmsg_account_thread_idx"),
        ),
        migrations.AddIndex(
            model_name="mailmessageindex",
            index=models.Index(fields=["account", "folder", "sent_at"], name="mailmsg_acc_folder_date_idx"),
        ),
        migrations.AddIndex(
            model_name="mailmessageindex",
            index=models.Index(fields=["account", "direction", "sent_at"], name="mailmsg_account_dir_date_idx"),
        ),
        migrations.AddIndex(
            model_name="mailmessageindex",
            index=models.Index(fields=["account", "dedupe_key"], name="mailmsg_account_dedupe_idx"),
        ),
    ]
