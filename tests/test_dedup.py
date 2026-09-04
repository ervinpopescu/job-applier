from __future__ import annotations

from job_applier.dedup import (
    get_role_signature,
    is_duplicate_application,
    normalize_job_url,
    prune_duplicate_applications,
)


def test_normalize_job_url() -> None:
    raw = "https://careers.tether.io/o/devops-engineer-100-remote-17?source=Indeed&utm_campaign=winter2026/"
    norm = normalize_job_url(raw)
    assert norm == "https://careers.tether.io/o/devops-engineer-100-remote-17"

    greenhouse = "https://job-boards.greenhouse.io/gitlab/jobs/12345/?gh_src=custom"
    assert (
        normalize_job_url(greenhouse)
        == "https://job-boards.greenhouse.io/gitlab/jobs/12345"
    )

    assert normalize_job_url("") == ""
    assert normalize_job_url("invalid_url") == ""


def test_get_role_signature() -> None:
    sig1 = get_role_signature("Gitlab", "Staff Backend Engineer (EMEA)")
    sig2 = get_role_signature("Gitlab", "Staff_Backend_Engineer_EMEA")
    assert sig1 == sig2
    assert "gitlab" in sig1


def test_is_duplicate_application() -> None:
    existing_urls = {"https://example.com/jobs/1": "Company_Role_0"}
    existing_roles = {"company__software_engineer": "Company_Role_0"}

    # 1. Matches canonical URL
    is_dup, reason = is_duplicate_application(
        job_url="https://example.com/jobs/1?source=Indeed",
        company="OtherCo",
        title="OtherRole",
        existing_urls=existing_urls,
        existing_roles=existing_roles,
    )
    assert is_dup is True
    assert "Canonical URL already processed" in reason

    # 2. Matches role + company
    is_dup, reason = is_duplicate_application(
        job_url="https://newsite.com/jobs/99",
        company="Company",
        title="Software Engineer",
        existing_urls=existing_urls,
        existing_roles=existing_roles,
    )
    assert is_dup is True
    assert "Identical role" in reason

    # 3. New unique role
    is_dup, _ = is_duplicate_application(
        job_url="https://newsite.com/jobs/100",
        company="NewCompany",
        title="Cloud Architect",
        existing_urls=existing_urls,
        existing_roles=existing_roles,
    )
    assert is_dup is False


def test_prune_duplicate_applications(tmp_path) -> None:
    apps_dir = tmp_path / "output" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)

    folder1 = apps_dir / "Company_Role_1"
    folder2 = apps_dir / "Company_Role_2"
    folder3 = apps_dir / "UniqueCompany_UniqueRole_3"

    for f in [folder1, folder2, folder3]:
        f.mkdir()

    # folder1 and folder2 have the exact same canonical job URL
    (folder1 / "APPLY_HERE.txt").write_text(
        "https://example.com/job/10?source=Indeed", encoding="utf-8"
    )
    (folder2 / "APPLY_HERE.txt").write_text(
        "https://example.com/job/10?utm_source=Google", encoding="utf-8"
    )
    (folder3 / "APPLY_HERE.txt").write_text(
        "https://example.com/job/99", encoding="utf-8"
    )

    result = prune_duplicate_applications(project_root=tmp_path)
    assert result["pruned_count"] == 1
    assert result["remaining_count"] == 2
    assert folder1.exists() is True
    assert folder2.exists() is False
    assert folder3.exists() is True
