from django.shortcuts import render, redirect
from django.contrib import messages
from django.db import IntegrityError, transaction
from .models import Brand, Radio
from .forms_merge_fields import MergeRadiosFieldsForm
from .accounts_decorators import staff_required
from .fcc_id_utils import (
    canonical_fcc_id,
    fcc_id_stripped_expression,
    strip_fcc_id_hyphens,
)

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

                # Mirror Radio.save() FCC ID normalization so the kept record
                # always stores the correctly-hyphenated GRANTEE-PRODUCT form.
                if keep.fcc_id:
                    keep.fcc_id = canonical_fcc_id(keep.fcc_id)

                # Check if resulting (brand, model) would conflict with an existing record
                conflict = Radio.objects.filter(
                    brand=keep.brand, model=keep.model
                ).exclude(pk__in=radio_ids).first()
                
                if conflict:
                    messages.error(
                        request,
                        f'Cannot merge: A radio with brand "{keep.brand}" '
                        f'and model "{keep.model}" already exists '
                        f'(ID: {conflict.pk}). Consider including that record '
                        'in the merge.'
                    )
                    return render(
                        request,
                        'radios/merge_radios.html',
                        {'radios': radios, 'radio_ids': radio_ids, 'form': form},
                    )

                # Check for an FCC ID conflict, ignoring hyphen placement so
                # "K44-524000" and "K44524000" are treated as the same device.
                if keep.fcc_id:
                    fcc_conflict = Radio.objects.annotate(
                        _fcc_stripped=fcc_id_stripped_expression('fcc_id'),
                    ).filter(
                        _fcc_stripped__iexact=strip_fcc_id_hyphens(keep.fcc_id),
                    ).exclude(pk__in=radio_ids).first()
                    if fcc_conflict:
                        messages.error(
                            request,
                            f'Cannot merge: The merged FCC ID "{keep.fcc_id}" '
                            f'is already used by another radio '
                            f'(ID: {fcc_conflict.pk}, '
                            f'fcc_id "{fcc_conflict.fcc_id}"). Consider '
                            'including that record in the merge.'
                        )
                        return render(
                            request,
                            'radios/merge_radios.html',
                            {'radios': radios, 'radio_ids': radio_ids, 'form': form},
                        )
                
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
                    return render(
                        request,
                        'radios/merge_radios.html',
                        {'radios': radios, 'radio_ids': radio_ids, 'form': form},
                    )
                return redirect('radio_list')
        else:
            form = MergeRadiosFieldsForm(radios)
        return render(
            request,
            'radios/merge_radios.html',
            {'radios': radios, 'radio_ids': radio_ids, 'form': form},
        )
    return redirect('radio_list')
