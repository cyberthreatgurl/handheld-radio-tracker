from django import forms
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory
from .models import (
    Radio, Brand, RadioCertification, RadioFirmware,
    RadioServiceType, Manufacturer, RadioManual, RadioImage,
)


class RadioForm(forms.ModelForm):
    """Form for creating and editing radio entries"""
    
    # Override brand field to use a dropdown of existing brands
    brand = forms.ChoiceField(
        choices=[],
        widget=forms.Select(attrs={
            'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
        }),
        help_text="Select the radio manufacturer/brand"
    )

    # Override manufacturer to use the Manufacturer model (not Brand)
    manufacturer = forms.ModelChoiceField(
        queryset=Manufacturer.objects.order_by('alias', 'full_name'),
        required=False,
        empty_label='— Not specified —',
        widget=forms.Select(attrs={
            'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
        }),
        help_text="The legal manufacturing entity that built this radio"
    )

    service_types = forms.ModelMultipleChoiceField(
        queryset=RadioServiceType.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={
            'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded',
        }),
        help_text="Service classifications for this radio (GMRS, FRS, Amateur, etc.)"
    )

    class Meta:
        model = Radio
        fields = [
            'brand', 'model', 'is_a_whitelabel', 'manufacturer', 'radio_type',
            'service_types', 'fcc_id',
            'freq_bands_tx', 'power_watts',
            'satellite_tracking', 'harmonic_suppression',
            'gps', 'aprs', 'air_band', 'dmr',
            'display', 'battery_mah',
            'usb_c_charging', 'removable_antenna', 'unlockable', 'firmware_updates',
            'cost_approx', 'rebadges_clones', 'white_label_vendors', 'website', 'review_url',
            'youtube_video_urla', 'notes'
        ]
        widgets = {
            'is_a_whitelabel': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded'
            }),
            'model': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., UV-5R, IC-7300'
            }),
            'radio_type': forms.Select(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            }),
            'fcc_id': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., 2AJGM-UV5R'
            }),
            'freq_bands_tx': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., VHF, UHF, 220'
            }),
            'power_watts': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., 5W, 10W'
            }),
            'satellite_tracking': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Yes, No, Native'
            }),
            'harmonic_suppression': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Good, Excellent, Poor, Unknown'
            }),
            'gps': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Yes, No, Optional'
            }),
            'aprs': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Yes, Analog, Digital, Beacon'
            }),
            'air_band': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Yes, No'
            }),
            'dmr': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Yes, No'
            }),
            'display': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., LCD, Color TFT, Dot-matrix'
            }),
            'battery_mah': forms.NumberInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., 2500'
            }),
            'white_label_vendors': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., BTech, Retevis'
            }),
            'cost_approx': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., $50, $100-150'
            }),
            'rebadges_clones': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 3,
                'placeholder': 'Known rebadges or clones...'
            }),
            'website': forms.URLInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'https://manufacturer.com'
            }),
            'review_url': forms.URLInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'https://eham.net/reviews/...'
            }),
            'youtube_video_urla': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 4,
                'placeholder': 'https://youtube.com/watch?v=...\nhttps://youtu.be/...'
            }),
            'notes': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 4,
                'placeholder': 'Additional notes or specifications...'
            }),
            'usb_c_charging': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded'
            }),
            'removable_antenna': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded'
            }),
            'unlockable': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded'
            }),
            'firmware_updates': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate brand choices from Brand table, sorted alphabetically
        brand_choices = [('', '-- Select a Brand --')]
        brand_choices += [(b.name, b.name) for b in Brand.objects.all().order_by('name')]
        self.fields['brand'].choices = brand_choices

    def clean_youtube_video_urla(self):
        value = (self.cleaned_data.get('youtube_video_urla') or '').strip()
        if not value:
            return ''

        validator = URLValidator(schemes=['http', 'https'])
        cleaned_urls = []
        for line in value.splitlines():
            url = line.strip()
            if not url:
                continue
            try:
                validator(url)
            except ValidationError as exc:
                raise forms.ValidationError(f'Invalid URL in YouTube Video URLs: {url}') from exc
            cleaned_urls.append(url)
        return '\n'.join(cleaned_urls)


