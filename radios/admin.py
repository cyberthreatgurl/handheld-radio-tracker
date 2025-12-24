from django.contrib import admin
from .models import Radio, Brand


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ['name', 'grantee_code', 'full_name', 'country']
    search_fields = ['name', 'grantee_code', 'full_name']
    ordering = ['name']


@admin.register(Radio)
class RadioAdmin(admin.ModelAdmin):
    list_display = ['brand', 'model', 'intro_year', 'freq_bands_tx', 'power_watts', 'cost_approx']
    list_filter = ['brand', 'intro_year', 'dmr', 'gps', 'aprs']
    search_fields = ['brand', 'model', 'fcc_id']
    ordering = ['brand', 'model']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('brand', 'model', 'fcc_id', 'intro_year')
        }),
        ('Technical Specifications', {
            'fields': ('freq_bands_tx', 'power_watts')
        }),
        ('Features', {
            'fields': ('satellite_tracking', 'harmonic_suppression', 'gps', 'aprs', 'air_band', 'dmr')
        }),
        ('Hardware', {
            'fields': ('display', 'battery_mah')
        }),
        ('Pricing & Related', {
            'fields': ('cost_approx', 'rebadges_clones', 'website')
        }),
        ('Additional Details', {
            'fields': ('notes',)
        }),
    )
