"""Listing JSON + detail HTML parsing, including real captured gu.se markup."""

import json
from datetime import date

from gu_eco_events.pipeline import build, collect
from gu_eco_events.source.gu import parse_detail_page, parse_listing_page, parse_swedish_dates
from gu_eco_events.source.transport import FixtureTransport, SourceError

from conftest import FIXTURES, fx

import pytest


def test_listing_page_fields():
    hits, total, per_page = parse_listing_page(
        json.loads((FIXTURES / "baseline/search/page-1.json").read_text(encoding="utf-8"))
    )
    assert total == 15 and per_page == 10 and len(hits) == 10
    h = hits[0]
    assert h.source_id.startswith("entity:node/") and h.url.startswith("/evenemang/")
    assert h.start_utc.tzinfo is not None


def test_listing_strips_search_highlight_markup():
    payload = {"stats": {"totalHits": 1}, "documentList": {"documents": [
        {"_id": "entity:node/1:sv_1", "type": "event", "url": "/evenemang/x",
         "title": "<em>INSTÄLLT</em>: Något", "event_start_time": "2026-10-01T10:00:00Z",
         "event_end_time": "2026-10-01T11:00:00Z", "is_all_day_event": "false"}]}}
    hits, _, _ = parse_listing_page(payload)
    assert hits[0].title == "INSTÄLLT: Något"


def test_all_result_pages_are_fetched():
    groups, total = collect(FixtureTransport(fx("baseline")))
    assert total == 15
    assert sum(len(h) for h, _ in groups) == 15
    assert len(groups) == 12  # unique event pages, each fetched once


def test_incomplete_pagination_is_a_source_error(tmp_path):
    import shutil
    shutil.copytree(fx("baseline"), tmp_path / "set")
    (tmp_path / "set/search/page-2.json").unlink()
    with pytest.raises(SourceError):
        build(FixtureTransport(tmp_path / "set"))


def test_missing_detail_page_is_a_source_error(tmp_path):
    import shutil
    shutil.copytree(fx("baseline"), tmp_path / "set")
    (tmp_path / "set/detail/hallbar-stad-panel.html").unlink()
    with pytest.raises(SourceError):
        build(FixtureTransport(tmp_path / "set"))


def test_real_gu_page_multi_occurrence_with_deadline():
    p = parse_detail_page(
        (FIXTURES / "real/webbkurs-for-foretagare-hantverk-i-digital-form.html").read_text(encoding="utf-8")
    )
    assert p.canonical_url == "https://www.gu.se/evenemang/webbkurs-for-foretagare-hantverk-i-digital-form"
    assert p.title == "Webbkurs för företagare: Hantverk i digital form"
    assert "Hållbarhet & miljö" in p.categories
    assert p.event_types == ("Webinar", "Workshop")
    assert [r.date_start for r in p.rows] == [date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20)]
    assert all(r.time_text == "15:00 - 16:30" for r in p.rows)
    assert p.location == "Online"
    assert p.registration_deadline == date(2026, 9, 30)
    assert p.last_modified == date(2026, 9, 11)
    assert any(l.href == "https://forms.office.com/e/Kq2ANGXDbJ" for l in p.links)


def test_real_gu_page_place_as_map_link():
    p = parse_detail_page(
        (FIXTURES / "real/skogaryd-research-centre-platsbesok.html").read_text(encoding="utf-8")
    )
    assert p.location == "Skogaryd Research Catchment"
    assert p.rows[0].date_start == date(2026, 9, 29) and p.rows[0].time_text == "08:30 - 16:30"


def test_swedish_dates():
    assert parse_swedish_dates("21 okt 2026 - 22 okt 2026") == [date(2026, 10, 21), date(2026, 10, 22)]
    assert parse_swedish_dates("30 september 2026") == [date(2026, 9, 30)]
    assert parse_swedish_dates("24 sept 2026") == [date(2026, 9, 24)]
    assert parse_swedish_dates("1 maj 2027") == [date(2027, 5, 1)]


def test_baseline_normalization():
    r = build(FixtureTransport(fx("baseline")))
    by_url = {}
    for e in r.events:
        by_url.setdefault(e.url.rsplit("/", 1)[-1], []).append(e)
    panel = by_url["hallbar-stad-panel"][0]
    # Only a start time on the page -> documented one-hour default.
    assert panel.start == "2026-10-08T18:00:00+02:00" and panel.end == "2026-10-08T19:00:00+02:00"
    week = by_url["hallbarhetsveckan-2026"]
    assert [(e.start, e.end, e.all_day) for e in week] == [
        ("2026-10-12", "2026-10-13", True), ("2026-10-13", "2026-10-14", True)]
    assert len(by_url["workshop-hallbar-mat"]) == 3
    cancelled = by_url["installt-stadsodling-seminarium"][0]
    assert cancelled.cancelled
    assert r.parsed_count == 15 and len(r.events) == 12
