from django import forms

class MergeRadiosForm(forms.Form):
    radio_ids = forms.MultipleChoiceField(widget=forms.MultipleHiddenInput)
    # Add fields for each mergeable attribute if you want to let user pick which value to keep
