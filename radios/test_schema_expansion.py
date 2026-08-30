"""
Regression tests for the radio schema expansion (RadioCertification,
RadioServiceType, and new Radio fields).

Covers:
  - Model creation and field defaults
  - Certification summary recomputation
  - RadioForm includes new fields
  - RadioCertificationFormSet CRUD
  - Admin registrations
  - delete_radios_and_related includes certifications
  - Detail view context includes new data
  - Update view handles certification formset
"""
# pylint: disable=no-member, broad-except, too-many-public-methods
# pylint: disable=too-many-ancestors, invalid-name, too-many-locals
# no-member: Django ORM metaclass-based managers are undetectable by pylint

from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse

from radios.models import (
    Radio, RadioCertification, RadioServiceType,
    Brand, Manufacturer, delete_radios_and_related,
)
from radios.forms import RadioForm, RadioCertificationFormSet


class RadioServiceTypeModelTests(TestCase):
    """Tests for the RadioServiceType model and seed migration."""

    def test_seed_migration_created_all_types(self):
        """All canonical service types were seeded by the data migrations."""
        expected = [
            ('GMRS', 'Part 95E'),
            ('FRS', 'Part 95B'),
            ('CB', 'Part 95D'),
            ('MURS', 'Part 95J'),
            ('Amateur', 'Part 97'),
            ('Commercial', 'Part 90'),
            ('Marine', 'Part 80'),
            ('Aviation', 'Part 87'),
            ('PoC', 'Parts 22/24/27'),
            ('Part 15 Subpart B', 'Part 15B'),
            ('Part 15 Subpart C', 'Part 15C'),
        ]
        names = list(
            RadioServiceType.objects.values_list('name', flat=True)
        )
        self.assertEqual(len(names), 11)
        for name, rule_part in expected:
            obj = RadioServiceType.objects.get(name=name)
            self.assertEqual(
                obj.rule_part, rule_part,
                f'{name} should have rule_part={rule_part}',
            )

    def test_str_representation(self):
        """RadioServiceType.__str__ includes name and rule part."""
        gmrs = RadioServiceType.objects.get(name='GMRS')
        self.assertEqual(str(gmrs), 'GMRS (Part 95E)')

    def test_ordering(self):
        """RadioServiceType ordering follows sort_order."""
        names = list(
            RadioServiceType.objects.values_list('name', flat=True)
        )
        self.assertEqual(names[0], 'GMRS')
        self.assertEqual(names[-1], 'Part 15 Subpart C')


class RadioCertificationModelTests(TestCase):
    """Tests for the RadioCertification model."""

    def setUp(self):
        self.brand = Brand.objects.create(name='TestBrand')
        self.radio = Radio.objects.create(
            brand='TestBrand', model='TEST-100', fcc_id='2AJGM-TEST100',
        )

    def test_create_certification(self):
        """Can create a certification record linked to a radio."""
        cert = RadioCertification.objects.create(
            radio=self.radio,
            fcc_id='2AJGM-TEST100',
            grant_date=date(2023, 6, 15),
            authorization_type='certification',
            rule_parts='Part 95E, Part 90',
            freq_range_lower_mhz=462.5500,
            freq_range_upper_mhz=467.7250,
            power_output_watts=5.0,
            power_type='ERP',
            emission_designators='11K0F3E, 7K60FXD',
        )
        self.assertEqual(cert.radio_id, self.radio.pk)
        self.assertEqual(cert.fcc_id, '2AJGM-TEST100')
        self.assertEqual(cert.grant_date, date(2023, 6, 15))
        self.assertEqual(cert.rule_parts, 'Part 95E, Part 90')
        self.assertEqual(float(cert.freq_range_lower_mhz), 462.5500)
        self.assertEqual(float(cert.power_output_watts), 5.0)
        self.assertEqual(cert.power_type, 'ERP')

    def test_certification_defaults(self):
        """Default values are sensible."""
        cert = RadioCertification.objects.create(radio=self.radio)
        self.assertEqual(cert.authorization_type, 'certification')
        self.assertEqual(cert.power_type, '')
        self.assertIsNone(cert.grant_date)

    def test_ordering_by_grant_date(self):
        """Certifications are ordered by grant_date descending."""
        cert1 = RadioCertification.objects.create(
            radio=self.radio, grant_date=date(2020, 1, 1),
        )
        cert2 = RadioCertification.objects.create(
            radio=self.radio, grant_date=date(2023, 6, 1),
        )
        certs = list(self.radio.certifications.all())
        self.assertEqual(certs[0].pk, cert2.pk)
        self.assertEqual(certs[1].pk, cert1.pk)


