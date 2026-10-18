"""scripts/ci_config.py builds the scheduled run's config from repository secrets."""

import subprocess
import sys

import yaml

from tests.conftest import ROOT
from tracker.config import load_config

SCRIPT = ROOT / "scripts" / "ci_config.py"
DEMO = ROOT / "demo" / "books-sandbox.yaml"


def build(tmp_path, env):
    out = tmp_path / "ci-config.yaml"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(DEMO), str(out)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return out, result.stdout


def test_email_turned_on_from_secret(tmp_path):
    out, stdout = build(tmp_path, {"DIGEST_EMAIL_TO": "a@example.com, b@example.com"})
    cfg = load_config(out)
    assert cfg.alerts.email.enabled is True
    assert cfg.alerts.email.to == ["a@example.com", "b@example.com"]
    assert cfg.sheets.enabled is False
    assert "email: on (2 recipients)" in stdout
    assert "a@example.com" not in stdout  # never echoes secret values into the CI log


def test_without_secrets_everything_stays_off(tmp_path):
    out, stdout = build(tmp_path, {})
    cfg = load_config(out)
    assert cfg.alerts.email.enabled is False
    assert cfg.sheets.enabled is False
    assert "email: off" in stdout and "sheets: off" in stdout


def test_sheets_turned_on_from_secret(tmp_path):
    out, stdout = build(tmp_path, {"SHEETS_SPREADSHEET_ID": "abc123"})
    cfg = load_config(out)
    assert cfg.sheets.enabled is True
    assert cfg.sheets.spreadsheet_id == "abc123"
    assert "sheets: on" in stdout and "abc123" not in stdout


def test_everything_else_is_kept(tmp_path):
    out, _ = build(tmp_path, {"DIGEST_EMAIL_TO": "a@example.com"})
    original = yaml.safe_load(DEMO.read_text())
    built = yaml.safe_load(out.read_text())
    for key in ("database", "fetch", "sites", "products", "logs"):
        assert built[key] == original[key]
    assert len(built["products"]) == 20
