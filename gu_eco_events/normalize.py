"""Turn raw GU listing hits + detail pages into `Event`s.

Rules implemented here (documented in README):
- UID from GU's stable source id (node id + occurrence), else from the
  canonical URL; never from the date, so time edits keep the UID.
- Timed events resolve in Europe/Stockholm; missing end time => +60 min.
- True all-day entries stay all-day with an exclusive end date.
- Location rule: Göteborg/Gothenburg (or a known Göteborg venue) or online.
- Registration signals and cancellation markers.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import config
from .config import TZ
from .model import DetailPage, DetailRow, Event, ListingHit


@dataclass(frozen=True)
class Rejected:
    url: str
    title: str
    reason: str


def fold(s: str) -> str:
    """Casefold and strip diacritics for keyword matching."""
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return "".join(c for c in s if not unicodedata.combining(c))


# --- UID ---------------------------------------------------------------

_SOURCE_ID_RE = re.compile(r"node/(\d+):[a-z]{2}_(\d+)")


def make_uid(source_id: str | None, canonical_url: str, occurrence_index: int) -> str:
    m = _SOURCE_ID_RE.search(source_id or "")
    if m:
        return f"gu-node-{m.group(1)}-{m.group(2)}@{config.UID_DOMAIN}"
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]
    return f"gu-url-{digest}-{occurrence_index + 1}@{config.UID_DOMAIN}"


def canonical_url(detail: DetailPage | None, hit_url: str) -> str:
    """Prefer the page's canonical link when it points at gu.se."""
    cand = (detail.canonical_url if detail else None) or ""
    host = urllib.parse.urlparse(cand).netloc
    if cand and host in ("www.gu.se", "gu.se"):
        return cand
    return urllib.parse.urljoin(config.GU_BASE, hit_url)


# --- location ------------------------------------------------------------

def location_class(location: str | None, event_types: tuple[str, ...] = ()) -> tuple[bool, bool]:
    """Return (in_goteborg, online)."""
    loc = fold(location or "")
    types = fold(" ".join(event_types))
    goteborg = any(m in loc for m in config.GOTEBORG_MARKERS) or any(
        v in loc for v in config.GOTEBORG_VENUES
    )
    online = any(m in loc for m in config.ONLINE_MARKERS) or any(
        m in types for m in ("webinar", "webbinar")
    )
    return goteborg, online


# --- registration ----------------------------------------------------------

_REG_WORD = r"(anmal(an|ningsdag)?|anmal dig|registrer\w*|sign[ -]?up|register|biljett\w*|ticket\w*)"
_REG_LINK_TEXT_RE = re.compile(rf"\b{_REG_WORD}")
_REG_TEXT_RE = re.compile(
    r"\b(anmalan|anmal dig|anmal er|registrering|registrera dig|sista anmalningsdag|"
    r"registration|register here|sign up)\b"
)
_NEG_BEFORE_RE = re.compile(r"\b(ingen|utan|ej|inte|no|without)\s+(for)?$")
_NEG_AFTER_RE = re.compile(
    r"^\s*(kravs|behovs|ar)?\s*(ej|inte|icke)\b|^\s*(is\s+)?not\s+(required|needed)"
)
REGISTRATION_HOSTS = (
    "forms.office.com",
    "forms.cloud.microsoft",
    "forms.microsoft.com",
    "docs.google.com/forms",
    "forms.gle",
    "typeform.com",
    "eventbrite.",
    "simplesignup.",
    "invajo.com",
    "ungapped.",
    "trippus.",
    "lyyti.",
    "webropol",
    "survey.",
    "surveymonkey.",
    "netigate.",
    "tickster.",
    "lu.ma",
    "meetup.com",
    "confetti.events",
    "coompanion.se",  # seen on GU pages as ticket shop
)


def _is_form_host(href: str) -> bool:
    h = href.lower()
    return any(host in h for host in REGISTRATION_HOSTS)


def _text_signal(text: str) -> bool:
    t = fold(text)
    for m in _REG_TEXT_RE.finditer(t):
        before = t[max(0, m.start() - 12) : m.start()]
        after = t[m.end() : m.end() + 20]
        if _NEG_BEFORE_RE.search(before) or _NEG_AFTER_RE.search(after):
            continue
        return True
    return False


def registration(detail: DetailPage) -> tuple[bool, str | None]:
    """Return (required, preferred registration URL)."""
    by_text = [
        l for l in detail.links
        if l.href.startswith("http") and _REG_LINK_TEXT_RE.search(fold(l.text))
    ]
    by_host = [l for l in detail.links if l.href.startswith("http") and _is_form_host(l.href)]
    url = (by_text or by_host or [None])[0]
    required = bool(
        by_text or by_host or detail.registration_deadline or _text_signal(detail.text)
    )
    return required, (url.href if url else None)


# --- cancellation -----------------------------------------------------------

_CANCEL_RE = re.compile(r"\b(installt|installd|cancelled|canceled|avlyst|avbokat)\b")


def is_cancelled(title: str) -> bool:
    """GU marks cancellations with a title prefix, e.g. 'INSTÄLLT:',
    '(Inställd!)', 'Kursen inställd!' (verified against gu.se search 2026-09)."""
    return bool(_CANCEL_RE.search(fold(title)))


# --- time ------------------------------------------------------------------

_TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def _parse_times(text: str | None) -> tuple[time | None, time | None]:
    found = _TIME_RE.findall(text or "")
    ts = [time(int(h) % 24, int(m)) for h, m in found if int(h) <= 24 and int(m) < 60]
    if not ts:
        return None, None
    return ts[0], (ts[1] if len(ts) > 1 else None)


def _local(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=TZ)


def _timed(day_start: date, day_end: date, ts: time, te: time | None) -> tuple[str, str]:
    # Build wall-clock times in Europe/Stockholm; zoneinfo picks the right
    # UTC offset on either side of a DST transition.
    start = _local(day_start, ts)
    if te is None:
        end = start + timedelta(minutes=config.DEFAULT_DURATION_MINUTES)
    else:
        end = _local(day_end, te)
        if end <= start:  # e.g. "22:00 - 01:00" runs past midnight
            end = _local(day_end + timedelta(days=1), te)
    return start.isoformat(), end.isoformat()


def _all_day(d0: date, d1: date) -> tuple[str, str]:
    return d0.isoformat(), (d1 + timedelta(days=1)).isoformat()


def resolve_times(hit: ListingHit, index: int, group_size: int, rows: tuple[DetailRow, ...]):
    """Return (all_day, start_iso, end_iso) or None if no usable date."""
    local_day = hit.start_utc.astimezone(TZ).date() if hit.start_utc else None

    row = None
    if local_day is not None:
        row = next((r for r in rows if r.date_start <= local_day <= r.date_end), None)
    if row is None and rows and len(rows) == group_size:
        row = rows[index]
    if row is None and len(rows) == 1 and group_size == 1:
        row = rows[0]

    if row is not None:
        multi_day_row = row.date_end != row.date_start
        if multi_day_row and group_size > 1 and local_day and row.date_start <= local_day <= row.date_end:
            d0 = d1 = local_day  # GU lists each day of the range as its own hit
        else:
            d0, d1 = row.date_start, row.date_end
        ts, te = _parse_times(row.time_text)
        if ts is None:
            return (True, *_all_day(d0, d1))
        return (False, *_timed(d0, d1, ts, te))

    # Detail page gave no dates: fall back to the searcher's own times.
    if hit.start_utc is None:
        return None
    if hit.all_day:
        return (True, *_all_day(local_day, hit.end_utc.astimezone(TZ).date() if hit.end_utc else local_day))
    s = hit.start_utc.astimezone(TZ)
    e = hit.end_utc.astimezone(TZ) if hit.end_utc and hit.end_utc > hit.start_utc else None
    return (False, *_timed(s.date(), (e or s).date(), s.time(), e.time() if e else None))


def sort_key(e: Event) -> tuple[float, str]:
    """Order by instant (all-day entries at local midnight), then UID."""
    v = e.start_value()
    if not isinstance(v, datetime):
        v = datetime.combine(v, time(0), tzinfo=TZ)
    return v.timestamp(), e.uid


# --- assembly ---------------------------------------------------------------

def _description(preamble: str | None, reg_required: bool, reg_url: str | None,
                 deadline: date | None, url: str) -> str:
    parts = []
    if preamble:
        parts.append(preamble)
    if reg_required:
        line = "Anmälan krävs"
        if deadline:
            line += f" (sista anmälningsdag {deadline.isoformat()})"
        parts.append(line + (f": {reg_url}" if reg_url else "."))
    parts.append(f"Källa: {url}")
    return "\n\n".join(parts)


def build_events(groups: list[tuple[list[ListingHit], DetailPage]]):
    """groups: one entry per unique event URL (hits in listing order).

    Returns (events, rejected, missing_required_count, parsed_count).
    """
    events: list[Event] = []
    rejected: list[Rejected] = []
    missing = 0
    parsed = 0
    for hits, detail in groups:
        url = canonical_url(detail, hits[0].url)
        categories = detail.categories or hits[0].categories
        event_types = detail.event_types or hits[0].event_types
        title = detail.title or hits[0].title
        reg_required, reg_url = registration(detail)
        cancelled = is_cancelled(title)
        location = detail.location
        in_gbg, online = location_class(location, event_types)
        if not location and online:
            location = "Online"

        for i, hit in enumerate(hits):
            parsed += 1
            times = resolve_times(hit, i, len(hits), detail.rows)
            if not title or times is None or not location:
                missing += 1
                rejected.append(Rejected(url, title or "", "missing required field (title/date/location)"))
                continue
            if config.CATEGORY not in categories:
                rejected.append(Rejected(url, title, "not in category"))
                continue
            if not (in_gbg or online):
                rejected.append(Rejected(url, title, f"location outside rule: {location}"))
                continue
            all_day, start, end = times
            events.append(
                Event(
                    uid=make_uid(hit.source_id, url, i),
                    url=url,
                    title=title,
                    all_day=all_day,
                    start=start,
                    end=end,
                    location=location,
                    online=online,
                    cancelled=cancelled,
                    registration_required=reg_required,
                    registration_url=reg_url,
                    registration_deadline=(
                        detail.registration_deadline.isoformat() if detail.registration_deadline else None
                    ),
                    description=_description(detail.preamble, reg_required, reg_url,
                                             detail.registration_deadline, url),
                    last_modified=detail.last_modified.isoformat() if detail.last_modified else None,
                    source_id=hit.source_id,
                    categories=tuple(categories),
                )
            )
    # Deterministic order; drop accidental duplicates of the same UID.
    uniq: dict[str, Event] = {}
    for e in sorted(events, key=sort_key):
        uniq.setdefault(e.uid, e)
    return list(uniq.values()), rejected, missing, parsed
