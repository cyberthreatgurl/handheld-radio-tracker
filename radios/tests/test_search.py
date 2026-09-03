"""Regression tests for search query hyphen/quote handling.

Unquoted search terms are compared with hyphens (and other punctuation)
stripped from both sides, so ``BT-8000`` matches ``BT8000``.  Quoted terms
(``"A-P8000"``) are matched literally, hyphens included.
"""

# pylint: disable=no-member, missing-function-docstring
# no-member: Django ORM metaclass-based managers are undetectable by pylint
# missing-function-docstring: test methods are self-documenting by name

from django.test import TestCase
from django.urls import reverse

from ..models import Radio
from ..views import (
    _parse_search_tokens,
    _search_queryset,
    _RADIO_SEARCH_FIELDS,
)


class SearchTokenParsingTest(TestCase):
    """Tokenizing a query into quoted vs unquoted terms."""

    def test_quoted_token(self):
        self.assertEqual(_parse_search_tokens('"A-P8000"'), [('A-P8000', True)])

    def test_unquoted_token(self):
        self.assertEqual(_parse_search_tokens('BT-8000'), [('BT-8000', False)])

    def test_mixed_tokens(self):
        self.assertEqual(
            _parse_search_tokens('baofeng "UV-5R"'),
            [('baofeng', False), ('UV-5R', True)],
        )

    def test_empty_query(self):
        self.assertEqual(_parse_search_tokens(''), [])


class RadioSearchHyphenTest(TestCase):
    """Hyphen handling in the radio search queryset."""

    def _make(self, model):
        return Radio.objects.create(brand='BrandX', model=model, fcc_id='')

    def test_unquoted_search_matches_any_hyphen_placement(self):
        a = self._make('BT8000')
        b = self._make('BT-8000')
        c = self._make('BT800-0')
        self._make('A-P8000')
        self._make('AP8000')

        qs = _search_queryset(
            Radio.objects.all(), 'BT-8000', _RADIO_SEARCH_FIELDS,
        )
        self.assertEqual(set(qs.values_list('id', flat=True)), {a.id, b.id, c.id})

    def test_quoted_search_matches_literal_only(self):
        d = self._make('A-P8000')
        self._make('AP8000')

        qs = _search_queryset(
            Radio.objects.all(), '"A-P8000"', _RADIO_SEARCH_FIELDS,
        )
        self.assertEqual(set(qs.values_list('id', flat=True)), {d.id})


class RadioSearchViewTest(TestCase):
    """End-to-end search through the radio list view."""

    def _make(self, model):
        return Radio.objects.create(brand='BrandX', model=model, fcc_id='')

    def test_view_unquoted_hyphen_search(self):
        self._make('BT8000')
        self._make('BT-8000')
        self._make('BT800-0')
        self._make('A-P8000')
        self._make('AP8000')

        response = self.client.get(reverse('radio_list'), {'query': 'BT-8000'})
        self.assertEqual(response.status_code, 200)
        models = {radio.model for radio in response.context['radios']}
        self.assertEqual(models, {'BT8000', 'BT-8000', 'BT800-0'})

    def test_view_quoted_hyphen_search(self):
        self._make('A-P8000')
        self._make('AP8000')

        response = self.client.get(reverse('radio_list'), {'query': '"A-P8000"'})
        self.assertEqual(response.status_code, 200)
        models = {radio.model for radio in response.context['radios']}
        self.assertEqual(models, {'A-P8000'})
