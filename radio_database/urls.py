"""
URL configuration for radio_database project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse, Http404
from pathlib import Path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', RedirectView.as_view(url='/radios/', permanent=False)),
    path('radios/', include('radios.urls')),
]

# Serve the grantee audit report from project root
BASE_DIR = Path(__file__).resolve().parent.parent
GRANTEE_AUDIT_PATH = BASE_DIR / 'grantee_audit.html'


def grantee_audit_view(request):
    """Serve the standalone grantee audit HTML report."""
    if not GRANTEE_AUDIT_PATH.exists():
        raise Http404('Grantee audit report not found. Run validate_grantee_mappings.py first.')
    return HttpResponse(
        GRANTEE_AUDIT_PATH.read_text(encoding='utf-8'),
        content_type='text/html',
    )


urlpatterns += [
    path('grantee_audit.html', grantee_audit_view, name='grantee_audit'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
