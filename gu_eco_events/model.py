"""Source-independent data contracts.

`ListingHit` and `DetailPage` are what a source adapter produces; `Event` is
what the feed generator and notifier consume. A future GU JSON/RSS/ICS
endpoint only needs to produce `Event`s (or the two raw types) to keep the
feed and notifier contracts unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from .config import TZ


@dataclass(frozen=True)
class ListingHit:
    """One occurrence returned by the GU event searcher."""

    source_id: str | None  # e.g. "entity:node/190572:sv_2"
    url: str  # as given by GU (usually site-relative)
    title: str
    start_utc: datetime | None
    end_utc: datetime | None
    all_day: bool
    categories: tuple[str, ...] = ()
    event_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetailRow:
    """One Datum entry (optionally a date range) with its Tid entry."""

    date_start: date
    date_end: date
    time_text: str | None = None


@dataclass(frozen=True)
class Link:
    text: str
    href: str
    context: str = ""  # the meta label the link sits under, if any


@dataclass(frozen=True)
class DetailPage:
    canonical_url: str | None
    title: str | None
    categories: tuple[str, ...]
    event_types: tuple[str, ...]
    rows: tuple[DetailRow, ...]
    location: str | None
    registration_deadline: date | None
    links: tuple[Link, ...]
    text: str  # visible text of the event body, for keyword signals
    preamble: str | None
    last_modified: date | None


@dataclass(frozen=True)
class Event:
    uid: str
    url: str
    title: str
    all_day: bool
    # Timed: ISO datetimes with Europe/Stockholm offset. All-day: ISO dates,
    # `end` exclusive.
    start: str
    end: str
    location: str
    online: bool
    cancelled: bool
    registration_required: bool
    registration_url: str | None
    registration_deadline: str | None  # ISO date
    description: str
    last_modified: str | None  # ISO date
    source_id: str | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)

    # --- helpers -------------------------------------------------------
    def start_value(self) -> date | datetime:
        return date.fromisoformat(self.start) if self.all_day else datetime.fromisoformat(self.start)

    def end_value(self) -> date | datetime:
        return date.fromisoformat(self.end) if self.all_day else datetime.fromisoformat(self.end)

    def end_local_date(self) -> date:
        """Last local calendar day the event occupies."""
        if self.all_day:
            return date.fromordinal(date.fromisoformat(self.end).toordinal() - 1)
        return datetime.fromisoformat(self.end).astimezone(TZ).date()

    def material_hash(self) -> str:
        """Hash of the fields whose change warrants an update notification."""
        payload = [self.title, self.all_day, self.start, self.end, self.location]
        return _hash(payload)

    def content_hash(self) -> str:
        return _hash(self.to_dict())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["categories"] = list(self.categories)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d)
        d["categories"] = tuple(d.get("categories") or ())
        return cls(**d)


def _hash(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
