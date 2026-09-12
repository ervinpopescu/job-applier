from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from job_applier.automation.adapters.models import ConfirmationEvidence
from job_applier.automation.queue import (
    claim_next_job,
    enqueue_job,
    validate_application_artifacts,
)
from job_applier.automation.safety_guard import (
    MissingArtifactError,
    SubmissionSafetyGuard,
)
from job_applier.db import get_connection, init_db, upsert_application
from job_applier.dedup import (
    get_role_signature,
    is_duplicate_application,
)


@pytest.fixture
def pipeline_env(tmp_path: Path):
    db_file = tmp_path / "pipeline_test.db"
    init_db(custom_path=db_file)

    app_base = tmp_path / "output" / "applications"
    applied_base = tmp_path / "output" / "applied"
    app_base.mkdir(parents=True, exist_ok=True)
    applied_base.mkdir(parents=True, exist_ok=True)

    return {
        "db": db_file,
        "app_base": app_base,
        "applied_base": applied_base,
        "root": tmp_path,
    }


def test_conservative_deduplication():
    """Conservative dedup: canonical URL normalization and (company, title) signature."""
    existing_urls = {
        "https://boards.greenhouse.io/corp/jobs/123": "app_1",
    }
    existing_roles = {
        get_role_signature("Corp Inc", "Senior Backend Engineer"): "app_1",
    }

    # Same URL with query tracking params matches canonical URL
    is_dup, reason = is_duplicate_application(
        job_url="https://boards.greenhouse.io/corp/jobs/123?gh_src=linkedin&utm_medium=job_board",
        company="Other Corp",
        title="Frontend",
        existing_urls=existing_urls,
        existing_roles=existing_roles,
    )
    assert is_dup is True
    assert "url" in reason.lower()

    # Same company and title matches signature
    is_dup, reason = is_duplicate_application(
        job_url="https://newcorp.example/jobs/999",
        company="Corp Inc",
        title="Senior Backend Engineer",
        existing_urls=existing_urls,
        existing_roles=existing_roles,
    )
    assert is_dup is True
    assert "identical role" in reason.lower()

    # Distinct role and URL is not duplicate
    is_dup, _ = is_duplicate_application(
        job_url="https://distinct.example/jobs/456",
        company="Distinct Corp",
        title="Product Manager",
        existing_urls=existing_urls,
        existing_roles=existing_roles,
    )
    assert is_dup is False


def test_missing_artifacts_blocks_enqueue_without_deleting_files(pipeline_env):
    """
    Missing artifacts block enqueueing without hiding applications.
    Never delete unmatched files automatically.
    """
    db = pipeline_env["db"]
    app_id = "Corp_Engineer_0"
    app_dir = pipeline_env["app_base"] / app_id
    app_dir.mkdir(parents=True, exist_ok=True)

    # Put a partial file (e.g. cover letter or note) but NO CV PDF
    note_file = app_dir / "APPLY_HERE.txt"
    note_file.write_text("https://boards.greenhouse.io/corp/123", encoding="utf-8")

    upsert_application(
        app_id=app_id,
        company="Corp",
        title="Engineer",
        job_url="https://boards.greenhouse.io/corp/123",
        platform="Greenhouse",
        status="pending",
        folder_name=app_id,
        custom_path=db,
    )

    # Validation confirms missing artifact
    has_art, reason = validate_application_artifacts(app_id, custom_path=db)
    assert has_art is False

    # Enqueueing with verify_artifacts=True blocks enqueueing
    with pytest.raises(MissingArtifactError):
        enqueue_job(
            app_id=app_id, adapter="greenhouse", verify_artifacts=True, custom_path=db
        )

    # CRITICAL: Note file and folder must still exist (not deleted!)
    assert app_dir.exists()
    assert note_file.exists()

    # DB application row preserved with status missing_artifacts
    conn = get_connection(db)
    row = conn.execute(
        "SELECT status, has_artifacts FROM applications WHERE id = ?;", (app_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "missing_artifacts"
    assert row["has_artifacts"] == 0


def test_durable_enqueue_and_archive_upon_confirmed_application(pipeline_env):
    """
    Tests complete lifecycle:
    artifact validation -> enqueue -> apply -> confirm -> archive to output/applied/.
    """
    db = pipeline_env["db"]
    app_id = "Datadog_SRE_0"
    app_dir = pipeline_env["app_base"] / app_id
    app_dir.mkdir(parents=True, exist_ok=True)

    cv_pdf = app_dir / "CV_Datadog_SRE.pdf"
    cv_pdf.write_bytes(b"%PDF-1.4 Datadog CV file content of sufficient size")

    upsert_application(
        app_id=app_id,
        company="Datadog",
        title="SRE",
        job_url="https://boards.greenhouse.io/datadog/jobs/555",
        platform="Greenhouse",
        status="pending",
        folder_name=app_id,
        cv_filename=cv_pdf.name,
        custom_path=db,
    )

    # 1. Enqueue job into DB queue
    job = enqueue_job(
        app_id=app_id,
        adapter="greenhouse",
        priority=10,
        verify_artifacts=True,
        custom_path=db,
    )
    assert job.id.startswith(f"job_{app_id}_")
    assert job.adapter == "greenhouse"

    # 2. Worker claims job atomically
    claimed = claim_next_job(
        worker_id="server-worker", lease_seconds=60, custom_path=db
    )
    assert claimed is not None
    assert claimed.id == job.id

    # 3. Simulate confirmed submission reconciliation
    evidence = ConfirmationEvidence(
        platform="greenhouse",
        adapter_version="1.0.0",
        confirmed=True,
        confirmation_id="GH-DATADOG-1234",
        confirmation_text="Application submitted successfully to Datadog",
        proof_element="#application_confirmation",
        is_ambiguous=False,
    )

    status, msg = SubmissionSafetyGuard.reconcile_submission_outcome(
        job_id=job.id,
        app_id=app_id,
        attempt_id="att_1",
        worker_id="server-worker",
        generation=1,
        adapter_name="greenhouse",
        company="Datadog",
        evidence=evidence,
        custom_db_path=db,
    )
    assert status == "applied"

    # 4. Archival: Move to output/applied/ upon confirmed application
    target_dir = pipeline_env["applied_base"] / app_dir.name
    shutil.move(str(app_dir), str(target_dir))

    assert not app_dir.exists()
    assert target_dir.exists()
    assert (target_dir / "CV_Datadog_SRE.pdf").exists()

    # DB application row updated to applied
    conn = get_connection(db)
    row = conn.execute(
        "SELECT status FROM applications WHERE id = ?;", (app_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "applied"
