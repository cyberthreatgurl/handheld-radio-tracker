"""Views for account signup, login, profile, comments, and user admin."""

import logging

from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render

from .accounts_security import (
    captcha_ok,
    check_rate_limit,
    is_honeypot_submitted,
    reset_rate_limit,
)
from .forms_accounts import (
    LogInForm,
    ProfileUpdateForm,
    RadioCommentForm,
    SignUpForm,
)
from .models import Radio, RadioComment, UserProfile

logger = logging.getLogger(__name__)


def signup_view(request):
    """Create a new account (handle, name, email, call sign, password).

    Protected against bots with a honeypot field and per-IP rate limiting.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = SignUpForm()
    if request.method == 'POST':
        # Honeypot: silently swallow bot submissions instead of erroring.
        if is_honeypot_submitted(request):
            return redirect('dashboard')

        allowed, retry = check_rate_limit('signup', request)
        if not allowed:
            messages.error(
                request, f'Too many signup attempts. Try again in {retry}s.',
            )
            return render(request, 'radios/accounts/signup.html', {'form': form})

        if not captcha_ok(request):
            messages.error(request, 'CAPTCHA verification failed. Please try again.')
            return render(request, 'radios/accounts/signup.html', {'form': form})

        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.email = form.cleaned_data['email'].strip().lower()
            user.save()
            UserProfile.objects.create(
                user=user,
                callsign=(form.cleaned_data.get('callsign') or '').strip().upper(),
            )
            reset_rate_limit('signup', request)
            logger.info('User action signup actor=anonymous new_user=%s', user.username)
            messages.success(request, 'Account created. You can now sign in.')
            return redirect('login')
        messages.error(request, 'Please correct the errors below.')

    return render(request, 'radios/accounts/signup.html', {'form': form})


def login_view(request):
    """Authenticate a user by handle/username + password, rate-limited."""
    if request.user.is_authenticated:
        return redirect('dashboard')

    form = LogInForm(request)
    if request.method == 'POST':
        identifier = (request.POST.get('username') or '').strip()
        allowed, retry = check_rate_limit('login', request, identifier=identifier)
        if not allowed:
            messages.error(
                request, f'Too many login attempts. Try again in {retry}s.',
            )
            return render(request, 'radios/accounts/login.html', {'form': form})

        form = LogInForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            reset_rate_limit('login', request, identifier=identifier)
            logger.info('User action login actor=%s', form.get_user().username)
            return redirect(request.GET.get('next') or 'dashboard')
        messages.error(request, 'Invalid handle/username or password.')

    return render(request, 'radios/accounts/login.html', {'form': form})


def logout_view(request):
    """Log the user out (POST only, CSRF-protected)."""
    if request.method == 'POST':
        logger.info(
            'User action logout actor=%s',
            getattr(request.user, 'username', 'anonymous'),
        )
        auth_logout(request)
    return redirect('dashboard')


@login_required
def profile_view(request):
    """The "Modify" page — edit private profile fields and call sign."""
    profile = UserProfile.objects.get_or_create(user=request.user)[0]
    form = ProfileUpdateForm(instance=profile, user=request.user)

    if request.method == 'POST':
        form = ProfileUpdateForm(request.POST, instance=profile, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your profile has been updated.')
            logger.info('User action profile_update actor=%s', request.user.username)
            return redirect('profile')
        messages.error(request, 'Please correct the errors below.')

    return render(request, 'radios/accounts/profile.html', {'form': form, 'profile': profile})


# ---------------------------------------------------------------------------
# Radio comments — users may create/edit/delete only their own
# ---------------------------------------------------------------------------


@login_required
def radio_comment_add(request, pk):
    """Add a comment to a radio."""
    radio = get_object_or_404(Radio, pk=pk)
    if request.method == 'POST':
        form = RadioCommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.radio = radio
            comment.user = request.user
            comment.save()
            messages.success(request, 'Comment added.')
            logger.info(
                'User action comment_add actor=%s radio_id=%s',
                request.user.username, radio.pk,
            )
        else:
            messages.error(request, 'Comment could not be saved.')
    return redirect(radio.get_absolute_url())


@login_required
def radio_comment_edit(request, pk, comment_pk):
    """Edit an existing comment (owner only)."""
    radio = get_object_or_404(Radio, pk=pk)
    comment = get_object_or_404(RadioComment, pk=comment_pk, radio=radio)
    if comment.user_id != request.user.id:
        messages.error(request, 'You can only edit your own comments.')
        return redirect(radio.get_absolute_url())

    form = RadioCommentForm(request.POST or None, instance=comment)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Comment updated.')
        return redirect(radio.get_absolute_url())

    return render(
        request, 'radios/accounts/comment_edit.html',
        {'form': form, 'radio': radio, 'comment': comment},
    )


@login_required
def radio_comment_delete(request, pk, comment_pk):
    """Delete an existing comment (owner only)."""
    radio = get_object_or_404(Radio, pk=pk)
    comment = get_object_or_404(RadioComment, pk=comment_pk, radio=radio)
    if comment.user_id == request.user.id:
        comment.delete()
        messages.success(request, 'Comment deleted.')
    else:
        messages.error(request, 'You can only delete your own comments.')
    return redirect(radio.get_absolute_url())


# ---------------------------------------------------------------------------
# User administration (staff only) — new-user notification dashboard
# ---------------------------------------------------------------------------


def _is_admin(user):
    """Admins are authenticated staff members."""
    return user.is_authenticated and user.is_staff


@user_passes_test(_is_admin)
def user_admin_view(request):
    """List all users, flagging new (unreviewed) accounts for the admin."""
    users = User.objects.select_related('profile').order_by('-date_joined')
    new_user_count = users.filter(profile__admin_reviewed=False).count()

    if request.method == 'POST' and request.POST.get('action') == 'mark_reviewed':
        ids = request.POST.getlist('user_ids')
        UserProfile.objects.filter(user_id__in=ids).update(admin_reviewed=True)
        messages.success(request, f'Marked {len(ids)} user(s) as reviewed.')
        return redirect('user_admin')

    return render(
        request, 'radios/accounts/user_admin.html',
        {'users': users, 'new_user_count': new_user_count},
    )


@user_passes_test(_is_admin)
def user_admin_detail_view(request, pk):
    """Show a single user's details. The password hash is never rendered."""
    account = get_object_or_404(User.objects.select_related('profile'), pk=pk)
    return render(request, 'radios/accounts/user_admin_detail.html', {'account': account})
