import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compose_web_port_matches_secondary_nginx_alias_documentation() -> None:
    """Keep the host-port contract used by the Hetzner Nginx alias explicit."""
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    deployment = (PROJECT_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    assert "JOB_APPLIER_PORT:-8001}:8000" in compose
    assert "GATEWAY_PORT:-8089}:80" in compose
    assert "${DATA_DIR:-/home/ervin/prjs/job-applier/data}:/app/data" in compose
    assert "${OUTPUT_DIR:-/home/ervin/prjs/job-applier/output}:/app/output" in compose
    assert "job-applier-data:/app/data" not in compose
    assert "job-applier-output:/app/output" not in compose
    assert "aslan.archnet.lol/job-applier/" in deployment
    assert "127.0.0.1:8001" in deployment
    assert "127.0.0.1:8089" in deployment
    assert "GATEWAY_PORT=8089" in deployment
    assert "separate development port `8000`" in deployment


def test_justfile_service_management_recipes() -> None:
    """Verify that service management recipes exist in justfile and parse cleanly."""
    justfile_text = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")

    expected_recipes = [
        "services-up",
        "services-down",
        "services-restart",
        "services-reload",
        "services-logs",
        "services-status",
        "services-ps",
        "services-pull",
        "services-build",
        "services-exec",
        "completions",
        "systemd-status",
        "systemd-restart",
        "systemd-logs",
        "systemd-start",
        "systemd-stop",
    ]
    for recipe in expected_recipes:
        assert recipe in justfile_text, f"Recipe '{recipe}' missing from justfile"

    if shutil.which("just"):
        res = subprocess.run(
            ["just", "--list"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        for recipe in expected_recipes:
            assert recipe in res.stdout, f"Recipe '{recipe}' not listed by just --list"

        dry_run = subprocess.run(
            ["just", "-n", "services-reload", "web"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "up -d --no-deps web" in (dry_run.stderr + dry_run.stdout)

        # Verify arg patterns reject unknown services
        invalid_run = subprocess.run(
            ["just", "-n", "services-restart", "invalid_service"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid_run.returncode != 0
        assert "does not match pattern" in (invalid_run.stderr + invalid_run.stdout)

        # Verify usage documentation displays allowed service patterns
        usage_run = subprocess.run(
            ["just", "--usage", "services-restart"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "web|gateway|runtime|cloudflared|ntfy" in usage_run.stdout
