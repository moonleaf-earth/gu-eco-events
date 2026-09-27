"""GU-specific parsing. All selectors and source field names live here.

Listing: JSON from the searcher the gu.se search page calls.
Detail:  the public HTML event page (authoritative for date, time, place,
         canonical URL, registration and cancellation).
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from ..model import DetailPage, DetailRow, Link, ListingHit

# --- listing (JSON) ------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", s or "")).strip()


def _as_tuple(v) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def _utc(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_listing_page(payload: dict) -> tuple[list[ListingHit], int, int]:
    """Return (hits, total_hits, hits_per_page) for one searcher response."""
    doc_list = payload.get("documentList") or {}
    total = int((payload.get("stats") or {}).get("totalHits") or 0)
    per_page = int((doc_list.get("pagination") or {}).get("hitsPerPage") or 0)
    hits = []
    for d in doc_list.get("documents") or []:
        if d.get("type") not in (None, "event"):
            continue
        hits.append(
            ListingHit(
                source_id=d.get("_id"),
                url=d.get("url") or "",
                title=_strip_tags(d.get("title") or ""),
                start_utc=_utc(d.get("event_start_time")),
                end_utc=_utc(d.get("event_end_time")),
                all_day=str(d.get("is_all_day_event")).lower() == "true",
                categories=_as_tuple(d.get("event_area")),
                event_types=_as_tuple(d.get("event_type")),
            )
        )
    return hits, total, per_page


# --- detail (HTML) -------------------------------------------------------

SEL_ARTICLE = "article.node--event-full"
SEL_CONTENT = ".layout__column--content"  # excludes the contact-card aside
SEL_EVENT_TYPES = ".label--framed > div"
SEL_META = "div.meta"
SEL_LAST_MODIFIED = ".content-footer time[datetime]"

LABEL_DATE = "datum"
LABEL_TIME = "tid"
LABEL_PLACE = "plats"
LABEL_COST = ("kostnad", "cost")
LABEL_DEADLINE = ("sista anmälningsdag", "sista dag för anmälan", "anmälan senast")

MONTHS = {
    "jan": 1, "januari": 1, "january": 1,
    "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "mars": 3, "march": 3,
    "apr": 4, "april": 4,
    "maj": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "augusti": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_DATE_RE = re.compile(r"(\d{1,2})\s+([a-zåäö]+)\.?\s+(\d{4})", re.IGNORECASE)


def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def parse_swedish_dates(s: str) -> list[date]:
    out = []
    for day, mon, year in _DATE_RE.findall(s or ""):
        m = MONTHS.get(mon.lower().rstrip("."))
        if m:
            out.append(date(int(year), m, int(day)))
    return out


def parse_detail_page(html: str) -> DetailPage:
    soup = BeautifulSoup(html, "html.parser")
    canonical = None
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        canonical = link["href"].strip()

    article = soup.select_one(SEL_ARTICLE) or soup
    content = article.select_one(SEL_CONTENT) or article

    h1 = content.find("h1") or article.find("h1")
    title = _text(h1) or None

    categories: tuple[str, ...] = ()
    if h1 is not None:
        cat_box = h1.find_next_sibling("div", class_="label")
        if cat_box is not None:
            inner = cat_box.find("div") or cat_box
            categories = tuple(
                t for t in (_text(d) for d in inner.find_all("div", recursive=False)) if t
            )

    event_types = tuple(
        t.rstrip(",").strip() for t in (_text(d) for d in content.select(SEL_EVENT_TYPES)) if t
    )

    pre = content.select_one("p.preamble")
    preamble = _text(pre) or None

    meta: dict[str, list[str]] = {}
    for m in content.select(SEL_META):
        label = m.find("div", class_="label")
        # Values are .meta__data blocks, or a bare link (e.g. a map link on Plats).
        values = [_text(v) for v in m.find_all(class_="meta__data")]
        if not values:
            values = [_text(a) for a in m.find_all("a")]
        if label is not None:
            meta.setdefault(_text(label).lower(), []).extend(v for v in values if v)

    rows: list[DetailRow] = []
    times = meta.get(LABEL_TIME, [])
    for i, dtext in enumerate(meta.get(LABEL_DATE, [])):
        ds = parse_swedish_dates(dtext)
        if not ds:
            continue
        if len(times) == len(meta.get(LABEL_DATE, [])):
            ttext = times[i]
        elif len(times) == 1:
            ttext = times[0]
        else:
            ttext = None
        rows.append(DetailRow(date_start=ds[0], date_end=ds[-1], time_text=ttext))

    places = meta.get(LABEL_PLACE, [])
    location = ", ".join(places) if places else None

    cost = None
    for key in LABEL_COST:
        if key in meta and meta[key]:
            cost = ", ".join(meta[key])
            break

    deadline = None
    for key in LABEL_DEADLINE:
        for v in meta.get(key, []):
            ds = parse_swedish_dates(v)
            if ds:
                deadline = ds[0]
                break
        if deadline:
            break

    links = []
    for a in content.find_all("a", href=True):
        ctx = ""
        col = a.find_parent(class_="columns")
        if col is not None:
            lab = col.find("div", class_="label")
            if lab is not None:
                ctx = _text(lab)
        links.append(Link(text=_text(a), href=a["href"].strip(), context=ctx))

    last_modified = None
    lm = soup.select_one(SEL_LAST_MODIFIED)
    if lm is not None:
        try:
            last_modified = date.fromisoformat(lm["datetime"][:10])
        except ValueError:
            last_modified = None

    return DetailPage(
        canonical_url=canonical,
        title=title,
        categories=categories,
        event_types=event_types,
        rows=tuple(rows),
        location=location,
        cost=cost,
        registration_deadline=deadline,
        links=tuple(links),
        text=_text(content),
        preamble=preamble,
        last_modified=last_modified,
    )