class BrandForm(forms.ModelForm):
    """Form for creating and editing radio brands"""

    _css = ('mt-1 block w-full rounded-md border-gray-300 shadow-sm '
            'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm')

    parent_brand = forms.ModelChoiceField(
        queryset=Brand.objects.none(),  # set dynamically in __init__
        required=False,
        empty_label='— None —',
        widget=forms.Select(attrs={'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'}),
        help_text="Only for subsidiary or shell-company relationships (e.g. EVOTE → Wouxun). Not for OEM.",
    )

    manufacturer_oem = forms.ModelChoiceField(
        queryset=Manufacturer.objects.order_by('full_name'),
        required=False,
        empty_label='— None —',
        label='Manufacturer (OEM)',
        widget=forms.Select(attrs={'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'}),
        help_text='The legal manufacturing entity (OEM) that produces this brand.',
    )

    white_label_vendors = forms.MultipleChoiceField(
        choices=[],  # populated in __init__
        required=False,
        widget=forms.SelectMultiple(attrs={
            'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            'size': '8',
        }),
        help_text="Hold Ctrl / Cmd to select multiple. Select all brands that sell white-label versions of this radio.",
    )

    class Meta:
        model = Brand
        fields = [
            'name', 'alias', 'grantee_code', 'parent_brand',
            'country', 'website', 'white_label_vendors', 'notes'
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Baofeng, Icom'
            }),
            'alias': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., Senhaix'
            }),
            'grantee_code': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., 2AJGM'
            }),
            'country': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., China, Japan'
            }),
            'website': forms.URLInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'https://manufacturer.com'
            }),
            'notes': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 4,
                'placeholder': 'Additional notes about the manufacturer...'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # parent_brand dropdown — exclude self to prevent circular reference
        qs = Brand.objects.all().order_by('name')
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields['parent_brand'].queryset = qs

        # manufacturer_oem — pre-select current primary manufacturer from M2M
        if self.instance and self.instance.pk:
            self.fields['manufacturer_oem'].initial = self.instance.manufacturers.first()

        # white_label_vendors multi-select — choices are all brand names
        brand_names = list(Brand.objects.values_list('name', flat=True).order_by('name'))
        self.fields['white_label_vendors'].choices = [(n, n) for n in brand_names]

        # Pre-select existing comma-separated values
        if self.instance and self.instance.pk:
            existing = (self.instance.white_label_vendors or '').strip()
            if existing:
                self.initial['white_label_vendors'] = [
                    v.strip() for v in existing.split(',') if v.strip()
                ]

    def clean_white_label_vendors(self):
        """Join multi-select list back to comma-separated string for storage."""
        values = self.cleaned_data.get('white_label_vendors') or []
        return ', '.join(values)

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            mfr = self.cleaned_data.get('manufacturer_oem')
            # Replace the brand's manufacturers M2M with the selected OEM (single)
            instance.manufacturers.clear()
            if mfr:
                instance.manufacturers.add(mfr)
        return instance

class RadioSearchForm(forms.Form):
    """Form for searching radios"""

    query = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            'placeholder': 'Search by brand, model, or FCC ID...'
        })
    )
    
    brand = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            'placeholder': 'Filter by brand...'
        })
    )


class ImportGranteeXMLForm(forms.Form):
    """Form for uploading FCC XML files"""
    
    xml_file = forms.FileField(
        label="FCC XML File",
        help_text="Upload FCC XML file for a specific grantee."
    )
    overwrite_records = forms.BooleanField(
        label="Overwrite existing records",
        required=False,
        help_text="If checked, existing records will be updated."
    )


class DocumentUploadForm(forms.Form):
    """Simple form for uploading any document file linked to an optional radio."""

    _css = ('mt-1 block w-full rounded-md border-gray-300 shadow-sm '
            'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm')

    radio = forms.ModelChoiceField(
        queryset=Radio.objects.all().order_by('brand', 'model'),
        required=False,
        empty_label='— Not linked to a specific radio —',
        widget=forms.Select(attrs={'class': _css}),
        help_text='Optionally link this file to a radio record.',
    )
    doc_type = forms.ChoiceField(
        choices=RadioManual.DocType.choices,
        label='Document Type',
        widget=forms.Select(attrs={'class': _css}),
    )
    document_file = forms.FileField(
        label='File',
        widget=forms.ClearableFileInput(attrs={
            'class': 'mt-1 block w-full text-sm text-gray-700',
        }),
        help_text='Select any file to upload (PDF, binary, etc.).',
    )


_FIELD_CSS = 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'


class RadioFirmwareForm(forms.ModelForm):
    """Inline form for a single firmware version entry."""

    class Meta:
        model = RadioFirmware
        fields = ['label', 'version', 'download_url', 'firmware_file', 'notes']
        widgets = {
            'label': forms.TextInput(attrs={
                'class': _FIELD_CSS,
                'placeholder': "e.g., Main Radio or APRS Module",
            }),
            'version': forms.TextInput(attrs={
                'class': _FIELD_CSS,
                'placeholder': "e.g., v1.23",
            }),
            'download_url': forms.URLInput(attrs={
                'class': _FIELD_CSS,
                'placeholder': 'https://manufacturer.com/firmware',
            }),
            'firmware_file': forms.ClearableFileInput(attrs={
                'class': 'mt-1 block w-full text-sm text-gray-700',
            }),
            'notes': forms.Textarea(attrs={
                'class': _FIELD_CSS,
                'rows': 2,
                'placeholder': 'Optional notes about this firmware version...',
            }),
        }


