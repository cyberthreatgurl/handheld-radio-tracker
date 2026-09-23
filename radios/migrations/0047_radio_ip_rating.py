from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('radios', '0046_alter_radio_satellite_tracking'),
    ]

    operations = [
        migrations.AddField(
            model_name='radio',
            name='ip_rating',
            field=models.CharField(
                blank=True,
                help_text='Ingress Protection (IP) rating (e.g., IP54, IP67, IP68)',
                max_length=50,
            ),
        ),
    ]
