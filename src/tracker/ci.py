"""Build a scheduled run's config from a committed one plus secrets (`tracker ci-config`).

DIGEST_EMAIL_TO (comma-separated) turns email on; SHEETS_SPREADSHEET_ID turns Sheets on.
Everything else in the config is kept. Secret values are never part of the summary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CiConfig:
    data: dict
    recipients: list[str]
    sheets: bool

    def summary(self) -> str:
        n = len(self.recipients)
        email = f"on ({n} recipient{'' if n == 1 else 's'})" if n else "off"
        return f"email: {email}, sheets: {'on' if self.sheets else 'off'}"


def build(source: Path, env: Mapping[str, str]) -> CiConfig:
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}

    recipients = [a.strip() for a in env.get("DIGEST_EMAIL_TO", "").split(",") if a.strip()]
    data.setdefault("alerts", {})["email"] = {"enabled": bool(recipients), "to": recipients}

    sheet = env.get("SHEETS_SPREADSHEET_ID", "").strip()
    sheets = data.setdefault("sheets", {})
    sheets["enabled"] = bool(sheet)
    sheets["spreadsheet_id"] = sheet
    return CiConfig(data, recipients, bool(sheet))


def dump(config: CiConfig) -> str:
    return yaml.safe_dump(config.data, sort_keys=False)
