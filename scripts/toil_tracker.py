#!/usr/bin/env python3
"""
Toil Tracker — Log, Measure, and Eliminate Operational Toil

Tracks toil events with type, duration, and automation potential.
Produces weekly summaries and ranks automation opportunities by ROI.

SRE definition of toil (Google):
  Work that is manual, repetitive, automatable, tactical, without enduring value,
  and scales linearly with service growth.

Target: <50% toil/total-engineering-time ratio.

Usage:
    # Log a toil event
    python3 toil_tracker.py log --type "manual-deploy" --duration 45 --automatable yes \
        --description "Manually pushed config to 12 servers before deploy window"

    # Weekly summary
    python3 toil_tracker.py summary --weeks 1

    # Top automation opportunities
    python3 toil_tracker.py opportunities

    # Export all records
    python3 toil_tracker.py export --format csv

Storage:
    ~/.sre/toil_tracker.jsonl (JSON lines, append-only)
    Override with --db-path
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal


DEFAULT_DB_PATH = Path.home() / ".sre" / "toil_tracker.jsonl"

TOIL_TYPES = [
    "manual-deploy",
    "manual-restart",
    "manual-scaling",
    "ticket-routing",
    "log-digging",
    "cert-rotation",
    "config-sync",
    "password-rotation",
    "backup-verification",
    "capacity-planning",
    "oncall-interrupt",
    "report-generation",
    "other",
]

AutomatableLevel = Literal["yes", "partial", "no"]


@dataclass
class ToilEvent:
    """A single logged toil event."""

    ts: str  # ISO 8601 UTC
    toil_type: str
    duration_minutes: float
    automatable: AutomatableLevel
    description: str
    engineer: str
    ticket: str  # Optional ticket reference

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ToilEvent":
        return cls(
            ts=str(data.get("ts", "")),
            toil_type=str(data.get("toil_type", "other")),
            duration_minutes=float(data.get("duration_minutes", 0)),
            automatable=data.get("automatable", "no"),  # type: ignore[arg-type]
            description=str(data.get("description", "")),
            engineer=str(data.get("engineer", "unknown")),
            ticket=str(data.get("ticket", "")),
        )

    @property
    def ts_dt(self) -> datetime:
        return datetime.fromisoformat(self.ts.replace("Z", "+00:00"))


@dataclass
class AutomationOpportunity:
    """A ranked automation opportunity derived from toil data."""

    toil_type: str
    total_minutes: float
    occurrence_count: int
    automatable_minutes: float
    automatable_pct: float
    estimated_weekly_savings_minutes: float
    roi_score: float  # higher = more valuable to automate
    engineers_affected: int


def load_events(db_path: Path) -> list[ToilEvent]:
    """Load all toil events from the database file."""
    if not db_path.exists():
        return []

    events: list[ToilEvent] = []
    with open(db_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                events.append(ToilEvent.from_dict(data))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    return sorted(events, key=lambda e: e.ts)


def append_event(db_path: Path, event: ToilEvent) -> None:
    """Append a toil event to the database file."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open(db_path, "a") as fh:
        fh.write(json.dumps(event.as_dict()) + "\n")


