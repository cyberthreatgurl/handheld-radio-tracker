from django.shortcuts import render, redirect
from django.contrib import messages
from django.db import IntegrityError, transaction
from .models import Brand, Radio
from .forms_merge_fields import MergeRadiosFieldsForm
from .accounts_decorators import staff_required

# Enhanced merge view: lets user pick which record's data to keep for each field

@staff_required
def merge_radios(request):
    if request.method == 'POST':
        radio_ids = request.POST.getlist('radio_ids')
        radios = Radio.objects.filter(pk__in=radio_ids)
        if not radios:
            messages.error(request, 'No radios selected for merge.')
            return redirect('radio_list')
        if 'confirm' in request.POST:
            form = MergeRadiosFieldsForm(radios, request.POST)
            if form.is_valid():
                # Pick the selected values for each field
                keep = radios.first()
                selected_values = {}
                for field in form.fields:
                    selected_pk = form.cleaned_data[field]
                    if selected_pk:
                        selected_values[field] = getattr(radios.get(pk=selected_pk), field)
                for field, value in selected_values.items():
                    setattr(keep, field, value)

                # Mirror Radio.save() alias normalization so conflict checks are accurate.
                effective_brand = keep.brand
                if effective_brand:
                    alias_match = Brand.objects.filter(alias__iexact=effective_brand).first()
                    if alias_match and alias_match.name != effective_brand:
                        effective_brand = alias_match.name
                keep.brand = effective_brand
                
                # Check if resulting (brand, model) would conflict with an existing record
                conflict = Radio.objects.filter(
                    brand=keep.brand, model=keep.model
                ).exclude(pk__in=radio_ids).first()
                
                if conflict:
                    messages.error(
                        request, 
                        f'Cannot merge: A radio with brand "{keep.brand}" and model "{keep.model}" '
                        f'already exists (ID: {conflict.pk}). Consider including that record in the merge.'
                    )
                    return render(request, 'radios/merge_radios.html', {'radios': radios, 'radio_ids': radio_ids, 'form': form})
                
                try:
                    with transaction.atomic():
                        # Remove selected records that would otherwise collide with keep on save.
                        Radio.objects.filter(
                            pk__in=radio_ids,
                            brand=keep.brand,
                            model=keep.model,
                        ).exclude(pk=keep.pk).delete()

                        keep.save()
                        Radio.objects.filter(pk__in=radio_ids).exclude(pk=keep.pk).delete()
                    messages.success(request, f'Merged {len(radios)} radios into one record.')
                except IntegrityError as e:
                    messages.error(request, f'Merge failed due to duplicate constraint: {e}')
                    return render(request, 'radios/merge_radios.html', {'radios': radios, 'radio_ids': radio_ids, 'form': form})
                return redirect('radio_list')
        else:
            form = MergeRadiosFieldsForm(radios)
        return render(request, 'radios/merge_radios.html', {'radios': radios, 'radio_ids': radio_ids, 'form': form})
    return redirect('radio_list')
