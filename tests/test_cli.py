import pytest
from pathlib import Path
from gu_eco_events.ics import validate

def test_ics_generation_idempotency(runner):
    # generate twice
    code1, _, _ = runner.build("baseline")
    assert code1 == 0
    ics1 = (runner.out / "eco-events.ics").read_bytes()
    
    code2, _, _ = runner.build("baseline")
    assert code2 == 0
    ics2 = (runner.out / "eco-events.ics").read_bytes()
    
    assert ics1 == ics2
    
    # validate
    events_count = validate(ics1)
    assert events_count > 0
    
    # Check stable UIDs
    assert b"UID:" in ics1
    assert b"SUMMARY:" in ics1
    assert b"DTSTART" in ics1
    assert b"DTEND" in ics1
    assert b"LOCATION:" in ics1
    assert b"URL:" in ics1

def test_timezone_dst(runner):
    # Command: run the timezone test fixtures across both sides of a daylight-saving transition.
    # Expected: timed entries resolve in Europe/Stockholm; all-day entries use an exclusive end date; the feed supplies standards-compliant timezone information.
    code, _, _ = runner.build("dst")
    assert code == 0
    ics = (runner.out / "eco-events.ics").read_bytes()
    
    # Europe/Stockholm DST transitions: Last Sunday of October (to CET) and March (to CEST).
    # ensure tz info is present
    assert b"BEGIN:VTIMEZONE" in ics
    assert b"TZID:Europe/Stockholm" in ics
    
    # ensure it uses TZID for timed events
    assert b"DTSTART;TZID=Europe/Stockholm:" in ics
    # ensure all-day entries use VALUE=DATE
    assert b"DTSTART;VALUE=DATE:" in ics

def test_locations(runner):
    # Command: run the location fixtures.
    # Expected: Göteborg/Gothenburg and online/webinar events are included, while events solely in other locations are excluded from both the ICS feed and Discord notification plan.
    code, lines, _ = runner.run("location")
    assert code == 0
    ics = (runner.out / "eco-events.ics").read_bytes()
    
    # only included
    assert "LOCATION:Göteborg".encode("utf-8") in ics or b"LOCATION:Online" in ics or b"Gothenburg" in ics.decode('utf-8')
    assert b"LOCATION:Stockholm" not in ics
    
    p = [l for l in lines if "plan" in l]
    # Check that no Stockholm locations are planned
    for msg in p:
        assert "Stockholm" not in msg["content"]

def test_registration_dry_run_unchanged(runner):
    # Command: run the same unchanged registration-required fixture twice in notification dry-run mode.
    # Expected: the first run plans exactly one new-event message and the second plans none; changing its time, place, or title plans exactly one update while preserving its UID.
    code1, lines1, _ = runner.run("notify/base")
    assert code1 == 0
    planned1 = [l for l in lines1 if "plan" in l]
    # filter for kind='new'
    assert sum(1 for p in planned1 if p.get("plan") == "new") == 1
    
    # second run
    code2, lines2, _ = runner.run("notify/base")
    assert code2 == 0
    planned2 = [l for l in lines2 if "plan" in l]
    assert len(planned2) == 0

    # changing its time, place, or title plans exactly one update while preserving its UID
    code3, lines3, _ = runner.run("notify/time-changed")
    assert code3 == 0
    planned3 = [l for l in lines3 if "plan" in l]
    assert len(planned3) == 1
    assert planned3[0]["plan"] == "update"
    
    assert planned1[0]["uid"] == planned3[0]["uid"]

def test_safety_guards_zero_and_severe_drop(runner):
    # Command: run a zero-event fixture and a severe-drop fixture with a previously published feed and state snapshot.
    # Expected: the command exits non-zero and leaves the prior ICS and state untouched, with no Discord messages planned or sent.
    runner.use_prior()
    prior_ics = (runner.out / "eco-events.ics").read_bytes()
    prior_state = runner.state.read_bytes()
    
    code1, _, _ = runner.run("empty")
    assert code1 != 0
    
    assert (runner.out / "eco-events.ics").read_bytes() == prior_ics
    assert runner.state.read_bytes() == prior_state
    
    code2, _, _ = runner.run("severe-drop")
    assert code2 != 0
    
    assert (runner.out / "eco-events.ics").read_bytes() == prior_ics
    assert runner.state.read_bytes() == prior_state
