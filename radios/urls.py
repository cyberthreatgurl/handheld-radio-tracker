from django.urls import path
from . import views
from .views_import import import_grantee_radios
from .views_merge import merge_radios
from .views_manual import manual_upload_view
from .views_accounts import (
    login_view,
    logout_view,
    profile_view,
    radio_comment_add,
    radio_comment_delete,
    radio_comment_edit,
    signup_view,
    user_admin_detail_view,
    user_admin_view,
)

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('radios/', views.RadioListView.as_view(), name='radio_list'),
    path('radios/add/', views.RadioCreateView.as_view(), name='radio_add'),
    path('radios/<int:pk>/', views.RadioDetailView.as_view(), name='radio_detail'),
    path(
        'radios/<int:radio_pk>/images/<int:pk>/delete/',
        views.radio_image_delete,
        name='radio_image_delete',
    ),
    path('radios/<int:pk>/edit/', views.RadioUpdateView.as_view(), name='radio_edit'),
    path('radios/<int:pk>/delete/', views.RadioDeleteView.as_view(), name='radio_delete'),
    path('radios/<int:pk>/sync-fcc/', views.sync_radio_fcc_view, name='sync_radio_fcc'),
    path(
        'radios/<int:pk>/scrape-website/',
        views.scrape_radio_website_view,
        name='scrape_radio_website',
    ),
    path('radios/import-from-url/', views.import_radio_from_url_view, name='import_radio_from_url'),

    path('sync-fcc/', views.sync_fcc_view, name='sync_fcc_id'),
    path('sync-all-grantees/', views.sync_all_grantees_view, name='sync_all_grantees'),
    path('sync-progress/', views.sync_progress_view, name='sync_progress'),
    path('processing-logs/', views.processing_logs_view, name='processing_logs'),
    path('nodal-visualization/', views.nodal_visualization_view, name='nodal_visualization'),
    path('maintenance/', views.maintenance_view, name='maintenance'),
    path('fcc-lookup/', views.fcc_lookup_view, name='fcc_lookup'),
    path('fcc-validate-fccids/', views.fcc_validate_fccids_view, name='fcc_validate_fccids'),

    path('brands/', views.BrandListView.as_view(), name='brand_list'),
    path('brands/bulk-delete/', views.brand_bulk_delete_view, name='brand_bulk_delete'),
    path('brands/<int:pk>/', views.brand_detail_view, name='brand_detail'),
    path('brands/add/', views.BrandCreateView.as_view(), name='brand_add'),
    path('brands/<int:pk>/edit/', views.brand_detail_view, {'edit': True}, name='brand_edit'),
    path('brands/<int:pk>/delete/', views.BrandDeleteView.as_view(), name='brand_delete'),
    path('brands/<int:pk>/merge/', views.brand_merge_view, name='brand_merge'),

    path('manufacturers/', views.ManufacturerListView.as_view(), name='manufacturer_list'),
    path('manufacturers/map/', views.manufacturer_map_view, name='manufacturer_map'),
    path(
        'manufacturers/map/data/',
        views.manufacturer_map_data_view,
        name='manufacturer_map_data',
    ),
    path(
        'manufacturers/add/',
        views.ManufacturerCreateView.as_view(),
        name='manufacturer_add',
    ),
    path(
        'manufacturers/<int:pk>/edit/',
        views.ManufacturerUpdateView.as_view(),
        name='manufacturer_edit',
    ),
    path(
        'manufacturers/<int:pk>/delete/',
        views.ManufacturerDeleteView.as_view(),
        name='manufacturer_delete',
    ),

    path('import-grantee-radios/', import_grantee_radios, name='import_grantee_radios'),
    path('manual-upload/', manual_upload_view, name='manual_upload'),
    path('merge-radios/', merge_radios, name='merge_radios'),

    # Accounts: signup, login, profile, radio comments, user admin
    path('accounts/signup/', signup_view, name='signup'),
    path('accounts/login/', login_view, name='login'),
    path('accounts/logout/', logout_view, name='logout'),
    path('accounts/profile/', profile_view, name='profile'),
    path('accounts/users/', user_admin_view, name='user_admin'),
    path('accounts/users/<int:pk>/', user_admin_detail_view, name='user_admin_detail'),
    path('radios/<int:pk>/comments/add/', radio_comment_add, name='radio_comment_add'),
    path(
        'radios/<int:pk>/comments/<int:comment_pk>/edit/',
        radio_comment_edit,
        name='radio_comment_edit',
    ),
    path(
        'radios/<int:pk>/comments/<int:comment_pk>/delete/',
        radio_comment_delete,
        name='radio_comment_delete',
    ),
]
