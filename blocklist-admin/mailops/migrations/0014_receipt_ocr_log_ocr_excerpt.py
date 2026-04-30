from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0013_receipt_ocr_log_draft_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiptocrlog",
            name="ocr_text_excerpt",
            field=models.TextField(blank=True, default=""),
        ),
    ]

