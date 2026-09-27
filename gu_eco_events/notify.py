"""Discord and Slack notification planning (pure) and delivery (webhooks).

Discord: registration-required events that are new or materially changed
(title/time/place) are announced, plus one cancellation notice for an event
previously seen active. Slack: the strict subset of those events whose
structured GU cost is confidently non-free, tracked with its own markers and
a hash that also covers the cost. Expired registration deadlines and past
events are skipped. Webhook URLs are read from the environment and never
printed.
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



# Cost classification works only on the structured `Kostnad`/`Cost` field;
# body prose is never consulted.
_FREE_WORDS_RE = re.compile(r"\b(free|gratis|kostnadsfri|kostnadsfritt|avgiftsfri|avgiftsfritt)\b", re.IGNORECASE)
_FREE_ONLY_RE = re.compile(
    r"^(free( of charge)?|gratis|kostnadsfri|kostnadsfritt|avgiftsfri|avgiftsfritt)[.!]?$", re.IGNORECASE
)
_CURRENCY = r"(kr\.?|kronor|sek|:-|,-|€|eur|euro|\$|usd|£|gbp)"
_AMOUNT_RE = re.compile(r"\d+(?:[  ]\d{3})*(?:[.,]\d+)?")
_ZERO_PRICE_RE = re.compile(
    rf"^{_CURRENCY}?\s*0+(?:[.,]0+)?\s*{_CURRENCY}?\.?$", re.IGNORECASE
)
_PRICED_RE = re.compile(
    rf"(\d[\d  .,]*\s*{_CURRENCY}|(?<![a-z]){_CURRENCY}\s*\d)", re.IGNORECASE
)


def classify_cost(cost: str | None) -> str:
    """Return "free", "paid" or "unknown" for a structured cost value.

    free:    an unambiguous free marker ("Free", "Gratis", "Kostnadsfritt",
             ...) or a zero price ("0 kr", "0,00 SEK", "0:-").
    paid:    a non-zero amount with a currency ("950 kr plus moms"), or a
             bare non-zero number, and no free marker next to it.
    unknown: missing, blank, or anything else (e.g. "Se hemsidan", or mixed
             "Gratis för studenter, 200 kr för övriga"). Never paid.
    """
    text = " ".join((cost or "").split())
    if not text:
        return "unknown"
    if _FREE_ONLY_RE.match(text) or _ZERO_PRICE_RE.match(text):
        return "free"
    if _FREE_WORDS_RE.search(text):
        return "unknown"
    if not (_PRICED_RE.search(text) or _AMOUNT_RE.fullmatch(text)):
        return "unknown"
    amounts = [float(a.replace(" ", "").replace(" ", "").replace(",", ".")) for a in _AMOUNT_RE.findall(text)]
    if not any(amounts):
        return "unknown"
    return "paid"


def is_paid(cost: str | None) -> bool:
    return classify_cost(cost) == "paid"

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



def slack_escape(text: str) -> str:
    """Slack's documented control-character escaping: scraped text can never
    form `<!channel>`, `<!here>`, `<@U…>` or link syntax."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_link(url: str) -> str:
    return "<" + slack_escape(url).replace("|", "%7C") + ">"


SLACK_HEADINGS = {
    "new": "Nytt avgiftsbelagt evenemang med anmälan",
    "update": "Uppdaterat avgiftsbelagt evenemang (anmälan)",
    "cancel": "INSTÄLLT avgiftsbelagt evenemang",
}


def format_slack_message(kind: str, e: Event) -> str:
    esc = slack_escape
    lines = [
        f"*{SLACK_HEADINGS[kind]}:* {esc(e.title)}",
        f"Tid: {format_when(e)}",
        f"Plats: {esc(e.location)}",
        f"Kostnad: {esc(e.cost or '')}",
    ]
    if kind != "cancel":
        if e.registration_deadline:
            lines.append(f"Sista anmälningsdag: {_fmt_day(date.fromisoformat(e.registration_deadline))}")
        lines.append(f"Anmälan: {_slack_link(e.registration_url or e.url)}")
    lines.append(f"Evenemang: {_slack_link(e.url)}")
    return "\n".join(lines)[:3000]


# --- planning --------------------------------------------------------------

def _deadline_passed(e: Event, today: date) -> bool:
    return bool(e.registration_deadline) and date.fromisoformat(e.registration_deadline) < today


def plan(events: list[Event], state: dict, today: date) -> list[Message]:
    """Discord notices first, then Slack notices; each channel is planned
    against its own markers so one never suppresses the other."""
    seen = state.get("events", {})
    discord: list[Message] = []
    slack: list[Message] = []
    for e in events:
        prev = seen.get(e.uid) or {}
        if e.end_local_date() < today:
            continue

        # Discord: unchanged rule (registration required).
        if e.cancelled:
            if prev.get("seen_active") and not prev.get("cancellation_notified"):
                was_notified = prev.get("notified_hash") is not None
                was_req = prev.get("event", {}).get("registration_required")
                if was_notified or was_req:
                    discord.append(Message("discord", "cancel", e.uid, e.title, e.material_hash(), format_message("cancel", e)))
        elif e.registration_required and not _deadline_passed(e, today):
            notified = prev.get("notified_hash")
            kind = "new" if notified is None else "update" if notified != e.material_hash() else None
            if kind:
                discord.append(Message("discord", kind, e.uid, e.title, e.material_hash(), format_message(kind, e)))

        # Slack: registration required AND confidently paid.
        if e.cancelled:
            if (prev.get("seen_active") and prev.get("slack_notified_hash") is not None
                    and not prev.get("slack_cancellation_notified")):
                slack.append(Message("slack", "cancel", e.uid, e.title, e.paid_hash(), format_slack_message("cancel", e)))
        elif e.registration_required and is_paid(e.cost) and not _deadline_passed(e, today):
            notified = prev.get("slack_notified_hash")
            kind = "new" if notified is None else "update" if notified != e.paid_hash() else None
            if kind:
                slack.append(Message("slack", kind, e.uid, e.title, e.paid_hash(), format_slack_message(kind, e)))

    return discord + slack


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
        body = json.dumps({"text": content, "unfurl_links": False, "unfurl_media": False}).encode("utf-8")
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
