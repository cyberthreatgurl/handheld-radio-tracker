from django.urls import path
from . import views
from .views_import import import_grantee_radios
from .views_merge import merge_radios
from .views_manual import manual_upload_view

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('radios/', views.RadioListView.as_view(), name='radio_list'),
    path('radios/add/', views.RadioCreateView.as_view(), name='radio_add'),
    path('radios/<int:pk>/', views.RadioDetailView.as_view(), name='radio_detail'),
    path('radios/<int:pk>/edit/', views.RadioUpdateView.as_view(), name='radio_edit'),
    path('radios/<int:pk>/delete/', views.RadioDeleteView.as_view(), name='radio_delete'),
    
    path('sync-fcc/', views.sync_fcc_view, name='sync_fcc_id'),
    path('sync-all-grantees/', views.sync_all_grantees_view, name='sync_all_grantees'),
    path('processing-logs/', views.processing_logs_view, name='processing_logs'),
    
    path('brands/', views.BrandListView.as_view(), name='brand_list'),
    path('brands/add/', views.BrandCreateView.as_view(), name='brand_add'),
    path('brands/<int:pk>/edit/', views.BrandUpdateView.as_view(), name='brand_edit'),
    path('brands/<int:pk>/delete/', views.BrandDeleteView.as_view(), name='brand_delete'),
    
    path('import-grantee-radios/', import_grantee_radios, name='import_grantee_radios'),
    path('manual-upload/', manual_upload_view, name='manual_upload'),
    path('merge-radios/', merge_radios, name='merge_radios'),
]
