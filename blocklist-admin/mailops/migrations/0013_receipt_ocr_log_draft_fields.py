from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0012_receipt_ocr_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="receiptocrlog",
            name="draft_subject",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="receiptocrlog",
            name="draft_body",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="receiptocrlog",
            name="openai_model",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="receiptocrlog",
            name="openai_duration_ms",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="receiptocrlog",
            name="warnings",
            field=models.TextField(blank=True, default=""),
        ),
    ]

