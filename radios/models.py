from django.db import models
from django.conf import settings
import logging


logger = logging.getLogger(__name__)


class Brand(models.Model):
    """Model representing a radio manufacturer with FCC Grantee Code"""
    
    name = models.CharField(max_length=200, unique=True, help_text="Official manufacturer/brand name")
    alias = models.CharField(max_length=100, blank=True, help_text="Short/common brand alias (e.g., Senhaix, Baofeng)")
    grantee_code = models.CharField(max_length=20, unique=True, blank=True, null=True, help_text="FCC Grantee Code (e.g., 2AJGM, 2AZSA)")
    full_name = models.CharField(max_length=500, blank=True, help_text="Full legal company name")
    website = models.URLField(max_length=500, blank=True, help_text="Official website")
    country = models.CharField(max_length=100, blank=True, help_text="Country of origin")
    parent_brand = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subsidiaries', help_text="Select primary brand if this is a subsidiary or shell company (e.g., EVOTE -> Wouxun)")
    white_label_vendors = models.CharField(max_length=500, blank=True, help_text="Comma-separated list of white label vendors")
    notes = models.TextField(blank=True, help_text="Additional notes about the manufacturer")
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['name']
        verbose_name = 'Brand'
        verbose_name_plural = 'Brands'
        indexes = [
            models.Index(fields=['grantee_code']),
            models.Index(fields=['alias']),
        ]
    
    def __str__(self):
        parts = []
        if self.name:
            parts.append(self.name)
        if self.full_name and self.full_name != self.name:
            parts.append(f"[{self.full_name}]")
        if self.alias:
            parts.append(f"({self.alias})")
        if self.grantee_code:
            parts.append(f"FCC: {self.grantee_code}")
        return " - ".join(parts)


