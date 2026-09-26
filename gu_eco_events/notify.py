"""Discord notification planning (pure) and delivery (webhook).

Only registration-required events that are new or materially changed
(title/time/place) are announced, plus one cancellation notice for an event
previously seen active. Expired registration deadlines and past events are
skipped. The webhook URL is read from the environment and never printed.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from .config import TZ
from .model import Event

WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
_WEBHOOK_RE = re.compile(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/\d+/[\w-]+$")

WEEKDAYS = ["mån", "tis", "ons", "tors", "fre", "lör", "sön"]
MONTHS = ["jan", "feb", "mars", "apr", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec"]


@dataclass(frozen=True)
class Message:
    kind: str  # "new" | "update" | "cancel"
    uid: str
    title: str
    material_hash: str
    content: str

    def to_dict(self) -> dict:
        return {"plan": self.kind, "uid": self.uid, "title": self.title, "content": self.content}


# --- formatting ------------------------------------------------------------

def _fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year}"


def format_when(e: Event) -> str:
    if e.all_day:
        first, last = date.fromisoformat(e.start), e.end_local_date()
        return f"{_fmt_day(first)} (heldag)" if first == last else f"{_fmt_day(first)} – {_fmt_day(last)} (heldag)"
    s = datetime.fromisoformat(e.start).astimezone(TZ)
    en = datetime.fromisoformat(e.end).astimezone(TZ)
    if s.date() == en.date():
        return f"{_fmt_day(s.date())} {s:%H:%M}–{en:%H:%M}"
    return f"{_fmt_day(s.date())} {s:%H:%M} – {_fmt_day(en.date())} {en:%H:%M}"


HEADINGS = {
    "new": "Nytt evenemang med anmälan",
    "update": "Uppdaterat evenemang (anmälan)",
    "cancel": "INSTÄLLT evenemang",
}


def format_message(kind: str, e: Event) -> str:
    lines = [f"**{HEADINGS[kind]}:** {e.title}", f"Tid: {format_when(e)}", f"Plats: {e.location}"]
    if kind != "cancel":
        if e.registration_deadline:
            lines.append(f"Sista anmälningsdag: {_fmt_day(date.fromisoformat(e.registration_deadline))}")
        lines.append(f"Anmälan: <{e.registration_url or e.url}>")
    lines.append(f"Evenemang: <{e.url}>")
    return "\n".join(lines)[:1900]


# --- planning --------------------------------------------------------------

def plan(events: list[Event], state: dict, today: date) -> list[Message]:
    seen = state.get("events", {})
    out: list[Message] = []
    for e in events:
        prev = seen.get(e.uid) or {}
        if e.end_local_date() < today:
            continue
        if e.cancelled:
            if prev.get("seen_active") and not prev.get("cancellation_notified"):
                was_notified = prev.get("notified_hash") is not None
                was_req = prev.get("event", {}).get("registration_required")
                if was_notified or was_req:
                    out.append(Message("cancel", e.uid, e.title, e.material_hash(), format_message("cancel", e)))
            continue
        if not e.registration_required:
            continue
        if e.registration_deadline and date.fromisoformat(e.registration_deadline) < today:
            continue
        notified = prev.get("notified_hash")
        if notified is None:
            kind = "new"
        elif notified != e.material_hash():
            kind = "update"
        else:
            continue
        out.append(Message(kind, e.uid, e.title, e.material_hash(), format_message(kind, e)))
    return out


def mark_sent(state: dict, msg: Message) -> None:
    entry = state.setdefault("events", {}).setdefault(msg.uid, {})
    if msg.kind == "cancel":
        entry["cancellation_notified"] = True
    else:
        entry["notified_hash"] = msg.material_hash


# --- delivery --------------------------------------------------------------

class NotifyError(RuntimeError):
    """Delivery failed. Message never includes the webhook URL."""


def webhook_from_env() -> str:
    url = os.environ.get(WEBHOOK_ENV, "").strip()
    if not url:
        raise NotifyError(f"{WEBHOOK_ENV} is not set")
    if not _WEBHOOK_RE.match(url):
        raise NotifyError(f"{WEBHOOK_ENV} is not a Discord webhook URL (value not shown)")
    return url


def discord_sender(webhook_url: str) -> Callable[[str], None]:
    def send(content: str) -> None:
        body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url + "?wait=true",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "DiscordBot (https://github.com/moonleaf-earth/gu-eco-events, 0.1)",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    r.read()
                    return
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    try:
                        retry = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                    except (ValueError, AttributeError):
                        retry = 2.0
                    time.sleep(min(retry, 30))
                    continue
                raise NotifyError(f"Discord webhook returned HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                reason = type(getattr(e, "reason", e)).__name__
                raise NotifyError(f"Discord webhook unreachable ({reason})") from None
        raise NotifyError("Discord webhook rate limit persisted")

    return send
