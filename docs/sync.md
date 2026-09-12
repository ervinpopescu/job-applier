# Database & Portable Sync

`job-applier` uses an embedded relational SQLite database with WAL concurrency mode (`data/job_applier.db`). Private profile and resume JSON files are ignored by Git but included with application artifacts and mappings in portable `.zip` backups.

---

## 1. Portable Bundle Structure

When exported, the bundle contains:

```text
job_applier_backup_YYYYMMDD_HHMMSS.zip
├── manifest.json                # Export metadata, machine details, and record counts
├── data/
│   ├── job_applier.db           # SQLite database with all application history & mappings
│   ├── candidate_profile.json   # Candidate profile and custom screening answers
│   └── master_resume.json       # Master source-of-truth resume JSON
└── output/
    ├── applications/            # All tailored CV PDFs, cover letters, and bookmarklets
    └── applied/                 # Completed applications & verification proof screenshots
```

---

## 2. Web Dashboard Export & Import

In the top navbar of the web dashboard (`http://127.0.0.1:8000`):

- Click **`Export`** to download a portable `.zip` archive through your browser.
- On your other machine, click **`Import`** and select the `.zip` file. The server extracts the artifacts and merges the SQLite records atomically.

---

## 3. CLI Export & Import

```bash
# Export all data to a backup archive:
python src/job_applier/cli/sync_cli.py export --output my_backup.zip

# Import and merge on another machine:
python src/job_applier/cli/sync_cli.py import --input my_backup.zip
```

### Automatic Legacy Migration

When running `db.py`, the system automatically detects legacy `data/applications_tracker.csv` files and pending disk folders in `output/applications`, importing them directly into SQLite.

---

## 4. Operational Snapshots & Age Encryption (`ops_cli.py`)

For server-side disaster recovery and backup automation, the operational reliability suite provides enhanced snapshot capabilities:

- **Online SQLite Snapshot**: Captures live WAL pages via `Connection.backup` without locking concurrent readers or writers.
- **Quiesced Queue Snapshotting**: Temporarily pauses worker execution while packaging artifacts to guarantee cross-file consistency.
- **Profile Lock Probing**: Tests exclusive lock acquisition on `.browser_profile` before archiving, failing closed if another process is mutating browser state.
- **Lock & Ephemeral Sanitization**: Strips runtime lockfiles (`SingletonLock`, `.profile_ownership.lock`) and ephemeral IPC sockets from archives.
- **Age Encryption**: Encrypts backups using `pyrage` with an off-server recipient key.
- **Schema Downgrade Refusal**: Refuses restoration if the archive schema version exceeds the local codebase version.

```bash
# Quiesced, age-encrypted online backup:
just ops-backup --recipient "age1..."

# Restore from backup with downgrade verification:
just ops-restore backup.zip --identity-file /path/to/key.txt
```
