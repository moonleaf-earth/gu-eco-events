"""Command line entry point: python -m gu_eco_events <command>."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from . import config, ics, pipeline
from . import state as state_mod
from .model import Event
from .source.transport import FixtureTransport, LiveTransport, SourceError

EXIT_GUARD = 2
EXIT_SOURCE = 3


def _today(s: str | None) -> date:
    return date.fromisoformat(s) if s else datetime.now(config.TZ).date()


def _transport(args, today: date):
    if args.fixtures:
        return FixtureTransport(args.fixtures)
    return LiveTransport(date_from=today.isoformat())


def _add_source(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true", help="fetch from gu.se")
    g.add_argument("--fixtures", metavar="DIR", help="read a checked-in fixture set")
    p.add_argument("--state", required=True, help="state JSON (read for guards)")
    p.add_argument("--out-dir", required=True, help="where eco-events.ics/events.json go")
    p.add_argument("--today", help="override run date (YYYY-MM-DD, Europe/Stockholm)")


def _do_build(args) -> tuple[int, pipeline.BuildResult | None]:
    today = _today(args.today)
    prior = state_mod.load(args.state)
    try:
        result = pipeline.build(_transport(args, today))
    except SourceError as e:
        print(f"ABORT (source): {e}", file=sys.stderr)
        return EXIT_SOURCE, None
    pipeline.report(result)
    try:
        pipeline.check_guards(result, prior)
    except pipeline.GuardError as e:
        print(f"ABORT (safety guard): {e}. Prior feed and state left untouched.", file=sys.stderr)
        return EXIT_GUARD, None
    path = pipeline.write_outputs(args.out_dir, result, today)
    print(f"wrote {path} ({len(result.events)} events)", file=sys.stderr)
    return 0, result


def cmd_build(args) -> int:
    return _do_build(args)[0]


def cmd_notify(args) -> int:
    data = json.loads(Path(args.events).read_text(encoding="utf-8"))
    events = [Event.from_dict(d) for d in data["events"]]
    counts = {k: data[k] for k in ("parsed_count", "event_count", "source_total_hits")}
    return pipeline.run_notify(events, counts, args.state, _today(args.today), args.mode)


def cmd_run(args) -> int:
    code, result = _do_build(args)
    if code:
        return code
    return pipeline.run_notify(result.events, result.counts(), args.state, _today(args.today), args.mode)


def cmd_validate(args) -> int:
    n = ics.validate(Path(args.file).read_bytes())
    print(f"{args.file}: valid RFC 5545 calendar with {n} events")
    return 0


_WEBHOOK_PATTERN = re.compile(rb"discord(app)?\.com/api/webhooks/\d+/[\w-]{20,}")
_SLACK_WEBHOOK_PATTERN = re.compile(rb"hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+")


def cmd_check_leaks(args) -> int:
    """Fail if a webhook URL / secret value appears in tracked files or artifacts."""
    needles = []
    for var in args.env:
        v = os.environ.get(var, "").strip()
        if v:
            needles.append(v.encode())
    paths: list[Path] = []
    if args.tracked:
        out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
        paths += [Path(p.decode()) for p in out.split(b"\0") if p]
    for p in args.paths:
        pp = Path(p)
        paths += [f for f in pp.rglob("*") if f.is_file()] if pp.is_dir() else [pp]
    bad = []
    for f in paths:
        try:
            data = f.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if _WEBHOOK_PATTERN.search(data) or _SLACK_WEBHOOK_PATTERN.search(data) or any(n in data for n in needles):
            bad.append(str(f))
    if bad:
        print("LEAK: webhook URL or secret value found in: " + ", ".join(sorted(set(bad))), file=sys.stderr)
        return 1
    print(f"no webhook URL or secret value in {len(paths)} file(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gu_eco_events")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="fetch/parse, run safety guards, write feed (state untouched)")
    _add_source(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("notify", help="plan/send Discord (registration) and Slack (paid registration) notices from events.json and update state")
    p.add_argument("--events", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--mode", choices=pipeline.MODES, required=True)
    p.add_argument("--today")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("run", help="build then notify in one go")
    _add_source(p)
    p.add_argument("--mode", choices=pipeline.MODES, default="dry-run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("validate", help="parse an .ics with the icalendar validator")
    p.add_argument("file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("check-leaks", help="scan for webhook URLs / secret values")
    p.add_argument("--tracked", action="store_true", help="include all git-tracked files")
    p.add_argument("--env", action="append", default=[], help="env var whose value must not appear")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_check_leaks)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ics.InvalidCalendar as e:
        print(f"ABORT (invalid calendar): {e}", file=sys.stderr)
        return EXIT_GUARD
