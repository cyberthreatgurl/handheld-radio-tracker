from django.apps import AppConfig


class RadiosConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'radios'
    verbose_name = 'Ham Radios'

    def ready(self):
        # Import signal handlers at app startup.
        from . import signals  # noqa: F401
