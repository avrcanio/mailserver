from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0006_mail_indexing"),
    ]

    operations = [
        migrations.CreateModel(
            name="GmailImportAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("gmail_email", models.EmailField(db_index=True, max_length=254, unique=True)),
                ("target_mailbox_email", models.EmailField(db_index=True, max_length=254)),
                ("refresh_token", models.TextField()),
                ("last_history_id", models.CharField(blank=True, default="", max_length=128)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("historical_import_completed_at", models.DateTimeField(blank=True, null=True)),
                ("consecutive_failures", models.PositiveIntegerField(default=0)),
                ("delete_after_import", models.BooleanField(default=False)),
                ("last_error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Gmail import account",
                "verbose_name_plural": "Gmail import accounts",
                "ordering": ["gmail_email"],
            },
        ),
        migrations.CreateModel(
            name="GmailImportMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("gmail_message_id", models.CharField(max_length=255)),
                ("gmail_thread_id", models.CharField(blank=True, default="", max_length=255)),
                ("rfc_message_id", models.CharField(blank=True, default="", max_length=998)),
                ("target_folder", models.CharField(blank=True, default="", max_length=255)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("fetched", "Fetched"),
                            ("appended", "Appended"),
                            ("committed", "Committed"),
                            ("cleaned", "Cleaned"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="fetched",
                        max_length=16,
                    ),
                ),
                (
                    "append_status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("success", "Success"), ("failed", "Failed"), ("skipped", "Skipped")],
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "cleanup_status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("success", "Success"), ("failed", "Failed"), ("skipped", "Skipped")],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("fetched_at", models.DateTimeField(blank=True, null=True)),
                ("appended_at", models.DateTimeField(blank=True, null=True)),
                ("committed_at", models.DateTimeField(blank=True, null=True)),
                ("cleaned_at", models.DateTimeField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "import_account",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="mailops.gmailimportaccount"),
                ),
            ],
            options={
                "verbose_name": "Gmail import message",
                "verbose_name_plural": "Gmail import messages",
                "ordering": ["import_account", "gmail_message_id"],
            },
        ),
        migrations.CreateModel(
            name="GmailImportRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "mode",
                    models.CharField(
                        choices=[("historical", "Historical"), ("incremental", "Incremental"), ("dry_run", "Dry run")],
                        default="historical",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("running", "Running"), ("success", "Success"), ("partial", "Partial"), ("failed", "Failed")],
                        db_index=True,
                        default="running",
                        max_length=16,
                    ),
                ),
                ("scanned_count", models.PositiveIntegerField(default=0)),
                ("appended_count", models.PositiveIntegerField(default=0)),
                ("committed_count", models.PositiveIntegerField(default=0)),
                ("cleaned_count", models.PositiveIntegerField(default=0)),
                ("skipped_count", models.PositiveIntegerField(default=0)),
                ("failed_count", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "import_account",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="runs", to="mailops.gmailimportaccount"),
                ),
            ],
            options={
                "verbose_name": "Gmail import run",
                "verbose_name_plural": "Gmail import runs",
                "ordering": ["-started_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="gmailimportmessage",
            constraint=models.UniqueConstraint(fields=("import_account", "gmail_message_id"), name="uniq_gmail_import_msg_acct_id"),
        ),
        migrations.AddIndex(
            model_name="gmailimportmessage",
            index=models.Index(fields=["import_account", "state"], name="gmailmsg_account_state_idx"),
        ),
        migrations.AddIndex(
            model_name="gmailimportmessage",
            index=models.Index(fields=["import_account", "rfc_message_id"], name="gmailmsg_account_rfcid_idx"),
        ),
        migrations.AddIndex(
            model_name="gmailimportrun",
            index=models.Index(fields=["import_account", "started_at"], name="gmailrun_account_started_idx"),
        ),
        migrations.AddIndex(
            model_name="gmailimportrun",
            index=models.Index(fields=["import_account", "status"], name="gmailrun_account_status_idx"),
        ),
    ]
