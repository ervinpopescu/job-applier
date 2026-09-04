from unittest.mock import patch

from job_applier.cli.apply_assistant import (  # type: ignore[import-not-found]
    copy_to_clipboard,
    parse_app_folder_info,
)


def test_parse_app_folder_info(tmp_path):
    app_folder = tmp_path / "Adobe_Senior_Software_Engineer_24"
    app_folder.mkdir(parents=True, exist_ok=True)

    # Add mock files
    with open(app_folder / "APPLY_HERE.txt", "w", encoding="utf-8") as f:
        f.write("https://adobe.com/job/123\n")

    with open(app_folder / "cover_letter.txt", "w", encoding="utf-8") as f:
        f.write("Dear Adobe team, excited to apply.")

    with open(
        app_folder / "CV_Adobe_Senior_Software_Engineer.pdf", "w", encoding="utf-8"
    ) as f:
        f.write("dummy pdf content")

    info = parse_app_folder_info(app_folder)

    assert info["company"] == "Adobe"
    assert "Senior Software Engineer" in info["title"]
    assert info["job_url"] == "https://adobe.com/job/123"
    assert "CV_Adobe" in info["cv_name"]
    assert info["has_cover_letter"] == "Yes"
    assert "Dear Adobe team" in info["cover_letter_text"]


def test_copy_to_clipboard():
    # Test that calling copy_to_clipboard doesn't crash or clobber real user clipboard
    with patch("pyperclip.copy") as _:
        result = copy_to_clipboard("test text")
        assert isinstance(result, bool)
