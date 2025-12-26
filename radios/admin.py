from django.contrib import admin
from .models import Radio, Brand


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ['name', 'grantee_code', 'full_name', 'country']
    search_fields = ['name', 'grantee_code', 'full_name']
    ordering = ['name']

    actions = ['rename_brand_globally']

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
