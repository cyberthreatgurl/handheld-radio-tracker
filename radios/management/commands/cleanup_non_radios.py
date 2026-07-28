"""
Management command to find and optionally remove non-radio accessory
devices that were erroneously added to the database.

These are FCC-certified accessories (speaker microphones, headsets,
chargers, cables, antennas, etc.) that matched a broad allowlist term
like "RECEIVER" but are not actually two-way radios.

Dry-run (safe, shows what would be removed):
    python manage.py cleanup_non_radios

Delete after confirmation:
    python manage.py cleanup_non_radios --delete

Target a specific grantee:
    python manage.py cleanup_non_radios --grantee YAM --delete

Only flag high-confidence matches:
    python manage.py cleanup_non_radios --min-confidence high
"""

from django.core.management.base import BaseCommand

from radios.models import Radio

# Each tuple is (search_term, category, confidence).
# - search_term: case-insensitive substring to look for in the combined
#   text of model, notes, fcc_id, and brand fields.
# - category: human-readable group for reporting.
# - confidence: 'high' (almost certainly not a radio) or
#   'medium' (likely not a radio but could be a model-name collision).
#
# NOTE: This text-based approach will NOT catch accessories whose FCC
# notes only say "Purpose: Original Equipment" with no descriptive text.
# Those must be handled by adding the grantee code to the
# SyncSkippedGrantee table or by manually reviewing the grantee's
# product line to identify accessory model-number prefixes.
NON_RADIO_PATTERNS = [
    # -- Speaker microphones / audio accessories --
    ("SPEAKER MICROPHONE", "speaker_mic", "high"),
    ("REMOTE SPEAKER MIC", "speaker_mic", "high"),
    ("REMOTE MICROPHONE", "speaker_mic", "high"),
    ("HAND MICROPHONE", "speaker_mic", "high"),
    ("DESK MICROPHONE", "speaker_mic", "high"),
    ("PTT MICROPHONE", "speaker_mic", "medium"),
    ("BLUETOOTH HEADSET", "headset", "high"),
    ("WIRELESS HEADSET", "headset", "high"),
    ("EARPIECE", "headset", "high"),

    # -- Chargers / power --
    ("BATTERY CHARGER", "charger", "high"),
    ("CHARGING CRADLE", "charger", "high"),
    ("DESKTOP CHARGER", "charger", "high"),
    ("RAPID CHARGER", "charger", "high"),
    ("CHARGER CUP", "charger", "high"),
    ("POWER SUPPLY", "power", "high"),
    ("AC ADAPTER", "power", "high"),
    ("DC ADAPTER", "power", "high"),

    # -- Cables --
    ("PROGRAMMING CABLE", "cable", "high"),
    ("CLONING CABLE", "cable", "high"),
    ("DATA CABLE", "cable", "high"),
    ("USB CABLE", "cable", "medium"),

    # -- Mounting / carrying --
    # NOTE: "BELT CLIP" is deliberately excluded — nearly every
    # handheld radio product description mentions a belt clip as an
    # included accessory, so it is not a reliable indicator.
    ("MOUNTING BRACKET", "mounting", "high"),
    ("MOUNTING KIT", "mounting", "high"),
    ("CARRYING CASE", "carrying", "high"),
    ("WATERPROOF BAG", "carrying", "medium"),

    # -- Standalone antennas --
    ("ANTENNA REPLACEMENT", "antenna", "high"),
    ("WHIP ANTENNA", "antenna", "high"),
    ("STUBBY ANTENNA", "antenna", "high"),
    ("REPLACEMENT ANTENNA", "antenna", "high"),

    # -- Known accessory model-number prefixes --
    # Motorola RLN / PMLN / HKLN / NNTN are accessory part numbers.
    ("RLN", "motorola_accessory", "medium"),
    ("PMLN", "motorola_accessory", "medium"),
    ("HKLN", "motorola_accessory", "medium"),
    ("NNTN", "motorola_accessory", "medium"),
    # Hytera YAM-VMxxx = remote speaker microphones (e.g. YAMVM550).
    # The FCC ID is stored without a dash so we match on the prefix.
    ("YAMVM", "hytera_speaker_mic", "medium"),
]


