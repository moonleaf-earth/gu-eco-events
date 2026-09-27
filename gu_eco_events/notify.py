"""Discord notification planning (pure) and delivery (webhook).

Only registration-required events that are new or materially changed
(title/time/place) are announced, plus one cancellation notice for an event
previously seen active. Expired registration deadlines and past events are
skipped. The webhook URL is read from the environment and never printed.
"""

from __future__ import annotations

import http.client
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

WEBHOOK_ENV = "ECO_EVENTS_DISCORD_WEBHOOK_URL"
SLACK_WEBHOOK_ENV = "ECO_EVENTS_SLACK_WEBHOOK_URL"
_WEBHOOK_RE = re.compile(r"^https://(discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/\d+/[\w-]+$")
_SLACK_WEBHOOK_RE = re.compile(r"^https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+$")

WEEKDAYS = ["mån", "tis", "ons", "tors", "fre", "lör", "sön"]
MONTHS = ["jan", "feb", "mars", "apr", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec"]


@dataclass(frozen=True)
class Message:
    channel: str
    kind: str  # "new" | "update" | "cancel"
    uid: str
    title: str
    material_hash: str
    content: str

    def to_dict(self) -> dict:
        return {"channel": self.channel, "plan": self.kind, "uid": self.uid, "title": self.title, "content": self.content}



_FREE_RE = re.compile(r"^(free|gratis|kostnadsfritt|kostnadsfri|0(\s*kr)?)$", re.IGNORECASE)

def is_paid(cost: str | None) -> bool:
    if not cost or not cost.strip():
        return False
    if _FREE_RE.match(cost.strip()):
        return False
    if not any(c.isdigit() for c in cost):
        return False
    return True

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



def format_slack_message(kind: str, e: Event) -> str:
    lines = [f"**{HEADINGS[kind]} (Avgift):** {e.title}", f"Tid: {format_when(e)}", f"Plats: {e.location}", f"Kostnad: {e.cost}"]
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

        # Common checks
        if e.registration_deadline and date.fromisoformat(e.registration_deadline) < today:
            continue

        # Discord
        if e.cancelled:
            if prev.get("seen_active") and not prev.get("cancellation_notified"):
                was_notified = prev.get("notified_hash") is not None
                was_req = prev.get("event", {}).get("registration_required")
                if was_notified or was_req:
                    out.append(Message("discord", "cancel", e.uid, e.title, e.material_hash(), format_message("cancel", e)))
        elif e.registration_required:
            notified = prev.get("notified_hash")
            if notified is None:
                out.append(Message("discord", "new", e.uid, e.title, e.material_hash(), format_message("new", e)))
            elif notified != e.material_hash():
                out.append(Message("discord", "update", e.uid, e.title, e.material_hash(), format_message("update", e)))

        # Slack
        if e.cancelled:
            if prev.get("seen_active") and not prev.get("slack_cancellation_notified"):
                was_notified = prev.get("slack_notified_hash") is not None
                was_req = prev.get("event", {}).get("registration_required")
                # Need to know if it was paid previously to send a cancel to Slack?
                # A cancellation is sent if it was previously notified.
                if was_notified:
                    out.append(Message("slack", "cancel", e.uid, e.title, e.material_hash(), format_slack_message("cancel", e)))
        elif e.registration_required and is_paid(e.cost):
            notified = prev.get("slack_notified_hash")
            if notified is None:
                out.append(Message("slack", "new", e.uid, e.title, e.material_hash(), format_slack_message("new", e)))
            elif notified != e.material_hash():
                out.append(Message("slack", "update", e.uid, e.title, e.material_hash(), format_slack_message("update", e)))

    return out

def mark_sent(state: dict, msg: Message) -> None:
    entry = state.setdefault("events", {}).setdefault(msg.uid, {})
    prefix = "" if msg.channel == "discord" else f"{msg.channel}_"
    if msg.kind == "cancel":
        entry[f"{prefix}cancellation_notified"] = True
    else:
        entry[f"{prefix}notified_hash"] = msg.material_hash


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
                    except (ValueError, AttributeError, OSError, http.client.HTTPException):
                        retry = 2.0
                    time.sleep(min(retry, 30))
                    continue
                raise NotifyError(f"Discord webhook returned HTTP {e.code}") from None
            except urllib.error.URLError as e:
                reason = type(getattr(e, "reason", e)).__name__
                raise NotifyError(f"Discord webhook unreachable ({reason})") from None
            except (OSError, http.client.HTTPException) as e:
                # getresponse()/read() errors (RemoteDisconnected, ConnectionResetError,
                # ssl.SSLError, IncompleteRead, timeouts) are not wrapped by urllib.
                raise NotifyError(f"Discord webhook connection failed ({type(e).__name__})") from None
        raise NotifyError("Discord webhook rate limit persisted")

    return send

def slack_webhook_from_env() -> str:
    url = os.environ.get(SLACK_WEBHOOK_ENV, "").strip()
    if not url:
        raise NotifyError(f"{SLACK_WEBHOOK_ENV} is not set")
    if not _SLACK_WEBHOOK_RE.match(url):
        raise NotifyError(f"{SLACK_WEBHOOK_ENV} is not a Slack webhook URL (value not shown)")
    return url

def slack_sender(webhook_url: str) -> Callable[[str], None]:
    def send(content: str) -> None:
        body = json.dumps({"text": content}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
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
                        retry = float(e.headers.get("retry-after", 2))
                    except (ValueError, TypeError):
                        retry = 2.0
                    time.sleep(min(retry, 30))
                    continue
                raise NotifyError(f"Slack webhook returned HTTP {e.code}") from None
            except urllib.error.URLError as e:
                reason = type(getattr(e, "reason", e)).__name__
                raise NotifyError(f"Slack webhook unreachable ({reason})") from None
            except (OSError, http.client.HTTPException) as e:
                raise NotifyError(f"Slack webhook connection failed ({type(e).__name__})") from None
        raise NotifyError("Slack webhook rate limit persisted")

    return send