def filter_by_window(events: list[ToilEvent], weeks: int) -> list[ToilEvent]:
    """Filter events to a rolling window of N weeks."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(weeks=weeks)
    return [e for e in events if e.ts_dt >= cutoff]


def calculate_weekly_summary(events: list[ToilEvent], weeks: int = 1) -> dict[str, object]:
    """Calculate weekly toil summary statistics."""
    window_events = filter_by_window(events, weeks)

    if not window_events:
        return {
            "period_weeks": weeks,
            "total_events": 0,
            "total_toil_minutes": 0.0,
            "automatable_minutes": 0.0,
            "non_automatable_minutes": 0.0,
            "toil_by_type": {},
            "engineers": [],
        }

    total_minutes = sum(e.duration_minutes for e in window_events)
    automatable_minutes = sum(
        e.duration_minutes for e in window_events if e.automatable in ("yes", "partial")
    )
    non_automatable_minutes = total_minutes - automatable_minutes

    # Group by type
    by_type: dict[str, dict[str, object]] = {}
    for event in window_events:
        if event.toil_type not in by_type:
            by_type[event.toil_type] = {
                "count": 0,
                "total_minutes": 0.0,
                "automatable_minutes": 0.0,
            }
        entry = by_type[event.toil_type]
        entry["count"] = int(entry["count"]) + 1  # type: ignore[assignment]
        entry["total_minutes"] = float(entry["total_minutes"]) + event.duration_minutes  # type: ignore[assignment]
        if event.automatable in ("yes", "partial"):
            entry["automatable_minutes"] = float(entry["automatable_minutes"]) + event.duration_minutes  # type: ignore[assignment]

    engineers = list({e.engineer for e in window_events})

    return {
        "period_weeks": weeks,
        "total_events": len(window_events),
        "total_toil_minutes": total_minutes,
        "automatable_minutes": automatable_minutes,
        "non_automatable_minutes": non_automatable_minutes,
        "toil_pct_automatable": (automatable_minutes / total_minutes * 100) if total_minutes > 0 else 0.0,
        "toil_by_type": by_type,
        "engineers": engineers,
    }


def calculate_opportunities(events: list[ToilEvent], weeks: int = 4) -> list[AutomationOpportunity]:
    """
    Rank automation opportunities by ROI.

    ROI score = (automatable_minutes_per_week * automatable_pct) / 100
    Higher score = more weekly time saved if automated.
    """
    window_events = filter_by_window(events, weeks)

    by_type: dict[str, list[ToilEvent]] = {}
    for event in window_events:
        by_type.setdefault(event.toil_type, []).append(event)

    opportunities: list[AutomationOpportunity] = []
    for toil_type, type_events in by_type.items():
        total_minutes = sum(e.duration_minutes for e in type_events)
        automatable_minutes = sum(
            e.duration_minutes for e in type_events if e.automatable in ("yes", "partial")
        )
        automatable_pct = (automatable_minutes / total_minutes * 100) if total_minutes > 0 else 0.0
        weekly_savings = automatable_minutes / weeks
        engineers_affected = len({e.engineer for e in type_events})

        roi_score = weekly_savings * (automatable_pct / 100.0)

        opportunities.append(
            AutomationOpportunity(
                toil_type=toil_type,
                total_minutes=total_minutes,
                occurrence_count=len(type_events),
                automatable_minutes=automatable_minutes,
                automatable_pct=automatable_pct,
                estimated_weekly_savings_minutes=weekly_savings,
                roi_score=roi_score,
                engineers_affected=engineers_affected,
            )
        )

    return sorted(opportunities, key=lambda o: o.roi_score, reverse=True)


def cmd_log(args: argparse.Namespace) -> int:
    """Log a new toil event."""
    db_path = Path(args.db_path)

    event = ToilEvent(
        ts=datetime.now(tz=timezone.utc).isoformat(),
        toil_type=args.type,
        duration_minutes=args.duration,
        automatable=args.automatable,
        description=args.description,
        engineer=args.engineer or os.environ.get("USER", "unknown"),
        ticket=args.ticket or "",
    )

    append_event(db_path, event)
    print(f"Logged: [{event.toil_type}] {event.duration_minutes:.0f}min (automatable={event.automatable})")
    print(f"  {event.description}")
    print(f"  Stored in: {db_path}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Print weekly summary."""
    db_path = Path(args.db_path)
    events = load_events(db_path)
    summary = calculate_weekly_summary(events, weeks=args.weeks)

    sep = "-" * 60
    total_minutes = float(summary["total_toil_minutes"])  # type: ignore[arg-type]
    automatable = float(summary["automatable_minutes"])  # type: ignore[arg-type]
    automatable_pct = float(summary.get("toil_pct_automatable", 0))  # type: ignore[arg-type]
    total_events = int(summary["total_events"])  # type: ignore[arg-type]

    print(sep)
    print(f"  Toil Summary — last {args.weeks} week(s)")
    print(sep)
    print(f"  Total events:       {total_events}")
    print(f"  Total toil:         {total_minutes:.0f} minutes ({total_minutes/60:.1f} hours)")
    print(f"  Automatable:        {automatable:.0f} min ({automatable_pct:.1f}%)")
    print(f"  Non-automatable:    {float(summary['non_automatable_minutes']):.0f} min")  # type: ignore[arg-type]
    print(sep)

    # 50% toil target
    if total_minutes > 0:
        if automatable_pct >= 50:
            print(f"  Target (<50% auto): PASS ({automatable_pct:.1f}% automatable)")
        else:
            print(f"  Target (<50% auto): REVIEW ({automatable_pct:.1f}% automatable)")
    print(sep)

    by_type = summary.get("toil_by_type", {})
    if by_type and isinstance(by_type, dict):
        print()
        print("  Breakdown by type:")
        print(f"  {'Type':<25} {'Count':>6} {'Minutes':>8} {'Auto%':>7}")
        print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*7}")
        for toil_type, data in sorted(
            by_type.items(),
            key=lambda kv: float(kv[1].get("total_minutes", 0)),  # type: ignore[union-attr]
            reverse=True,
        ):
            if isinstance(data, dict):
                count = int(data.get("count", 0))
                mins = float(data.get("total_minutes", 0))
                auto_mins = float(data.get("automatable_minutes", 0))
                auto_pct = (auto_mins / mins * 100) if mins > 0 else 0.0
                print(f"  {toil_type:<25} {count:>6} {mins:>8.0f} {auto_pct:>6.0f}%")
    print()
    return 0


