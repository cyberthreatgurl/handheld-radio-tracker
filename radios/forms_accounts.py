"""Forms for account signup, login, profile editing, and comments."""

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from .models import RadioComment, UserProfile

# Shared Tailwind input styling so account forms match the rest of the UI.
_INPUT_CLASSES = (
    'mt-1 block w-full rounded-md border-gray-300 shadow-sm '
    'focus:border-indigo-500 focus:ring-indigo-500 sm:text-sm'
)


def _apply_input_styles(form, skip=('website',)):
    """Add Tailwind classes to every visible field on a form."""
    for name, field in form.fields.items():
        if name in skip:
            continue
        current = field.widget.attrs.get('class', '')
        field.widget.attrs['class'] = (current + ' ' + _INPUT_CLASSES).strip()


class SignUpForm(UserCreationForm):
    """Collect handle, name, email, and call sign for a new account.

    ``username`` is the public handle. The ``website`` field is a honeypot:
    real users never see it, bots fill it, and the view rejects the request.
    """

    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=True)
    email = forms.EmailField(required=True)
    callsign = forms.CharField(
        max_length=20, required=False, label='Call sign',
        help_text='Optional FCC call sign.',
    )
    website = forms.CharField(required=False, widget=forms.HiddenInput())  # honeypot

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'last_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_input_styles(self)

    def clean_email(self):
        """Normalize email and reject duplicates (case-insensitive)."""
        email = self.cleaned_data.get('email', '').strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('An account with this email already exists.')
        return email


class LogInForm(AuthenticationForm):
    """Thin wrapper around Django's login form (username + password)."""

    username = forms.CharField(label='Handle or username', max_length=150)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_input_styles(self)


class ProfileUpdateForm(forms.ModelForm):
    """Edit the private profile fields and call sign (the "Modify" page)."""

    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=True)

    class Meta:
        model = UserProfile
        fields = ('callsign',)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if self.user is not None:
            self.fields['first_name'].initial = self.user.first_name
            self.fields['last_name'].initial = self.user.last_name
            self.fields['email'].initial = self.user.email
        _apply_input_styles(self)

    def save(self, commit=True):
        """Save callsign on the profile and copy User fields back."""
        profile = super().save(commit=False)
        self.user.first_name = self.cleaned_data['first_name']
        self.user.last_name = self.cleaned_data['last_name']
        self.user.email = self.cleaned_data['email']
        if commit:
            self.user.save(update_fields=['first_name', 'last_name', 'email'])
            profile.save()
        return profile


class RadioCommentForm(forms.ModelForm):
    """A short text comment on a radio."""

    class Meta:
        model = RadioComment
        fields = ('body',)
        widgets = {
            'body': forms.Textarea(
                attrs={'rows': 3, 'placeholder': 'Share your notes...'},
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_input_styles(self)
