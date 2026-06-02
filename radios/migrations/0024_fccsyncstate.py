from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0023_radio_manufacturer_fk_to_manufacturer'),
    ]

    operations = [
        migrations.CreateModel(
            name='FCCSyncState',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_grantee_sync_at', models.DateTimeField(
                    blank=True,
                    null=True,
                    help_text=(
                        "Timestamp of the last successful 'Update All Known Grantees' FCC sync. "
                        "Used as the start-date filter on subsequent runs to avoid re-fetching the full history."
                    ),
                )),
            ],
            options={
                'verbose_name': 'FCC Sync State',
                'verbose_name_plural': 'FCC Sync State',
            },
        ),
    ]
