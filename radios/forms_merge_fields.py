from django import forms
from .models import Radio

class MergeRadiosFieldsForm(forms.Form):
    def __init__(self, radios, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # For each field, let user pick which radio's value to keep
        for field in [
            'brand', 'model', 'fcc_id', 'intro_year', 'freq_bands_tx', 'power_watts',
            'satellite_tracking', 'harmonic_suppression', 'gps', 'aprs', 'air_band', 'dmr',
            'display', 'battery_mah', 'cost_approx', 'rebadges_clones', 'website', 'notes']:
            choices = [(str(r.pk), getattr(r, field, '')) for r in radios]
            self.fields[field] = forms.ChoiceField(
                choices=choices,
                label=field.replace('_', ' ').title(),
                widget=forms.RadioSelect,
                required=False
            )
