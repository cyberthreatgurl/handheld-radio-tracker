"""
Management command: check_db_consistency

Audits the database for consistency issues across five categories:
  brands        — Brand.grantee_code vs FCC registry applicant name
  radios        — Radio.fcc_id grantee vs Radio.brand (white-label awareness)
  manufacturers — Manufacturer.full_name format (corporate designator required)
  hierarchy     — Parent/subsidiary chain integrity, duplicate grantees, orphan records
  fcc-ids       — FCC ID grantee length rule and product-code presence

Usage:
  python manage.py check_db_consistency
  python manage.py check_db_consistency --check brands,radios
  python manage.py check_db_consistency --check brands --fetch-live --verbose
  python manage.py check_db_consistency --xml-dir /path/to/data --quiet
"""

from django.core.management.base import BaseCommand

from radios.consistency_checks import ALL_CHECKS, run_all_checks

LEVEL_COLORS = {
    "ERROR":   "ERROR",
    "WARNING": "WARNING",
    "INFO":    "SUCCESS",
}


class Command(BaseCommand):
    help = "Run database consistency checks and print discrepancies to the console."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            dest="checks",
            default=",".join(ALL_CHECKS),
            help=(
                "Comma-separated list of checks to run. "
                f"Available: {', '.join(ALL_CHECKS)}. "
                "Default: all."
            ),
        )
        parser.add_argument(
            "--xml-dir",
            dest="xml_dir",
            default="data",
            help="Directory containing cached FCC XML files (default: data/).",
        )
        parser.add_argument(
            "--fetch-live",
            dest="fetch_live",
            action="store_true",
            default=False,
            help="Query the FCC API for grantees not found in local XML files.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            default=False,
            help="Show passing (OK) records in addition to failures.",
        )
        parser.add_argument(
            "--quiet",
            action="store_true",
            default=False,
            help="Suppress the summary footer.",
        )

    def handle(self, *args, **options):
        raw_checks = [c.strip() for c in options["checks"].split(",") if c.strip()]
        invalid = [c for c in raw_checks if c not in ALL_CHECKS]
        if invalid:
            self.stderr.write(
                self.style.ERROR(
                    f"Unknown check(s): {', '.join(invalid)}. "
                    f"Valid options: {', '.join(ALL_CHECKS)}"
                )
            )
            return

        xml_dir    = options["xml_dir"]
        fetch_live = options["fetch_live"]
        verbose    = options["verbose"]
        quiet      = options["quiet"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"\n=== DB Consistency Check | checks={','.join(raw_checks)} "
            f"| xml_dir={xml_dir} | fetch_live={fetch_live} ===\n"
        ))

        issues = run_all_checks(
            checks=raw_checks,
            xml_dir=xml_dir,
            fetch_live=fetch_live,
            verbose=verbose,
        )

        # --- Print issues grouped by check phase ---
        current_check = None
        counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}

        for issue in issues:
            level   = issue["level"]
            check   = issue["check"]
            message = issue["message"]

            counts[level] = counts.get(level, 0) + 1

            if check != current_check:
                current_check = check
                self.stdout.write("")
                self.stdout.write(self.style.MIGRATE_HEADING(f"--- {check.upper()} ---"))

            style_fn = getattr(self.style, LEVEL_COLORS.get(level, "WARNING"), self.style.WARNING)
            self.stdout.write(style_fn(f"  [{level}] {message}"))

        # --- Summary footer ---
        if not quiet:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("=== SUMMARY ==="))

            error_count   = counts.get("ERROR", 0)
            warning_count = counts.get("WARNING", 0)
            info_count    = counts.get("INFO", 0)

            if error_count:
                self.stdout.write(self.style.ERROR(f"  ERRORS:   {error_count}"))
            else:
                self.stdout.write(self.style.SUCCESS("  ERRORS:   0"))

            if warning_count:
                self.stdout.write(self.style.WARNING(f"  WARNINGS: {warning_count}"))
            else:
                self.stdout.write(self.style.SUCCESS("  WARNINGS: 0"))

            if verbose:
                self.stdout.write(self.style.SUCCESS(f"  INFO:     {info_count}"))

            total_issues = error_count + warning_count
            if total_issues == 0:
                self.stdout.write(self.style.SUCCESS("\n  All checks passed. Database looks consistent."))
            else:
                self.stdout.write(self.style.WARNING(
                    f"\n  {total_issues} issue(s) found. Review output above for details."
                ))
            self.stdout.write("")