def cmd_opportunities(args: argparse.Namespace) -> int:
    """Print ranked automation opportunities."""
    db_path = Path(args.db_path)
    events = load_events(db_path)
    opportunities = calculate_opportunities(events, weeks=args.weeks)

    if not opportunities:
        print("No toil data found. Log some events first with: toil_tracker.py log ...")
        return 0

    sep = "-" * 70
    print(sep)
    print(f"  Top Automation Opportunities (last {args.weeks} weeks)")
    print(f"  Ranked by: weekly time savings × automation feasibility")
    print(sep)
    print(f"  {'Rank':<5} {'Type':<25} {'ROI Score':>10} {'Wkly Save':>10} {'Auto%':>7} {'Eng':>5}")
    print(f"  {'-'*5} {'-'*25} {'-'*10} {'-'*10} {'-'*7} {'-'*5}")

    for rank, opp in enumerate(opportunities[:10], start=1):
        if opp.automatable_pct < 1.0:
            continue
        print(
            f"  {rank:<5} {opp.toil_type:<25} {opp.roi_score:>10.1f} "
            f"{opp.estimated_weekly_savings_minutes:>9.0f}m "
            f"{opp.automatable_pct:>6.0f}% {opp.engineers_affected:>5}"
        )

    print(sep)
    print()
    print("  Automation ROI interpretation:")
    print("  ROI Score = (weekly_savings_minutes × automatable_fraction)")
    print("  Score >60: High priority — automate within sprint")
    print("  Score 20-60: Medium priority — schedule within quarter")
    print("  Score <20: Low priority — document, revisit later")
    print()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export all toil records."""
    db_path = Path(args.db_path)
    events = load_events(db_path)

    if not events:
        print("No records found.")
        return 0

    if args.format == "json":
        print(json.dumps([e.as_dict() for e in events], indent=2))
    elif args.format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["ts", "toil_type", "duration_minutes", "automatable", "description", "engineer", "ticket"],
        )
        writer.writeheader()
        for event in events:
            writer.writerow(event.as_dict())
        print(output.getvalue())

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Track and eliminate SRE toil",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to toil database file (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command")

    # log subcommand
    log_parser = subparsers.add_parser("log", help="Log a toil event")
    log_parser.add_argument("--type", required=True, choices=TOIL_TYPES, help="Toil type")
    log_parser.add_argument("--duration", type=float, required=True, help="Duration in minutes")
    log_parser.add_argument(
        "--automatable",
        required=True,
        choices=["yes", "partial", "no"],
        help="Can this be automated?",
    )
    log_parser.add_argument("--description", default="", help="Free-form description")
    log_parser.add_argument("--engineer", default=None, help="Engineer name (default: $USER)")
    log_parser.add_argument("--ticket", default="", help="Ticket/issue reference")

    # summary subcommand
    summary_parser = subparsers.add_parser("summary", help="Print weekly toil summary")
    summary_parser.add_argument("--weeks", type=int, default=1, help="Weeks to summarize (default: 1)")

    # opportunities subcommand
    opp_parser = subparsers.add_parser("opportunities", help="Rank automation opportunities")
    opp_parser.add_argument("--weeks", type=int, default=4, help="Analysis window in weeks (default: 4)")

    # export subcommand
    export_parser = subparsers.add_parser("export", help="Export all records")
    export_parser.add_argument("--format", choices=["json", "csv"], default="json")

    args = parser.parse_args(argv)

    if args.command == "log":
        return cmd_log(args)
    elif args.command == "summary":
        return cmd_summary(args)
    elif args.command == "opportunities":
        return cmd_opportunities(args)
    elif args.command == "export":
        return cmd_export(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
