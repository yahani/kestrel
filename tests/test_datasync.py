"""`tracker data restore|save` keeps the SQLite history on a git branch between Actions runs.

Only a *missing* branch may start empty history. Anything else (remote unreachable, branch
without tracker.db, unreadable database) must fail and set no outputs, or the next save
would force-push an empty database over the real history.
"""

import sqlite3
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from tests.conftest import ROOT
from tracker import datasync
from tracker.cli import app

runner = CliRunner()
WORKFLOW = ROOT / ".github" / "workflows" / "daily.yml"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    output = tmp_path / "github_output"
    output.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    return output


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_db(path, observations: int):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, note TEXT)")
    conn.executemany("INSERT INTO observations (note) VALUES (?)", [("x",)] * observations)
    conn.commit()
    conn.close()


def count(path) -> int:
    return sqlite3.connect(path).execute("SELECT COUNT(*) FROM observations").fetchone()[0]


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(path))
    return path


def push_branch(remote, tmp_path, files: dict[str, bytes], branch="data"):
    work = tmp_path / f"seed-{branch.replace('/', '-')}"
    work.mkdir()
    git("init", "-q", "-b", branch, cwd=work)
    for name, content in files.items():
        (work / name).write_bytes(content)
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("push", "-q", str(remote), f"HEAD:refs/heads/{branch}", cwd=work)


def data(*args, tmp_path):
    result = runner.invoke(app, ["data", *map(str, args)])
    output = (tmp_path / "github_output").read_text()
    outputs = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return result, outputs


# --- restore ------------------------------------------------------------------------------


def test_missing_branch_starts_empty(remote, tmp_path):
    db = tmp_path / "tracker.db"
    result, outputs = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 0, result.output
    assert outputs == {"restored": "new", "observations": "0"}
    assert not db.exists()
    assert "no data branch yet" in result.output


def test_existing_branch_is_restored(remote, tmp_path):
    seed = tmp_path / "seed.db"
    make_db(seed, 40)
    push_branch(remote, tmp_path, {"tracker.db": seed.read_bytes(), "README.md": b"# History\n"})

    db = tmp_path / "out" / "tracker.db"
    result, outputs = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 0, result.output
    assert outputs == {"restored": "true", "observations": "40"}
    assert db.read_bytes() == seed.read_bytes()
    assert "restored" in result.output and "(40 observations)" in result.output


def test_branch_name_option(remote, tmp_path):
    seed = tmp_path / "seed.db"
    make_db(seed, 2)
    push_branch(remote, tmp_path, {"tracker.db": seed.read_bytes()}, branch="history")
    db = tmp_path / "tracker.db"
    _, outputs = data("restore", "--remote", remote, "--branch", "history", "--db", db,
                      tmp_path=tmp_path)  # fmt: skip
    assert outputs["observations"] == "2"


def test_unreachable_remote_fails_and_writes_nothing(tmp_path):
    db = tmp_path / "tracker.db"
    result, outputs = data(
        "restore", "--remote", tmp_path / "no-such-remote.git", "--db", db, tmp_path=tmp_path
    )
    assert result.exit_code == 1
    assert outputs == {}  # so the workflow's save step is skipped
    assert not db.exists()
    assert "could not reach" in result.output


def test_branch_without_database_fails_without_a_zero_byte_file(remote, tmp_path):
    push_branch(remote, tmp_path, {"README.md": b"no db here\n"})
    db = tmp_path / "tracker.db"
    result, outputs = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 1
    assert outputs == {}
    assert not db.exists()
    assert "has no tracker.db" in result.output


def test_unreadable_database_fails(remote, tmp_path):
    push_branch(remote, tmp_path, {"tracker.db": b"this is not sqlite"})
    db = tmp_path / "tracker.db"
    result, outputs = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 1
    assert outputs == {}
    assert not db.exists()
    assert "not a readable tracker database" in result.output


def test_failed_restore_keeps_an_existing_local_file(remote, tmp_path):
    db = tmp_path / "tracker.db"
    make_db(db, 3)
    before = db.read_bytes()
    push_branch(remote, tmp_path, {"README.md": b"no db\n"})
    result, _ = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 1
    assert db.read_bytes() == before


def test_branch_with_same_suffix_is_not_mistaken_for_data(remote, tmp_path):
    # `ls-remote --heads origin data` would also match refs/heads/feature/data.
    push_branch(remote, tmp_path, {"tracker.db": b"x"}, branch="feature/data")
    db = tmp_path / "tracker.db"
    result, outputs = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 0, result.output
    assert outputs == {"restored": "new", "observations": "0"}


