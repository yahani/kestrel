"""Keep the SQLite history on a git branch between GitHub Actions runs.

    python scripts/data_branch.py restore --remote URL --db demo/tracker.db [--branch data]
    python scripts/data_branch.py save --remote URL --db demo/tracker.db --min-observations N

restore: only a *missing* branch starts empty history ("restored=new"). If the remote can't
be reached, or the branch has no readable tracker.db, it exits 1 and sets no outputs, so the
workflow skips saving and the real history can't be overwritten by an empty database.

save: replaces the branch with one commit holding the database, and refuses if the database
has fewer observations than were restored (history is append-only, so that means a bug).

The remote URL may contain a token, so it is never printed.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

DB_NAME = "tracker.db"
README = (
    "# History\n\nSQLite history written by the Daily run workflow. Replaced on every run;\n"
    "the database itself keeps every observation.\n"
)
LS_REMOTE_MISSING = 2  # `git ls-remote --exit-code` when no ref matches


class Failure(Exception):
    pass


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
        raise Failure(f"{DB_NAME} is not a readable tracker database ({exc})") from exc


def _set_outputs(**values: object) -> None:
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(lines)


def restore(remote: str, branch: str, db: Path) -> None:
    found = _git("ls-remote", "--exit-code", "--heads", remote, branch)
    if found.returncode == LS_REMOTE_MISSING:
        print(f"no {branch} branch yet: this first run starts with empty history")
        _set_outputs(restored="new", observations=0)
        return
    if found.returncode != 0:
        raise Failure(f"could not reach the remote to look for the {branch} branch")

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo.git"
        _git("init", "-q", "--bare", str(repo))
        if _git("fetch", "-q", "--depth=1", remote, branch, cwd=repo).returncode != 0:
            raise Failure(f"could not fetch the {branch} branch")
        shown = subprocess.run(
            ["git", "show", f"FETCH_HEAD:{DB_NAME}"], cwd=repo, capture_output=True
        )
        if shown.returncode != 0:
            raise Failure(f"the {branch} branch has no {DB_NAME}")
        staged = Path(tmp) / DB_NAME
        staged.write_bytes(shown.stdout)
        observations = _observations(staged)  # refuse anything that isn't our database
        db.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, db)  # only now does the real file change

    print(f"restored {db.stat().st_size} bytes of history ({observations} observations)")
    _set_outputs(restored="true", observations=observations)


def save(remote: str, branch: str, db: Path, min_observations: int) -> None:
    if not db.is_file():
        raise Failure(f"no {db} to save")
    observations = _observations(db)
    if observations < min_observations:
        raise Failure(
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
            if _git(*step, cwd=work).returncode != 0:
                raise Failure(f"git {step[0]} failed while saving the {branch} branch")

    print(f"saved {db.stat().st_size} bytes to {branch} ({observations} observations)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/data_branch.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("restore", "save"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--remote", required=True)
        cmd.add_argument("--branch", default="data")
        cmd.add_argument("--db", type=Path, required=True)
        if name == "save":
            cmd.add_argument("--min-observations", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "restore":
            restore(args.remote, args.branch, args.db)
        else:
            save(args.remote, args.branch, args.db, args.min_observations)
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
