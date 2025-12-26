from django.urls import path
from . import views
from .views_import import import_grantee_radios
from .views_merge import merge_radios

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('radios/', views.RadioListView.as_view(), name='radio_list'),
    path('radios/add/', views.RadioCreateView.as_view(), name='radio_add'),
    path('radios/<int:pk>/', views.RadioDetailView.as_view(), name='radio_detail'),
    path('radios/<int:pk>/edit/', views.RadioUpdateView.as_view(), name='radio_edit'),
    path('radios/<int:pk>/delete/', views.RadioDeleteView.as_view(), name='radio_delete'),
    path('import-grantee-radios/', import_grantee_radios, name='import_grantee_radios'),
    path('merge-radios/', merge_radios, name='merge_radios'),
]
