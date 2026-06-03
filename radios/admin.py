from django.contrib import admin
from .models import Radio, Brand, RadioManual, RadioFCCTestReport, RadioOETDocument, RadioFirmware, Manufacturer, IgnoredGrantee, delete_brand_and_related


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ['name', 'grantee_code', 'full_name', 'country']
    search_fields = ['name', 'grantee_code', 'full_name']
    ordering = ['name']

    actions = ['rename_brand_globally']

    def delete_model(self, request, obj):
        delete_brand_and_related(obj)

    def delete_queryset(self, request, queryset):
        for brand in queryset:
            delete_brand_and_related(brand)

    def rename_brand_globally(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Please select exactly one brand to rename.", level='error')
            return
        brand = queryset.first()
        from django import forms
        from django.shortcuts import render, redirect
        class RenameForm(forms.Form):
            new_name = forms.CharField(label='New Brand Name', max_length=200, initial=brand.name)
        if 'apply' in request.POST:
            form = RenameForm(request.POST)
            if form.is_valid():
                new_name = form.cleaned_data['new_name']
                old_name = brand.name
                # Update Brand
                Brand.objects.filter(name=old_name).update(name=new_name)
                # Update Radio
                from radios.models import Radio
                Radio.objects.filter(brand=old_name).update(brand=new_name)
                self.message_user(request, f"Renamed brand and all radios from '{old_name}' to '{new_name}'.")
                return
        else:
            form = RenameForm(initial={'new_name': brand.name})
        return render(request, 'admin/rename_brand.html', {'form': form, 'brand': brand})
    rename_brand_globally.short_description = "Globally rename selected brand (Brand & Radio)"


@admin.register(Manufacturer)
class ManufacturerAdmin(admin.ModelAdmin):
    list_display = ['full_name', 'alias', 'country', 'brand_count']
    search_fields = ['full_name', 'alias', 'country']
    filter_horizontal = ['brands']
    ordering = ['full_name']

    def brand_count(self, obj):
        return obj.brands.count()
    brand_count.short_description = 'Brands'


@admin.register(IgnoredGrantee)
class IgnoredGranteeAdmin(admin.ModelAdmin):
    list_display = ['grantee_code', 'reason', 'updated_at']
    search_fields = ['grantee_code', 'reason', 'notes']
    ordering = ['grantee_code']


@admin.register(Radio)
class RadioAdmin(admin.ModelAdmin):
    list_display = ['brand', 'model', 'fcc_id', 'last_fccid_lookup_at', 'intro_year', 'freq_bands_tx', 'power_watts', 'cost_approx']
    list_filter = ['brand', 'last_fccid_lookup_at', 'intro_year', 'dmr', 'gps', 'aprs']
    search_fields = ['brand', 'model', 'fcc_id']
    ordering = ['brand', 'model']
    
    fieldsets = (
        ('Basic Information', {
            'fields': ('brand', 'model', 'radio_type', 'is_a_whitelabel', 'manufacturer', 'fcc_id', 'last_fccid_lookup_at', 'intro_year')
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
            'fields': ('cost_approx', 'rebadges_clones', 'white_label_vendors', 'website', 'review_url', 'youtube_video_urla')
        }),
        ('Additional Details', {
            'fields': ('notes',)
        }),
    )


@admin.register(RadioManual)
class RadioManualAdmin(admin.ModelAdmin):
    list_display = ['id', 'radio', 'status', 'extraction_confidence', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['radio__brand', 'radio__model', 'source_url']
    ordering = ['-created_at']


@admin.register(RadioFCCTestReport)
class RadioFCCTestReportAdmin(admin.ModelAdmin):
    list_display = ['id', 'radio', 'fcc_id', 'report_title', 'created_at']
    list_filter = ['created_at']
    search_fields = ['radio__brand', 'radio__model', 'fcc_id', 'report_title', 'source_url', 'product_designation']
    ordering = ['-created_at']


@admin.register(RadioOETDocument)
class RadioOETDocumentAdmin(admin.ModelAdmin):
    list_display = ['id', 'radio', 'fcc_id', 'view_attachment', 'exhibit_type', 'date_submitted_to_fcc', 'display_type', 'date_available']
    list_filter = ['exhibit_type', 'display_type', 'date_submitted_to_fcc', 'date_available']
    search_fields = ['radio__brand', 'radio__model', 'fcc_id', 'view_attachment', 'exhibit_type', 'document_url']
    ordering = ['fcc_id', 'exhibit_type', 'view_attachment']


@admin.register(RadioFirmware)
class RadioFirmwareAdmin(admin.ModelAdmin):
    list_display = ['id', 'radio', 'label', 'version', 'download_url', 'firmware_file', 'created_at']
    list_filter = ['created_at', 'updated_at']
    search_fields = ['radio__brand', 'radio__model', 'label', 'version']
    ordering = ['radio', 'label']
