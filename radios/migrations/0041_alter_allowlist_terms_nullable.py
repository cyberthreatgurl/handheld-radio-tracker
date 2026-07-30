# Generated manually — alters allowlist_terms to be nullable after the
# initial migration (0040) was applied with null=False, causing
# psycopg2.errors.NotNullViolation for existing NULL rows.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0040_add_allowlist_terms_field'),
    ]

    operations = [
        migrations.AlterField(
            model_name='radio',
            name='allowlist_terms',
            field=models.JSONField(blank=True, default=list, null=True, help_text="Allowlist terms that matched this radio's FCC record during ingestion (searchable for radio type filtering)"),
        ),
    ]
