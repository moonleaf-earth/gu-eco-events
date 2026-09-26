"""Deterministic RFC 5545 writer + validation via the `icalendar` parser.

Same events in => byte-identical output: no wall-clock timestamps, sorted
events, CRLF endings, 75-octet folding that never splits a UTF-8 sequence.
"""

from __future__ import annotations

from datetime import date, datetime

from . import config
from .model import Event

CALNAME = "GU Hållbarhet & miljö (Göteborg/online)"

# Europe/Stockholm rules (EU DST since 1996): CEST from last Sunday of March
# 02:00, CET from last Sunday of October 03:00.
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{config.TZID}",
    f"X-LIC-LOCATION:{config.TZID}",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]

FALLBACK_DTSTAMP = "20260101T000000Z"


def escape_text(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """Fold to <=75 octets per physical line (RFC 5545 3.1)."""
    out = []
    cur = b""
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > limit:
            out.append(cur)
            cur = b" "
            limit = 75
        cur += b
    out.append(cur)
    return "\r\n".join(part.decode("utf-8") for part in out)


def _dt_local(value: datetime) -> str:
    return value.astimezone(config.TZ).strftime("%Y%m%dT%H%M%S")


def _date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _dtstamp(e: Event) -> str:
    if e.last_modified:
        return date.fromisoformat(e.last_modified).strftime("%Y%m%dT000000Z")
    return FALLBACK_DTSTAMP


def event_lines(e: Event) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{e.uid}", f"DTSTAMP:{_dtstamp(e)}"]
    if e.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{_date(e.start_value())}")
        lines.append(f"DTEND;VALUE=DATE:{_date(e.end_value())}")
    else:
        lines.append(f"DTSTART;TZID={config.TZID}:{_dt_local(e.start_value())}")
        lines.append(f"DTEND;TZID={config.TZID}:{_dt_local(e.end_value())}")
    if e.last_modified:
        lines.append(f"LAST-MODIFIED:{_dtstamp(e)}")
    lines += [
        f"SUMMARY:{escape_text(e.title)}",
        f"LOCATION:{escape_text(e.location)}",
        f"URL:{e.url}",
        f"DESCRIPTION:{escape_text(e.description)}",
        f"STATUS:{'CANCELLED' if e.cancelled else 'CONFIRMED'}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ]
    return lines


def render(events: list[Event]) -> bytes:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//moonleaf-earth//gu-eco-events//SV",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(CALNAME)}",
        f"NAME:{escape_text(CALNAME)}",
        f"X-WR-TIMEZONE:{config.TZID}",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        "X-PUBLISHED-TTL:P1D",
        *VTIMEZONE,
    ]
    for e in events:
        lines += event_lines(e)
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold_line(l) for l in lines) + "\r\n").encode("utf-8")


class InvalidCalendar(ValueError):
    pass


def validate(data: bytes) -> int:
    """Parse with `icalendar` and check required properties. Returns #events."""
    import icalendar

    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as e:  # noqa: BLE001 - surface any parser failure
        raise InvalidCalendar(f"icalendar could not parse feed: {e}") from e
    for prop in ("VERSION", "PRODID"):
        if prop not in cal:
            raise InvalidCalendar(f"VCALENDAR missing {prop}")
    tz_ids = {str(tz.get("TZID")) for tz in cal.walk("VTIMEZONE")}
    uids = set()
    count = 0
    for ev in cal.walk("VEVENT"):
        count += 1
        for prop in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY", "LOCATION", "URL"):
            if prop not in ev:
                raise InvalidCalendar(f"VEVENT {ev.get('UID')} missing {prop}")
        uid = str(ev["UID"])
        if uid in uids:
            raise InvalidCalendar(f"duplicate UID {uid}")
        uids.add(uid)
        start, end = ev["DTSTART"].dt, ev["DTEND"].dt
        if isinstance(start, datetime):
            tzid = ev["DTSTART"].params.get("TZID")
            if tzid and tzid not in tz_ids:
                raise InvalidCalendar(f"TZID {tzid} has no VTIMEZONE")
            if start.tzinfo is None:
                raise InvalidCalendar(f"{uid}: floating DTSTART")
        if not end > start:
            raise InvalidCalendar(f"{uid}: DTEND must be after DTSTART")
    for raw in data.split(b"\r\n"):
        if len(raw) > 75:
            raise InvalidCalendar("unfolded line longer than 75 octets")
    return count

