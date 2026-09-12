"""Regression tests for the production Angular artifact and bundled Tailwind CSS."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
BROWSER_DIST = FRONTEND_ROOT / "dist" / "frontend" / "browser"


class _StylesheetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if attributes.get("rel") == "stylesheet" and href:
            self.stylesheets.append(href)


def _build_frontend() -> None:
    env = os.environ.copy()
    node_version_bin = (
        Path.home()
        / ".local"
        / "share"
        / "nvm"
        / "versions"
        / "node"
        / "v22.22.1"
        / "bin"
    )
    if node_version_bin.is_dir():
        env["PATH"] = f"{node_version_bin}{os.pathsep}{env.get('PATH', '')}"

    npm = shutil.which("npm", path=env["PATH"])
    assert npm is not None, "npm is required to validate the production frontend build"
    result = subprocess.run(
        [npm, "run", "build"],
        cwd=FRONTEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module", autouse=True)
def production_frontend() -> None:
    _build_frontend()


def test_production_index_references_existing_bundled_css() -> None:
    """The production HTML must load a real local stylesheet without CSP-blocked inline loaders."""
    index_path = BROWSER_DIST / "index.html"
    assert index_path.is_file()

    parser = _StylesheetParser()
    parser.feed(index_path.read_text(encoding="utf-8"))
    assert parser.stylesheets

    for href in parser.stylesheets:
        assert not href.startswith(("http:", "https:"))
        assert "onload=" not in index_path.read_text(encoding="utf-8")
        assert (BROWSER_DIST / href).is_file(), href


def test_production_css_contains_representative_tailwind_utilities() -> None:
    """The generated stylesheet must contain utilities used by the dashboard layout."""
    index_path = BROWSER_DIST / "index.html"
    parser = _StylesheetParser()
    parser.feed(index_path.read_text(encoding="utf-8"))
    css = "\n".join(
        (BROWSER_DIST / href).read_text(encoding="utf-8") for href in parser.stylesheets
    )

    expected_selectors = (
        ".bg-slate-950",
        ".text-slate-100",
        ".flex",
        ".grid",
        ".rounded-2xl",
        ".p-4",
        ".md\\:grid-cols-2",
    )
    missing = [selector for selector in expected_selectors if selector not in css]
    assert not missing, f"Missing generated Tailwind utilities: {missing}"


def test_build_output_manifest_is_valid() -> None:
    """The Angular build must emit a valid route manifest alongside browser assets."""
    manifest_path = FRONTEND_ROOT / "dist" / "frontend" / "prerendered-routes.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    assert isinstance(manifest.get("routes"), dict)
