from django.db import models, transaction
from django.conf import settings
import logging


logger = logging.getLogger(__name__)


def normalize_grantee_code(value):
    return (value or '').strip().upper()


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
    last_modified_date = models.DateTimeField(auto_now=True, null=True, blank=True, db_index=True)
    
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

    def delete(self, *args, **kwargs):
        return delete_brand_and_related(self, *args, **kwargs)


class IgnoredGrantee(models.Model):
    """FCC grantee codes that should be excluded from sync/import workflows."""

    grantee_code = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        help_text="FCC grantee code to exclude from sync/import workflows.",
    )
    reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Short reason this grantee should be ignored.",
    )
    notes = models.TextField(
        blank=True,
        help_text="Optional notes about why this grantee is out of scope.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['grantee_code']
        verbose_name = 'Ignored Grantee ID'
        verbose_name_plural = 'Ignored Grantee IDs'

    def __str__(self):
        if self.reason:
            return f"{self.grantee_code} - {self.reason}"
        return self.grantee_code

    def save(self, *args, **kwargs):
        self.grantee_code = normalize_grantee_code(self.grantee_code)
        super().save(*args, **kwargs)

    @classmethod
    def ignored_codes(cls):
        return list(cls.objects.values_list('grantee_code', flat=True))

    @classmethod
    def is_ignored(cls, grantee_code):
        normalized_code = normalize_grantee_code(grantee_code)
        if not normalized_code:
            return False
        return cls.objects.filter(grantee_code=normalized_code).exists()


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
    manufacturer = models.ForeignKey('Manufacturer', on_delete=models.SET_NULL, null=True, blank=True, related_name='manufactured_models', help_text="The legal manufacturing entity that built this radio")
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
    oet_page_url = models.URLField(
        max_length=1000,
        blank=True,
        help_text="FCC EAS OET exhibits page URL (auto-populated by FCC Update)",
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
        # update it to the canonical brand name before saving the Radio.
        # Skip this if the value is already a canonical Brand.name — that takes priority.
        if self.brand and not Brand.objects.filter(name__iexact=self.brand).exists():
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
                # Walk up to the true parent brand for white-label detection
                true_brand = grantee_match.parent_brand if grantee_match.parent_brand else grantee_match

                # Resolve the Manufacturer record via the Brand's M2M relation
                resolved_mfr = true_brand.manufacturers.first()
                if resolved_mfr is None:
                    # Fallback: try the raw grantee brand before walking to parent
                    resolved_mfr = grantee_match.manufacturers.first()

                if resolved_mfr:
                    self.manufacturer = resolved_mfr

                # If the radio's brand differs from the true brand name/alias it's a white label
                if self.brand:
                    b_lower = self.brand.lower()
                    m_name = true_brand.name.lower()
                    m_alias = true_brand.alias.lower() if true_brand.alias else ''

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


def oet_document_upload_to(instance, filename):
    oet_dir = getattr(settings, 'OET_DOCUMENTS_DIR', 'artifacts/oet_documents').strip('/ ')
    return f"{oet_dir}/{filename}"


def firmware_upload_to(instance, filename):
    firmware_dir = getattr(settings, 'FIRMWARE_DIR', 'artifacts/firmware').strip('/ ')
    return f"{firmware_dir}/{filename}"


class RadioManual(models.Model):
    """Uploaded document files (manuals, firmware, test reports, etc.) linked to a radio."""

    class ProcessingStatus(models.TextChoices):
        UPLOADED = 'uploaded', 'Uploaded'
        REVIEW = 'review', 'Needs Review'
        LINKED = 'linked', 'Linked'
        ERROR = 'error', 'Error'

    class DocType(models.TextChoices):
        TEST_REPORT = 'test_report', 'Test Report'
        CHANGE_IN_ID = 'change_in_id', 'Change in Identification'
        AUTHORIZATION = 'authorization', 'Authorization'
        MANUAL = 'manual', 'Manual'
        FIRMWARE = 'firmware', 'Firmware'
        CPS = 'cps', 'CPS'
        OTHER = 'other', 'Other'

    radio = models.ForeignKey(
        Radio,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='manuals',
    )
    manual_pdf = models.FileField(upload_to=manual_upload_to)
    doc_type = models.CharField(
        max_length=20,
        choices=DocType.choices,
        default=DocType.MANUAL,
        db_index=True,
        verbose_name='Document Type',
    )
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
            models.Index(fields=['doc_type']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        linked = f" -> {self.radio}" if self.radio else ''
        return f"{self.get_doc_type_display()} {self.id}{linked}"


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
    document_file = models.FileField(upload_to=oet_document_upload_to, blank=True)
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


class RadioFirmware(models.Model):
    """Firmware versions for a radio (up to two per radio, e.g. main firmware and APRS module firmware)."""

    radio = models.ForeignKey(
        Radio,
        on_delete=models.CASCADE,
        related_name='firmware_versions',
    )
    label = models.CharField(
        max_length=100,
        blank=True,
        help_text="Component label, e.g. 'Main Radio' or 'APRS Module'",
    )
    version = models.CharField(
        max_length=100,
        blank=True,
        help_text="Firmware version string, e.g. 'v1.23'",
    )
    download_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="Official firmware download page URL",
    )
    firmware_file = models.FileField(
        upload_to=firmware_upload_to,
        blank=True,
        null=True,
        help_text="Optional: upload a copy of the firmware file for local download",
    )
    notes = models.TextField(
        blank=True,
        help_text="Additional notes about this firmware version",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['radio', 'label']
        verbose_name = 'Radio Firmware'
        verbose_name_plural = 'Radio Firmware Versions'

    def __str__(self):
        parts = [str(self.radio)]
        if self.label:
            parts.append(self.label)
        if self.version:
            parts.append(self.version)
        return ' \u2013 '.join(parts)


class Manufacturer(models.Model):
    """A legal manufacturing entity that may sell under one or more brand labels."""

    full_name = models.CharField(
        max_length=500,
        unique=True,
        help_text="Full legal company name, e.g. 'Hiroyasu Electronics (Hong Kong) Co., Ltd'",
    )
    alias = models.CharField(
        max_length=200,
        blank=True,
        help_text="Common short name used commercially, e.g. 'Hiroyasu'",
    )
    brands = models.ManyToManyField(
        Brand,
        blank=True,
        related_name='manufacturers',
        help_text="Brand labels this manufacturer sells under",
    )
    website = models.URLField(max_length=500, blank=True)
    country = models.CharField(max_length=100, blank=True)
    address = models.TextField(
        blank=True,
        help_text="Full street/mailing address, used for geo-mapping manufacturing locations.",
    )
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_failed = models.BooleanField(
        default=False,
        help_text="Set when Nominatim could not resolve the address.",
    )
    geocode_precision = models.CharField(
        max_length=20,
        blank=True,
        help_text="Resolution level achieved: full, city, state, country, or empty when not yet geocoded.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['full_name']
        verbose_name = 'Manufacturer'
        verbose_name_plural = 'Manufacturers'

    def __str__(self):
        if self.alias:
            return f"{self.alias} ({self.full_name})"
        return self.full_name


def delete_radios_and_related(radio_queryset):
    radio_ids = list(radio_queryset.values_list('id', flat=True))

    manual_count = RadioManual.objects.filter(radio_id__in=radio_ids).delete()[0]
    report_count = RadioFCCTestReport.objects.filter(radio_id__in=radio_ids).delete()[0]
    oet_count = RadioOETDocument.objects.filter(radio_id__in=radio_ids).delete()[0]
    firmware_count = RadioFirmware.objects.filter(radio_id__in=radio_ids).delete()[0]
    radio_count = radio_queryset.delete()[0]

    return {
        'radios_deleted': radio_count,
        'manuals_deleted': manual_count,
        'test_reports_deleted': report_count,
        'oet_documents_deleted': oet_count,
        'firmware_deleted': firmware_count,
    }


def delete_brand_and_related(brand, *args, **kwargs):
    radio_queryset = Radio.objects.filter(brand__iexact=brand.name).distinct()
    linked_manufacturer_ids = list(
        Manufacturer.objects.filter(brands=brand).values_list('id', flat=True)
    )

    manufacturers_to_delete = list(
        Manufacturer.objects.annotate(brand_count=models.Count('brands', distinct=True))
        .filter(id__in=linked_manufacturer_ids)
        .annotate(brand_count=models.Count('brands', distinct=True))
        .filter(brand_count=1)
        .values_list('id', flat=True)
    )

    with transaction.atomic():
        delete_summary = delete_radios_and_related(radio_queryset)
        models.Model.delete(brand, *args, **kwargs)
        manufacturer_count = Manufacturer.objects.filter(id__in=manufacturers_to_delete).delete()[0]

    return {
        **delete_summary,
        'manufacturers_deleted': manufacturer_count,
    }


class FCCSyncState(models.Model):
    """Singleton model that tracks the last time 'Update All Known Grantees' was run.

    Only one row (pk=1) should ever exist.  Use ``FCCSyncState.get_instance()``
    to retrieve or lazily create it.
    """

    last_grantee_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last successful 'Update All Known Grantees' FCC sync. "
                  "Used as the start-date filter on subsequent runs to avoid re-fetching the full history.",
    )

    class Meta:
        verbose_name = 'FCC Sync State'
        verbose_name_plural = 'FCC Sync State'

    @classmethod
    def get_instance(cls):
        """Return the singleton FCCSyncState row, creating it if it does not exist."""
        instance, _ = cls.objects.get_or_create(pk=1)
        return instance

    def __str__(self):
        if self.last_grantee_sync_at:
            return f"Last grantee sync: {self.last_grantee_sync_at.strftime('%Y-%m-%d %H:%M UTC')}"
        return "Never synced"
