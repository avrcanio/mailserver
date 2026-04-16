from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviceRegistration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_email", models.EmailField(db_index=True, max_length=254)),
                ("fcm_token", models.TextField(unique=True)),
                (
                    "platform",
                    models.CharField(
                        choices=[
                            ("android", "Android"),
                            ("ios", "iOS"),
                            ("web", "Web"),
                            ("unknown", "Unknown"),
                        ],
                        default="unknown",
                        max_length=16,
                    ),
                ),
                ("app_version", models.CharField(blank=True, default="", max_length=64)),
                ("enabled", models.BooleanField(default=True)),
                ("last_seen_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Device registration",
                "verbose_name_plural": "Device registrations",
                "ordering": ["account_email", "-last_seen_at"],
            },
        ),
        migrations.CreateModel(
            name="PushNotificationLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_email", models.EmailField(db_index=True, max_length=254)),
                ("sender", models.CharField(blank=True, default="", max_length=255)),
                ("subject", models.CharField(blank=True, default="", max_length=255)),
                ("message_id", models.CharField(blank=True, default="", max_length=512)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("success", "Success"),
                            ("partial", "Partial"),
                            ("skipped", "Skipped"),
                            ("error", "Error"),
                        ],
                        max_length=16,
                    ),
                ),
                ("device_count", models.PositiveIntegerField(default=0)),
                ("success_count", models.PositiveIntegerField(default=0)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Push notification log",
                "verbose_name_plural": "Push notification logs",
                "ordering": ["-created_at"],
            },
        ),
    ]