class RadioNewFieldTests(TestCase):
    """Tests for the new fields added to the Radio model."""

    def setUp(self):
        self.brand = Brand.objects.create(name='FieldTestBrand')
        self.radio = Radio.objects.create(
            brand='FieldTestBrand', model='FT-200',
        )

    def test_hardware_feature_defaults(self):
        """New boolean fields have correct defaults."""
        self.assertFalse(self.radio.usb_c_charging)
        self.assertTrue(self.radio.removable_antenna)
        self.assertFalse(self.radio.unlockable)
        self.assertFalse(self.radio.firmware_updates)

    def test_hardware_features_persist(self):
        """Hardware boolean fields survive round-trip."""
        self.radio.usb_c_charging = True
        self.radio.removable_antenna = False
        self.radio.unlockable = True
        self.radio.firmware_updates = True
        self.radio.save()
        reloaded = Radio.objects.get(pk=self.radio.pk)
        self.assertTrue(reloaded.usb_c_charging)
        self.assertFalse(reloaded.removable_antenna)
        self.assertTrue(reloaded.unlockable)
        self.assertTrue(reloaded.firmware_updates)

    def test_summary_fields_default_empty(self):
        """Summary fields start empty."""
        self.assertEqual(self.radio.rule_parts_summary, '')
        self.assertEqual(self.radio.emission_designators_summary, '')
        self.assertEqual(self.radio.authorization_type_summary, '')

    def test_service_types_m2m(self):
        """Can assign multiple service types to a radio."""
        gmrs = RadioServiceType.objects.get(name='GMRS')
        amateur = RadioServiceType.objects.get(name='Amateur')
        self.radio.service_types.add(gmrs, amateur)
        self.assertEqual(self.radio.service_types.count(), 2)
        names = sorted(
            self.radio.service_types.values_list('name', flat=True),
        )
        self.assertEqual(names, ['Amateur', 'GMRS'])


class CertificationSummaryTests(TestCase):
    """Tests for recompute_certification_summary."""

    def setUp(self):
        self.brand = Brand.objects.create(name='SummaryBrand')
        self.radio = Radio.objects.create(
            brand='SummaryBrand', model='SUM-300', fcc_id='TEST-SUM300',
        )

    def test_empty_certifications_clears_summaries(self):
        """When there are no certifications, summaries are empty."""
        self.radio.rule_parts_summary = 'old value'
        self.radio.save()
        self.radio.recompute_certification_summary(save=True)
        self.assertEqual(self.radio.rule_parts_summary, '')
        self.assertEqual(self.radio.emission_designators_summary, '')
        self.assertEqual(self.radio.authorization_type_summary, '')

    def test_single_certification_populates_summaries(self):
        """One certification's data populates all three summary fields."""
        RadioCertification.objects.create(
            radio=self.radio,
            rule_parts='Part 95E, Part 90',
            emission_designators='11K0F3E, 7K60FXD',
            authorization_type='certification',
        )
        self.radio.recompute_certification_summary(save=True)
        self.assertIn('Part 90', self.radio.rule_parts_summary)
        self.assertIn('Part 95E', self.radio.rule_parts_summary)
        self.assertIn('11K0F3E', self.radio.emission_designators_summary)
        self.assertIn('7K60FXD', self.radio.emission_designators_summary)
        self.assertIn('Certification', self.radio.authorization_type_summary)

    def test_multiple_certifications_deduplicate(self):
        """Overlapping values across certifications are deduplicated."""
        RadioCertification.objects.create(
            radio=self.radio,
            rule_parts='Part 90, Part 95E',
            emission_designators='11K0F3E',
            authorization_type='certification',
        )
        RadioCertification.objects.create(
            radio=self.radio,
            rule_parts='Part 95E',
            emission_designators='11K0F3E, 7K60FXD',
            authorization_type='certification',
        )
        self.radio.recompute_certification_summary(save=True)
        # 'Part 95E' should appear only once
        self.assertEqual(
            self.radio.rule_parts_summary.count('Part 95E'), 1,
        )
        # '11K0F3E' should appear only once
        self.assertEqual(
            self.radio.emission_designators_summary.count('11K0F3E'), 1,
        )
        # Only one 'Certification'
        self.assertEqual(self.radio.authorization_type_summary, 'Certification')

    def test_mixed_authorization_types(self):
        """Different authorization types are joined with ' + '."""
        RadioCertification.objects.create(
            radio=self.radio,
            authorization_type='certification',
            rule_parts='Part 90',
            emission_designators='11K0F3E',
        )
        RadioCertification.objects.create(
            radio=self.radio,
            authorization_type='sdoc',
            rule_parts='Part 15B',
            emission_designators='N/A',
        )
        self.radio.recompute_certification_summary(save=True)
        self.assertIn('Certification', self.radio.authorization_type_summary)
        self.assertIn("Supplier's Declaration of Conformity (SDoC)",
                       self.radio.authorization_type_summary)
        self.assertIn(' + ', self.radio.authorization_type_summary)


