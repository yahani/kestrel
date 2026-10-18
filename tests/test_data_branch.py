"""scripts/data_branch.py keeps the SQLite history on a git branch between Actions runs.

Review must-fix 1: only a *missing* branch may start empty history. Anything else
(remote unreachable, branch without tracker.db, unreadable database) must fail and save
nothing, or the next save would force-push an empty database over the real history.
"""

import sqlite3
import subprocess
import sys

import pytest
import yaml

from tests.conftest import ROOT

SCRIPT = ROOT / "scripts" / "data_branch.py"
WORKFLOW = ROOT / ".github" / "workflows" / "daily.yml"
GIT_ENV = {
    "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}  # fmt: skip


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True)


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
    work = tmp_path / "seed"
    work.mkdir()
    git("init", "-q", "-b", branch, cwd=work)
    for name, content in files.items():
        (work / name).write_bytes(content)
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "seed", cwd=work)
    git("push", "-q", str(remote), f"HEAD:refs/heads/{branch}", cwd=work)


def script(*args, tmp_path):
    output = tmp_path / "github_output"
    output.touch()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        env={**GIT_ENV, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return result, outputs


# --- restore ------------------------------------------------------------------------------


def test_missing_branch_starts_empty(remote, tmp_path):
    db = tmp_path / "tracker.db"
    result, outputs = script("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert outputs == {"restored": "new", "observations": "0"}
    assert not db.exists()
    assert "no data branch yet" in result.stdout


def test_existing_branch_is_restored(remote, tmp_path):
    seed = tmp_path / "seed.db"
    make_db(seed, 40)
    push_branch(remote, tmp_path, {"tracker.db": seed.read_bytes(), "README.md": b"# History\n"})

    db = tmp_path / "out" / "tracker.db"
    result, outputs = script("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert outputs == {"restored": "true", "observations": "40"}
    assert db.read_bytes() == seed.read_bytes()


def test_unreachable_remote_fails_and_writes_nothing(tmp_path):
    db = tmp_path / "tracker.db"
    result, outputs = script(
        "restore", "--remote", tmp_path / "no-such-remote.git", "--db", db, tmp_path=tmp_path
    )
    assert result.returncode != 0
    assert outputs == {}  # so the workflow's save step is skipped
    assert not db.exists()
    assert "could not reach" in result.stderr


def test_branch_without_database_fails_without_a_zero_byte_file(remote, tmp_path):
    push_branch(remote, tmp_path, {"README.md": b"no db here\n"})
    db = tmp_path / "tracker.db"
    result, outputs = script("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.returncode != 0
    assert outputs == {}
    assert not db.exists()


def test_unreadable_database_fails(remote, tmp_path):
    push_branch(remote, tmp_path, {"tracker.db": b"this is not sqlite"})
    db = tmp_path / "tracker.db"
    result, outputs = script("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.returncode != 0
    assert outputs == {}
    assert not db.exists()


def test_failed_restore_keeps_an_existing_local_file(remote, tmp_path):
    db = tmp_path / "tracker.db"
    make_db(db, 3)
    before = db.read_bytes()
    push_branch(remote, tmp_path, {"README.md": b"no db\n"})
    result, _ = script("restore", "--remote", remote, "--db", db, tmp_path=tmp_path)
    assert result.returncode != 0
    assert db.read_bytes() == before


# --- save ---------------------------------------------------------------------------------


def test_save_then_restore_round_trips_and_keeps_one_commit(remote, tmp_path):
    db = tmp_path / "tracker.db"
    make_db(db, 5)
    for _ in range(2):
        result, _ = script("save", "--remote", remote, "--db", db, "--min-observations", 0,
                           tmp_path=tmp_path)  # fmt: skip
        assert result.returncode == 0, result.stderr

    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "data"],
                         capture_output=True, text=True, env=GIT_ENV, check=True)  # fmt: skip
    assert len(log.stdout.splitlines()) == 1  # replaced each time, so the repo doesn't grow

    out = tmp_path / "restored.db"
    result, outputs = script("restore", "--remote", remote, "--db", out, tmp_path=tmp_path)
    assert outputs["observations"] == "5"
    assert out.read_bytes() == db.read_bytes()


def test_save_refuses_a_database_with_fewer_observations_than_restored(remote, tmp_path):
    seed = tmp_path / "seed.db"
    make_db(seed, 40)
    push_branch(remote, tmp_path, {"tracker.db": seed.read_bytes()})
    smaller = tmp_path / "tracker.db"
    make_db(smaller, 0)

    result, _ = script("save", "--remote", remote, "--db", smaller, "--min-observations", 40,
                       tmp_path=tmp_path)  # fmt: skip
    assert result.returncode != 0
    assert "fewer observations" in result.stderr

    out = tmp_path / "check.db"
    script("restore", "--remote", remote, "--db", out, tmp_path=tmp_path)
    assert count(out) == 40  # history untouched


def test_save_without_a_database_fails(remote, tmp_path):
    result, _ = script("save", "--remote", remote, "--db", tmp_path / "missing.db",
                       "--min-observations", 0, tmp_path=tmp_path)  # fmt: skip
    assert result.returncode != 0


def test_save_to_unreachable_remote_fails(tmp_path):
    db = tmp_path / "tracker.db"
    make_db(db, 1)
    result, _ = script("save", "--remote", tmp_path / "nope.git", "--db", db,
                       "--min-observations", 0, tmp_path=tmp_path)  # fmt: skip
    assert result.returncode != 0


def test_remote_url_is_never_printed(tmp_path):
    secret_url = tmp_path / "x-access-token-SECRET.git"
    result, _ = script("restore", "--remote", secret_url, "--db", tmp_path / "t.db",
                       tmp_path=tmp_path)  # fmt: skip
    assert "SECRET" not in result.stdout + result.stderr


def test_usage_without_a_command():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2


# --- the workflow uses it safely -------------------------------------------------------------


def test_workflow_saves_only_after_a_successful_restore():
    steps = {s.get("name"): s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["run"]["steps"]}
    restore = steps["Restore history from the data branch"]
    save = steps["Save history to the data branch"]
    assert restore["id"] == "restore"
    assert "scripts/data_branch.py restore" in restore["run"]
    assert "steps.restore.outputs.restored != ''" in save["if"]
    assert "scripts/data_branch.py save" in save["run"]
    assert "steps.restore.outputs.observations" in save["run"]
