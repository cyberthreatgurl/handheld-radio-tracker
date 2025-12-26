from django.shortcuts import render, redirect, get_list_or_404
from django.contrib import messages
from .models import Radio
from .forms_merge_fields import MergeRadiosFieldsForm

# Enhanced merge view: lets user pick which record's data to keep for each field

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
                for field in form.fields:
                    selected_pk = form.cleaned_data[field]
                    if selected_pk:
                        value = getattr(radios.get(pk=selected_pk), field)
                        setattr(keep, field, value)
                keep.save()
                for r in radios.exclude(pk=keep.pk):
                    r.delete()
                messages.success(request, f'Merged {len(radios)} radios into one record.')
                return redirect('radio_list')
        else:
            form = MergeRadiosFieldsForm(radios)
        return render(request, 'radios/merge_radios.html', {'radios': radios, 'radio_ids': radio_ids, 'form': form})
    return redirect('radio_list')