class RadioFormTests(TestCase):
    """Tests for RadioForm with the new schema fields."""

    def setUp(self):
        Brand.objects.create(name='FormBrand', grantee_code='2AJGM')
        Manufacturer.objects.create(full_name='FormMfr Inc.', alias='FormMfr')

    def test_form_includes_new_fields(self):
        """RadioForm.Meta.fields includes hardware booleans and service_types."""
        form = RadioForm()
        field_names = list(form.fields.keys())
        for field in ('usb_c_charging', 'removable_antenna', 'unlockable',
                      'firmware_updates', 'service_types'):
            self.assertIn(field, field_names,
                          f'{field} missing from RadioForm fields')

    def test_form_valid_with_new_fields(self):
        """Submitting new fields passes validation."""
        form = RadioForm(data={
            'brand': 'FormBrand',
            'model': 'FT-999',
            'usb_c_charging': True,
            'removable_antenna': False,
            'unlockable': False,
            'firmware_updates': True,
        })
        self.assertTrue(form.is_valid(), f'Form errors: {form.errors}')

    def test_form_saves_hardware_features(self):
        """Saving the form persists hardware booleans."""
        form = RadioForm(data={
            'brand': 'FormBrand',
            'model': 'FT-SAVE',
            'usb_c_charging': True,
            'removable_antenna': False,
            'unlockable': True,
            'firmware_updates': False,
        })
        self.assertTrue(form.is_valid(), f'Form errors: {form.errors}')
        radio = form.save()
        self.assertTrue(radio.usb_c_charging)
        self.assertFalse(radio.removable_antenna)
        self.assertTrue(radio.unlockable)
        self.assertFalse(radio.firmware_updates)

    def test_form_saves_service_types(self):
        """Saving with service_types M2M works."""
        gmrs = RadioServiceType.objects.get(name='GMRS')
        amateur = RadioServiceType.objects.get(name='Amateur')
        form = RadioForm(data={
            'brand': 'FormBrand',
            'model': 'FT-M2M',
            'service_types': [gmrs.pk, amateur.pk],
        })
        self.assertTrue(form.is_valid(), f'Form errors: {form.errors}')
        radio = form.save()
        self.assertEqual(radio.service_types.count(), 2)


