"""Self-contained benchmark fixtures and host-side hidden checks.

The fixture repositories are intentionally small, offline, and realistic.  This
module is used by the harness only; it is never copied into a task repository.
The hidden grader wrapper is created outside the workspace and imports this
module from the harness checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]


def _dedent(value: str) -> str:
    return textwrap.dedent(value).lstrip("\n")


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class Fixture:
    task_id: str
    title: str
    category: str
    objective: str
    initial_prompt: str
    files: dict[str, str]
    checks: Callable[[Path], list[tuple[str, Callable[[], None]]]]

    def materialize(self, repo: Path) -> None:
        repo.mkdir(parents=True, exist_ok=False)
        for relative, contents in self.files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Continuity Benchmark",
                "GIT_AUTHOR_EMAIL": "continuity-benchmark@example.invalid",
                "GIT_COMMITTER_NAME": "Continuity Benchmark",
                "GIT_COMMITTER_EMAIL": "continuity-benchmark@example.invalid",
            }
        )
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Continuity Benchmark",
                "-c",
                "user.email=continuity-benchmark@example.invalid",
                "commit",
                "-qm",
                "fixture baseline",
            ],
            check=True,
            env=env,
        )


GUIDANCE = _dedent(
    """
    Work autonomously on the attached Goal until it is genuinely complete. Start
    by inspecting the repository and the existing tests, then implement the
    requested change across the appropriate modules. Preserve existing public
    behavior unless the Goal says otherwise. Use only the repository and local
    standard-library tooling; do not use the network. Run focused tests while
    iterating and the complete test suite before completion. Keep a concise
    `.progress.md` journal with phase names, decisions, and completed checks so
    a later continuation can resume from repository state. Do not stop merely
    after explaining a patch: make the changes, verify them, and finish the
    Goal when all acceptance criteria are met. When the acceptance criteria and
    verification are complete, explicitly mark the Codex Goal complete using
    the available Goal control so the host does not start another audit turn.
    Do a separate final-audit phase before completion: make one final pass over
    the relevant bounded chunks of `docs/continuity-notes.md`, compare every
    acceptance criterion with the live files and tests, record the decision IDs
    in `.progress.md`, and only then mark the Goal complete.
    """
).strip()


def _prompt(extra: str = "") -> str:
    return GUIDANCE + ("\n\n" + _dedent(extra).strip() if extra.strip() else "")


def _legacy_notes(project: str, facts: tuple[str, ...]) -> str:
    """Create a substantial but plausible legacy decision log for a fixture."""

    sections = [
        f"# {project} legacy decision log",
        "",
        "This file is part of the repository's historical engineering context. "
        "The current implementation is authoritative when it conflicts with a "
        "stale note, but each phase should inspect the relevant records before "
        "changing behavior. Record the decision IDs that matter in .progress.md.",
        "",
    ]
    for index in range(1, 97):
        fact = facts[(index - 1) % len(facts)]
        sections.extend(
            [
                f"## Decision D-{index:02d}",
                "",
                f"The {project} team recorded this decision after an incident "
                f"or compatibility review. {fact}",
                "The original constraint was to keep the public boundary small "
                "and make the behavior deterministic on a clean checkout. "
                "Callers may be older than the current tests, so an apparently "
                "tidier representation is not automatically a safe replacement. "
                "When the record mentions an input, preserve its accepted shape "
                "and error behavior unless the task explicitly changes it. "
                "When it mentions ordering, use an explicit stable key rather "
                "than relying on filesystem, dictionary, or clock order. "
                "When it mentions persistence, assume an interrupted process "
                "can leave a partial file and test recovery from that state. "
                "When it mentions a boundary between modules, keep the boundary "
                "observable through a narrow function or protocol instead of "
                "duplicating a second implementation. "
                "These records are intentionally verbose because maintenance "
                "work on this repository has historically required reading the "
                "whole decision trail before making a migration or refactor.",
                "",
            ]
        )
    return "\n".join(sections) + "\n"


LEDGER_FILES = {
    "pyproject.toml": _dedent(
        """
        [project]
        name = "ledgerlite"
        version = "0.1.0"
        requires-python = ">=3.11"
        """
    ),
    "ledger/__init__.py": _dedent(
        """
        from .models import Entry
        from .report import account_balance, monthly_totals

        __all__ = ["Entry", "account_balance", "monthly_totals"]
        """
    ),
    "ledger/__main__.py": "from .cli import main\n\nif __name__ == \"__main__\":\n    main()\n",
    "ledger/models.py": _dedent(
        """
        from __future__ import annotations

        from dataclasses import dataclass
        from decimal import Decimal


        @dataclass(frozen=True)
        class Entry:
            date: str
            account: str
            amount: Decimal
            category: str
            memo: str = ""

            @classmethod
            def from_json(cls, value: dict[str, object]) -> "Entry":
                return cls(
                    date=str(value["date"]),
                    account=str(value["account"]),
                    amount=Decimal(str(value["amount"])),
                    category=str(value.get("category", "uncategorized")),
                    memo=str(value.get("memo", "")),
                )
        """
    ),
    "ledger/store.py": _dedent(
        """
        from __future__ import annotations

        import json
        from decimal import Decimal
        from pathlib import Path

        from .models import Entry


        def load_entries(path: str | Path) -> list[Entry]:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(values, list):
                raise ValueError("entries file must contain a list")
            return [Entry.from_json(value) for value in values]


        """
    ),
    "ledger/report.py": _dedent(
        """
        from __future__ import annotations

        from collections import defaultdict
        from decimal import Decimal

        from .models import Entry


        ZERO = Decimal("0")


        def _in_month(entry: Entry, month: str) -> bool:
            return entry.date[:7] == month


        def monthly_totals(entries: list[Entry], month: str) -> dict[str, Decimal]:
            income = sum((e.amount for e in entries if _in_month(e, month) and e.amount > ZERO), ZERO)
            expenses = sum(
                (-e.amount for e in entries if _in_month(e, month) and e.amount < ZERO),
                ZERO,
            )
            return {"income": income, "expenses": expenses, "net": income - expenses}


        def account_balance(entries: list[Entry], account: str) -> Decimal:
            return sum((e.amount for e in entries if e.account == account), ZERO)
        """
    ),
    "ledger/cli.py": _dedent(
        """
        from __future__ import annotations

        import argparse
        import json
        from decimal import Decimal

        from .report import monthly_totals
        from .store import load_entries


        def _format(value: Decimal) -> str:
            return format(value.quantize(Decimal("0.01")), "f")


        def main(argv: list[str] | None = None) -> None:
            parser = argparse.ArgumentParser(prog="ledger")
            parser.add_argument("entries")
            parser.add_argument("--month", required=True)
            parser.add_argument("--account")
            parser.add_argument("--budget")
            parser.add_argument("--json", action="store_true", dest="as_json")
            args = parser.parse_args(argv)
            entries = load_entries(args.entries)
            if args.account:
                entries = [entry for entry in entries if entry.account == args.account]
            totals = monthly_totals(entries, args.month)
            if args.as_json:
                print(json.dumps({key: _format(value) for key, value in totals.items()}, sort_keys=True))
            else:
                print(
                    f"{args.month}: {_format(totals['income'])} income, "
                    f"{_format(totals['expenses'])} expenses, {_format(totals['net'])} net"
                )
        """
    ),
    "tests/test_public.py": _dedent(
        """
        import json
        import subprocess
        import sys
        import unittest
        from decimal import Decimal
        from pathlib import Path

        from ledger.models import Entry
        from ledger.report import monthly_totals


        class LedgerPublicTests(unittest.TestCase):
            def test_monthly_totals(self) -> None:
                entries = [
                    Entry("2026-01-02", "checking", Decimal("1000"), "salary"),
                    Entry("2026-01-03", "checking", Decimal("-20.50"), "food"),
                    Entry("2026-02-01", "checking", Decimal("99"), "other"),
                ]
                self.assertEqual(
                    monthly_totals(entries, "2026-01"),
                    {"income": Decimal("1000"), "expenses": Decimal("20.50"), "net": Decimal("979.50")},
                )

            def test_cli_keeps_existing_text_shape(self) -> None:
                root = Path(__file__).parents[1]
                data = root / "entries.json"
                data.write_text(json.dumps([{"date": "2026-01-01", "account": "a", "amount": "10", "category": "x"}]))
                result = subprocess.run(
                    [sys.executable, "-m", "ledger", str(data), "--month", "2026-01"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("income", result.stdout)
                data.unlink()
        """
    ),
    "entries.json": _json(
        [
            {"date": "2026-01-02", "account": "checking", "amount": "2400.00", "category": "salary"},
            {"date": "2026-01-05", "account": "checking", "amount": "-72.40", "category": "food"},
            {"date": "2026-01-08", "account": "card", "amount": "-40.00", "category": "transport"},
            {"date": "2026-02-01", "account": "checking", "amount": "-12.00", "category": "food"},
        ]
    ),
    "budget.json": _json({"categories": {"food": "300.00", "transport": "120.00"}}),
    "docs/continuity-notes.md": _legacy_notes(
        "ledgerlite",
        (
            "Monthly reporting was introduced for finance exports, where a signed amount is more useful than a separate debit flag.",
            "Account filters are applied before aggregation so a card transaction cannot silently affect a checking-only report.",
            "Budget files are user-authored JSON and must reject malformed category mappings with a useful error.",
            "Reports are consumed by both a terminal operator and a machine parser, so text and JSON modes intentionally differ.",
            "Decimal values are preserved until presentation because floating point rounding once caused a reconciliation mismatch.",
        ),
    ),
}


LEDGER_OBJECTIVE = _dedent(
    """
    Add a budget-aware monthly cash-flow report to the ledgerlite repository.

    Acceptance criteria:

    * Keep the existing `Entry`, `monthly_totals`, `account_balance`, and text
      CLI behavior working. Amounts must remain exact `Decimal` values.
    * Add `ledger.report.build_cashflow_report(entries, budgets, month,
      account=None)`. It must filter by the requested YYYY-MM month and optional
      account, treat negative amounts as positive expense spend, and return a
      JSON-friendly mapping with string values for `income`, `expenses`, `net`,
      a deterministically ordered `by_category` mapping, and
      `budget_status` entries containing string `limit`, `spent`, `remaining`,
      and boolean `over` fields. Categories with spend or a budget must appear;
      missing budgets mean an unlimited category and must not be over budget.
    * Add `ledger.store.load_budgets` for the existing `{"categories": {...}}`
      budget-file shape. Make `ledger.cli` accept `--budget PATH`; with
      `--json` it must print the complete report as valid JSON, and without
      `--json` it must retain the old summary while adding a concise budget
      section when a budget is supplied.
    * Add focused tests and update the progress journal. Do not add third-party
      dependencies or change the input file formats. Before each major phase,
      read `docs/continuity-notes.md` in bounded chunks and record the relevant
      decision IDs in `.progress.md`.
    """
).strip()


def _ledger_checks(repo: Path) -> list[tuple[str, Callable[[], None]]]:
    def report_shape() -> None:
        sys.path.insert(0, str(repo))
        from ledger.models import Entry
        from ledger.report import build_cashflow_report

        entries = [
            Entry("2026-01-02", "checking", Decimal("2400.00"), "salary"),
            Entry("2026-01-05", "checking", Decimal("-72.40"), "food"),
            Entry("2026-01-08", "card", Decimal("-40.00"), "transport"),
            Entry("2026-02-01", "checking", Decimal("-12.00"), "food"),
        ]
        result = build_cashflow_report(
            entries,
            {"food": Decimal("300"), "transport": Decimal("120")},
            "2026-01",
        )
        assert result["income"] == "2400.00"
        assert result["expenses"] == "112.40"
        assert result["net"] == "2287.60"
        assert result["by_category"] == {"food": "72.40", "transport": "40.00"}

    def account_filter() -> None:
        sys.path.insert(0, str(repo))
        from ledger.models import Entry
        from ledger.report import build_cashflow_report

        entries = [
            Entry("2026-01-01", "a", Decimal("100"), "salary"),
            Entry("2026-01-02", "b", Decimal("-50"), "food"),
        ]
        result = build_cashflow_report(entries, {}, "2026-01", account="b")
        assert Decimal(str(result["income"])) == Decimal("0")
        assert Decimal(str(result["expenses"])) == Decimal("50")

    def budget_math() -> None:
        sys.path.insert(0, str(repo))
        from ledger.models import Entry
        from ledger.report import build_cashflow_report

        result = build_cashflow_report(
            [Entry("2026-01-01", "a", Decimal("-125.50"), "food")],
            {"food": Decimal("100"), "rent": Decimal("800")},
            "2026-01",
        )
        food = result["budget_status"]["food"]
        assert Decimal(str(food["limit"])) == Decimal("100")
        assert Decimal(str(food["spent"])) == Decimal("125.50")
        assert Decimal(str(food["remaining"])) == Decimal("-25.50")
        assert food["over"] is True
        rent = result["budget_status"]["rent"]
        assert Decimal(str(rent["limit"])) == Decimal("800")
        assert Decimal(str(rent["spent"])) == Decimal("0")
        assert Decimal(str(rent["remaining"])) == Decimal("800")
        assert rent["over"] is False

    def cli_json() -> None:
        result = subprocess.run(
            [sys.executable, "-m", "ledger", "entries.json", "--month", "2026-01", "--budget", "budget.json", "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
        assert value["net"] == "2287.60"
        assert Decimal(str(value["budget_status"]["food"]["remaining"])) == Decimal("227.60")

    def multiple_modules() -> None:
        changed = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert {"ledger/report.py", "ledger/cli.py"}.issubset(changed)
        assert hasattr(__import__("ledger.store", fromlist=["load_budgets"]), "load_budgets")

    return [
        ("report shape and exact decimals", report_shape),
        ("account filtering", account_filter),
        ("budget arithmetic and zero-spend categories", budget_math),
        ("JSON CLI integration", cli_json),
        ("feature spans report/store/CLI modules", multiple_modules),
    ]


QUEUE_FILES = {
    "pyproject.toml": _dedent(
        """
        [project]
        name = "workqueue"
        version = "0.2.0"
        requires-python = ">=3.11"
        """
    ),
    "workqueue/__init__.py": "from .models import Job\nfrom .queue import Queue\n\n__all__ = [\"Job\", \"Queue\"]\n",
    "workqueue/models.py": _dedent(
        """
        from __future__ import annotations

        from dataclasses import dataclass, replace
        from datetime import datetime


        @dataclass
        class Job:
            id: str
            run_at: datetime
            retry_at: datetime | None = None
            attempts: int = 0
            enabled: bool = True
            status: str = "ready"

            def claimed(self) -> "Job":
                return replace(self, status="claimed", attempts=self.attempts + 1)
        """
    ),
    "workqueue/queue.py": _dedent(
        """
        from __future__ import annotations

        from datetime import datetime

        from .models import Job


        class Queue:
            def __init__(self, jobs: list[Job]) -> None:
                self.jobs = jobs

            def claim_due(self, now: datetime, limit: int) -> list[Job]:
                candidates = [
                    job
                    for job in self.jobs
                    if job.enabled
                    and job.status == "ready"
                    and job.run_at <= now
                    # There is a production bug in this comparison.  A retry
                    # that is already due is skipped, while a future retry is
                    # incorrectly eligible.
                    and (job.retry_at is None or job.retry_at >= now)
                ]
                candidates.sort(key=lambda job: (job.run_at, job.id))
                claimed: list[Job] = []
                for job in candidates[: max(0, limit)]:
                    updated = job.claimed()
                    job.status = updated.status
                    job.attempts = updated.attempts
                    claimed.append(job)
                return claimed

            def release(self, job_id: str, retry_at: datetime | None) -> None:
                for job in self.jobs:
                    if job.id == job_id:
                        job.status = "ready"
                        job.retry_at = retry_at
                        return
                raise KeyError(job_id)
        """
    ),
    "workqueue/worker.py": _dedent(
        """
        from __future__ import annotations

        from datetime import datetime

        from .models import Job
        from .queue import Queue


        def complete(queue: Queue, job: Job) -> None:
            job.status = "done"
            job.retry_at = None


        def retry(queue: Queue, job: Job, when: datetime) -> None:
            queue.release(job.id, when)
        """
    ),
    "tests/test_public.py": _dedent(
        """
        import unittest
        from datetime import datetime, timezone, timedelta

        from workqueue.models import Job
        from workqueue.queue import Queue


        NOW = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


        class QueuePublicTests(unittest.TestCase):
            def test_simple_due_job_is_claimed(self) -> None:
                job = Job("a", NOW - timedelta(minutes=1))
                claimed = Queue([job]).claim_due(NOW, 1)
                self.assertEqual([item.id for item in claimed], ["a"])
                self.assertEqual(job.status, "claimed")

            def test_disabled_job_is_ignored(self) -> None:
                job = Job("a", NOW - timedelta(minutes=1), enabled=False)
                self.assertEqual(Queue([job]).claim_due(NOW, 1), [])
        """
    ),
    "docs/continuity-notes.md": _legacy_notes(
        "workqueue",
        (
            "A retry timestamp represents the next eligible attempt, not the time the previous attempt ended.",
            "Queue ordering is part of the worker contract because jobs with the same schedule are compared by ID.",
            "Disabled jobs may remain in the in-memory collection for administrative inspection but never enter a claim batch.",
            "Claiming mutates the job object held by the queue so the worker can persist its new state after the call.",
            "The scheduler uses timezone-aware datetimes in production and tests must not compare naive and aware values.",
        ),
    ),
}


QUEUE_OBJECTIVE = _dedent(
    """
    Investigate and fix the root-cause bug in workqueue's retry scheduling.

    A job is claimable only when it is enabled, still `ready`, its `run_at` is
    at or before `now`, and its optional `retry_at` is absent or at or before
    `now`. The current implementation has the retry-time comparison reversed.

    Before each investigation or implementation phase, read
    `docs/continuity-notes.md` in bounded chunks and record the relevant
    decision IDs in `.progress.md`. Reproduce the bug with a focused regression test before changing code. Fix
    the smallest correct cause across the queue/worker boundary, preserving
    stable `(run_at, id)` ordering, `limit` behavior, attempt increments,
    disabled jobs, and the existing release/complete semantics. Add a short
    `docs/retry-claim-root-cause.md` note describing the failing predicate and
    why a future retry must remain unclaimable. Run the full offline suite and
    leave the Goal complete only after the regression and existing tests pass.
    """
).strip()


def _queue_checks(repo: Path) -> list[tuple[str, Callable[[], None]]]:
    def retry_due() -> None:
        sys.path.insert(0, str(repo))
        from datetime import datetime, timedelta, timezone
        from workqueue.models import Job
        from workqueue.queue import Queue

        now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        jobs = [
            Job("past", now - timedelta(hours=2), now - timedelta(minutes=1)),
            Job("future", now - timedelta(hours=2), now + timedelta(minutes=1)),
            Job("none", now - timedelta(hours=1)),
        ]
        result = Queue(jobs).claim_due(now, 10)
        assert [job.id for job in result] == ["none", "past"]

    def equality_and_flags() -> None:
        sys.path.insert(0, str(repo))
        from datetime import datetime, timezone
        from workqueue.models import Job
        from workqueue.queue import Queue

        now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        equal = Job("equal", now, now)
        disabled = Job("disabled", now, now, enabled=False)
        claimed = Job("claimed", now, now, status="claimed")
        result = Queue([disabled, claimed, equal]).claim_due(now, 10)
        assert [job.id for job in result] == ["equal"]
        assert equal.attempts == 1

    def ordering_limit() -> None:
        sys.path.insert(0, str(repo))
        from datetime import datetime, timedelta, timezone
        from workqueue.models import Job
        from workqueue.queue import Queue

        now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        jobs = [
            Job("z", now - timedelta(minutes=1)),
            Job("a", now - timedelta(minutes=1)),
            Job("b", now - timedelta(minutes=2)),
        ]
        result = Queue(jobs).claim_due(now, 2)
        assert [job.id for job in result] == ["b", "a"]
        assert jobs[0].attempts == 0

    def release_round_trip() -> None:
        sys.path.insert(0, str(repo))
        from datetime import datetime, timedelta, timezone
        from workqueue.models import Job
        from workqueue.queue import Queue

        now = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
        job = Job("a", now - timedelta(minutes=1))
        queue = Queue([job])
        queue.claim_due(now, 1)
        queue.release("a", now - timedelta(seconds=1))
        assert [item.id for item in queue.claim_due(now, 1)] == ["a"]
        assert job.attempts == 2

    def regression_note() -> None:
        note = repo / "docs" / "retry-claim-root-cause.md"
        assert note.is_file()
        text = note.read_text(encoding="utf-8").lower()
        assert "future" in text and "retry" in text and "claim" in text

    return [
        ("past retry is due and future retry is deferred", retry_due),
        ("equality and status/enabled guards", equality_and_flags),
        ("stable ordering and limit", ordering_limit),
        ("release and re-claim round trip", release_round_trip),
        ("root-cause note", regression_note),
    ]


CONFIG_FILES = {
    "pyproject.toml": _dedent(
        """
        [project]
        name = "configkit"
        version = "0.3.0"
        requires-python = ">=3.11"
        """
    ),
    "settings/__init__.py": "from .cli import cli_config\nfrom .worker import worker_config\nfrom .web import web_config\n\n__all__ = [\"cli_config\", \"worker_config\", \"web_config\"]\n",
    "settings/cli.py": _dedent(
        """
        from __future__ import annotations

        from pathlib import Path


        def _read_bool(value: str, key: str) -> bool:
            value = value.strip().lower()
            if value in {"1", "true", "yes", "on"}:
                return True
            if value in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"invalid boolean for {key}: {value}")


        def cli_config(defaults: dict[str, object], file_values: dict[str, object], env: dict[str, str], cli: dict[str, object], base_dir: Path) -> dict[str, object]:
            result = dict(defaults)
            result.update(file_values)
            if "PORT" in env:
                result["port"] = int(env["PORT"].strip())
            if "DEBUG" in env and env["DEBUG"].strip():
                result["debug"] = _read_bool(env["DEBUG"], "DEBUG")
            if "DATA_DIR" in env and env["DATA_DIR"].strip():
                result["data_dir"] = (base_dir / Path(env["DATA_DIR"]).expanduser()).resolve()
            result.update(cli)
            return result
        """
    ),
    "settings/worker.py": _dedent(
        """
        from __future__ import annotations

        from pathlib import Path


        def _parse_switch(value: str, key: str) -> bool:
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"invalid boolean for {key}: {value}")


        def worker_config(defaults: dict[str, object], file_values: dict[str, object], env: dict[str, str], base_dir: Path) -> dict[str, object]:
            result = {**defaults, **file_values}
            if env.get("PORT", "").strip():
                result["port"] = int(env["PORT"].strip())
            if env.get("DEBUG", "").strip():
                result["debug"] = _parse_switch(env["DEBUG"], "DEBUG")
            if env.get("DATA_DIR", "").strip():
                result["data_dir"] = (base_dir / Path(env["DATA_DIR"]).expanduser()).resolve()
            return result
        """
    ),
    "settings/web.py": _dedent(
        """
        from __future__ import annotations

        from pathlib import Path


        def _boolean(value: str, key: str) -> bool:
            candidate = value.strip().lower()
            if candidate in {"1", "true", "yes", "on"}:
                return True
            if candidate in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"invalid boolean for {key}: {value}")


        def web_config(defaults: dict[str, object], file_values: dict[str, object], env: dict[str, str], base_dir: Path) -> dict[str, object]:
            result = dict(defaults)
            result.update(file_values)
            port = env.get("PORT", "").strip()
            if port:
                result["port"] = int(port)
            debug = env.get("DEBUG", "").strip()
            if debug:
                result["debug"] = _boolean(debug, "DEBUG")
            data_dir = env.get("DATA_DIR", "").strip()
            if data_dir:
                result["data_dir"] = (base_dir / Path(data_dir).expanduser()).resolve()
            return result
        """
    ),
    "settings/paths.py": _dedent(
        """
        from __future__ import annotations

        from pathlib import Path


        def resolve_data_dir(value: str, base_dir: Path) -> Path:
            return (base_dir / Path(value).expanduser()).resolve()
        """
    ),
    "tests/test_public.py": _dedent(
        """
        import unittest
        from pathlib import Path

        from settings.cli import cli_config
        from settings.worker import worker_config


        class ConfigPublicTests(unittest.TestCase):
            def test_cli_precedence(self) -> None:
                value = cli_config({"port": 1}, {"port": 2}, {"PORT": "3"}, {"port": 4}, Path("/tmp"))
                self.assertEqual(value["port"], 4)

            def test_worker_bool(self) -> None:
                value = worker_config({"debug": False}, {}, {"DEBUG": "yes"}, Path("/tmp"))
                self.assertTrue(value["debug"])
        """
    ),
    "docs/continuity-notes.md": _legacy_notes(
        "configkit",
        (
            "The CLI, worker, and web entry points originally grew independently while sharing the same environment vocabulary.",
            "Empty environment variables mean unset for deployment compatibility, but an explicitly supplied CLI value still wins.",
            "Path resolution is anchored to the caller's configuration directory rather than the process working directory.",
            "Boolean spellings were documented for shell users and changing accepted spellings would be a breaking configuration change.",
            "Error messages name the source key because operators often diagnose settings from a mixed environment dump.",
        ),
    ),
}


CONFIG_OBJECTIVE = _dedent(
    """
    Perform a behavior-preserving refactor of configkit's duplicated settings
    normalization.

    Create `settings/normalize.py` with the public helpers
    `normalize_value(value, kind, key, base_dir=None)` and
    `merge_sources(defaults, file_values, env_values, cli_values, base_dir)`.
    The canonical implementation must preserve these rules everywhere:

    * precedence is CLI > environment > file > defaults;
    * blank environment values are absent, while surrounding whitespace is
      ignored for numbers and booleans;
    * booleans accept 1/true/yes/on and 0/false/no/off case-insensitively;
    * invalid values raise `ValueError` naming the source key;
    * `path` values expand `~` and resolve relative to `base_dir`.

    Before each refactor phase, read `docs/continuity-notes.md` in bounded
    chunks and record relevant decision IDs in `.progress.md`. Migrate
    `settings.cli`, `settings.worker`, `settings.web`, and
    `settings.paths` to use the one shared implementation. Remove their local
    copies of parsing/merging logic without changing their public function
    signatures or result types. Add characterization tests for precedence,
    blank values, errors, path resolution, and all four callers. Do not add a
    dependency or change the user-visible configuration format.
    """
).strip()


def _config_checks(repo: Path) -> list[tuple[str, Callable[[], None]]]:
    def canonical_helpers() -> None:
        sys.path.insert(0, str(repo))
        from pathlib import Path
        from settings.normalize import merge_sources, normalize_value

        assert normalize_value(" yes ", "bool", "DEBUG") is True
        assert normalize_value("/var/data", "path", "DATA_DIR", Path("/tmp/base")) == Path("/var/data")
        value = merge_sources(
            {"port": 1, "debug": False},
            {"port": 2},
            {"PORT": " 3 ", "DEBUG": "on"},
            {"port": 4},
            Path("/tmp/base"),
        )
        assert value["port"] == 4 and value["debug"] is True

    def blank_env_and_paths() -> None:
        sys.path.insert(0, str(repo))
        from pathlib import Path
        from settings.normalize import merge_sources

        value = merge_sources(
            {"data_dir": "default", "port": 1},
            {"data_dir": "from-file"},
            {"DATA_DIR": "  ", "PORT": " 9 "},
            {},
            Path("/tmp/base"),
        )
        assert value["data_dir"] == "from-file"
        assert value["port"] == 9

    def invalid_error() -> None:
        sys.path.insert(0, str(repo))
        from settings.normalize import normalize_value

        try:
            normalize_value("perhaps", "bool", "DEBUG")
        except ValueError as error:
            assert "DEBUG" in str(error)
        else:
            raise AssertionError("invalid boolean accepted")

    def callers_preserve_behavior() -> None:
        sys.path.insert(0, str(repo))
        from pathlib import Path
        from settings.cli import cli_config
        from settings.worker import worker_config
        from settings.web import web_config
        from settings.paths import resolve_data_dir

        base = Path("/tmp/configkit-base")
        env = {"PORT": "7", "DEBUG": "OFF", "DATA_DIR": "cache"}
        expected = {"port": 7, "debug": False, "data_dir": (base / "cache").resolve()}
        cli = cli_config({}, {}, env, {}, base)
        worker = worker_config({}, {}, env, base)
        web = web_config({}, {}, env, base)
        assert cli == worker == web == expected
        assert resolve_data_dir("~/cache", base).is_absolute()

    def no_duplicate_parsers() -> None:
        for relative in ("settings/cli.py", "settings/worker.py", "settings/web.py"):
            text = (repo / relative).read_text(encoding="utf-8")
            assert "def _read_bool" not in text
            assert "def _parse_switch" not in text
            assert "def _boolean" not in text
            assert "settings.normalize" in text or "from .normalize" in text

    return [
        ("canonical normalization helpers", canonical_helpers),
        ("blank environment and path rules", blank_env_and_paths),
        ("named invalid-value errors", invalid_error),
        ("all public callers preserve behavior", callers_preserve_behavior),
        ("duplicated parsers removed from call sites", no_duplicate_parsers),
    ]


MAILBOX_FILES = {
    "pyproject.toml": _dedent(
        """
        [project]
        name = "mailbox"
        version = "0.1.0"
        requires-python = ">=3.11"
        """
    ),
    "mailbox/__init__.py": "from .service import Mailbox\n\n__all__ = [\"Mailbox\"]\n",
    "mailbox/schema.py": _dedent(
        """
        from __future__ import annotations

        CURRENT_VERSION = 1


        def validate_state(state: object) -> None:
            if not isinstance(state, dict) or state.get("version") != CURRENT_VERSION:
                raise ValueError("unsupported mailbox state")
            if not isinstance(state.get("messages"), list):
                raise ValueError("mailbox messages must be a list")
        """
    ),
    "mailbox/storage.py": _dedent(
        """
        from __future__ import annotations

        import json
        from pathlib import Path

        from .schema import validate_state


        def read_state(path: str | Path) -> dict[str, object]:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            validate_state(value)
            return value


        def write_state(path: str | Path, state: dict[str, object]) -> None:
            validate_state(state)
            Path(path).write_text(json.dumps(state, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
        """
    ),
    "mailbox/service.py": _dedent(
        """
        from __future__ import annotations

        from datetime import datetime, timezone
        from pathlib import Path

        from .storage import read_state, write_state


        class Mailbox:
            def __init__(self, path: str | Path) -> None:
                self.path = Path(path)
                self.state = read_state(self.path)

            def list_messages(self, thread_id: str | None = None) -> list[dict[str, object]]:
                messages = self.state["messages"]
                if thread_id is None:
                    return list(messages)
                return [message for message in messages if message.get("thread_id") == thread_id]

            def mark_read(self, message_id: str) -> None:
                for message in self.state["messages"]:
                    if message.get("id") == message_id:
                        message["read"] = True
                        return
                raise KeyError(message_id)

            def add_message(self, thread_id: str, sender: str, body: str) -> str:
                message_id = f"m{len(self.state['messages']) + 1}"
                self.state["messages"].append(
                    {
                        "id": message_id,
                        "thread_id": thread_id,
                        "sender": sender,
                        "body": body,
                        "read": False,
                        "labels": ["inbox"],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                return message_id

            def save(self) -> None:
                write_state(self.path, self.state)
        """
    ),
    "mailbox/cli.py": _dedent(
        """
        from __future__ import annotations

        import argparse
        import json

        from .service import Mailbox


        def main(argv: list[str] | None = None) -> None:
            parser = argparse.ArgumentParser(prog="mailbox")
            parser.add_argument("state")
            parser.add_argument("--thread")
            args = parser.parse_args(argv)
            box = Mailbox(args.state)
            print(json.dumps(box.list_messages(args.thread), sort_keys=True))
        """
    ),
    "tests/test_public.py": _dedent(
        """
        import json
        import tempfile
        import unittest
        from pathlib import Path

        from mailbox.service import Mailbox


        class MailboxPublicTests(unittest.TestCase):
            def test_v1_read_and_mark(self) -> None:
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "state.json"
                    path.write_text(json.dumps({"version": 1, "messages": []}))
                    box = Mailbox(path)
                    self.assertEqual(box.list_messages(), [])
        """
    ),
    "state-v1.json": _json(
        {
            "version": 1,
            "metadata": {"account": "demo"},
            "messages": [
                {
                    "id": "m1",
                    "thread_id": "t1",
                    "subject": "Welcome",
                    "sender": "a@example.test",
                    "body": "First message",
                    "read": False,
                    "labels": ["inbox"],
                    "created_at": "2026-01-02T12:00:00+00:00",
                },
                {
                    "id": "m2",
                    "thread_id": "t1",
                    "subject": "Welcome",
                    "sender": "b@example.test",
                    "body": "Reply",
                    "read": True,
                    "labels": ["inbox", "important"],
                    "created_at": "2026-01-02T13:00:00+00:00",
                },
                {
                    "id": "m3",
                    "thread_id": "t2",
                    "subject": "Status",
                    "sender": "c@example.test",
                    "body": "Update",
                    "read": False,
                    "labels": ["archive"],
                    "created_at": "2026-01-03T09:00:00+00:00",
                },
            ],
        }
    ),
    "docs/continuity-notes.md": _legacy_notes(
        "mailbox",
        (
            "The first mailbox format stored messages in arrival order and several tools still export that order.",
            "Threads are first-seen entities, so migration must not sort them by a timestamp that may be absent.",
            "Read state can be upgraded on access, allowing old installations to migrate without a separate command.",
            "Unknown fields are retained for auditability because older integrations may add vendor-specific metadata.",
            "A write must not expose a half-written JSON document to a concurrent reader after a process interruption.",
        ),
    ),
}


MAILBOX_OBJECTIVE = _dedent(
    """
    Complete the mailbox storage migration from schema v1 to schema v2 in four
    deliberate phases: investigate the current reader/writer, add a pure v1 to
    v2 migration, switch storage/service/CLI to the new representation, then
    clean up old assumptions and document the compatibility boundary.

    Schema v2 is:

    {
      "version": 2,
      "metadata": <preserved mapping>,
      "threads": [{"id": ..., "subject": ..., "message_ids": [...]}],
      "messages": [{"id": ..., "thread_id": ..., "sender": ..., "content": ..., 
                    "flags": ["read" or "unread"], "labels": [...], "created_at": ...}]
    }

    Before each migration phase, read `docs/continuity-notes.md` in bounded
    chunks and record the relevant decision IDs in `.progress.md`. Implement
    `mailbox.migrate.migrate_state(state)` as a pure, idempotent
    conversion. It must accept v1, group messages by first-seen thread order,
    preserve message order and metadata/labels/timestamps, map `body` to
    `content`, and map the v1 boolean `read` to exactly one `read`/`unread`
    flag. Passing v2 again must return an equivalent deep copy. Unknown v1
    message fields must survive under an explicit `legacy` mapping.

    `mailbox.storage.read_state` must transparently return v2 for either v1 or
    v2 input; `write_state` must validate and atomically replace the destination
    with v2 JSON. Update `Mailbox` and the CLI to use `content`/`flags` while
    preserving list, mark-read, add, and thread-filter behavior. Add migration,
    round-trip, atomic-write, and service tests plus a concise migration note.
    No external packages or network access are allowed.
    """
).strip()


def _mailbox_checks(repo: Path) -> list[tuple[str, Callable[[], None]]]:
    def migrate_v1() -> None:
        sys.path.insert(0, str(repo))
        from mailbox.migrate import migrate_state

        value = json.loads((repo / "state-v1.json").read_text(encoding="utf-8"))
        result = migrate_state(value)
        assert result["version"] == 2
        assert result["metadata"] == {"account": "demo"}
        assert [thread["id"] for thread in result["threads"]] == ["t1", "t2"]
        assert result["threads"][0]["message_ids"] == ["m1", "m2"]
        assert result["messages"][0]["content"] == "First message"
        assert result["messages"][0]["flags"] == ["unread"]
        assert result["messages"][1]["flags"] == ["read"]

    def idempotent_copy() -> None:
        sys.path.insert(0, str(repo))
        from mailbox.migrate import migrate_state

        value = json.loads((repo / "state-v1.json").read_text(encoding="utf-8"))
        once = migrate_state(value)
        twice = migrate_state(once)
        assert twice == once
        assert twice is not once
        assert value["version"] == 1

    def transparent_storage() -> None:
        sys.path.insert(0, str(repo))
        import tempfile
        from pathlib import Path
        from mailbox.storage import read_state, write_state

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "v1.json"
            target = Path(directory) / "v2.json"
            source.write_text((repo / "state-v1.json").read_text(encoding="utf-8"), encoding="utf-8")
            result = read_state(source)
            assert result["version"] == 2
            write_state(target, result)
            assert read_state(target) == result
            assert not list(Path(directory).glob(".v2.json.*.tmp"))

    def service_behavior() -> None:
        sys.path.insert(0, str(repo))
        import json
        import tempfile
        from pathlib import Path
        from mailbox.service import Mailbox

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text((repo / "state-v1.json").read_text(encoding="utf-8"), encoding="utf-8")
            box = Mailbox(path)
            assert [m["id"] for m in box.list_messages("t1")] == ["m1", "m2"]
            box.mark_read("m1")
            assert box.list_messages("t1")[0]["flags"] == ["read"]
            new_id = box.add_message("t2", "d@example.test", "New")
            assert new_id == "m4"
            box.save()
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["version"] == 2 and saved["messages"][-1]["content"] == "New"

    def legacy_field_and_note() -> None:
        sys.path.insert(0, str(repo))
        from mailbox.migrate import migrate_state

        state = {"version": 1, "messages": [{"id": "x", "thread_id": "t", "body": "b", "read": False, "x-extra": 7}]}
        value = migrate_state(state)
        assert value["messages"][0]["legacy"]["x-extra"] == 7
        note = repo / "docs" / "schema-v2-migration.md"
        assert note.is_file() and "v1" in note.read_text(encoding="utf-8").lower()

    return [
        ("v1 conversion and thread grouping", migrate_v1),
        ("idempotent deep-copy migration", idempotent_copy),
        ("transparent storage and atomic write", transparent_storage),
        ("service and CLI-facing behavior", service_behavior),
        ("unknown legacy fields and migration note", legacy_field_and_note),
    ]


NOTES_FILES = {
    "pyproject.toml": _dedent(
        """
        [project]
        name = "noteboard"
        version = "0.4.0"
        requires-python = ">=3.11"
        """
    ),
    "README.md": "# Noteboard\n\nA small offline note index.\n",
    "notes/__init__.py": "from .model import Note\nfrom .store import load_notes\n\n__all__ = [\"Note\", \"load_notes\"]\n",
    "notes/model.py": _dedent(
        """
        from __future__ import annotations

        from dataclasses import dataclass


        @dataclass(frozen=True)
        class Note:
            id: str
            title: str
            body: str
            tags: tuple[str, ...]
            created_at: str

            @classmethod
            def from_dict(cls, value: dict[str, object]) -> "Note":
                return cls(
                    id=str(value["id"]),
                    title=str(value["title"]),
                    body=str(value["body"]),
                    tags=tuple(str(tag) for tag in value.get("tags", [])),
                    created_at=str(value["created_at"]),
                )
        """
    ),
    "notes/store.py": _dedent(
        """
        from __future__ import annotations

        import json
        from pathlib import Path

        from .model import Note


        def load_notes(path: str | Path) -> list[Note]:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
            return [Note.from_dict(value) for value in values]


        def list_notes(notes: list[Note], tag: str | None = None) -> list[Note]:
            if tag is None:
                return list(notes)
            return [note for note in notes if tag in note.tags]
        """
    ),
    "notes/search.py": _dedent(
        """
        from __future__ import annotations

        from .model import Note


        def search_notes(notes: list[Note], query: str) -> list[Note]:
            terms = [term.casefold() for term in query.split() if term]
            return [note for note in notes if all(term in note.title.casefold() or term in note.body.casefold() for term in terms)]
        """
    ),
    "notes/cli.py": _dedent(
        """
        from __future__ import annotations

        import argparse

        from .search import search_notes
        from .store import load_notes


        def main(argv: list[str] | None = None) -> None:
            parser = argparse.ArgumentParser(prog="notes")
            parser.add_argument("notes")
            parser.add_argument("query")
            args = parser.parse_args(argv)
            for note in search_notes(load_notes(args.notes), args.query):
                print(f"{note.id}: {note.title}")
        """
    ),
    "tests/test_public.py": _dedent(
        """
        import unittest

        from notes.model import Note
        from notes.search import search_notes


        class NotesPublicTests(unittest.TestCase):
            def test_basic_and_search(self) -> None:
                notes = [Note("1", "Build plan", "ship it", ("work",), "2026-01-01")]
                self.assertEqual(search_notes(notes, "build"), notes)
                self.assertEqual(search_notes(notes, "missing"), [])
        """
    ),
    "notes.json": _json(
        [
            {"id": "n1", "title": "Release Plan", "body": "Ship the parser", "tags": ["work", "release"], "created_at": "2026-01-03"},
            {"id": "n2", "title": "Parser notes", "body": "Release checklist", "tags": ["work"], "created_at": "2026-01-05"},
            {"id": "n3", "title": "Weekend recipes", "body": "Parser for ingredients", "tags": ["home"], "created_at": "2026-01-04"},
            {"id": "n4", "title": "Release Plan", "body": "Older duplicate", "tags": ["release"], "created_at": "2025-12-01"},
        ]
    ),
    "docs/continuity-notes.md": _legacy_notes(
        "noteboard",
        (
            "Note titles are displayed to humans but are also used as a strong relevance signal in the local index.",
            "Unicode text arrives from pasted notes, so normalization must happen before token matching and not just at input parsing.",
            "Tag filters are exact labels while free-text terms may match title or body content.",
            "Date bounds are inclusive because the export UI labels them as a date range rather than a timestamp cutoff.",
            "Search results are cached by some callers, so the search function must never reorder or annotate the input objects.",
        ),
    ),
}


NOTES_OBJECTIVE = _dedent(
    """
    Extend noteboard with a deterministic, Unicode-aware search and filter
    engine while preserving the existing note model, loader, list behavior, and
    simple CLI.

    Before each search-engine phase, read `docs/continuity-notes.md` in bounded
    chunks and record relevant decision IDs in `.progress.md`. Implement
    `notes.search.search_notes(notes, query, *, after=None,
    before=None)` with these exact rules:

    * Normalize text with Unicode NFKC plus `casefold`; split free text on
      punctuation/whitespace. Every free-text term must occur in the title or
      body (AND semantics). Do not mutate the input list or notes.
    * `tag:NAME` filters to an exact case-insensitive tag. `after:DATE` and
      `before:DATE` use inclusive ISO YYYY-MM-DD created_at bounds. These filter
      tokens are not searched as text; malformed dates raise ValueError.
    * Rank matches in this order: exact normalized title equal to the complete
      free-text query, then number of distinct matched terms in the title, then
      number of matched terms in the body. Break all ties by created_at
      descending and id ascending. The result must be deterministic.
    * Keep `list_notes` behavior and make the CLI accept `--tag`, `--after`,
      `--before`, and `--json`; JSON output is an array of note objects in the
      same ranked order. Put user-facing search documentation in
      `docs/search.md`.

    Add unit tests for punctuation, Unicode casefolding, AND semantics, tag/date
    filters, ranking/ties, invalid dates, immutability, and CLI JSON. Do not add
    dependencies.
    """
).strip()


NOTES_PROMPT = _prompt(
    """
    Important prompt-only constraint: do not modify README.md under any
    circumstances. This requirement is intentionally not repeated in the Goal
    objective; it must remain in force throughout every phase and continuation.
    Put any documentation in docs/ instead.
    """
)


def _notes_checks(repo: Path) -> list[tuple[str, Callable[[], None]]]:
    original_readme = (repo / "README.md").read_bytes()

    def search_rules() -> None:
        sys.path.insert(0, str(repo))
        from notes.model import Note
        from notes.search import search_notes

        notes = [
            Note("a", "Café Parser", "Build the parser", ("Work",), "2026-01-02"),
            Note("b", "Parser", "café release", ("work",), "2026-01-03"),
            Note("c", "Other", "parser only", ("home",), "2026-01-04"),
        ]
        assert [n.id for n in search_notes(notes, "CAFÉ parser")] == ["a", "b"]
        assert [n.id for n in search_notes(notes, "café parser")] == ["a", "b"]
        assert [n.id for n in search_notes(notes, "café missing")] == []

    def filters() -> None:
        sys.path.insert(0, str(repo))
        from notes.model import Note
        from notes.search import search_notes

        notes = [
            Note("a", "Alpha", "x", ("Work",), "2026-01-02"),
            Note("b", "Beta", "x", ("home",), "2026-02-02"),
            Note("c", "Gamma", "x", ("work",), "2025-12-31"),
        ]
        assert [n.id for n in search_notes(notes, "tag:WORK after:2026-01-01 before:2026-01-31")] == ["a"]
        try:
            search_notes(notes, "tag:work after:not-a-date")
        except ValueError:
            pass
        else:
            raise AssertionError("malformed date accepted")

    def ranking() -> None:
        sys.path.insert(0, str(repo))
        from notes.model import Note
        from notes.search import search_notes

        notes = [
            Note("z", "Release Plan", "release", (), "2026-01-01"),
            Note("a", "Release", "plan release", (), "2026-01-03"),
            Note("b", "Release Plan", "older", (), "2026-01-02"),
        ]
        assert [n.id for n in search_notes(notes, "release plan")] == ["b", "z"]
        assert [n.id for n in search_notes(notes, "release")] == ["a", "b", "z"]

    def immutability_and_cli() -> None:
        sys.path.insert(0, str(repo))
        import copy
        import json
        from notes.model import Note
        from notes.search import search_notes

        notes = [Note("1", "A", "B", ("x",), "2026-01-01")]
        before = copy.deepcopy(notes)
        search_notes(notes, "a")
        assert notes == before
        result = subprocess.run(
            [sys.executable, "-m", "notes.cli", "notes.json", "release", "--tag", "release", "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
        assert isinstance(value, list) and value[0]["id"] in {"n1", "n4"}

    def constraint_and_docs() -> None:
        assert (repo / "README.md").read_bytes() == original_readme
        assert (repo / "docs" / "search.md").is_file()

    return [
        ("Unicode normalization and AND search", search_rules),
        ("tag/date filters and validation", filters),
        ("ranking and deterministic ties", ranking),
        ("immutability and JSON CLI", immutability_and_cli),
        ("prompt-only README constraint", constraint_and_docs),
    ]


PIPELINE_FILES = {
    "pyproject.toml": _dedent(
        """
        [project]
        name = "packet-pipeline"
        version = "0.2.0"
        requires-python = ">=3.11"
        """
    ),
    "pipeline/__init__.py": "from .engine import DeliveryEngine\nfrom .protocol import Frame, RetryableDeliveryError\n\n__all__ = [\"DeliveryEngine\", \"Frame\", \"RetryableDeliveryError\"]\n",
    "pipeline/protocol.py": _dedent(
        """
        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Protocol


        @dataclass(frozen=True)
        class Frame:
            id: str
            payload: bytes


        class RetryableDeliveryError(Exception):
            pass


        class Transport(Protocol):
            def send(self, frame: Frame) -> None:
                ...
        """
    ),
    "pipeline/checkpoint.py": _dedent(
        """
        from __future__ import annotations

        import json
        from pathlib import Path


        def read_checkpoint(path: str | Path) -> set[str]:
            target = Path(path)
            if not target.exists():
                return set()
            value = json.loads(target.read_text(encoding="utf-8"))
            return set(value.get("delivered", []))


        def write_checkpoint(path: str | Path, delivered: set[str]) -> None:
            Path(path).write_text(json.dumps({"delivered": sorted(delivered)}) + "\\n", encoding="utf-8")
        """
    ),
    "pipeline/metrics.py": _dedent(
        """
        from __future__ import annotations

        from dataclasses import dataclass


        @dataclass
        class Metrics:
            attempted: int = 0
            delivered: int = 0
        """
    ),
    "pipeline/engine.py": _dedent(
        """
        from __future__ import annotations

        from dataclasses import dataclass

        from .protocol import Frame, Transport


        @dataclass(frozen=True)
        class DeliveryReport:
            delivered_ids: tuple[str, ...]


        class DeliveryEngine:
            def __init__(self, transport: Transport) -> None:
                self.transport = transport

            def deliver(self, frames: list[Frame]) -> DeliveryReport:
                delivered: list[str] = []
                for frame in frames:
                    self.transport.send(frame)
                    delivered.append(frame.id)
                return DeliveryReport(tuple(delivered))
        """
    ),
    "pipeline/cli.py": _dedent(
        """
        from __future__ import annotations

        import argparse


        def main(argv: list[str] | None = None) -> None:
            parser = argparse.ArgumentParser(prog="packet-pipeline")
            parser.add_argument("--version", action="version", version="0.2.0")
            parser.parse_args(argv)
            print("packet-pipeline ready")
        """
    ),
    "tests/test_public.py": _dedent(
        """
        import unittest

        from pipeline.engine import DeliveryEngine
        from pipeline.protocol import Frame


        class Transport:
            def __init__(self) -> None:
                self.sent = []

            def send(self, frame: Frame) -> None:
                self.sent.append(frame.id)


        class PipelinePublicTests(unittest.TestCase):
            def test_simple_delivery(self) -> None:
                transport = Transport()
                report = DeliveryEngine(transport).deliver([Frame("a", b"x")])
                self.assertEqual(report.delivered_ids, ("a",))
                self.assertEqual(transport.sent, ["a"])
        """
    ),
    "docs/continuity-notes.md": _legacy_notes(
        "packet-pipeline",
        (
            "The transport was intentionally injected so offline tests can model transient and permanent delivery outcomes.",
            "A frame ID is the stable idempotency key across retries and process restarts; payload bytes may be repeated safely.",
            "Checkpoint writes occur after individual acknowledgements so a crash loses at most the in-flight frame.",
            "Backoff is injected rather than sleeping in tests because delivery timing must be deterministic and observable.",
            "Batch order is retained even when an earlier frame fails, allowing operators to repair one destination later.",
        ),
    ),
}


PIPELINE_OBJECTIVE = _dedent(
    """
    Turn packet-pipeline into a reliable, resumable batch delivery engine. Work
    through these phases in order: understand the current send path, add
    retry/metrics telemetry, add durable checkpoint resume and idempotency, then
    update the CLI/docs and run the complete suite. Keep the public `Frame` and
    `RetryableDeliveryError` names stable.

    Before each pipeline phase, read `docs/continuity-notes.md` in bounded
    chunks and record relevant decision IDs in `.progress.md`. Implement
    `DeliveryEngine(transport, checkpoint_path=None, max_attempts=3,
    base_delay=0.0, sleep=None)` and `deliver(frames)` with these rules:

    * Call `transport.send(frame, idempotency_key=frame.id)` for each frame not
      already in the checkpoint. A transport may accept the old one-argument
      form, which must remain supported.
    * Retry only `RetryableDeliveryError`, at most `max_attempts` total sends
      per frame. Before retry number n (starting at 1), call the injected sleep
      function with exactly `base_delay * 2 ** (n - 1)`; never sleep on the
      initial attempt. Other exceptions are permanent failures and must not be
      retried.
    * Persist a checkpoint after every successful frame using an atomic replace.
      A restarted engine skips checkpointed IDs, preserving input order. A
      duplicate ID in one batch is sent at most once.
    * Return a `DeliveryReport` with ordered `delivered_ids`, `skipped_ids`,
      `failed_ids`, integer `attempts`, `retries`, and `errors`. Expose matching
      counters through `Metrics`; failed frames do not prevent later frames
      from being attempted.
    * Extend the CLI with an offline JSON-lines batch mode and a checkpoint
      option, and document retry/resume semantics in `docs/reliability.md`.

    Add deterministic fake-transport tests for transient/permanent failures,
    exponential delay injection, idempotency, crash-safe resume, ordering,
    metrics, and CLI parsing. No network or third-party dependency is allowed.
    """
).strip()


def _pipeline_checks(repo: Path) -> list[tuple[str, Callable[[], None]]]:
    def retry_and_report() -> None:
        sys.path.insert(0, str(repo))
        from pipeline.engine import DeliveryEngine
        from pipeline.protocol import Frame, RetryableDeliveryError

        class Fake:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.remaining = {"a": 2}

            def send(self, frame: Frame, idempotency_key: str | None = None) -> None:
                assert idempotency_key == frame.id
                self.calls.append(frame.id)
                if self.remaining.get(frame.id, 0):
                    self.remaining[frame.id] -= 1
                    raise RetryableDeliveryError(frame.id)

        fake = Fake()
        delays: list[float] = []
        report = DeliveryEngine(fake, max_attempts=3, base_delay=0.25, sleep=delays.append).deliver(
            [Frame("a", b"a"), Frame("b", b"b")]
        )
        assert report.delivered_ids == ("a", "b")
        assert report.failed_ids == ()
        assert report.retries == 2 and report.attempts == 4
        assert delays == [0.25, 0.5]

    def checkpoint_and_dedupe() -> None:
        sys.path.insert(0, str(repo))
        import tempfile
        from pathlib import Path
        from pipeline.engine import DeliveryEngine
        from pipeline.protocol import Frame

        class Fake:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def send(self, frame: Frame, idempotency_key: str | None = None) -> None:
                self.sent.append(idempotency_key or frame.id)

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            first = Fake()
            frames = [Frame("a", b"a"), Frame("a", b"duplicate"), Frame("b", b"b")]
            report = DeliveryEngine(first, checkpoint_path=checkpoint).deliver(frames)
            assert report.delivered_ids == ("a", "b")
            second = Fake()
            resumed = DeliveryEngine(second, checkpoint_path=checkpoint).deliver(frames)
            assert resumed.skipped_ids == ("a", "b") and second.sent == []

    def permanent_and_continue() -> None:
        sys.path.insert(0, str(repo))
        from pipeline.engine import DeliveryEngine
        from pipeline.protocol import Frame

        class Fake:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def send(self, frame: Frame, idempotency_key: str | None = None) -> None:
                self.sent.append(frame.id)
                if frame.id == "bad":
                    raise ValueError("permanent")

        fake = Fake()
        report = DeliveryEngine(fake, max_attempts=5).deliver([Frame("bad", b""), Frame("good", b"")])
        assert report.failed_ids == ("bad",) and report.delivered_ids == ("good",)
        assert fake.sent == ["bad", "good"]
        assert report.errors["bad"] == "permanent"

    def compatibility_and_docs() -> None:
        sys.path.insert(0, str(repo))
        from pipeline.engine import DeliveryEngine
        from pipeline.protocol import Frame

        class OldTransport:
            def __init__(self) -> None:
                self.sent = []

            def send(self, frame: Frame) -> None:
                self.sent.append(frame.id)

        transport = OldTransport()
        DeliveryEngine(transport).deliver([Frame("old", b"x")])
        assert transport.sent == ["old"]
        note = repo / "docs" / "reliability.md"
        assert note.is_file() and "checkpoint" in note.read_text(encoding="utf-8").lower()

    def metrics() -> None:
        sys.path.insert(0, str(repo))
        import inspect
        from pipeline.engine import DeliveryEngine
        from pipeline.metrics import Metrics

        assert "Metrics" in inspect.getsource(DeliveryEngine)
        metrics = Metrics()
        assert hasattr(metrics, "attempted") and hasattr(metrics, "retries")

    return [
        ("retry policy and report counters", retry_and_report),
        ("checkpoint resume and duplicate idempotency", checkpoint_and_dedupe),
        ("permanent failure isolation", permanent_and_continue),
        ("old transport compatibility and docs", compatibility_and_docs),
        ("metrics are exposed", metrics),
    ]


FIXTURES = [
    Fixture(
        "ledger-report",
        "Budget-aware ledger report",
        "multi-file feature",
        LEDGER_OBJECTIVE,
        _prompt(),
        LEDGER_FILES,
        _ledger_checks,
    ),
    Fixture(
        "scheduler-window",
        "Retry scheduling root-cause fix",
        "root-cause bug",
        QUEUE_OBJECTIVE,
        _prompt(),
        QUEUE_FILES,
        _queue_checks,
    ),
    Fixture(
        "config-refactor",
        "Canonical configuration normalization",
        "behavior-preserving refactor",
        CONFIG_OBJECTIVE,
        _prompt(),
        CONFIG_FILES,
        _config_checks,
    ),
    Fixture(
        "queue-migration",
        "Mailbox schema v1 to v2 migration",
        "multi-phase migration",
        MAILBOX_OBJECTIVE,
        _prompt(),
        MAILBOX_FILES,
        _mailbox_checks,
    ),
    Fixture(
        "noteboard-search",
        "Deterministic note search",
        "constraint retention",
        NOTES_OBJECTIVE,
        NOTES_PROMPT,
        NOTES_FILES,
        _notes_checks,
    ),
    Fixture(
        "packet-pipeline",
        "Reliable resumable delivery pipeline",
        "repeated-transition stress",
        PIPELINE_OBJECTIVE,
        _prompt(),
        PIPELINE_FILES,
        _pipeline_checks,
    ),
]


FIXTURES_BY_ID = {fixture.task_id: fixture for fixture in FIXTURES}
# Use a representative ordinary fixture for the pre-freeze verification pair.
# The packet-pipeline fixture is the dedicated repeated-transition stress case;
# it remains in the six-pair sample but is not imposed as a requirement on the
# ordinary fixtures.
PILOT_FIXTURE = FIXTURES[0]


def run_hidden_grade(task_id: str, repo: Path) -> dict[str, object]:
    """Run checks from the host side; callers must keep this outside the repo."""

    fixture = FIXTURES_BY_ID[task_id]
    checks = fixture.checks(repo)
    results: list[dict[str, object]] = []
    public = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(repo)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    results.append(
        {
            "name": "public test suite",
            "ok": public.returncode == 0,
            "detail": (public.stderr or public.stdout)[-1000:],
        }
    )
    for name, check in checks:
        try:
            check()
        except Exception as error:  # noqa: BLE001 - grader must report every failed check.
            results.append({"name": name, "ok": False, "detail": f"{type(error).__name__}: {error}"})
        else:
            results.append({"name": name, "ok": True, "detail": ""})
    passed = sum(1 for result in results if result["ok"])
    return {
        "task_id": task_id,
        "passed": passed,
        "total": len(results),
        "checks": results,
        "status": "PASS" if passed == len(results) else "FAIL",
    }
