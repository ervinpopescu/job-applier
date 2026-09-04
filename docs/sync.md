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