class RadioCertificationFormSetTests(TestCase):
    """Tests for RadioCertificationFormSet."""

    def setUp(self):
        self.brand = Brand.objects.create(name='CertFormBrand')
        self.radio = Radio.objects.create(
            brand='CertFormBrand', model='CERT-500',
        )

    def test_formset_creates_certification(self):
        """Submitting formset with valid data creates a certification."""
        formset = RadioCertificationFormSet(
            data={
                'certifications-TOTAL_FORMS': '1',
                'certifications-INITIAL_FORMS': '0',
                'certifications-MIN_NUM_FORMS': '0',
                'certifications-MAX_NUM_FORMS': '1000',
                'certifications-0-radio': self.radio.pk,
                'certifications-0-fcc_id': 'TEST-CERT500',
                'certifications-0-grant_date': '2024-03-15',
                'certifications-0-authorization_type': 'certification',
                'certifications-0-rule_parts': 'Part 90',
                'certifications-0-emission_designators': '11K0F3E',
            },
            instance=self.radio,
            prefix='certifications',
        )
        self.assertTrue(formset.is_valid(), f'Formset errors: {formset.errors}')
        instances = formset.save()
        self.assertEqual(len(instances), 1)
        cert = instances[0]
        self.assertEqual(cert.fcc_id, 'TEST-CERT500')
        self.assertEqual(cert.rule_parts, 'Part 90')

    def test_formset_deletes_certification(self):
        """Formset with DELETE flag removes the certification."""
        cert = RadioCertification.objects.create(
            radio=self.radio, fcc_id='TO-DELETE',
        )
        self.assertEqual(self.radio.certifications.count(), 1)

        # Build formset data that mirrors what a browser would POST:
        # TOTAL_FORMS, INITIAL_FORMS, MIN/MAX_NUM_FORMS, plus one row
        # for the existing cert with DELETE=on.
        data = {
            'certifications-TOTAL_FORMS': '1',
            'certifications-INITIAL_FORMS': '1',
            'certifications-MIN_NUM_FORMS': '0',
            'certifications-MAX_NUM_FORMS': '1000',
            'certifications-0-id': str(cert.pk),
            'certifications-0-radio': str(self.radio.pk),
            'certifications-0-fcc_id': 'TO-DELETE',
            'certifications-0-grant_date': '',
            'certifications-0-authorization_type': 'certification',
            'certifications-0-rule_parts': '',
            'certifications-0-freq_range_lower_mhz': '',
            'certifications-0-freq_range_upper_mhz': '',
            'certifications-0-power_output_watts': '',
            'certifications-0-power_type': '',
            'certifications-0-emission_designators': '',
            'certifications-0-DELETE': 'on',
        }
        bound_formset = RadioCertificationFormSet(
            data=data,
            instance=self.radio,
            prefix='certifications',
        )
        self.assertTrue(
            bound_formset.is_valid(),
            (f'Formset errors: {bound_formset.errors} '
             f'non_form_errors: {bound_formset.non_form_errors()}'),
        )
        bound_formset.save()
        self.assertEqual(self.radio.certifications.count(), 0)


class DeleteRadiosAndRelatedTests(TestCase):
    """Tests that delete_radios_and_related includes certifications."""

    def setUp(self):
        self.brand = Brand.objects.create(name='DeleteBrand')
        self.radio = Radio.objects.create(
            brand='DeleteBrand', model='DEL-700',
        )

    def test_delete_includes_certifications(self):
        """Deleting a radio also deletes its certifications."""
        RadioCertification.objects.create(
            radio=self.radio, fcc_id='D-WILL-DELETE',
        )
        self.assertEqual(RadioCertification.objects.count(), 1)
        summary = delete_radios_and_related(Radio.objects.filter(pk=self.radio.pk))
        self.assertEqual(summary['radios_deleted'], 1)
        self.assertEqual(summary['certifications_deleted'], 1)
        self.assertEqual(RadioCertification.objects.count(), 0)

    def test_delete_key_in_return_dict(self):
        """The return dict has 'certifications_deleted' key."""
        summary = delete_radios_and_related(Radio.objects.filter(pk=self.radio.pk))
        self.assertIn('certifications_deleted', summary)


