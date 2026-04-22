from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mailops", "0008_gmail_import_user_scope"),
    ]

    operations = [
        migrations.CreateModel(
            name="AddressBookContact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("display_name", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "source",
                    models.CharField(
                        choices=[("manual", "Manual"), ("auto", "Auto")],
                        db_index=True,
                        default="manual",
                        max_length=16,
                    ),
                ),
                ("times_contacted", models.PositiveIntegerField(default=0)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="address_book_contacts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Address book contact",
                "verbose_name_plural": "Address book contacts",
                "ordering": ["display_name", "email"],
            },
        ),
        migrations.AddConstraint(
            model_name="addressbookcontact",
            constraint=models.UniqueConstraint(fields=("user", "email"), name="uniq_address_book_contact_user_email"),
        ),
        migrations.AddIndex(
            model_name="addressbookcontact",
            index=models.Index(fields=["user"], name="addrbook_user_idx"),
        ),
        migrations.AddIndex(
            model_name="addressbookcontact",
            index=models.Index(fields=["user", "email"], name="addrbook_user_email_idx"),
        ),
        migrations.AddIndex(
            model_name="addressbookcontact",
            index=models.Index(fields=["user", "display_name"], name="addrbook_user_name_idx"),
        ),
    ]
