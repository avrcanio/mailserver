from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0010_mail_message_translation"),
    ]

    operations = [
        migrations.AddField(
            model_name="mailmessagetranslation",
            name="translated_html",
            field=models.TextField(blank=True, default=""),
        ),
    ]

