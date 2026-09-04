from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_applier.sync import (  # type: ignore[import-not-found]
    export_bundle,
    import_bundle,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Job Applier Portable Sync & Migration Tool (Export & Import to another machine)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Export command
    export_parser = subparsers.add_parser(
        "export",
        help="Export all applications, database mappings, and profile to a portable .zip",
    )
    export_parser.add_argument(
        "--output",
        "-o",
        type=str,
        help="Destination .zip path (default: output/exports/job_applier_backup_*.zip)",
    )
    export_parser.add_argument(
        "--no-applied",
        action="store_true",
        help="Exclude applied applications from the export",
    )

    # Import command
    import_parser = subparsers.add_parser(
        "import",
        help="Import a backup bundle from another machine into local environment",
    )
    import_parser.add_argument(
        "--input",
        "-i",
        type=str,
        required=True,
        help="Path to the backup .zip file to import",
    )
    import_parser.add_argument(
        "--overwrite-profile",
        action="store_true",
        help="Overwrite local candidate profile and master resume",
    )

    args = parser.parse_args()

    if args.command == "export":
        out_path = Path(args.output) if args.output else None
        bundle = export_bundle(
            output_path=out_path,
            include_applied=not args.no_applied,
        )
        print(f"\n📦 Export complete! Ready to transfer: {bundle.resolve()}\n")

    elif args.command == "import":
        in_path = Path(args.input)
        if not in_path.exists():
            print(f"Error: Input file does not exist: {in_path}")
            sys.exit(1)
        res = import_bundle(
            zip_path=in_path,
            overwrite_profile=args.overwrite_profile,
        )
        print(
            f"\n✅ Import complete! Merged {res['merged_db_records']} applications into local SQLite database.\n"
        )


if __name__ == "__main__":
    main()
