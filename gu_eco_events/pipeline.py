"""Fetch -> parse -> normalize -> guard -> (ICS, events.json) -> notify."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config, ics, notify
from . import state as state_mod
from .model import DetailPage, Event, ListingHit
from .normalize import Rejected, build_events
from .source.gu import parse_detail_page, parse_listing_page
from .source.transport import SourceError


class GuardError(RuntimeError):
    """Safety check failed: do not publish, do not mutate state."""


@dataclass
class BuildResult:
    events: list[Event]
    rejected: list[Rejected]
    parsed_count: int
    missing_required: int
    source_total_hits: int
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "parsed_count": self.parsed_count,
            "event_count": len(self.events),
            "source_total_hits": self.source_total_hits,
        }


def collect(transport) -> tuple[list[tuple[list[ListingHit], DetailPage]], int]:
    """Read every listing page, then each unique detail page, sequentially."""
    hits: list[ListingHit] = []
    offset, total, pages = 0, None, 0
    while True:
        page_hits, page_total, per_page = parse_listing_page(transport.search_page(offset))
        total = page_total if total is None else total
        hits.extend(page_hits)
        pages += 1
        offset += per_page or len(page_hits) or config.HITS_PER_PAGE
        if offset >= total or not page_hits:
            break
        if pages >= config.MAX_PAGES:
            raise SourceError(f"more than {config.MAX_PAGES} listing pages; refusing to continue")
    if len(hits) < (total or 0):
        raise SourceError(f"listing incomplete: got {len(hits)} of {total} hits (pagination failure)")

    by_url: dict[str, list[ListingHit]] = {}
    for h in hits:
        by_url.setdefault(h.url, []).append(h)
    groups = []
    for url, group in by_url.items():
        html = transport.detail_html(config.GU_BASE + url if url.startswith("/") else url)
        groups.append((group, parse_detail_page(html)))
    return groups, total or 0


def build(transport) -> BuildResult:
    groups, total = collect(transport)
    events, rejected, missing, parsed = build_events(groups)
    return BuildResult(events, rejected, parsed, missing, total)


def check_guards(result: BuildResult, prior: dict) -> None:
    if result.source_total_hits == 0 or result.parsed_count == 0:
        raise GuardError("no events parsed from source; refusing to publish an empty calendar")
    if not result.events:
        raise GuardError("no events left after filtering; refusing to publish an empty calendar")
    ratio = result.missing_required / result.parsed_count
    if ratio > config.MAX_MISSING_REQUIRED_RATIO:
        raise GuardError(
            f"required-field quality collapsed: {result.missing_required}/{result.parsed_count} "
            "occurrences lack title/date/location"
        )
    last = (prior or {}).get("last_success") or {}
    prev = int(last.get("parsed_count") or 0)
    if prev >= config.SEVERE_DROP_MIN_PREVIOUS and result.parsed_count < prev * config.SEVERE_DROP_RATIO:
        raise GuardError(
            f"severe drop: {result.parsed_count} occurrences parsed vs {prev} on {last.get('date')}"
        )


def events_payload(result: BuildResult, today: date) -> bytes:
    data = {
        "generated_for": today.isoformat(),
        **result.counts(),
        "events": [e.to_dict() for e in result.events],
    }
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


INDEX_HTML = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><title>GU Hållbarhet &amp; miljö – kalender</title></head>
<body>
<h1>GU Hållbarhet &amp; miljö (Göteborg/online)</h1>
<p>Prenumerera på <a href="eco-events.ics">eco-events.ics</a>
(<a href="webcal://moonleaf-earth.github.io/gu-eco-events/eco-events.ics">webcal</a>).
Källan kontrolleras en gång i veckan; din kalenderapp bestämmer själv hur ofta den uppdaterar.</p>
</body></html>
"""


