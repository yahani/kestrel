"""Run summary and site health (F12)."""

from __future__ import annotations

from dataclasses import dataclass

from tracker.changes import Change
from tracker.pipeline import RunResult
from tracker.store import Store

# A site is "failing" in a run when every one of its products failed. After this many
# failing runs in a row its selectors have probably broken (or the site changed or blocks us).
SITE_FAILURE_RUNS = 2

# What to check depends on why the site failed.
ADVICE_SELECTORS = "selectors may have changed; check a page with `tracker test-selectors <url>`"
ADVICE_UNREACHABLE = "the site is unreachable, blocking us or disallowing us in robots.txt"
ADVICE_MIXED = "pages fail to load or parse; check the site, then `tracker test-selectors <url>`"


@dataclass(frozen=True)
class SiteHealth:
    site: str
    failed_runs: int  # consecutive failing runs, ending with this one
    statuses: tuple[str, ...]  # statuses seen in this run

    @property
    def newly_failing(self) -> bool:
        """True only on the run that reaches the threshold, so the alert isn't repeated daily."""
        return self.failed_runs == SITE_FAILURE_RUNS

    def describe(self) -> str:
        if set(self.statuses) == {"parse_error"}:
            advice = ADVICE_SELECTORS
        elif "parse_error" not in self.statuses:
            advice = ADVICE_UNREACHABLE
        else:
            advice = ADVICE_MIXED
        statuses = ", ".join(self.statuses)
        return f"{self.site} failed {self.failed_runs} runs in a row ({statuses}): {advice}"


@dataclass(frozen=True)
class RunReport:
    run_id: str
    checked: int
    succeeded: int
    failed: int
    seconds: float
    changes: tuple[Change, ...]
    failing_sites: tuple[SiteHealth, ...]  # failing for SITE_FAILURE_RUNS or more runs

    @property
    def new_site_alerts(self) -> tuple[SiteHealth, ...]:
        return tuple(s for s in self.failing_sites if s.newly_failing)

    def summary(self) -> str:
        n = len(self.changes)
        return (
            f"{self.checked} products checked: {self.succeeded} ok, {self.failed} failed, "
            f"{n} change{'' if n == 1 else 's'}, {self.seconds:.1f}s"
        )


def _failing_streak(store: Store, site: str) -> int:
    streak = 0
    for run in store.site_runs(site):
        if run.total == 0 or run.ok > 0:
            break
        streak += 1
    return streak


def build_report(result: RunResult, store: Store) -> RunReport:
    sites = list(dict.fromkeys(product.site for product, _ in result.observations))
    failing: list[SiteHealth] = []
    for site in sites:
        streak = _failing_streak(store, site)
        if streak >= SITE_FAILURE_RUNS:
            statuses = sorted({o.status for p, o in result.observations if p.site == site})
            failing.append(SiteHealth(site, streak, tuple(statuses)))
    return RunReport(
        run_id=result.run_id,
        checked=len(result.observations),
        succeeded=result.succeeded,
        failed=result.failed,
        seconds=result.seconds,
        changes=tuple(result.changes),
        failing_sites=tuple(failing),
    )
