import json
import pytest
from pathlib import Path
from gu_eco_events.ics import validate
from conftest import plans, summary

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
    code, _, _ = runner.build("dst")
    assert code == 0
    ics = (runner.out / "eco-events.ics").read_bytes()
    
    assert b"BEGIN:VTIMEZONE" in ics
    assert b"TZID:Europe/Stockholm" in ics
    
    # Assert concrete values on both sides of transitions
    assert b"DTSTART;TZID=Europe/Stockholm:20261024T100000" in ics
    assert b"DTSTART;TZID=Europe/Stockholm:20261026T100000" in ics
    assert b"DTSTART;TZID=Europe/Stockholm:20270326T100000" in ics
    assert b"DTSTART;TZID=Europe/Stockholm:20270329T100000" in ics
    
    # Sluttiden för dst-natten över omställningen
    assert b"DTEND;TZID=Europe/Stockholm:20261025T040000" in ics

    # All-day entries
    assert b"DTSTART;VALUE=DATE:20261025\r\n" in ics
    assert b"DTEND;VALUE=DATE:20261026\r\n" in ics

def test_locations(runner):
    code, lines, _ = runner.run("location")
    assert code == 0
    ics = (runner.out / "eco-events.ics").read_bytes()
    
    # Assert specific included and excluded titles/locations in ICS
    ics_str = ics.decode("utf-8")
    assert "LOCATION:Digitalt" in ics_str
    assert "LOCATION:Lindholmen Science Park\\, Gothenburg" in ics_str
    assert "LOCATION:Handelshögskolan\\, Vasagatan 1\\, Göteborg och online" in ics_str
    assert "LOCATION:Humanisten\\, Lilla hörsalen\\, Renströmsgatan 6" in ics_str
    assert "LOCATION:Online" in ics_str
    
    assert "LOCATION:Tjärnö" not in ics_str
    assert "LOCATION:Stockholm" not in ics_str
    assert "LOCATION:Malmö" not in ics_str
    
    p = [l for l in lines if "plan" in l]
    # Verify Discord notification plans contain the correct locations/titles
    planned_locations = [msg["content"] for msg in p]
    assert any("Plats: Digitalt" in c for c in planned_locations)
    assert any("Plats: Handelshögskolan, Vasagatan 1, Göteborg och online" in c for c in planned_locations)

    for c in planned_locations:
        assert "Tjärnö" not in c
        assert "Stockholm" not in c
        assert "Malmö" not in c

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
    
    code1, lines1, _ = runner.run("empty")
    assert code1 != 0
    assert not any("plan" in l for l in lines1)
    
    assert (runner.out / "eco-events.ics").read_bytes() == prior_ics
    assert runner.state.read_bytes() == prior_state
    
    code2, lines2, _ = runner.run("severe-drop")
    assert code2 != 0
    assert not any("plan" in l for l in lines2)
    
    assert (runner.out / "eco-events.ics").read_bytes() == prior_ics
    assert runner.state.read_bytes() == prior_state

def test_notify_changes(runner):
    # base
    code1, lines1, _ = runner.run("notify/base")
    assert code1 == 0
    p1 = [l for l in lines1 if "plan" in l]
    uid = p1[0]["uid"]
    
    # place-changed
    code2, lines2, _ = runner.run("notify/place-changed")
    assert code2 == 0
    p2 = [l for l in lines2 if "plan" in l]
    assert len(p2) == 1
    assert p2[0]["plan"] == "update"
    assert p2[0]["uid"] == uid
    
    # rerun place-changed => none
    code3, lines3, _ = runner.run("notify/place-changed")
    assert code3 == 0
    p3 = [l for l in lines3 if "plan" in l]
    assert len(p3) == 0

    # title-changed
    code4, lines4, _ = runner.run("notify/title-changed")
    assert code4 == 0
    p4 = [l for l in lines4 if "plan" in l]
    assert len(p4) == 1
    assert p4[0]["plan"] == "update"
    assert p4[0]["uid"] == uid
    
    # rerun title-changed => none
    code5, lines5, _ = runner.run("notify/title-changed")
    assert code5 == 0
    p5 = [l for l in lines5 if "plan" in l]
    assert len(p5) == 0

def test_notify_cancelled(runner):
    code1, _, _ = runner.run("notify/base")
    assert code1 == 0
    
    code2, lines2, _ = runner.run("notify/cancelled")
    assert code2 == 0
    p2 = [l for l in lines2 if "plan" in l]
    assert len(p2) == 1
    assert p2[0]["plan"] == "cancel"
    
    ics = (runner.out / "eco-events.ics").read_bytes()
    assert b"STATUS:CANCELLED" in ics
    
    # rerun cancelled => none
    code3, lines3, _ = runner.run("notify/cancelled")
    assert code3 == 0
    p3 = [l for l in lines3 if "plan" in l]
    assert len(p3) == 0

def test_quality_collapse(runner):
    runner.use_prior()
    prior_ics = (runner.out / "eco-events.ics").read_bytes()
    prior_state = runner.state.read_bytes()
    
    code, _, _ = runner.run("quality-collapse")
    assert code != 0
    
    assert (runner.out / "eco-events.ics").read_bytes() == prior_ics
    assert runner.state.read_bytes() == prior_state


def test_notify_missing_secret(runner, monkeypatch):
    import os
    from unittest.mock import patch
    
    with patch.dict(os.environ, clear=True):
        code, lines, _ = runner.run("notify/base", mode="send")
        assert code == 0
        
        # Should have fallen back to record-only and saved state.
        state = runner.state.read_bytes()
        assert b"events" in state
        
def test_notify_sender_fails(runner, monkeypatch):
    import os
    from unittest.mock import patch
    import gu_eco_events.notify
    
    def failing_sender(content):
        raise gu_eco_events.notify.NotifyError("Simulated failure")
        
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"}), \
         patch("gu_eco_events.notify.discord_sender", return_value=failing_sender):
         
        code, lines, _ = runner.run("notify/base", mode="send")
        assert code == 4
        
        # Should persist state despite failure
        state = runner.state.read_bytes()
        assert b"events" in state



def test_notify_partial_send_connection_reset_keeps_sent_markers(runner, monkeypatch):
    # One message succeeds, then the connection resets: exit 4, and the
    # delivered message must be recorded so the next run does not resend it.
    import os
    from unittest.mock import patch
    import gu_eco_events.notify
    import gu_eco_events.pipeline

    monkeypatch.setattr(gu_eco_events.pipeline, "SEND_DELAY_SECONDS", 0)
    delivered = []

    def flaky_sender(content):
        if delivered:
            raise ConnectionResetError(54, "Connection reset by peer")
        delivered.append(content)

    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"}), \
         patch("gu_eco_events.notify.discord_sender", return_value=flaky_sender):
        code, lines, captured = runner.run("baseline", mode="send")
    assert code == 4
    assert "ConnectionResetError" in captured.err
    first = plans(lines)[0]
    assert len(plans(lines)) >= 2
    assert summary(lines)["recorded"] == 1

    events = json.loads(runner.state.read_text())["events"]
    assert events[first["uid"]].get("notified_hash") is not None
    assert sum(1 for e in events.values() if e.get("notified_hash") is not None) == 1

    # Next run delivers only the remainder, never the first message again.
    resent = []
    with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/abc"}), \
         patch("gu_eco_events.notify.discord_sender", return_value=resent.append):
        code2, lines2, _ = runner.run("baseline", mode="send")
    assert code2 == 0
    assert first["uid"] not in {p["uid"] for p in plans(lines2)}
    assert delivered[0] not in resent
