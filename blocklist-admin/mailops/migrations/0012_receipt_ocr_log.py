from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0011_mail_message_translation_html"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReceiptOcrLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_email", models.EmailField(db_index=True, max_length=254)),
                ("artifacts_dir", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="receipt_ocr_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
                "verbose_name": "Receipt OCR log",
                "verbose_name_plural": "Receipt OCR logs",
            },
        ),
    ]

