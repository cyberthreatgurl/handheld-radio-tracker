"""Unit tests for the website spec import extraction engine."""
from bs4 import BeautifulSoup
from django.test import SimpleTestCase

from .manual_extraction import extract_specs_from_text
from .site_import import (
    _detect_service_hints,
    _derive_model,
    _merge_specs,
    _parse_spec_pairs,
    extract_spec_pairs,
)


class ModelDerivationTest(SimpleTestCase):
    """Model token extraction from heterogeneous product titles."""

    def test_model_from_example_titles(self):
        cases = {
            'BTECH DA-7X2 Digital & Analog Dual Band Two-Way Radio': 'DA-7X2',
            'Radtel RT-S10 Dual Band 10W Handheld 2 Way Radio': 'RT-S10',
            '3rd Gen TD-H8 10W Ham GMRS Radio Handheld 8-Band': 'TD-H8',
            'GT-5R Pro 5W Multi-Band Radio': 'GT-5R',
        }
        for title, expected in cases.items():
            self.assertEqual(_derive_model(title), expected)


class SpecExtractionTest(SimpleTestCase):
    """New capability fields emitted from free-text product copy."""

    def test_new_capability_fields(self):
        text = (
            "10W High Power, 199 Memory Channels, 2500mAh Battery, "
            "USB Type-C Charging, Built-in Bluetooth, NOAA Weather, "
            "Air Band Receive, GPS, DMR, 1.77 inch TFT Color Display, "
            "$79.00, SKU: 101293, FCC ID: 2AJGM-UV5R"
        )
        specs = extract_specs_from_text(text)
        self.assertEqual(specs['power_watts'], '10W')
        self.assertEqual(specs['channels'], 199)
        self.assertEqual(specs['battery_mah'], 2500)
        self.assertEqual(specs['display_color'], 'Color')
        self.assertEqual(specs['part_number'], '101293')
        self.assertTrue(specs['noaa'])
        self.assertTrue(specs['bluetooth'])
        self.assertTrue(specs['usb_chargeable'])
        self.assertFalse(specs['usb_programmable'])

    def test_usb_programmable_detection(self):
        specs = extract_specs_from_text(
            "Program the radio via USB programming cable using CHIRP or CPS.",
        )
        self.assertTrue(specs['usb_programmable'])


class SpecPairsTest(SimpleTestCase):
    """Label/value spec table parsing."""

    def test_table_rows_map_to_fields(self):
        html = (
            '<table><tr><th>Memory Channels</th><td>128</td></tr>'
            '<tr><th>Battery Capacity</th><td>1800 mAh</td></tr>'
            '<tr><th>Output Power</th><td>5W</td></tr></table>'
        )
        soup = BeautifulSoup(html, 'html.parser')
        parsed = _parse_spec_pairs(extract_spec_pairs(soup))
        self.assertEqual(parsed['channels'], 128)
        self.assertEqual(parsed['battery_mah'], 1800)
        self.assertEqual(parsed['power_watts'], '5W')


class ServiceHintsTest(SimpleTestCase):
    """GMRS / FRS / Amateur service detection."""

    def test_service_hints(self):
        self.assertEqual(
            _detect_service_hints('Ham GMRS FRS dual-band radio with NOAA'),
            ['GMRS', 'FRS', 'Amateur'],
        )


class MergePrecedenceTest(SimpleTestCase):
    """Higher-precedence layers win and empty values never clobber."""

    def test_first_non_empty_wins(self):
        merged = _merge_specs(
            {'power_watts': '10W', 'gps': 'Yes'},
            {'power_watts': '5W', 'dmr': 'Yes'},
        )
        self.assertEqual(merged['power_watts'], '10W')
        self.assertEqual(merged['gps'], 'Yes')
        self.assertEqual(merged['dmr'], 'Yes')
