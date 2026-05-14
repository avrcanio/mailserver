from django.db import migrations


# 0015 originally used a 31-character index name, which fails Django's E034
# (max 30). If anyone applied that with --skip-checks, the DB still has the long
# name while code expects mailmove_dst_usr_acc_used_idx.
_RENAME_INDEX_SQL = """
DO $rename_mailmove_idx$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind = 'i'
      AND c.relname = 'mailmove_dest_user_acc_used_idx'
  ) THEN
    IF EXISTS (
      SELECT 1
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public'
        AND c.relkind = 'i'
        AND c.relname = 'mailmove_dst_usr_acc_used_idx'
    ) THEN
      DROP INDEX IF EXISTS public.mailmove_dest_user_acc_used_idx;
    ELSE
      ALTER INDEX public.mailmove_dest_user_acc_used_idx
        RENAME TO mailmove_dst_usr_acc_used_idx;
    END IF;
  END IF;
END
$rename_mailmove_idx$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0015_mailbox_move_destination_history"),
    ]

    operations = [
        migrations.RunSQL(
            sql=_RENAME_INDEX_SQL,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
