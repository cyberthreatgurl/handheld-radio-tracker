from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0025_alter_fccsyncstate_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='IgnoredGrantee',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('grantee_code', models.CharField(db_index=True, help_text='FCC grantee code to exclude from sync/import workflows.', max_length=20, unique=True)),
                ('reason', models.CharField(blank=True, help_text='Short reason this grantee should be ignored.', max_length=255)),
                ('notes', models.TextField(blank=True, help_text='Optional notes about why this grantee is out of scope.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Ignored Grantee ID',
                'verbose_name_plural': 'Ignored Grantee IDs',
                'ordering': ['grantee_code'],
            },
        ),
    ]