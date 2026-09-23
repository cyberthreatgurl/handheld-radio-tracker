"""Template context processors for the radios app."""
# pylint: disable=no-member
# no-member: Django ORM metaclass-based managers are undetectable by pylint

from django.conf import settings

from .models import UserProfile


def app_version(_request):
    """Expose the app version to all templates."""
    return {"app_version": settings.APP_VERSION}


def user_profile(_request):
    """Expose the current user's profile (or None) to all templates."""
    user = getattr(_request, 'user', None)
    profile = None
    if user is not None and user.is_authenticated:
        profile = UserProfile.objects.filter(user=user).first()
    return {'user_profile': profile}