class Radio(models.Model):
    """Model representing a ham radio device"""
    
    # Radio type choices
    class RadioType(models.TextChoices):
        BASE = 'base', 'Base'
        MOBILE = 'mobile', 'Mobile'
        PORTABLE = 'portable', 'Portable'
    
    # Basic information
    brand = models.CharField(max_length=100, db_index=True, help_text="Radio manufacturer/brand")
    model = models.CharField(max_length=200, help_text="Radio model name/number")
    is_a_whitelabel = models.BooleanField(default=False, help_text="Is this radio a white label model?")
    manufacturer = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='manufactured_models', help_text="The actual original manufacturer of this radio")
    radio_type = models.CharField(
        max_length=20,
        choices=RadioType.choices,
        blank=True,
        help_text="Type of radio (Base, Mobile, Portable)"
    )
    fcc_id = models.CharField(max_length=50, blank=True, help_text="FCC ID (e.g., 2AJGM-UV5R)")
    last_fccid_lookup_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last FCC ID lookup attempt for this radio",
    )
    intro_year = models.IntegerField(null=True, blank=True, help_text="Year introduced")
    
    # Technical specifications
    freq_bands_tx = models.CharField(max_length=200, blank=True, help_text="Frequency bands (TX) (e.g., VHF, UHF, 220)")
    power_watts = models.CharField(max_length=100, blank=True, help_text="Power output (e.g., 5W, 10W)")
    
    # Features
    satellite_tracking = models.CharField(max_length=50, blank=True, help_text="Satellite tracking capability (Yes/No/Native)")
    harmonic_suppression = models.CharField(max_length=100, blank=True, help_text="Harmonic suppression status (e.g., Good, Excellent, Poor)")
    gps = models.CharField(max_length=50, blank=True, help_text="GPS capability (Yes/No/Optional)")
    aprs = models.CharField(max_length=100, blank=True, help_text="APRS support (e.g., Yes, Analog, Digital, Beacon)")
    air_band = models.CharField(max_length=50, blank=True, help_text="Air band receive capability (Yes/No)")
    dmr = models.CharField(max_length=50, blank=True, help_text="DMR support (Yes/No)")
    
    # Hardware details
    display = models.CharField(max_length=200, blank=True, help_text="Display type (e.g., LCD, Color TFT, Dot-matrix)")
    battery_mah = models.IntegerField(null=True, blank=True, help_text="Battery capacity in mAh")
    
    # Pricing and related models
    cost_approx = models.CharField(max_length=100, blank=True, help_text="Approximate cost (e.g., $50, $100-150)")
    rebadges_clones = models.TextField(blank=True, help_text="Known rebadges or clones")
    white_label_vendors = models.CharField(max_length=500, blank=True, help_text="Comma-separated list of white label vendors")
    website = models.URLField(max_length=500, blank=True, help_text="Manufacturer website")
    review_url = models.URLField(max_length=500, blank=True, help_text="Link to review (e.g., eHam.net)")
    youtube_video_urla = models.TextField(
        blank=True,
        help_text="One YouTube URL per line.",
    )
    
    # Additional notes
    notes = models.TextField(blank=True, help_text="Additional notes or specifications")
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['brand', 'model']
        unique_together = ['brand', 'model']
        indexes = [
            models.Index(fields=['brand', 'model']),
            models.Index(fields=['fcc_id']),
        ]
        verbose_name = 'Radio'
        verbose_name_plural = 'Radios'
    
    def __str__(self):
        return f"{self.brand} {self.model}"
    
    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('radio_detail', kwargs={'pk': self.pk})
    
    def save(self, *args, **kwargs):
        """Override save to ensure brand exists in Brand table"""
        # If the radio's brand string exactly matches a known brand's alias,
        # update it to the canonical brand name before saving the Radio
        if self.brand:
            # Case-insensitive check if it's the alias of an existing brand
            alias_match = Brand.objects.filter(alias__iexact=self.brand).first()
            if alias_match and alias_match.name != self.brand:
                self.brand = alias_match.name
                
        # Automatically correlate manufacturer and white_label status using FCC ID
        if self.fcc_id and not self.manufacturer:
            fcc_upper = self.fcc_id.upper().strip()
            grantee_match = None
            
            # Check hyphenated prefix first
            if '-' in fcc_upper:
                prefix = fcc_upper.split('-')[0]
                grantee_match = Brand.objects.filter(grantee_code__iexact=prefix).first()
                
            # Fallback to standard 5-char and 3-char grantee codes
            if not grantee_match and len(fcc_upper) >= 5:
                grantee_match = Brand.objects.filter(grantee_code__iexact=fcc_upper[:5]).first()
            if not grantee_match and len(fcc_upper) >= 3:
                grantee_match = Brand.objects.filter(grantee_code__iexact=fcc_upper[:3]).first()
                
            if grantee_match:
                # If the grantee company is essentially a sub-brand or white-label front, 
                # attribute the manufacturer to the true parent organization
                true_manufacturer = grantee_match
                if grantee_match.parent_brand:
                    true_manufacturer = grantee_match.parent_brand
                    
                self.manufacturer = true_manufacturer
                
                # If the radio's brand is different from the true manufacturer's name and alias, it's a white label
                if self.brand:
                    b_lower = self.brand.lower()
                    m_name = true_manufacturer.name.lower()
                    m_alias = true_manufacturer.alias.lower() if true_manufacturer.alias else ''
                    
                    if b_lower != m_name and (not m_alias or b_lower != m_alias):
                        self.is_a_whitelabel = True

        # Validation telemetry to catch suspicious FCC/brand assignments from any ingestion path.
        if self.fcc_id and self.brand:
            from .fcc_validation import validate_fcc_brand_assignment
            validation = validate_fcc_brand_assignment(self.fcc_id, self.brand)
            if validation.get('status') == 'white_label_possible':
                logger.info(
                    "FCC validation white-label candidate source=radio_model_save radio_id=%s brand=%s fcc_id=%s inferred_grantee=%s grantee_brand=%s resolved_brand=%s",
                    self.pk,
                    self.brand,
                    self.fcc_id,
                    validation.get('inferred_grantee_code', ''),
                    validation.get('grantee_brand_name', ''),
                    validation.get('resolved_brand_name', ''),
                )
            elif validation.get('status') == 'unknown_grantee':
                logger.warning(
                    "FCC validation unknown grantee source=radio_model_save radio_id=%s brand=%s fcc_id=%s inferred_grantee=%s",
                    self.pk,
                    self.brand,
                    self.fcc_id,
                    validation.get('inferred_grantee_code', ''),
                )
            elif validation.get('status') == 'invalid_fcc_id':
                logger.warning(
                    "FCC validation invalid id source=radio_model_save radio_id=%s brand=%s fcc_id=%s",
                    self.pk,
                    self.brand,
                    self.fcc_id,
                )

        super().save(*args, **kwargs)
        
        # Automatically create Brand entry if it doesn't exist
        if self.brand:
            Brand.objects.get_or_create(name=self.brand)