RadioFirmwareFormSet = inlineformset_factory(
    Radio,
    RadioFirmware,
    form=RadioFirmwareForm,
    extra=2,
    max_num=2,
    can_delete=True,
)


class ManufacturerForm(forms.ModelForm):
    """Form for creating and editing Manufacturer records."""

    brands = forms.ModelMultipleChoiceField(
        queryset=Brand.objects.all().order_by('name'),
        required=False,
        widget=forms.SelectMultiple(attrs={
            'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                     'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            'size': '12',
            'id': 'id_brands',
        }),
        help_text="Hold Ctrl / Cmd to select multiple brands.",
    )

    class Meta:
        model = Manufacturer
        fields = ['full_name', 'alias', 'brands', 'website', 'country', 'address', 'notes']
        widgets = {
            'full_name': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': "e.g. Hiroyasu Electronics (Hong Kong) Co., Ltd",
            }),
            'alias': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': "e.g. Hiroyasu",
            }),
            'website': forms.URLInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'https://manufacturer.com',
            }),
            'country': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g. China, Japan',
            }),
            'address': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 3,
                'placeholder': 'e.g. 123 Industrial Rd, Shenzhen, Guangdong, China 518000',
            }),
            'notes': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 4,
                'placeholder': 'Additional notes…',
            }),
        }


class RadioImageForm(forms.ModelForm):
    """Form for uploading or URL-importing a single RadioImage."""

    # URL import alternative to direct file upload.
    image_url = forms.URLField(
        required=False,
        label='Image URL',
        widget=forms.URLInput(attrs={
            'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                     'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            'placeholder': 'https://example.com/radio.jpg',
        }),
        help_text='Paste an image URL, or upload a file above — not both.',
    )

    class Meta:
        model = RadioImage
        fields = ['image_file', 'caption', 'sort_order']
        widgets = {
            'image_file': forms.ClearableFileInput(attrs={
                'class': 'mt-1 block w-full text-sm text-gray-700',
                'accept': 'image/*',
            }),
            'caption': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'Optional caption…',
            }),
            'sort_order': forms.NumberInput(attrs={
                'class': 'mt-1 block w-20 rounded-md border-gray-300 shadow-sm '
                         'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'min': '0',
            }),
        }

    def clean(self):
        cleaned = super().clean()
        has_file = bool(cleaned.get('image_file'))
        has_url = bool((cleaned.get('image_url') or '').strip())
        # For existing records (with image_file already set), neither field is
        # required unless the user explicitly clears the existing file.
        is_existing = bool(self.instance and self.instance.pk and self.instance.image_file)
        if has_file and has_url:
            raise ValidationError('Provide either a file upload or a URL, not both.')
        if not is_existing and not has_file and not has_url and not self.cleaned_data.get('DELETE', False):
            # Only raise if this form has any other data (i.e. it’s not a blank extra row)
            other_data = any(
                v for k, v in cleaned.items()
                if k not in ('image_file', 'image_url', 'sort_order', 'DELETE', 'id')
            )
            if other_data:
                raise ValidationError('Provide either a file upload or a URL.')
        return cleaned


RadioImageFormSet = inlineformset_factory(
    Radio,
    RadioImage,
    form=RadioImageForm,
    extra=1,
    can_delete=True,
)


class RadioCertificationForm(forms.ModelForm):
    """Inline form for a single FCC certification entry."""

    class Meta:
        model = RadioCertification
        fields = [
            'fcc_id', 'grant_date', 'authorization_type', 'rule_parts',
            'freq_range_lower_mhz', 'freq_range_upper_mhz',
            'power_output_watts', 'power_type', 'emission_designators',
        ]
        widgets = {
            'fcc_id': forms.TextInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': 'e.g., 2AJGM-UV5R',
            }),
            'grant_date': forms.DateInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'type': 'date',
            }),
            'authorization_type': forms.Select(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
            }),
            'rule_parts': forms.TextInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': "e.g., Part 95E, Part 90",
            }),
            'freq_range_lower_mhz': forms.NumberInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': 'e.g., 462.5500',
            }),
            'freq_range_upper_mhz': forms.NumberInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': 'e.g., 467.7250',
            }),
            'power_output_watts': forms.NumberInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': 'e.g., 2.0',
            }),
            'power_type': forms.TextInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': 'ERP, EIRP, or Conducted',
            }),
            'emission_designators': forms.TextInput(attrs={
                'class': (
                    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
                    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
                ),
                'placeholder': "e.g., 11K0F3E, 7K60FXD",
            }),
        }


RadioCertificationFormSet = inlineformset_factory(
    Radio,
    RadioCertification,
    form=RadioCertificationForm,
    extra=1,
    can_delete=True,
)
