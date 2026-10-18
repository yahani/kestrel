"""Build the scheduled run's config from a committed one plus repository secrets.

    python scripts/ci_config.py demo/books-sandbox.yaml demo/ci-config.yaml

DIGEST_EMAIL_TO (comma-separated) turns email on; SHEETS_SPREADSHEET_ID turns Sheets on.
The output goes next to the input, so relative paths (database, logs) still resolve the same.
Secret values are never printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml


def main(source: str, target: str) -> None:
    data = yaml.safe_load(Path(source).read_text(encoding="utf-8"))

    recipients = [a.strip() for a in os.environ.get("DIGEST_EMAIL_TO", "").split(",") if a.strip()]
    data.setdefault("alerts", {})["email"] = {"enabled": bool(recipients), "to": recipients}

    sheet = os.environ.get("SHEETS_SPREADSHEET_ID", "").strip()
    sheets = data.setdefault("sheets", {})
    sheets["enabled"] = bool(sheet)
    sheets["spreadsheet_id"] = sheet

    Path(target).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    n = len(recipients)
    email = f"on ({n} recipient{'' if n == 1 else 's'})" if recipients else "off"
    print(f"email: {email}, sheets: {'on' if sheet else 'off'}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