class Command(BaseCommand):
    help = (
        "Find and optionally remove non-radio accessory devices "
        "from the database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete',
            action='store_true',
            help='Delete flagged records (default: dry-run only).',
        )
        parser.add_argument(
            '--grantee',
            type=str,
            default='',
            help='Limit scan to radios whose FCC ID starts with this '
                 'grantee code (e.g. YAM).',
        )
        parser.add_argument(
            '--min-confidence',
            type=str,
            default='medium',
            choices=['high', 'medium'],
            help='Minimum confidence level to flag (default: medium).',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Number of records to fetch per database query.',
        )

    # pylint: disable=too-many-locals
    def handle(self, *args, **options):
        delete_mode = options['delete']
        grantee_filter = options['grantee'].strip().upper()
        min_confidence = options['min_confidence']
        batch_size = options['batch_size']

        queryset = Radio.objects.all()
        if grantee_filter:
            queryset = queryset.filter(fcc_id__istartswith=grantee_filter)

        total_scanned = 0
        flagged_high = []
        flagged_medium = []

        for radio in queryset.iterator(chunk_size=batch_size):
            total_scanned += 1

            # Combine all searchable text into one blob for matching.
            text_blob = ' | '.join(
                part.upper()
                for part in (
                    (radio.model or ''),
                    (radio.notes or ''),
                    (radio.fcc_id or ''),
                    (radio.brand or ''),
                )
                if part
            )

            for term, category, confidence in NON_RADIO_PATTERNS:
                if confidence == 'medium' and min_confidence == 'high':
                    continue

                if term.upper() in text_blob:
                    entry = {
                        'radio_id': radio.id,
                        'fcc_id': radio.fcc_id,
                        'brand': radio.brand,
                        'model': radio.model,
                        'matched_term': term,
                        'category': category,
                        'confidence': confidence,
                    }
                    if confidence == 'high':
                        flagged_high.append(entry)
                    else:
                        flagged_medium.append(entry)
                    break  # one match is enough per radio

        # -- Report --
        flagged = flagged_high + flagged_medium
        self.stdout.write(
            f"\nScanned {total_scanned} radios."
        )
        self.stdout.write(
            f"Flagged: {len(flagged_high)} high-confidence, "
            f"{len(flagged_medium)} medium-confidence.\n"
        )

        if not flagged:
            self.stdout.write(
                self.style.SUCCESS(
                    "No non-radio accessory devices found."
                )
            )
            return

        # Print each flagged item.
        for entry in flagged:
            self.stdout.write(
                f"  [{entry['confidence']:6s}] "
                f"{entry['fcc_id']:20s} "
                f"{entry['brand']:25s} "
                f"{entry['model']:20s} "
                f"matched: '{entry['matched_term']}' "
                f"({entry['category']})"
            )

        if delete_mode:
            self._confirm_and_delete(
                flagged_high, flagged_medium, min_confidence,
            )
        else:
            self.stdout.write(
                f"\nDry run complete.  Run with --delete to remove "
                f"{len(flagged)} flagged records."
            )

    def _confirm_and_delete(self, flagged_high, flagged_medium, min_confidence):
        """Prompt for confirmation, then delete the flagged radios."""
        to_delete_ids = {e['radio_id'] for e in flagged_high}
        if min_confidence == 'medium':
            to_delete_ids.update(e['radio_id'] for e in flagged_medium)

        self.stdout.write(
            self.style.WARNING(
                f"\nAbout to DELETE {len(to_delete_ids)} radio records "
                f"({len(flagged_high)} high + "
                f"{len(flagged_medium)} medium confidence)."
            )
        )
        confirm = input("Type 'yes' to confirm deletion: ")
        if confirm.strip().lower() != 'yes':
            self.stdout.write("Aborted.")
            return

        deleted_count, _deleted_details = Radio.objects.filter(
            pk__in=to_delete_ids,
        ).delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted_count} records "
                f"(including related objects)."
            )
        )
