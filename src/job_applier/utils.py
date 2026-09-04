import re
from pathlib import Path


def sanitize_name(name: str) -> str:
    """
    Sanitizes a string for use as a directory or file name.
    Replaces non-alphanumeric characters (except hyphens and underscores) with underscores.
    Collapses multiple underscores and removes leading/trailing underscores.
    """
    # Replace non-alphanumeric characters with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    # Collapse multiple underscores
    sanitized = re.sub(r"_+", "_", sanitized)
    # Remove leading/trailing underscores or hyphens
    sanitized = sanitized.strip("_").strip("-")
    return sanitized


def get_project_root() -> Path:
    """Returns the absolute path to the project root directory."""
    return Path(__file__).resolve().parents[2]


def parse_app_folder_info(app_folder: Path) -> dict[str, str]:
    """Extracts company, title, URL, CV, and cover letter for an application folder."""
    parts = app_folder.name.rsplit("_", 1)
    folder_prefix = parts[0] if len(parts) > 1 else app_folder.name
    name_parts = folder_prefix.split("_", 1)
    company = name_parts[0].replace("-", " ")
    title = name_parts[1].replace("_", " ") if len(name_parts) > 1 else "Position"

    apply_url_file = app_folder / "APPLY_HERE.txt"
    job_url = "URL not found"
    if apply_url_file.exists():
        try:
            with open(apply_url_file, encoding="utf-8") as f:
                job_url = f.read().strip()
        except Exception:
            pass

    pdf_cv = next(app_folder.glob("CV_*.pdf"), None)
    cv_path_str = str(pdf_cv.resolve()) if pdf_cv else ""

    cover_letter_file = app_folder / "cover_letter.txt"
    cover_letter_preview = ""
    if cover_letter_file.exists():
        try:
            with open(cover_letter_file, encoding="utf-8") as f:
                cover_letter_preview = f.read().strip()
        except Exception:
            pass

    return {
        "company": company,
        "title": title,
        "job_url": job_url,
        "cv_path": cv_path_str,
        "cv_name": pdf_cv.name if pdf_cv else "None",
        "has_cover_letter": "Yes" if cover_letter_preview else "No",
        "cover_letter_text": cover_letter_preview,
        "cover_letter_preview": cover_letter_preview,
    }
