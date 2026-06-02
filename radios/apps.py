import os
import sys

from django.apps import AppConfig


class RadiosConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'radios'
    verbose_name = 'Ham Radios'

    def ready(self):
        # Import signal handlers at app startup.
        from . import signals  # noqa: F401

        # Print startup diagnostics only for the actual runserver process
        # (RUN_MAIN=true is set by Django's autoreloader for the child server process).
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') == 'true':
            # Defer DB queries to a background thread so they run after
            # Apps.populate() sets apps.ready=True, avoiding the RuntimeWarning
            # about accessing the database during app initialization.
            import threading

            def _deferred(app_cfg):
                from django.apps import apps as registry
                import time
                while not registry.ready:
                    time.sleep(0.01)
                app_cfg._print_startup_info()

            threading.Thread(target=_deferred, args=(self,), daemon=True).start()

    def _print_startup_info(self):
        from django.conf import settings
        from django.db import connection, OperationalError

        db_conf = settings.DATABASES.get('default', {})
        engine = db_conf.get('ENGINE', '').replace('django.db.backends.', '')
        host = db_conf.get('HOST', 'localhost') or 'localhost'
        port = db_conf.get('PORT', '5432') or '5432'
        db_name = db_conf.get('NAME', '')
        db_user = db_conf.get('USER', '')

        sep = '-' * 60
        print(sep)
        print('  Radio Tracker — startup diagnostics')
        print(sep)
        print(f'  DB engine : {engine}')
        print(f'  DB host   : {host}:{port}')
        print(f'  DB name   : {db_name}')
        print(f'  DB user   : {db_user}')

        try:
            connection.ensure_connection()
            print('  DB status : connected')

            from radios.models import Brand, Radio, RadioManual, Manufacturer
            total_radios = Radio.objects.count()
            total_brands = Brand.objects.count()
            total_oems = Brand.objects.exclude(grantee_code__isnull=True).exclude(grantee_code='').count()
            total_manuals = RadioManual.objects.count()
            total_manufacturers = Manufacturer.objects.count()

            print(f'  Radios         : {total_radios}')
            print(f'  Brands         : {total_brands}')
            print(f'  OEMs           : {total_oems}  (brands with FCC grantee code)')
            print(f'  Manufacturers  : {total_manufacturers}')
            print(f'  Manuals        : {total_manuals}')
        except OperationalError as exc:
            print(f'  DB status : UNAVAILABLE ({exc})')

        print(sep)
