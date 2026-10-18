import pytest
from typer.testing import CliRunner

from tests.conftest import EXAMPLE_CONFIG
from tracker.cli import app

runner = CliRunner()


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "check-config", "test-selectors", "history"):
        assert command in result.output


def test_no_args_shows_help():
    result = runner.invoke(app, [])
    assert "check-config" in result.output


def test_check_config_valid():
    result = runner.invoke(app, ["check-config", "--config", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
    assert "sites:    1 (books-sandbox)" in result.output
    assert "products: 1" in result.output
    assert "email on (1 recipient), slack off, min change 1%" in result.output
    assert "sheets:   off" in result.output


def test_check_config_invalid(example_data, write_config):
    example_data["products"][0]["site"] = "rival-x"
    result = runner.invoke(app, ["check-config", "-c", str(write_config(example_data))])
    assert result.exit_code == 1
    assert "unknown site 'rival-x'" in result.output
    assert "Traceback" not in result.output


def test_check_config_missing_file(tmp_path):
    result = runner.invoke(app, ["check-config", "-c", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "config file not found" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--dry-run"],
        ["test-selectors", "https://books.toscrape.com/x"],
        ["history", "BOOK-001"],
    ],
)
def test_unbuilt_commands_exit_nonzero(args):
    result = runner.invoke(app, [*args, "--config", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 1
    assert "Not implemented yet" in result.output


def test_unbuilt_commands_still_validate_config(tmp_path):
    result = runner.invoke(app, ["run", "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "config file not found" in result.output