def write_outputs(out_dir: str | Path, result: BuildResult, today: date) -> Path:
    """Validate, then atomically replace the published files."""
    feed = ics.render(result.events)
    ics.validate(feed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "eco-events.ics": feed,
        "events.json": events_payload(result, today),
        "index.html": INDEX_HTML.encode("utf-8"),
    }
    staging = Path(tempfile.mkdtemp(dir=out, prefix=".staging-"))
    try:
        for name, data in files.items():
            (staging / name).write_bytes(data)
        for name in files:
            os.replace(staging / name, out / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return out / "eco-events.ics"


def report(result: BuildResult) -> None:
    print(
        f"parsed {result.parsed_count} occurrence(s) of {result.source_total_hits} hit(s); "
        f"kept {len(result.events)}; rejected {len(result.rejected)}",
        file=sys.stderr,
    )
    for r in result.rejected:
        print(f"  rejected: {r.reason}: {r.title} <{r.url}>", file=sys.stderr)


# --- notification step -----------------------------------------------------

MODES = ("dry-run", "send", "record-only")
CHANNELS = ("discord",)
SEND_DELAY_SECONDS = 1.0  # stay well under Discord webhook rate limits


def _senders_from_env(channels: set[str]) -> dict:
    """Build a sender per channel whose webhook secret is set and valid. A
    missing/invalid secret leaves that channel's notices pending."""
    senders = {}
    for channel, from_env, make in (
        ("discord", notify.webhook_from_env, notify.discord_sender),
    ):
        if channel not in channels:
            continue
        try:
            senders[channel] = make(from_env())
        except notify.NotifyError as e:
            print(f"{channel} webhook missing or invalid, {channel} notices left pending: {e}", file=sys.stderr)
    return senders


def run_notify(events: list[Event], counts: dict, state_path: str | Path, today: date,
               mode: str, senders: dict | None = None, out=None) -> int:
    """Plan against prior state, deliver per mode, persist state.

    dry-run:     print plan, record messages as sent (scratch/ops testing)
    send:        deliver via webhook; record only after success. Without a
                 (valid) secret, notices are left pending.
    record-only: no delivery, markers untouched (used when no secret is set,
                 so installing the secret later still announces events)
    """
    out = out or sys.stdout
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    prior = state_mod.load(state_path)
    messages = notify.plan(events, prior, today)
    new_state = state_mod.update_snapshot(json.loads(json.dumps(prior)), events, today, counts)

    senders = dict(senders or {})
    if mode == "send" and messages:
        senders.update(_senders_from_env({m.channel for m in messages} - set(senders)))

    sent = 0
    failures: dict[str, notify.NotifyError] = {}
    try:
        for msg in messages:
            print(json.dumps({**msg.to_dict(), "mode": mode}, ensure_ascii=False), file=out)
            if mode == "record-only":
                continue
            if mode == "send":
                sender = senders.get(msg.channel)
                if sender is None or msg.channel in failures:
                    continue  # leave pending for a later run
                try:
                    sender(msg.content)
                except notify.NotifyError as e:
                    failures[msg.channel] = e
                    continue
                except Exception as e:  # noqa: BLE001 - never lose already-sent markers
                    failures[msg.channel] = notify.NotifyError(
                        f"unexpected {type(e).__name__} during {msg.channel} delivery")
                    continue
                time.sleep(SEND_DELAY_SECONDS)
            notify.mark_sent(new_state, msg)
            sent += 1
    finally:
        # Persist markers for messages already delivered even on an unexpected
        # abort, so the next run does not resend them.
        state_mod.save(state_path, new_state)
    kinds = {k: sum(1 for m in messages if m.kind == k) for k in ("new", "update", "cancel")}
    channels = {c: sum(1 for m in messages if m.channel == c) for c in CHANNELS}
    failed = [c for c in CHANNELS if c in failures]
    summary = {"planned": len(messages), **kinds, "channels": channels, "recorded": sent,
               "mode": mode, "failed_channels": failed}
    print(json.dumps(summary), file=out)
    for c in failed:
        print(f"{c} notification failed: {failures[c]}", file=sys.stderr)
    return 4 if failed else 0
