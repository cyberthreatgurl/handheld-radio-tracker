Let me gather context about the current models, views, and templates to build a thorough plan.

Searched for files matching `**/views*.py`, 4 matches

Searched for files matching `**/forms*.py`, 3 matches

Searched for files matching `**/admin*.py`, 1 match

Read [](file:///Users/ashaw/code/radio-tracker/radios/models.py#1-1), lines 1 to 100

Read [](file:///Users/ashaw/code/radio-tracker/radios/models.py#100-100), lines 100 to 300

Read [](file:///Users/ashaw/code/radio-tracker/radios/forms.py#1-1), lines 1 to 120

Read [](file:///Users/ashaw/code/radio-tracker/radios/admin.py#1-1), lines 1 to 120

Read [](file:///Users/ashaw/code/radio-tracker/radios/views.py#1-1), lines 1 to 100

Read [](file:///Users/ashaw/code/radio-tracker/radios/models.py#300-300), lines 300 to 500

Read [](file:///Users/ashaw/code/radio-tracker/radios/admin.py#120-120), lines 120 to 200

Searched for files matching `**/templates/radios/*radio*`, 6 matches

Read [](file:///Users/ashaw/code/radio-tracker/radios/forms.py#120-120), lines 120 to 250

Read [](file:///Users/ashaw/code/radio-tracker/radios/views.py#100-100), lines 100 to 400

Read [](file:///Users/ashaw/code/radio-tracker/radios/templates/radios/radio_form.html#1-1), lines 1 to 150

Read [](file:///Users/ashaw/code/radio-tracker/radios/templates/radios/radio_detail.html#1-1), lines 1 to 150

Read [](file:///Users/ashaw/code/radio-tracker/radios/templates/radios/radio_detail.html#150-150), lines 150 to 350

Read [](file:///Users/ashaw/code/radio-tracker/radios/templates/radios/radio_form.html#150-150), lines 150 to 350

Searched for regex `class Radio(UpdateView|CreateView|DetailView)` (`**/radios/views.py`), 3 results

Read [](file:///Users/ashaw/code/radio-tracker/radios/views.py#400-400), lines 400 to 550

Read [](file:///Users/ashaw/code/radio-tracker/radios/views.py#630-630), lines 630 to 780

Read memory [](file:///Users/ashaw/Library/Application%20Support/Code/User/workspaceStorage/41a8dae8fa411ca39ed8fd761f13b357/GitHub.copilot-chat/memory-tool/memories/repo/django-radio-tracker-architecture.md)

Now I have a thorough understanding of the codebase. Here's the complete wireframe plan:

---

## Wireframe Plan: Expanding Radio Schema with FCC & Service Metadata

### 1. New/Modified Django Models (models.py)

#### 1a. New `RadioCertification` Model (many-to-one from Radio)

A radio can have multiple FCC certifications (e.g., Part 90 + Part 95E dual-certified). This is a separate table so each certification entry has its own grant date, rule parts, frequencies, power, and emission designators.

```python
class RadioCertification(models.Model):
    """A single FCC grant/certification for a radio model."""

    class AuthorizationType(models.TextChoices):
        CERTIFICATION = 'certification', 'Certification'
        SDOC = 'sdoc', 'Supplier\'s Declaration of Conformity (SDoC)'
        VERIFICATION = 'verification', 'Verification'

    radio = models.ForeignKey(
        'Radio', on_delete=models.CASCADE,
        related_name='certifications',
    )
    fcc_id = models.CharField(max_length=50, blank=True)  # may differ from radio.fcc_id for Change-in-ID
    grant_date = models.DateField(null=True, blank=True)
    authorization_type = models.CharField(
        max_length=20, choices=AuthorizationType.choices,
        blank=True, default='certification',
    )
    rule_parts = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated 47 CFR parts, e.g. 'Part 95E, Part 90'",
    )
    freq_range_lower_mhz = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
    )
    freq_range_upper_mhz = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
    )
    power_output_watts = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
    )
    power_type = models.CharField(
        max_length=20, blank=True,
        help_text="ERP, EIRP, or Conducted",
    )
    emission_designators = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated, e.g. '11K0F3E, 7K60FXD'",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-grant_date']
        indexes = [
            models.Index(fields=['radio', 'fcc_id']),
        ]
```

#### 1b. New `RadioServiceType` Model & M2M on Radio

A many-to-many so a radio can be tagged with multiple service classifications.

```python
class RadioServiceType(models.Model):
    """Service classification for a radio (GMRS, FRS, Amateur, etc.)."""

    name = models.CharField(max_length=100, unique=True)
    # Pre-populated choices: GMRS, FRS, PoC, Amateur, CB, MURS,
    #                        Commercial/Part 90, Marine, Aviation, Scanner
    rule_part = models.CharField(
        max_length=100, blank=True,
        help_text="Corresponding 47 CFR part, e.g. 'Part 95E'",
    )
    description = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} ({self.rule_part})" if self.rule_part else self.name
```

And on `Radio`, add the M2M:
```python
service_types = models.ManyToManyField(
    RadioServiceType, blank=True,
    related_name='radios',
    help_text="Service classifications for this radio",
)
```

#### 1c. New fields directly on `Radio` model

```python
# --- New hardware/feature fields ---
usb_c_charging = models.BooleanField(
    default=False,
    help_text="Has USB-C charging port",
)
removable_antenna = models.BooleanField(
    default=True,
    help_text="Antenna is user-removable (vs. fixed)",
)
unlockable = models.BooleanField(
    default=False,
    help_text="Can be unlocked/widened via key combo or software",
)
firmware_updates = models.BooleanField(
    default=False,
    help_text="Manufacturer provides firmware updates",
)

# --- FCC metadata (consolidated from certifications for quick display) ---
rule_parts_summary = models.CharField(
    max_length=500, blank=True,
    help_text="Auto-computed: unique rule parts across all certifications",
)
emission_designators_summary = models.CharField(
    max_length=500, blank=True,
    help_text="Auto-computed: unique emission designators across all certifications",
)
authorization_type_summary = models.CharField(
    max_length=100, blank=True,
    help_text="Auto-computed: e.g. 'Certification' or 'Certification + SDoC'",
)
```

---

### 2. Database Migrations

```bash
python manage.py makemigrations radios
python manage.py migrate radios
```

This will produce:
- New table `radios_radiocertification`
- New table `radios_radioservicetype`
- New through table `radios_radio_service_types`
- New columns on `radios_radio`: `usb_c_charging`, `removable_antenna`, `unlockable`, `firmware_updates`, `rule_parts_summary`, `emission_designators_summary`, `authorization_type_summary`

Also need a **data migration** to seed `RadioServiceType` with the canonical service list:
```
GMRS       (Part 95E)
FRS        (Part 95B)
CB         (Part 95D)
MURS       (Part 95J)
Amateur    (Part 97)
Commercial (Part 90)
Marine     (Part 80)
Aviation   (Part 87)
PoC        (Parts 22/24/27)
```

---

### 3. Form Changes (forms.py)

**`RadioForm.Meta.fields`** — add the new Radio-level fields:

```python
fields = [
    # ... existing fields ...
    'usb_c_charging', 'removable_antenna', 'unlockable', 'firmware_updates',
    'service_types',
]
```

Each boolean gets a `CheckboxInput` widget like `is_a_whitelabel` already uses. `service_types` gets a `CheckboxSelectMultiple` or a `SelectMultiple` widget.

**New inline formset**: `RadioCertificationFormSet` — allows editing certifications inline on the radio edit page, using Django's `inlineformset_factory`:

```python
RadioCertificationFormSet = inlineformset_factory(
    Radio, RadioCertification,
    fields=['fcc_id', 'grant_date', 'authorization_type', 'rule_parts',
            'freq_range_lower_mhz', 'freq_range_upper_mhz',
            'power_output_watts', 'power_type', 'emission_designators'],
    extra=1, can_delete=True,
)
```

---

### 4. View Changes (views.py)

**`RadioUpdateView`**: Add `RadioCertificationFormSet` to context and handle it in `post()` (same pattern as `RadioImageFormSet`).

**`RadioDetailView`**: Fetch related `certifications.all()` and `service_types.all()` in `get_context_data()` so the template can render them in a new "FCC Certifications" section and a "Service Types" badge row.

---

### 5. Template Changes

#### 5a. radio_detail.html

New sections after "FCC & Technical Information":

- **Service Types badge row**: Display each `radio.service_types.all` as a colored badge (e.g., `<span class="bg-blue-100 text-blue-800 rounded-full px-2 py-0.5 text-xs">GMRS</span>`)

- **FCC Certifications table**: Iterate `certifications.all`, showing:
  - FCC ID, Grant Date, Auth Type, Rule Parts, Frequency Range, Power, Emission Designators

- **New Hardware Features grid** in the "Technical" section, alongside existing GPS/APRS/DMR fields:
  - USB-C Charging: Yes/No badge
  - Removable Antenna: Yes/No badge
  - Unlockable: Yes/No badge
  - Firmware Updates: Yes/No badge

#### 5b. radio_form.html

- Add the 4 boolean checkboxes in a new "Hardware Features" fieldset
- Add `service_types` `CheckboxSelectMultiple` in the "Basic Information" or a new "Service Classification" section
- Render the `certifications_formset` inline below FCC fields (like images formset pattern)

---

### 6. Admin Changes (admin.py)

**`RadioAdmin.fieldsets`**: Add new fieldsets:

```python
('Service Classification', {
    'fields': ('service_types',)
}),
('Hardware Features', {
    'fields': ('usb_c_charging', 'removable_antenna', 'unlockable', 'firmware_updates')
}),
('FCC Certification Summary', {
    'fields': ('rule_parts_summary', 'emission_designators_summary', 'authorization_type_summary')
}),
```

Register `RadioCertification` as an inline:

```python
class RadioCertificationInline(admin.TabularInline):
    model = RadioCertification
    extra = 1
```

Register `RadioServiceType` in admin with `list_display = ['name', 'rule_part', 'sort_order']`.

Add `service_types` to `RadioAdmin.list_filter` and `search_fields`.

---

### 7. FCC Sync/Ingest Changes (fcc_utils.py)

The `fetch_fcc_secondary_metadata()` function already parses `original_equipment_rows` with `lower_freq_mhz`, `upper_freq_mhz`, etc. Extend it to also extract:

- **Rule Parts**: Parse from `equipment_class_description` or a new key from the XML/HTML
- **Emission Designators**: Map from the FCC search results
- **Authorization Type**: From `application_purpose` or `equipment_class`

Then in `fetch_and_sync_fcc_id()`, when creating/updating a radio, also:
1. Create/update `RadioCertification` records from the parsed FCC data
2. Auto-compute `rule_parts_summary`, `emission_designators_summary`, `authorization_type_summary` as a post-save signal or in the sync function
3. Auto-assign `service_types` M2M based on rule parts (e.g., `Part 95E` → `GMRS`, `Part 90` → `Commercial`)

---

### 8. Implementation Order (Recommended)

| Step | Task | Files |
|------|------|-------|
| 1 | Create `RadioServiceType` model + data migration to seed canonical services | models.py, data migration |
| 2 | Create `RadioCertification` model | models.py |
| 3 | Add 4 hardware booleans + 3 summary fields to `Radio` | models.py |
| 4 | Add M2M `service_types` to `Radio` | models.py |
| 5 | Run `makemigrations` + `migrate` | — |
| 6 | Update `RadioForm` with new fields + `RadioCertificationFormSet` | forms.py |
| 7 | Update `RadioAdmin` fieldsets + add `RadioCertificationInline` + register `RadioServiceType` admin | admin.py |
| 8 | Update `RadioUpdateView` to handle certifications formset | views.py |
| 9 | Update `RadioDetailView` context + radio_detail.html template | views.py, radio_detail.html |
| 10 | Update radio_form.html with new field sections + certifications formset | radio_form.html |
| 11 | Extend FCC sync to populate certifications + auto-assign service types + compute summaries | `fcc_utils.py` |

---

### 9. Key Design Decisions

- **Why a separate `RadioCertification` model instead of fields on `Radio`?** Many radios are multi-certified (Part 90 + Part 95E). Putting rule parts, emission designators, and per-grant power/frequency into the Radio row would denormalize and lose per-certification fidelity. The summary fields on `Radio` (`rule_parts_summary`, etc.) provide quick display without joins.

- **Why `RadioServiceType` as M2M instead of a CharField?** Services are a controlled vocabulary (GMRS, FRS, Amateur, etc.) and M2M allows a radio to be tagged with multiple services (e.g., a dual-certified GMRS + Part 90 radio). The separate table makes the service list user-maintainable and enables filtering in the list view.

- **Why booleans for hardware features instead of CharField?** USB-C charging, removable antenna, unlockable, and firmware updates are all binary yes/no questions. Boolean avoids free-text inconsistency and enables filtering (`list_filter`).