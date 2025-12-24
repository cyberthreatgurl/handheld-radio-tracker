from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('radios/', views.RadioListView.as_view(), name='radio_list'),
    path('radios/add/', views.RadioCreateView.as_view(), name='radio_add'),
    path('radios/<int:pk>/', views.RadioDetailView.as_view(), name='radio_detail'),
    path('radios/<int:pk>/edit/', views.RadioUpdateView.as_view(), name='radio_edit'),
    path('radios/<int:pk>/delete/', views.RadioDeleteView.as_view(), name='radio_delete'),
]
