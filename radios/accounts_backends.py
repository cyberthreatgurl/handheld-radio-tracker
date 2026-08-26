"""Authentication "arbitrator" and social-login stubs.

The arbitrator is the single place that decides how a user signs in. Local
username/password works today; Google/Apple/etc. can be added here later
without touching the view layer.

To add a provider:
  1. Install ``social-auth-app-django`` (recommended) or implement OAuth2
     directly with ``requests``.
  2. Register the backend in ``AUTHENTICATION_BACKENDS`` in settings.py.
  3. Implement the provider branch in ``authenticate_social`` below and add
     the OAuth callback routes to ``radios/urls.py``.
"""

from django.contrib.auth import authenticate


def authenticate_local(username, password):
    """Authenticate a local username/password pair.

    Uses Django's default backend, which verifies against PBKDF2 password
    hashes (industry standard — see the note in models about hashing vs.
    encryption).
    """
    return authenticate(username=username, password=password)


def authenticate_social(provider, credential):
    """Stub: exchange a provider credential for a local ``User``.

    Args:
        provider: one of 'google', 'apple', ...
        credential: access token or authorization code from the provider.

    Returns:
        (user, created) tuple, or raises ``NotImplementedError`` until wired.

    Intended flow once implemented::

        profile = fetch_provider_userinfo(provider, credential)
        user, created = User.objects.get_or_create(
            username=profile.handle,
            defaults={'email': profile.email},
        )
        return user, created
    """
    raise NotImplementedError(f"Social login for {provider!r} is not wired up yet.")
