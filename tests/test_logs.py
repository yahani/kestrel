import json
import logging
from datetime import UTC, datetime

import pytest

from tracker import logs


@pytest.fixture(autouse=True)
def _restore_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


NOW = datetime(2026, 10, 18, 6, 0, 5, tzinfo=UTC)


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_writes_one_json_object_per_line(tmp_path):
    path = logs.setup(tmp_path / "logs", now=NOW)
    logging.getLogger("tracker.fetch").warning("fetch %s: %s", "http://x", "blocked")
    logging.getLogger("tracker.cli").info(
        "run_finished", extra={"fields": {"run_id": "r1", "ok": 4}}
    )
    logs.close()

    assert path == tmp_path / "logs" / "tracker-2026-10-18.jsonl"
    first, second = read_lines(path)
    assert first["level"] == "WARNING"
    assert first["logger"] == "tracker.fetch"
    assert first["msg"] == "fetch http://x: blocked"
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None
    assert second["msg"] == "run_finished"
    assert (second["run_id"], second["ok"]) == ("r1", 4)


def test_exceptions_are_recorded(tmp_path):
    path = logs.setup(tmp_path, now=NOW)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("tracker.pipeline").exception("unexpected error on %s", "http://x")
    logs.close()
    [line] = read_lines(path)
    assert line["level"] == "ERROR"
    assert "ValueError: boom" in line["exc"]


def test_debug_is_not_written(tmp_path):
    path = logs.setup(tmp_path, now=NOW)
    logging.getLogger("tracker").debug("noise")
    logging.getLogger("tracker").info("kept")
    logs.close()
    assert [line["msg"] for line in read_lines(path)] == ["kept"]


def test_appends_across_runs_on_the_same_day(tmp_path):
    path = logs.setup(tmp_path, now=NOW)
    logging.getLogger("tracker").info("first run")
    logs.close()
    logs.setup(tmp_path, now=NOW)
    logging.getLogger("tracker").info("second run")
    logs.close()
    assert [line["msg"] for line in read_lines(path)] == ["first run", "second run"]


def test_setup_twice_does_not_duplicate_lines(tmp_path):
    logs.setup(tmp_path, now=NOW)
    path = logs.setup(tmp_path, now=NOW)
    logging.getLogger("tracker").info("once")
    logs.close()
    assert len(read_lines(path)) == 1


def test_old_log_files_are_deleted(tmp_path):
    for day in ("2026-09-17", "2026-09-18", "2026-09-19", "2026-10-17"):
        (tmp_path / f"tracker-{day}.jsonl").write_text("{}\n")
    (tmp_path / "notes.txt").write_text("not a log")
    (tmp_path / "tracker-garbage.jsonl").write_text("{}\n")

    logs.setup(tmp_path, now=NOW, keep_days=30)
    logs.close()

    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == [
        "notes.txt",  # not ours: left alone
        "tracker-2026-09-18.jsonl",  # exactly 30 days old: kept
        "tracker-2026-09-19.jsonl",
        "tracker-2026-10-17.jsonl",
        "tracker-2026-10-18.jsonl",  # today's, just opened
        "tracker-garbage.jsonl",  # name doesn't parse as a date: left alone
    ]


def test_close_without_setup_is_harmless():
    logs.close()
