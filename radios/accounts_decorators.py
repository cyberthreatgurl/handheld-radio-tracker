"""Access-control helpers: staff-only decorator and mixin.

Non-staff users are redirected to the login page (``LOGIN_URL``) when they hit
a staff-only view. Staff members (``is_staff=True``, e.g. the Django
superuser) may manage radios, brands, manufacturers, imports, and syncs.
"""

from django.contrib.auth.decorators import user_passes_test
from django.utils.decorators import method_decorator


def is_admin_user(user):
    """Admins are authenticated staff members."""
    return user.is_authenticated and user.is_staff


def is_superuser_user(user):
    """Django superusers only (site admins)."""
    return user.is_authenticated and user.is_superuser


# Decorator for function-based views.
staff_required = user_passes_test(is_admin_user)

# Decorator restricting a view to Django superusers only.
admin_required = user_passes_test(is_superuser_user)


class StaffRequiredMixin:
    """Mixin restricting a class-based view to staff members."""

    @method_decorator(staff_required)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)
