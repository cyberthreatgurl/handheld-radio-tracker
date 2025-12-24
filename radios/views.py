from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.urls import reverse_lazy
from django.contrib import messages
from django.db.models import Q, Count
from .models import Radio
from .forms import RadioForm, RadioSearchForm


class RadioListView(ListView):
    """View for listing all radios with search and filter"""
    model = Radio
    template_name = 'radios/radio_list.html'
    context_object_name = 'radios'
    paginate_by = 50
    
    def get_queryset(self):
        queryset = Radio.objects.all()
        
        # Search functionality
        query = self.request.GET.get('query')
        if query:
            queryset = queryset.filter(
                Q(brand__icontains=query) |
                Q(model__icontains=query) |
                Q(fcc_id__icontains=query)
            )
        
        # Brand filter
        brand = self.request.GET.get('brand')
        if brand:
            queryset = queryset.filter(brand__iexact=brand)
        
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_form'] = RadioSearchForm(self.request.GET)
        context['total_count'] = Radio.objects.count()
        context['brands'] = Radio.objects.values('brand').annotate(
            count=Count('id')
        ).order_by('brand')
        return context


class RadioDetailView(DetailView):
    """View for displaying a single radio's details"""
    model = Radio
    template_name = 'radios/radio_detail.html'
    context_object_name = 'radio'


class RadioCreateView(CreateView):
    """View for creating a new radio entry"""
    model = Radio
    form_class = RadioForm
    template_name = 'radios/radio_form.html'
    success_url = reverse_lazy('radio_list')
    
    def form_valid(self, form):
        messages.success(self.request, f'Radio {form.instance} has been created successfully!')
        return super().form_valid(form)


class RadioUpdateView(UpdateView):
    """View for updating an existing radio entry"""
    model = Radio
    form_class = RadioForm
    template_name = 'radios/radio_form.html'
    success_url = reverse_lazy('radio_list')
    
    def form_valid(self, form):
        messages.success(self.request, f'Radio {form.instance} has been updated successfully!')
        return super().form_valid(form)


class RadioDeleteView(DeleteView):
    """View for deleting a radio entry"""
    model = Radio
    template_name = 'radios/radio_confirm_delete.html'
    success_url = reverse_lazy('radio_list')
    
    def delete(self, request, *args, **kwargs):
        radio = self.get_object()
        messages.success(request, f'Radio {radio} has been deleted successfully!')
        return super().delete(request, *args, **kwargs)


def dashboard_view(request):
    """Dashboard view with statistics"""
    context = {
        'total_radios': Radio.objects.count(),
        'total_brands': Radio.objects.values('brand').distinct().count(),
        'recent_radios': Radio.objects.order_by('-created_at')[:10],
        'top_brands': Radio.objects.values('brand').annotate(
            count=Count('id')
        ).order_by('-count')[:10],
    }
    return render(request, 'radios/dashboard.html', context)
