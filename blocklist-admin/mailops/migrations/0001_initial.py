from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


CREATE_SENDER_BLOCKLIST_SQL = """
CREATE TABLE IF NOT EXISTS sender_blocklist_rules (
    id BIGSERIAL PRIMARY KEY,
    kind VARCHAR(32) NOT NULL,
    value VARCHAR(255) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT sender_blocklist_rules_kind_value_uniq UNIQUE (kind, value)
);
"""

DROP_SENDER_BLOCKLIST_SQL = "DROP TABLE IF EXISTS sender_blocklist_rules CASCADE;"


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(CREATE_SENDER_BLOCKLIST_SQL, DROP_SENDER_BLOCKLIST_SQL),
            ],
            state_operations=[
                migrations.CreateModel(
                    name="SenderBlocklistRule",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("kind", models.CharField(choices=[("sender_email", "Sender email"), ("sender_domain", "Sender domain")], max_length=32)),
                        ("value", models.CharField(max_length=255)),
                        ("enabled", models.BooleanField(default=True)),
                        ("note", models.TextField(blank=True, default="")),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        "verbose_name": "Sender blocklist rule",
                        "verbose_name_plural": "Sender blocklist rules",
                        "db_table": "sender_blocklist_rules",
                        "ordering": ["-enabled", "kind", "value"],
                        "constraints": [
                            models.UniqueConstraint(fields=("kind", "value"), name="sender_blocklist_rules_kind_value_uniq")
                        ],
                    },
                ),
            ],
        ),
        migrations.CreateModel(
            name="ApplyLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("success", "Success"), ("error", "Error")], max_length=16)),
                ("message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "applied_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="mailadmin_apply_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Apply log",
                "verbose_name_plural": "Apply logs",
                "ordering": ["-created_at"],
            },
        ),
    ]
