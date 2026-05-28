from .settings import *

# Use SQLite for local test execution to avoid PostgreSQL test database creation privileges.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'test_db.sqlite3',
    }
}

# Keep tests fast and deterministic for local CI/dev runs.
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

LOGGING_ENABLED = False
LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
}
