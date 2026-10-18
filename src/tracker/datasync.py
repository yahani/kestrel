"""Keep the SQLite history on a git branch between scheduled runs (`tracker data`).

restore: only a *missing* branch starts empty history ("restored=new"). If the remote can't
be reached, or the branch has no readable tracker.db, it fails and sets no outputs, so a
workflow can skip saving and the real history can't be overwritten by an empty database.

save: replaces the branch with one commit holding the database, and refuses if the database
has fewer observations than were restored (history is append-only, so that means a bug).

The remote URL may contain a token, so it is never shown.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DB_NAME = "tracker.db"
README = (
    "# History\n\nSQLite history written by the Daily run workflow. Replaced on every run;\n"
    "the database itself keeps every observation.\n"
)
LS_REMOTE_MISSING = 2  # `git ls-remote --exit-code` when no ref matches
_TOKEN = re.compile(r"(x-access-token:)[^@\s'\"]+@")


class DataSyncError(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    message: str
    outputs: dict[str, str] = field(default_factory=dict)  # for $GITHUB_OUTPUT


def redact(text: str, remote: str) -> str:
    """Remove the remote URL and any token from git's output before it's shown."""
    return _TOKEN.sub(r"\1***@", text.replace(remote, "<remote>"))


def git_detail(result: subprocess.CompletedProcess, remote: str) -> str:
    """git's own error message, made safe to show, so a failure on Actions can be diagnosed."""
    message = result.stderr if isinstance(result.stderr, str) else result.stderr.decode()
    lines = [line.strip() for line in redact(message, remote).splitlines() if line.strip()]
    return f" (git: {' / '.join(lines[-3:])})" if lines else ""


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _observations(db: Path) -> int:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise DataSyncError(f"{DB_NAME} is not a readable tracker database ({exc})") from exc


def restore(remote: str, branch: str, db: Path) -> Outcome:
    # The full ref, so refs/heads/feature/data doesn't count as "data".
    found = _git("ls-remote", "--exit-code", remote, f"refs/heads/{branch}")
    if found.returncode == LS_REMOTE_MISSING:
        return Outcome(
            f"no {branch} branch yet: this first run starts with empty history",
            {"restored": "new", "observations": "0"},
        )
    if found.returncode != 0:
        raise DataSyncError(
            f"could not reach the remote to look for the {branch} branch{git_detail(found, remote)}"
        )

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo.git"
        _git("init", "-q", "--bare", str(repo))
        fetched = _git("fetch", "-q", "--depth=1", remote, f"refs/heads/{branch}", cwd=repo)
        if fetched.returncode != 0:
            raise DataSyncError(f"could not fetch the {branch} branch{git_detail(fetched, remote)}")
        shown = subprocess.run(
            ["git", "show", f"FETCH_HEAD:{DB_NAME}"], cwd=repo, capture_output=True
        )
        if shown.returncode != 0:
            raise DataSyncError(f"the {branch} branch has no {DB_NAME}{git_detail(shown, remote)}")
        staged = Path(tmp) / DB_NAME
        staged.write_bytes(shown.stdout)
        observations = _observations(staged)  # refuse anything that isn't our database
        db.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, db)  # only now does the real file change

    return Outcome(
        f"restored {db.stat().st_size} bytes of history ({observations} observations)",
        {"restored": "true", "observations": str(observations)},
    )


def save(remote: str, branch: str, db: Path, min_observations: int) -> Outcome:
    if not db.is_file():
        raise DataSyncError(f"no {db} to save")
    observations = _observations(db)
    if observations < min_observations:
        raise DataSyncError(
            f"refusing to save: {observations} observations is fewer observations than the "
            f"{min_observations} restored; history is append-only, so something went wrong"
        )

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / DB_NAME).write_bytes(db.read_bytes())
        (work / "README.md").write_text(README, encoding="utf-8")
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        steps = [
            ("init", "-q", "-b", branch),
            ("config", "user.name", "github-actions[bot]"),
            ("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"),
            ("add", DB_NAME, "README.md"),
            ("commit", "-q", "-m", f"History after run {run_id} ({observations} observations)"),
            ("push", "-q", "-f", remote, f"HEAD:refs/heads/{branch}"),
        ]
        for step in steps:
            result = _git(*step, cwd=work)
            if result.returncode != 0:
                raise DataSyncError(
                    f"git {step[0]} failed while saving the {branch} branch"
                    f"{git_detail(result, remote)}"
                )

    return Outcome(f"saved {db.stat().st_size} bytes to {branch} ({observations} observations)")
