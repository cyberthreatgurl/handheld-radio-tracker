from django.test import TestCase
from .models import Radio


class RadioModelTest(TestCase):
    def setUp(self):
        Radio.objects.create(
            brand='Baofeng',
            model='UV-5R',
            fcc_id='2AJGM-UV5R',
            grantee_code='2AJGM'
        )
    
    def test_radio_string_representation(self):
        radio = Radio.objects.get(model='UV-5R')
        self.assertEqual(str(radio), 'Baofeng UV-5R')
    
    def test_radio_creation(self):
        radio = Radio.objects.get(model='UV-5R')
        self.assertEqual(radio.brand, 'Baofeng')
        self.assertEqual(radio.fcc_id, '2AJGM-UV5R')
