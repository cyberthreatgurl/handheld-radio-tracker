from django.db import models


class Brand(models.Model):
    """Model representing a radio manufacturer with FCC Grantee Code"""
    
    name = models.CharField(max_length=200, unique=True, help_text="Official manufacturer/brand name")
    grantee_code = models.CharField(max_length=20, unique=True, help_text="FCC Grantee Code (e.g., 2AJGM, 2AZSA)")
    full_name = models.CharField(max_length=500, blank=True, help_text="Full legal company name")
    website = models.URLField(max_length=500, blank=True, help_text="Official website")
    country = models.CharField(max_length=100, blank=True, help_text="Country of origin")
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
        ]
    
    def __str__(self):
        return f"{self.name} ({self.grantee_code})"


class Radio(models.Model):
    """Model representing a ham radio device"""
    
    # Basic information
    brand = models.CharField(max_length=100, db_index=True, help_text="Radio manufacturer/brand")
    model = models.CharField(max_length=200, help_text="Radio model name/number")
    fcc_id = models.CharField(max_length=50, blank=True, help_text="FCC ID (e.g., 2AJGM-UV5R)")
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
    website = models.URLField(max_length=500, blank=True, help_text="Manufacturer website")
    
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