def manual_upload_to(instance, filename):
    manuals_dir = getattr(settings, 'MANUALS_DIR', 'artifacts/manuals').strip('/ ')
    return f"{manuals_dir}/{filename}"


def test_report_upload_to(instance, filename):
    reports_dir = getattr(settings, 'FCC_TEST_REPORTS_DIR', 'artifacts/test_reports').strip('/ ')
    return f"{reports_dir}/{filename}"


class RadioManual(models.Model):
    """Uploaded manual PDFs and extraction artifacts."""

    class ProcessingStatus(models.TextChoices):
        UPLOADED = 'uploaded', 'Uploaded'
        REVIEW = 'review', 'Needs Review'
        LINKED = 'linked', 'Linked'
        ERROR = 'error', 'Error'

    radio = models.ForeignKey(
        Radio,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='manuals',
    )
    manual_pdf = models.FileField(upload_to=manual_upload_to)
    source_url = models.URLField(max_length=500, blank=True)
    extraction_confidence = models.FloatField(default=0.0)
    extracted_data = models.JSONField(default=dict, blank=True)
    extracted_text = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.UPLOADED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        linked = f" -> {self.radio}" if self.radio else ''
        return f"Manual {self.id}{linked}"


class RadioFCCTestReport(models.Model):
    """FCC test report PDFs linked to a radio record."""

    radio = models.ForeignKey(
        Radio,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='fcc_test_reports',
    )
    fcc_id = models.CharField(max_length=50, db_index=True)
    report_pdf = models.FileField(upload_to=test_report_upload_to)
    source_url = models.URLField(max_length=1000, blank=True)
    report_title = models.CharField(max_length=500, blank=True)
    product_designation = models.CharField(max_length=300, blank=True)
    extracted_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['fcc_id']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        linked = f" -> {self.radio}" if self.radio else ''
        label = self.report_title or self.report_pdf.name
        return f"FCC Test Report {self.id}: {label}{linked}"


class RadioOETDocument(models.Model):
    """FCC OET exhibit/document row linked to an FCC ID and optional radio."""

    radio = models.ForeignKey(
        Radio,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='oet_documents',
    )
    fcc_id = models.CharField(max_length=50, db_index=True)
    view_attachment = models.CharField(max_length=500, blank=True)
    exhibit_type = models.CharField(max_length=200, blank=True)
    date_submitted_to_fcc = models.DateField(null=True, blank=True)
    display_type = models.CharField(max_length=100, blank=True)
    date_available = models.DateField(null=True, blank=True)
    document_url = models.URLField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['exhibit_type', 'view_attachment', 'id']
        indexes = [
            models.Index(fields=['fcc_id']),
            models.Index(fields=['exhibit_type']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['radio', 'fcc_id', 'document_url', 'view_attachment'],
                name='uniq_oet_document_per_radio_fcc',
            ),
        ]

    def __str__(self):
        return f"{self.fcc_id} - {self.view_attachment or self.exhibit_type}"
