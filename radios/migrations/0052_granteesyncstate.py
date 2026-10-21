from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0051_userprofile_account_type_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='GranteeSyncState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('grantee_code', models.CharField(db_index=True, help_text="FCC grantee code (e.g. '2ASNS').", max_length=20, unique=True)),
                ('last_synced_at', models.DateTimeField(help_text='Grants dated on or after this day are processed by the next grantee sync. Clear this row to force a full-history sync for the grantee.')),
            ],
            options={
                'verbose_name': 'Grantee Sync State',
                'verbose_name_plural': 'Grantee Sync State',
                'ordering': ['grantee_code'],
            },
        ),
    ]
