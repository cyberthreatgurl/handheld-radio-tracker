from django import forms
from .models import Radio, Brand


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
    
    class Meta:
        model = Radio
        fields = [
            'brand', 'model', 'is_a_whitelabel', 'manufacturer', 'radio_type', 'fcc_id', 'intro_year',
            'freq_bands_tx', 'power_watts',
            'satellite_tracking', 'harmonic_suppression',
            'gps', 'aprs', 'air_band', 'dmr',
            'display', 'battery_mah',
            'cost_approx', 'rebadges_clones', 'white_label_vendors', 'website', 'review_url',
            'notes'
        ]
        widgets = {
            'is_a_whitelabel': forms.CheckboxInput(attrs={
                'class': 'h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded'
            }),
            'manufacturer': forms.Select(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
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
            'intro_year': forms.NumberInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., 2021'
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
            'notes': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 4,
                'placeholder': 'Additional notes or specifications...'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate brand choices from Brand table, sorted alphabetically
        brand_choices = [('', '-- Select a Brand --')]
        brand_choices += [(b.name, b.name) for b in Brand.objects.all().order_by('name')]
        self.fields['brand'].choices = brand_choices


class BrandForm(forms.ModelForm):
    """Form for creating and editing radio brands"""
    
    parent_brand = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
            'list': 'brand-options',
            'placeholder': 'Select or type a new parent brand...'
        }),
        help_text="Select primary brand if this is a subsidiary or shell company"
    )

    class Meta:
        model = Brand
        fields = [
            'name', 'alias', 'grantee_code', 'full_name', 'parent_brand',
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
            'full_name': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'Full legal company name'
            }),
            'country': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., China, Japan'
            }),
            'website': forms.URLInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'https://manufacturer.com'
            }),
            'white_label_vendors': forms.TextInput(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'placeholder': 'e.g., BTech, Retevis'
            }),
            'notes': forms.Textarea(attrs={
                'class': 'mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm',
                'rows': 4,
                'placeholder': 'Additional notes about the manufacturer...'
            }),
        }
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.parent_brand:
            self.fields['parent_brand'].initial = self.instance.parent_brand.name

    def clean_parent_brand(self):
        brand_name = self.cleaned_data.get('parent_brand')
        if not brand_name:
            return None
            
        # Get or create the parent brand
        brand, _ = Brand.objects.get_or_create(name=brand_name)
        return brand

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
