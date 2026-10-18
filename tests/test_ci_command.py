"""`tracker ci-config` builds a scheduled run's config from repository secrets."""

import yaml
from typer.testing import CliRunner

from tests.conftest import ROOT
from tracker.cli import app
from tracker.config import load_config

runner = CliRunner()
DEMO = ROOT / "demo" / "books-sandbox.yaml"


def build(tmp_path, monkeypatch, env: dict[str, str]):
    for key in ("DIGEST_EMAIL_TO", "SHEETS_SPREADSHEET_ID", "GITHUB_ACTIONS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "ci-config.yaml"
    result = runner.invoke(app, ["ci-config", str(DEMO), str(out)])
    assert result.exit_code == 0, result.output
    return out, result.output


def test_email_turned_on_from_secret(tmp_path, monkeypatch):
    out, output = build(tmp_path, monkeypatch, {"DIGEST_EMAIL_TO": "a@example.com, b@example.com"})
    cfg = load_config(out)
    assert cfg.alerts.email.enabled is True
    assert cfg.alerts.email.to == ["a@example.com", "b@example.com"]
    assert cfg.sheets.enabled is False
    assert "email: on (2 recipients), sheets: off" in output
    assert "a@example.com" not in output  # never echoes secret values into the CI log


def test_without_secrets_everything_stays_off(tmp_path, monkeypatch):
    out, output = build(tmp_path, monkeypatch, {})
    cfg = load_config(out)
    assert cfg.alerts.email.enabled is False
    assert cfg.sheets.enabled is False
    assert "email: off, sheets: off" in output


def test_one_recipient_wording(tmp_path, monkeypatch):
    _, output = build(tmp_path, monkeypatch, {"DIGEST_EMAIL_TO": "a@example.com"})
    assert "email: on (1 recipient)" in output


def test_sheets_turned_on_from_secret(tmp_path, monkeypatch):
    out, output = build(tmp_path, monkeypatch, {"SHEETS_SPREADSHEET_ID": "abc123"})
    cfg = load_config(out)
    assert cfg.sheets.enabled is True
    assert cfg.sheets.spreadsheet_id == "abc123"
    assert "sheets: on" in output and "abc123" not in output


def test_everything_else_is_kept(tmp_path, monkeypatch):
    out, _ = build(tmp_path, monkeypatch, {"DIGEST_EMAIL_TO": "a@example.com"})
    original = yaml.safe_load(DEMO.read_text())
    built = yaml.safe_load(out.read_text())
    for key in ("database", "fetch", "sites", "products", "logs"):
        assert built[key] == original[key]
    assert len(built["products"]) == 20


def test_config_without_alerts_or_sheets_sections(tmp_path, monkeypatch):
    source = tmp_path / "minimal.yaml"
    data = yaml.safe_load(DEMO.read_text())
    del data["alerts"], data["sheets"]
    source.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com")
    out = tmp_path / "out.yaml"
    result = runner.invoke(app, ["ci-config", str(source), str(out)])
    assert result.exit_code == 0, result.output
    assert load_config(out).alerts.email.to == ["a@example.com"]


def test_each_recipient_is_masked_on_github_actions(tmp_path, monkeypatch):
    # The secret is "a@example.com, b@example.com", but each address may appear on its own.
    _, output = build(
        tmp_path, monkeypatch,
        {"DIGEST_EMAIL_TO": "a@example.com, b@example.com", "GITHUB_ACTIONS": "true"},
    )  # fmt: skip
    assert "::add-mask::a@example.com" in output
    assert "::add-mask::b@example.com" in output


def test_no_mask_lines_outside_actions(tmp_path, monkeypatch):
    _, output = build(tmp_path, monkeypatch, {"DIGEST_EMAIL_TO": "a@example.com"})
    assert "add-mask" not in output


def test_missing_source_is_a_clear_error(tmp_path):
    result = runner.invoke(app, ["ci-config", str(tmp_path / "nope.yaml"), str(tmp_path / "o")])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_built_config_must_be_valid(tmp_path, monkeypatch):
    source = tmp_path / "bad.yaml"
    source.write_text("sites: []\n")
    out = tmp_path / "out.yaml"
    result = runner.invoke(app, ["ci-config", str(source), str(out)])
    assert result.exit_code == 1
    assert "invalid config" in result.output
    assert not out.exists()
