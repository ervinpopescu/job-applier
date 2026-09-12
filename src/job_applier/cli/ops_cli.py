"""
Operations and Disaster Recovery Command Line Tool.

Subcommands:
  backup         Creates an atomic, consistent online SQLite and artifact snapshot (with age encryption).
  restore        Restores from a backup archive with strict schema downgrade protection.
  retention      Enforces the 7-daily / 4-weekly backup retention policy.
  disk-guard     Inspects server storage headroom and checks for disk pressure.
  cleanup-debug  Sweeps diagnostic DOM dumps, traces, and screenshots older than 7 days.
  emergency-stop Immediately pauses all workers and revokes active leases.
  unstop         Clears the emergency stop flag (automation remains paused until resumed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_applier.ops.backup import (
    apply_retention_policy,
    create_backup,
    restore_backup,
)
from job_applier.ops.disk_guard import (
    check_disk_pressure,
    cleanup_expired_debug_artifacts,
    enforce_disk_pressure_guard,
)
from job_applier.ops.emergency_stop import (
    clear_emergency_stop,
    emergency_stop,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="job-applier ops",
        description="JobApplier Operational and Disaster Recovery Management",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # backup
    p_backup = subparsers.add_parser("backup", help="Create consistent system backup")
    p_backup.add_argument(
        "--output-dir", type=Path, default=None, help="Directory to store backup"
    )
    p_backup.add_argument(
        "--recipient", default=None, help="Age recipient public key (e.g. age1...)"
    )
    p_backup.add_argument(
        "--no-profile", action="store_true", help="Exclude browser profile"
    )
    p_backup.add_argument(
        "--allow-running-profile",
        action="store_true",
        help="Allow backup even if profile lock is held",
    )
    p_backup.add_argument(
        "--no-quiesce",
        action="store_true",
        help="Skip pausing automation queue during backup",
    )
    p_backup.add_argument(
        "--apply-retention",
        action="store_true",
        help="Enforce retention policy after backup",
    )
    p_backup.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help="Allow producing an unencrypted plaintext backup archive when no recipient is configured",
    )

    # restore
    p_restore = subparsers.add_parser(
        "restore", help="Restore system from backup archive"
    )
    p_restore.add_argument(
        "archive", type=Path, help="Path to backup archive (.zip or .zip.age)"
    )
    p_restore.add_argument(
        "--identity",
        default=None,
        help="Age private identity string (AGE-SECRET-KEY-1...)",
    )
    p_restore.add_argument(
        "--identity-file", type=Path, default=None, help="Path to age identity file"
    )
    p_restore.add_argument(
        "--target-dir", type=Path, default=None, help="Target extraction directory"
    )
    p_restore.add_argument(
        "--no-profile", action="store_true", help="Skip restoring browser profile"
    )
    p_restore.add_argument(
        "--overwrite-profile",
        action="store_true",
        help="Overwrite existing candidate profile/resume",
    )

    # retention
    p_retention = subparsers.add_parser(
        "retention", help="Apply 7 daily / 4 weekly retention policy"
    )
    p_retention.add_argument(
        "--backups-dir", type=Path, default=None, help="Directory containing backups"
    )
    p_retention.add_argument(
        "--daily", type=int, default=7, help="Number of daily backups to retain"
    )
    p_retention.add_argument(
        "--weekly", type=int, default=4, help="Number of weekly backups to retain"
    )
    p_retention.add_argument(
        "--dry-run", action="store_true", help="Report without pruning files"
    )

    # disk-guard
    p_disk = subparsers.add_parser("disk-guard", help="Check storage volume headroom")
    p_disk.add_argument(
        "--enforce",
        action="store_true",
        help="Engage fail-stop pause if pressure detected",
    )

    # cleanup-debug
    p_clean = subparsers.add_parser(
        "cleanup-debug", help="Clean up debug traces and DOM dumps older than TTL"
    )
    p_clean.add_argument(
        "--ttl-days", type=int, default=7, help="Maximum age in days before pruning"
    )

    # emergency-stop
    p_stop = subparsers.add_parser(
        "emergency-stop", help="Immediately halt all automation workers"
    )
    p_stop.add_argument(
        "--reason", default="Operator emergency stop", help="Reason for halting"
    )

    # unstop
    p_unstop = subparsers.add_parser("unstop", help="Clear emergency stop condition")
    p_unstop.add_argument(
        "--reason",
        default="Operator cleared emergency stop",
        help="Reason for clearing",
    )

    args = parser.parse_args()

    try:
        if args.command == "backup":
            path = create_backup(
                output_dir=args.output_dir,
                recipient=args.recipient,
                include_profile=not args.no_profile,
                require_stopped_profile=not args.allow_running_profile,
                quiesce_worker=not args.no_quiesce,
                allow_unencrypted=args.allow_unencrypted,
            )
            print(f"✅ Backup completed: {path}")
            if args.apply_retention:
                res = apply_retention_policy(backups_dir=args.output_dir)
                print(
                    f"🧹 Retention applied: {len(res['retained'])} retained, {len(res['pruned'])} pruned."
                )
            return 0

        elif args.command == "restore":
            res = restore_backup(
                backup_path=args.archive,
                identity=args.identity,
                identity_file=args.identity_file,
                target_dir=args.target_dir,
                restore_profile=not args.no_profile,
                overwrite_profile=args.overwrite_profile,
            )
            print("✅ Restore successfully completed.")
            print(json.dumps(res, indent=2))
            return 0

        elif args.command == "retention":
            res = apply_retention_policy(
                backups_dir=args.backups_dir,
                daily_limit=args.daily,
                weekly_limit=args.weekly,
                dry_run=args.dry_run,
            )
            print(json.dumps(res, indent=2))
            return 0

        elif args.command == "disk-guard":
            if args.enforce:
                ok = enforce_disk_pressure_guard()
                status = check_disk_pressure()
                print(f"Status: {'HEALTHY' if ok else 'DISK_PRESSURE_ENGAGED'}")
                print(json.dumps(status.to_dict(), indent=2))
                return 0 if ok else 2
            else:
                status = check_disk_pressure()
                print(json.dumps(status.to_dict(), indent=2))
                return 0 if not status.is_under_pressure else 2

        elif args.command == "cleanup-debug":
            count = cleanup_expired_debug_artifacts(max_age_days=args.ttl_days)
            print(
                f"🧹 Cleaned up {count} debug artifacts older than {args.ttl_days} days."
            )
            return 0

        elif args.command == "emergency-stop":
            res = emergency_stop(reason=args.reason)
            print(f"🛑 Emergency stop engaged: {res['message']}")
            return 0

        elif args.command == "unstop":
            res = clear_emergency_stop(reason=args.reason)
            print(f"✅ Emergency stop cleared: {res['message']}")
            return 0

    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