class RadioDetailViewTests(TestCase):
    """Tests for RadioDetailView with new context data."""

    def setUp(self):
        self.brand = Brand.objects.create(name='DetailBrand')
        self.radio = Radio.objects.create(
            brand='DetailBrand', model='DET-800', fcc_id='2AJGM-DET800',
        )
        self.gmrs = RadioServiceType.objects.get(name='GMRS')
        self.radio.service_types.add(self.gmrs)
        RadioCertification.objects.create(
            radio=self.radio,
            fcc_id='2AJGM-DET800',
            rule_parts='Part 95E',
            emission_designators='11K0F3E',
            grant_date=date(2024, 1, 15),
        )

    def test_detail_view_has_service_types(self):
        """Detail view context includes service_types."""
        client = Client()
        response = client.get(
            reverse('radio_detail', kwargs={'pk': self.radio.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('service_types', response.context)
        svc_names = [st.name for st in response.context['service_types']]
        self.assertIn('GMRS', svc_names)

    def test_detail_view_has_certifications(self):
        """Detail view context includes certifications."""
        client = Client()
        response = client.get(
            reverse('radio_detail', kwargs={'pk': self.radio.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('certifications', response.context)
        certs = list(response.context['certifications'])
        self.assertEqual(len(certs), 1)
        self.assertEqual(certs[0].rule_parts, 'Part 95E')

    def test_detail_view_renders_certification_data(self):
        """Detail page HTML contains certification rule parts."""
        client = Client()
        response = client.get(
            reverse('radio_detail', kwargs={'pk': self.radio.pk}),
        )
        self.assertContains(response, 'Part 95E', status_code=200)

    def test_detail_view_renders_service_type_badges(self):
        """Detail page HTML contains service type badges."""
        client = Client()
        response = client.get(
            reverse('radio_detail', kwargs={'pk': self.radio.pk}),
        )
        # Service type badge should appear
        self.assertContains(response, 'GMRS', status_code=200)


class RadioUpdateViewTests(TestCase):
    """Tests for RadioUpdateView with certification formset."""

    def setUp(self):
        self.brand = Brand.objects.create(name='UpdateBrand')
        self.radio = Radio.objects.create(
            brand='UpdateBrand', model='UPD-900',
        )
        staff = User.objects.create_user(
            username='staff', password='testpass123', is_staff=True,
        )
        self.client.force_login(staff)

    def test_edit_page_has_certification_formset(self):
        """Edit page context includes certification_formset."""
        response = self.client.get(
            reverse('radio_edit', kwargs={'pk': self.radio.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('certification_formset', response.context)

    def test_edit_page_renders_certification_section(self):
        """Edit page HTML contains FCC Certifications section."""
        response = self.client.get(
            reverse('radio_edit', kwargs={'pk': self.radio.pk}),
        )
        self.assertContains(response, 'FCC Certifications', status_code=200)

    def test_edit_page_renders_hardware_features(self):
        """Edit page HTML contains hardware feature checkboxes."""
        response = self.client.get(
            reverse('radio_edit', kwargs={'pk': self.radio.pk}),
        )
        self.assertContains(response, 'USB-C Charging', status_code=200)
        self.assertContains(response, 'Removable Antenna', status_code=200)
        self.assertContains(response, 'Unlockable', status_code=200)
        self.assertContains(response, 'Firmware Updates', status_code=200)


class RadioCreateViewTests(TestCase):
    """Tests for RadioCreateView."""

    def setUp(self):
        Brand.objects.create(name='CreateBrand')
        staff = User.objects.create_user(
            username='staff', password='testpass123', is_staff=True,
        )
        self.client.force_login(staff)

    def test_create_page_has_hardware_features(self):
        """Create page HTML includes hardware feature checkboxes."""
        response = self.client.get(reverse('radio_add'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'USB-C Charging')
        self.assertContains(response, 'Removable Antenna')

    def test_create_page_has_certification_section(self):
        """Create page HTML includes FCC Certifications section."""
        response = self.client.get(reverse('radio_add'))
        self.assertContains(response, 'FCC Certifications', status_code=200)


class RadioAdminTests(TestCase):
    """Tests for admin registrations of new models."""

    def test_radio_certification_admin_registered(self):
        """RadioCertification has an admin class registered."""
        from django.contrib import admin
        from radios.models import RadioCertification
        self.assertTrue(admin.site.is_registered(RadioCertification))

    def test_radio_service_type_admin_registered(self):
        """RadioServiceType has an admin class registered."""
        from django.contrib import admin
        from radios.models import RadioServiceType
        self.assertTrue(admin.site.is_registered(RadioServiceType))


class FormsetEdgeCaseTests(TestCase):
    """Edge case tests for certification formset."""

    def setUp(self):
        self.brand = Brand.objects.create(name='EdgeBrand')
        self.radio = Radio.objects.create(
            brand='EdgeBrand', model='EDGE-001',
        )

    def test_empty_formset_save_recomputes_empty_summaries(self):
        """Saving empty formset calls recompute which clears summaries."""
        self.radio.rule_parts_summary = 'stale'
        self.radio.save()
        self.radio.recompute_certification_summary(save=True)
        self.assertEqual(self.radio.rule_parts_summary, '')

    def test_certification_queryset_is_empty_for_new_radio(self):
        """A brand-new radio with no certifications returns empty queryset."""
        self.assertEqual(self.radio.certifications.count(), 0)
        self.assertEqual(list(self.radio.certifications.all()), [])