def test_without_github_output_still_works(remote, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT")
    result = runner.invoke(app, ["data", "restore", "--remote", str(remote), "--db",
                                 str(tmp_path / "t.db")])  # fmt: skip
    assert result.exit_code == 0, result.output


# --- save ---------------------------------------------------------------------------------


def test_save_then_restore_round_trips_and_keeps_one_commit(remote, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    db = tmp_path / "tracker.db"
    make_db(db, 5)
    for _ in range(2):
        result, _ = data("save", "--remote", remote, "--db", db, "--min-observations", 0,
                         tmp_path=tmp_path)  # fmt: skip
        assert result.exit_code == 0, result.output
        assert "(5 observations)" in result.output

    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "data"],
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert log.stdout.splitlines() == ["History after run 12345 (5 observations)"]

    out = tmp_path / "restored.db"
    _, outputs = data("restore", "--remote", remote, "--db", out, tmp_path=tmp_path)
    assert outputs["observations"] == "5"
    assert out.read_bytes() == db.read_bytes()


def test_save_refuses_fewer_observations_than_restored(remote, tmp_path):
    seed = tmp_path / "seed.db"
    make_db(seed, 40)
    push_branch(remote, tmp_path, {"tracker.db": seed.read_bytes()})
    smaller = tmp_path / "tracker.db"
    make_db(smaller, 0)

    result, _ = data("save", "--remote", remote, "--db", smaller, "--min-observations", 40,
                     tmp_path=tmp_path)  # fmt: skip
    assert result.exit_code == 1
    assert "fewer observations" in result.output

    out = tmp_path / "check.db"
    data("restore", "--remote", remote, "--db", out, tmp_path=tmp_path)
    assert count(out) == 40  # history untouched


def test_save_without_a_database_fails(remote, tmp_path):
    result, _ = data("save", "--remote", remote, "--db", tmp_path / "missing.db",
                     "--min-observations", 0, tmp_path=tmp_path)  # fmt: skip
    assert result.exit_code == 1
    assert "to save" in result.output


def test_save_to_unreachable_remote_shows_git_error_without_the_token(tmp_path):
    db = tmp_path / "tracker.db"
    make_db(db, 1)
    secret = tmp_path / "x-access-token:ghs_SECRET123@host" / "repo.git"
    result, _ = data("save", "--remote", secret, "--db", db, "--min-observations", 0,
                     tmp_path=tmp_path)  # fmt: skip
    assert result.exit_code == 1
    assert "git push failed" in result.output
    assert "git: " in result.output  # git's own message, so failures can be diagnosed
    assert "ghs_SECRET123" not in result.output


def test_remote_url_is_never_printed(tmp_path):
    secret_url = tmp_path / "x-access-token-SECRET.git"
    result, _ = data("restore", "--remote", secret_url, "--db", tmp_path / "t.db",
                     tmp_path=tmp_path)  # fmt: skip
    assert "SECRET" not in result.output


def test_redact():
    text = "fatal: unable to access 'https://x-access-token:ghs_abc@github.com/o/r.git/': 403"
    assert datasync.redact(text, remote="other") == (
        "fatal: unable to access 'https://x-access-token:***@github.com/o/r.git/': 403"
    )
    assert datasync.redact("fatal: '/tmp/r.git' does not exist", remote="/tmp/r.git") == (
        "fatal: '<remote>' does not exist"
    )


def test_git_detail_handles_bytes_and_empty_output():
    def result(stderr):
        return subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=stderr)

    assert datasync.git_detail(result(b"fatal: bad\n"), remote="r") == " (git: fatal: bad)"
    assert datasync.git_detail(result(b""), remote="r") == ""


def test_data_needs_a_subcommand():
    result = runner.invoke(app, ["data"])
    assert result.exit_code != 0


# --- the demo workflow uses it safely --------------------------------------------------------


def test_workflow_saves_only_after_a_successful_restore():
    steps = {s.get("name"): s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["run"]["steps"]}
    restore = steps["Restore history from the data branch"]
    save = steps["Save history to the data branch"]
    assert restore["id"] == "restore"
    assert "tracker data restore" in restore["run"]
    assert "steps.restore.outputs.restored != ''" in save["if"]
    assert "tracker data save" in save["run"]
    assert "steps.restore.outputs.observations" in save["run"]
    assert "scripts/" not in WORKFLOW.read_text()


def test_fetch_failure_after_the_branch_was_found(remote, tmp_path, monkeypatch):
    seed = tmp_path / "seed.db"
    make_db(seed, 1)
    push_branch(remote, tmp_path, {"tracker.db": seed.read_bytes()})
    real_git = datasync._git

    def flaky(*args, cwd=None):
        if args[0] == "fetch":  # e.g. the network drops between ls-remote and fetch
            return subprocess.CompletedProcess(["git"], 128, "", "fatal: early EOF\n")
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(datasync, "_git", flaky)
    db = tmp_path / "tracker.db"
    result, outputs = data("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.exit_code == 1
    assert "could not fetch the data branch (git: fatal: early EOF)" in result.output
    assert outputs == {}
    assert not db.exists()
