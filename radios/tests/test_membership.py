"""Regression tests for account membership types and expiration."""

# pylint: disable=no-member, missing-function-docstring
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# missing-function-docstring: test methods are self-documenting by name

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ..models import UserProfile


class MembershipTypeTest(TestCase):
    """Account types drive expiration; free/admin accounts never expire."""

    def _profile(self):
        user = User.objects.create_user(username='member', password='pw')
        return UserProfile.objects.get(user=user)

    def test_new_user_defaults_to_free_and_never_expires(self):
        profile = self._profile()
        self.assertEqual(
            profile.account_type, UserProfile.AccountType.USER_FREE)
        self.assertIsNone(profile.membership_expires_at)
        self.assertFalse(profile.is_expired())

    def test_admin_account_never_expires(self):
        profile = self._profile()
        profile.account_type = UserProfile.AccountType.ADMIN
        profile.membership_duration = UserProfile.MembershipDuration.ONE_MONTH
        profile.save()

        self.assertIsNone(profile.membership_expires_at)
        self.assertTrue(profile.is_admin_account)
        self.assertFalse(profile.is_expired())

    def test_paid_account_expires_after_duration(self):
        profile = self._profile()
        profile.account_type = UserProfile.AccountType.VENDOR
        profile.membership_duration = UserProfile.MembershipDuration.ONE_MONTH
        profile.save()

        self.assertIsNotNone(profile.membership_expires_at)
        self.assertFalse(profile.is_expired())

    def test_never_duration_does_not_expire(self):
        profile = self._profile()
        profile.account_type = UserProfile.AccountType.USER
        profile.membership_duration = UserProfile.MembershipDuration.NEVER
        profile.save()

        self.assertIsNone(profile.membership_expires_at)
        self.assertFalse(profile.is_expired())

    def test_superuser_is_admin_account(self):
        user = User.objects.create_superuser(
            username='root', password='pw', email='root@example.com')
        profile = UserProfile.objects.get(user=user)

        self.assertTrue(profile.is_admin_account)
        self.assertFalse(profile.is_expired())


class MembershipExpirationTest(TestCase):
    """Expired memberships are deactivated when checked."""

    def _expired_member(self):
        user = User.objects.create_user(username='member', password='pw')
        profile = UserProfile.objects.get(user=user)
        profile.account_type = UserProfile.AccountType.USER
        profile.membership_active = True
        profile.membership_duration = UserProfile.MembershipDuration.ONE_MONTH
        profile.save()
        profile.membership_expires_at = timezone.now() - timedelta(days=1)
        profile.save(update_fields=['membership_expires_at'])
        return user, profile

    def test_refresh_deactivates_expired_membership(self):
        _user, profile = self._expired_member()
        self.assertTrue(profile.is_expired())
        self.assertTrue(profile.refresh_membership_status())
        profile.refresh_from_db()
        self.assertFalse(profile.membership_active)

    def test_refresh_is_noop_when_not_expired(self):
        _user, profile = self._expired_member()
        profile.membership_expires_at = timezone.now() + timedelta(days=5)
        profile.save(update_fields=['membership_expires_at'])

        self.assertFalse(profile.refresh_membership_status())
        self.assertTrue(profile.membership_active)


class LoginExpirationCheckTest(TestCase):
    """Logging in deactivates an expired membership."""

    def test_login_deactivates_expired_membership(self):
        user = User.objects.create_user(
            username='member', password='testpass123')
        profile = UserProfile.objects.get(user=user)
        profile.account_type = UserProfile.AccountType.USER
        profile.membership_active = True
        profile.membership_duration = UserProfile.MembershipDuration.ONE_MONTH
        profile.save()
        profile.membership_expires_at = timezone.now() - timedelta(days=1)
        profile.save(update_fields=['membership_expires_at'])

        response = self.client.post(
            reverse('login'),
            {'username': 'member', 'password': 'testpass123'},
        )

        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertFalse(profile.membership_active)
