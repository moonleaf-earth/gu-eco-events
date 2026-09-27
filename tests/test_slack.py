"""Paid-event Slack routing: cost parsing, routing, state migration, delivery."""

import argparse
import email.message
import json
import os
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from conftest import FIXTURES, fx, plans, summary
from gu_eco_events import notify, pipeline
from gu_eco_events import state as state_mod
from gu_eco_events.cli import cmd_check_leaks
from gu_eco_events.model import Event
from gu_eco_events.notify import classify_cost, format_slack_message, mark_sent, plan
from gu_eco_events.source.gu import parse_detail_page

ROOT = Path(__file__).resolve().parents[1]
# Frozen copy of data/state.json as committed before cost/Slack existed. The
# live file is rewritten by every workflow run, so tests must not pin it.
COMMITTED_STATE = FIXTURES / "legacy-state" / "state.json"
LIVE_STATE = ROOT / "data" / "state.json"
DISCORD = "https://discord.com/api/webhooks/123/abc"
# Built by concatenation so the tracked-file leak check never sees a URL.
SLACK = "https://hooks.slack.com/services/" + "T000TEST/B000TEST/" + "notARealSecretValue0"
WEBBKURS_UID = "gu-node-190572-1@gu-eco-events.moonleaf-earth.github.io"


def _e(uid="1", req=True, cost=None, **kw):
    base = dict(
        uid=uid, url="https://www.gu.se/evenemang/x", title="T", all_day=False,
        start="2026-10-10T10:00:00+02:00", end="2026-10-10T11:00:00+02:00", location="L", cost=cost,
        online=False, cancelled=False, registration_required=req, registration_url=None,
        registration_deadline=None, description="", last_modified=None, source_id="", categories=(),
    )
    base.update(kw)
    return Event(**base)


def _by_channel(messages):
    out = {}
    for m in messages:
        out.setdefault(m.channel, []).append(m)
    return out


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(pipeline, "SEND_DELAY_SECONDS", 0)


class Recorder:
    def __init__(self, fail_on=None, exc=None):
        self.sent = []
        self.calls = 0
        self.fail_on = fail_on or set()
        self.exc = exc

    def __call__(self, content):
        self.calls += 1
        if self.calls in self.fail_on or (self.exc and not self.fail_on):
            raise self.exc or notify.NotifyError("Slack webhook returned HTTP 404")
        self.sent.append(content)


def _senders(discord, slack, env=None):
    env = {"ECO_EVENTS_DISCORD_WEBHOOK_URL": DISCORD, "ECO_EVENTS_SLACK_WEBHOOK_URL": SLACK} if env is None else env
    return (
        patch.dict(os.environ, env, clear=True),
        patch("gu_eco_events.notify.discord_sender", return_value=discord),
        patch("gu_eco_events.notify.slack_sender", return_value=slack),
    )


def _send(runner, fixture, discord, slack, env=None):
    a, b, c = _senders(discord, slack, env)
    with a, b, c:
        return runner.run(fixture, mode="send")


def _notify_json(runner, events, discord, slack, env=None, today="2026-09-26"):
    path = runner.tmp / "events.json"
    path.write_text(json.dumps({
        "generated_for": today, "parsed_count": len(events), "event_count": len(events),
        "source_total_hits": len(events), "events": [e.to_dict() for e in events],
    }), encoding="utf-8")
    a, b, c = _senders(discord, slack, env)
    with a, b, c:
        return runner("notify", "--events", str(path), "--state", str(runner.state),
                      "--mode", "send", "--today", today)


# --- parsing & classification ---------------------------------------------

def test_real_fixture_free_cost():
    p = parse_detail_page((FIXTURES / "real/skogaryd-research-centre-platsbesok.html").read_text(encoding="utf-8"))
    assert p.cost == "Free"
    assert classify_cost(p.cost) == "free"


def test_real_fixture_paid_cost_preserved_exactly():
    p = parse_detail_page(
        (FIXTURES / "real/webbkurs-for-foretagare-hantverk-i-digital-form.html").read_text(encoding="utf-8"))
    assert p.cost == "950 kr plus moms"
    assert classify_cost(p.cost) == "paid"


def test_missing_structured_cost_is_unknown_even_with_price_in_prose():
    p = parse_detail_page((FIXTURES / "notify/base/detail/anmalan-stadsodling-workshop.html").read_text(encoding="utf-8"))
    assert p.cost is None
    assert classify_cost(p.cost) == "unknown"


def test_cost_flows_into_event_json(runner):
    runner.build("notify/paid")
    data = json.loads((runner.out / "events.json").read_text(encoding="utf-8"))
    assert [e["cost"] for e in data["events"]] == ["950 kr plus moms"]
    runner.build("notify/base")
    data = json.loads((runner.out / "events.json").read_text(encoding="utf-8"))
    assert [e["cost"] for e in data["events"]] == [None]


@pytest.mark.parametrize("cost", [
    "Free", "free.", "Free of charge", "Gratis", "gratis!", "Kostnadsfritt", "Kostnadsfri", "Avgiftsfritt",
    "0", "0 kr", "0kr", "0,00 kr", "0.00 SEK", "0 SEK", "SEK 0", "0:-", "0,-", "0 kronor", "€0", "  0 kr  ",
])
def test_classify_free(cost):
    assert classify_cost(cost) == "free"


@pytest.mark.parametrize("cost", [
    "950 kr plus moms", "100 kr", "1 200 kr", "SEK 500", "500:-", "250 SEK inkl. moms", "Pris: 300 kr", "100",
    "20 €",
])
def test_classify_paid(cost):
    assert classify_cost(cost) == "paid"


@pytest.mark.parametrize("cost", [
    None, "", "   ", "Se hemsidan", "Enligt överenskommelse", "Gratis för studenter, 200 kr för övriga",
    "Free for members, 500 kr for others", "3 dagar", "Ingår i konferensavgiften",
])
def test_classify_missing_or_ambiguous_is_unknown(cost):
    assert classify_cost(cost) == "unknown"
    assert not notify.is_paid(cost)


@pytest.mark.parametrize("cost", ["950 kr plus moms", "Free", None, "Se hemsidan"])
def test_event_cost_roundtrip(cost):
    e = _e(cost=cost)
    d = json.loads(json.dumps(e.to_dict()))
    assert d["cost"] == cost
    assert Event.from_dict(d) == e


def test_legacy_snapshot_without_cost_reads_as_unknown():
    d = _e(cost="100 kr").to_dict()
    del d["cost"]
    e = Event.from_dict(d)
    assert e.cost is None
    assert classify_cost(e.cost) == "unknown"


# --- state migration ------------------------------------------------------

def test_committed_state_is_readable_through_update_snapshot():
    prior = state_mod.load(COMMITTED_STATE)
    assert any("cost" not in v["event"] for v in prior["events"].values())
    # Events absent from the current run go through Event.from_dict for expiry.
    new = state_mod.update_snapshot(json.loads(json.dumps(prior)), [], date(2027, 6, 1), {"parsed_count": 1})
    assert new["last_success"]["date"] == "2027-06-01"


def test_live_state_is_readable():
    # Contents-agnostic: whatever the workflow last committed must load and
    # survive retention (which rebuilds every snapshot via Event.from_dict).
    prior = state_mod.load(LIVE_STATE)
    state_mod.update_snapshot(json.loads(json.dumps(prior)), [], date(2099, 1, 1), {"parsed_count": 1})


def test_committed_state_run_notify_legacy_snapshots(runner):
    shutil.copy(COMMITTED_STATE, runner.state)
    prior = state_mod.load(COMMITTED_STATE)
    events = [Event.from_dict(v["event"]) for v in prior["events"].values()]
    discord, slack = Recorder(), Recorder()
    code, lines, _ = _notify_json(runner, events[:5], discord, slack, today="2026-09-27")
    assert code == 0
    new = json.loads(runner.state.read_text(encoding="utf-8"))
    for uid in (e.uid for e in events[:5]):
        assert new["events"][uid]["slack_notified_hash"] is None
        assert new["events"][uid]["slack_cancellation_notified"] is False
    # Snapshots not in this run are kept (and still readable).
    assert set(prior["events"]) <= set(new["events"])


def test_committed_discord_hash_is_unchanged():
    prior = state_mod.load(COMMITTED_STATE)
    entry = prior["events"][WEBBKURS_UID]
    assert entry["notified_hash"] == "463452d63346c500"
    e = Event.from_dict(entry["event"])
    assert e.material_hash() == "463452d63346c500"
    # A newly parsed cost does not change the Discord hash.
    assert replace(e, cost="950 kr plus moms").material_hash() == "463452d63346c500"


def test_committed_discord_notified_events_plan_no_discord_update():
    prior = state_mod.load(COMMITTED_STATE)
    notified = {uid: v for uid, v in prior["events"].items() if v.get("notified_hash")}
    assert len(notified) == 7
    events = [Event.from_dict(v["event"]) for v in notified.values()]
    msgs = plan(events, prior, date(2026, 9, 27))
    assert [m for m in msgs if m.channel == "discord"] == []


def test_already_discord_notified_paid_event_gets_first_slack_send():
    prior = state_mod.load(COMMITTED_STATE)
    e = replace(Event.from_dict(prior["events"][WEBBKURS_UID]["event"]), cost="950 kr plus moms")
    msgs = plan([e], prior, date(2026, 9, 27))
    assert [(m.channel, m.kind) for m in msgs] == [("slack", "new")]
    assert "Kostnad: 950 kr plus moms" in msgs[0].content


# --- routing --------------------------------------------------------------

def test_routing_matrix():
    events = [
        _e("req-free", True, "Free"),
        _e("req-paid", True, "950 kr plus moms"),
        _e("open-paid", False, "950 kr plus moms"),
        _e("open-free", False, "Free"),
        _e("req-unknown", True, "Se hemsidan"),
        _e("req-missing", True, None),
        _e("req-zero", True, "0,00 kr"),
    ]
    got = {}
    for m in plan(events, {}, date(2026, 9, 21)):
        got.setdefault(m.uid, set()).add(m.channel)
    assert got == {
        "req-free": {"discord"},
        "req-paid": {"discord", "slack"},
        "req-unknown": {"discord"},
        "req-missing": {"discord"},
        "req-zero": {"discord"},
    }


def test_open_events_never_reach_any_channel_even_when_cancelled():
    e = _e("open", False, "950 kr plus moms")
    state = {"events": {"open": {"seen_active": True, "event": e.to_dict()}}}
    assert plan([e], state, date(2026, 9, 21)) == []
    assert plan([replace(e, cancelled=True)], state, date(2026, 9, 21)) == []


def test_cost_update_produces_one_slack_update_and_no_discord():
    e = _e(cost="950 kr plus moms")
    state = {"events": {}}
    for m in plan([e], state, date(2026, 9, 21)):
        mark_sent(state, m)
    changed = replace(e, cost="1 100 kr plus moms")
    msgs = plan([changed], state, date(2026, 9, 21))
    assert [(m.channel, m.kind) for m in msgs] == [("slack", "update")]
    assert "1 100 kr plus moms" in msgs[0].content
    mark_sent(state, msgs[0])
    assert plan([changed], state, date(2026, 9, 21)) == []


def test_free_cost_rewording_does_not_trigger_discord_update():
    e = _e(cost="Free")
    state = {"events": {}}
    for m in plan([e], state, date(2026, 9, 21)):
        mark_sent(state, m)
    assert plan([replace(e, cost="Gratis")], state, date(2026, 9, 21)) == []


def test_cancelled_after_deadline_still_sends_cancel_on_both_channels():
    e = _e(cost="950 kr plus moms", registration_deadline="2026-09-30")
    state = {"events": {}}
    for m in plan([e], state, date(2026, 9, 21)):
        mark_sent(state, m)
    state["events"]["1"].update(seen_active=True, event=e.to_dict())
    msgs = plan([replace(e, cancelled=True)], state, date(2026, 10, 2))
    assert sorted((m.channel, m.kind) for m in msgs) == [("discord", "cancel"), ("slack", "cancel")]
    for m in msgs:
        mark_sent(state, m)
    assert plan([replace(e, cancelled=True)], state, date(2026, 10, 2)) == []


def test_new_notices_skipped_after_deadline_on_both_channels():
    e = _e(cost="950 kr plus moms", registration_deadline="2026-09-20")
    assert plan([e], {}, date(2026, 9, 21)) == []


def test_slack_cancel_only_if_slack_announced():
    e = _e(cost="Free")
    state = {"events": {"1": {"seen_active": True, "notified_hash": e.material_hash(), "event": e.to_dict()}}}
    msgs = plan([replace(e, cancelled=True)], state, date(2026, 9, 21))
    assert [(m.channel, m.kind) for m in msgs] == [("discord", "cancel")]


# --- formatting -----------------------------------------------------------

def test_slack_message_fields():
    e = _e(cost="950 kr plus moms", title="Webbkurs", location="Online", registration_deadline="2026-09-30",
           registration_url="https://forms.office.com/e/abc")
    text = format_slack_message("new", e)
    assert text.startswith("*Nytt avgiftsbelagt evenemang med anmälan:* Webbkurs")
    assert "**" not in text
    assert "Tid: lör 10 okt 2026 10:00–11:00" in text
    assert "Plats: Online" in text
    assert "Kostnad: 950 kr plus moms" in text
    assert "Anmälan: <https://forms.office.com/e/abc>" in text
    assert "Evenemang: <https://www.gu.se/evenemang/x>" in text


def test_slack_message_escapes_mentions_in_scraped_text():
    e = _e(cost="<!here> 100 kr", title="<!channel> Hej & <@U123|x>", location="<!everyone> Rum <#C1>",
           url="https://www.gu.se/evenemang/x?a=1&b=<2>|y")
    text = format_slack_message("new", e)
    for bad in ("<!channel>", "<!here>", "<!everyone>", "<@U123", "<#C1>"):
        assert bad not in text
    assert "&lt;!channel&gt; Hej &amp; &lt;@U123|x&gt;" in text
    assert "Kostnad: &lt;!here&gt; 100 kr" in text
    # Only the deliberate link brackets remain, with the URL itself escaped.
    assert "Evenemang: <https://www.gu.se/evenemang/x?a=1&amp;b=&lt;2&gt;%7Cy>" in text
    assert text.count("<") == 2 and text.count(">") == 2


def test_slack_payload_disables_unfurl_and_escapes(monkeypatch):
    import urllib.request
    captured = {}

    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def fake(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return Ok()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    notify.slack_sender(SLACK)(format_slack_message("new", _e(cost="100 kr", title="<!channel>")))
    assert captured["url"] == SLACK
    assert "<!channel>" not in captured["body"]["text"]
    assert captured["body"]["unfurl_links"] is False


# --- webhook validation & safe errors -------------------------------------

@pytest.mark.parametrize("value", [SLACK, "  " + SLACK + "\n"])
def test_slack_webhook_from_env_accepts(value):
    with patch.dict(os.environ, {"ECO_EVENTS_SLACK_WEBHOOK_URL": value}):
        assert notify.slack_webhook_from_env() == SLACK


@pytest.mark.parametrize("value", [
    "", "   ", "not a url", DISCORD, SLACK.replace("https://", "http://"), SLACK + "/extra",
    SLACK.replace("hooks.slack.com", "hooks.slack.com.evil.example"), SLACK + "?x=1",
])
def test_slack_webhook_from_env_rejects_without_echoing(value):
    with patch.dict(os.environ, {"ECO_EVENTS_SLACK_WEBHOOK_URL": value}):
        with pytest.raises(notify.NotifyError) as ei:
            notify.slack_webhook_from_env()
    if value.strip():
        assert value.strip() not in str(ei.value)
    assert "ECO_EVENTS_SLACK_WEBHOOK_URL" in str(ei.value)


def test_slack_webhook_from_env_missing():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(notify.NotifyError, match="not set"):
            notify.slack_webhook_from_env()


@pytest.mark.parametrize("exc, expect", [
    (lambda: HTTPError(SLACK, 404, "no_service", None, None), "HTTP 404"),
    (lambda: HTTPError(SLACK, 410, "channel_is_archived", None, None), "HTTP 410"),
    (lambda: URLError(OSError("dns " + SLACK)), "unreachable"),
    (lambda: ConnectionResetError(54, "reset " + SLACK), "ConnectionResetError"),
])
def test_slack_sender_errors_never_include_url(monkeypatch, exc, expect):
    import urllib.request

    def boom(*a, **k):
        raise exc()

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(notify.NotifyError) as ei:
        notify.slack_sender(SLACK)("hello")
    assert expect in str(ei.value)
    assert "hooks.slack.com" not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def _429(retry_after):
    h = email.message.Message()
    if retry_after is not None:
        h["Retry-After"] = retry_after
    return HTTPError(SLACK, 429, "Too Many Requests", h, None)


def test_slack_sender_honours_retry_after(monkeypatch):
    import urllib.request
    calls, sleeps = [], []

    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def fake(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise _429("3")
        return Ok()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    notify.slack_sender(SLACK)("hello")
    assert len(calls) == 2
    assert sleeps == [3.0]


def test_slack_sender_rate_limit_persisting_fails_safely(monkeypatch):
    import urllib.request
    sleeps = []

    def fake(req, timeout):
        raise _429("bogus")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    with pytest.raises(notify.NotifyError) as ei:
        notify.slack_sender(SLACK)("hello")
    assert "hooks.slack.com" not in str(ei.value)
    assert sleeps == [2.0, 2.0, 2.0]


def test_slack_sender_caps_retry_after(monkeypatch):
    import urllib.request
    sleeps, calls = [], []

    def fake(req, timeout):
        calls.append(1)
        raise _429("600")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    with pytest.raises(notify.NotifyError):
        notify.slack_sender(SLACK)("hello")
    assert all(s <= 30 for s in sleeps)


# --- end-to-end delivery --------------------------------------------------

def test_paid_event_sent_to_both_then_repeated_run_sends_nothing(runner):
    discord, slack = Recorder(), Recorder()
    code, lines, _ = _send(runner, "notify/paid", discord, slack)
    assert code == 0
    assert len(discord.sent) == 1 and len(slack.sent) == 1
    assert "950 kr plus moms" in slack.sent[0]
    assert summary(lines)["failed_channels"] == []

    for _ in range(2):
        code, lines, _ = _send(runner, "notify/paid", discord, slack)
        assert code == 0
        assert plans(lines) == []
    assert len(discord.sent) == 1 and len(slack.sent) == 1


def test_free_registration_event_is_discord_only(runner):
    discord, slack = Recorder(), Recorder()
    code, _, _ = _send(runner, "notify/base", discord, slack)
    assert code == 0
    assert len(discord.sent) == 1 and slack.sent == []


def test_discord_notified_before_cost_known_then_first_slack_send(runner):
    discord, slack = Recorder(), Recorder()
    _send(runner, "notify/base", discord, slack)
    code, lines, _ = _send(runner, "notify/paid", discord, slack)
    assert code == 0
    assert [(p["channel"], p["plan"]) for p in plans(lines)] == [("slack", "new")]
    assert len(discord.sent) == 1 and len(slack.sent) == 1


def test_cost_update_end_to_end_one_slack_update(runner):
    discord, slack = Recorder(), Recorder()
    _send(runner, "notify/paid", discord, slack)
    code, lines, _ = _send(runner, "notify/paid-price-changed", discord, slack)
    assert code == 0
    assert [(p["channel"], p["plan"]) for p in plans(lines)] == [("slack", "update")]
    assert "1 100 kr plus moms" in slack.sent[-1]
    code, lines, _ = _send(runner, "notify/paid-price-changed", discord, slack)
    assert plans(lines) == []
    assert len(discord.sent) == 1 and len(slack.sent) == 2


def _paid_events(n=3):
    return [_e(f"u{i}", True, f"{100 * (i + 1)} kr", title=f"Kurs {i}") for i in range(n)]


def test_partial_slack_failure_retries_only_remaining_slack(runner):
    events = _paid_events()
    discord, slack = Recorder(), Recorder(fail_on={2})
    code, lines, captured = _notify_json(runner, events, discord, slack)
    assert code == 4
    assert summary(lines)["failed_channels"] == ["slack"]
    assert "slack notification failed" in captured.err
    assert len(discord.sent) == 3  # Discord unaffected by the Slack failure
    assert len(slack.sent) == 1

    st = json.loads(runner.state.read_text())["events"]
    assert [st[f"u{i}"]["slack_notified_hash"] is not None for i in range(3)] == [True, False, False]
    assert all(st[f"u{i}"]["notified_hash"] is not None for i in range(3))

    discord2, slack2 = Recorder(), Recorder()
    code, lines, _ = _notify_json(runner, events, discord2, slack2)
    assert code == 0
    assert sorted((p["channel"], p["uid"]) for p in plans(lines)) == [("slack", "u1"), ("slack", "u2")]
    assert discord2.sent == [] and len(slack2.sent) == 2
    assert slack.sent[0] not in slack2.sent


def test_revoked_slack_webhook_does_not_block_discord(runner):
    events = _paid_events()
    discord = Recorder()
    slack = Recorder(exc=notify.NotifyError("Slack webhook returned HTTP 404"))
    code, lines, captured = _notify_json(runner, events, discord, slack)
    assert code == 4
    assert len(discord.sent) == 3
    assert slack.calls == 1  # the failing channel stops after its first failure
    assert "slack notification failed: Slack webhook returned HTTP 404" in captured.err
    assert "discord notification failed" not in captured.err
    st = json.loads(runner.state.read_text())["events"]
    assert all(st[f"u{i}"]["slack_notified_hash"] is None for i in range(3))


def test_failing_discord_does_not_block_slack(runner):
    events = _paid_events()
    discord = Recorder(exc=RuntimeError("boom"))
    slack = Recorder()
    code, lines, captured = _notify_json(runner, events, discord, slack)
    assert code == 4
    assert summary(lines)["failed_channels"] == ["discord"]
    assert len(slack.sent) == 3
    assert "unexpected RuntimeError during discord delivery" in captured.err


def test_absent_slack_secret_keeps_slack_pending(runner, tmp_path):
    discord, slack = Recorder(), Recorder()
    code, lines, captured = _send(runner, "notify/paid", discord, slack,
                                  env={"ECO_EVENTS_DISCORD_WEBHOOK_URL": DISCORD})
    assert code == 0
    assert "slack webhook missing or invalid" in captured.err
    assert len(discord.sent) == 1 and slack.sent == []
    st = json.loads(runner.state.read_text())["events"]
    (entry,) = st.values()
    assert entry["notified_hash"] is not None
    assert entry["slack_notified_hash"] is None

    # Feed identical to a build that never touches notifications.
    ref = tmp_path / "ref"
    runner.build("notify/paid", out_dir=ref)
    assert (runner.out / "eco-events.ics").read_bytes() == (ref / "eco-events.ics").read_bytes()

    # Installing the secret later delivers only the pending Slack notice.
    code, lines, _ = _send(runner, "notify/paid", discord, slack)
    assert code == 0
    assert [(p["channel"], p["plan"]) for p in plans(lines)] == [("slack", "new")]
    assert len(discord.sent) == 1 and len(slack.sent) == 1


def test_run_with_slack_sentinel_does_not_leak(runner):
    sentinel = "https://hooks.slack.com/services/" + "T0SENTINEL/B0SENTINEL/" + "neverLeakThisSlackSentinel"
    discord_sentinel = "https://discord.com/api/webhooks/" + "99999/" + "test-sentinel-never-leak-this-string"
    env = {"ECO_EVENTS_DISCORD_WEBHOOK_URL": discord_sentinel, "ECO_EVENTS_SLACK_WEBHOOK_URL": sentinel}
    with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = lambda req, timeout: (_ for _ in ()).throw(
            HTTPError(url=req.full_url, code=403, msg="Forbidden " + req.full_url, hdrs=None, fp=None))
        code, lines, captured = runner.run("notify/paid", mode="send")
        assert code == 4
        assert summary(lines)["failed_channels"] == ["discord", "slack"]
        assert {r.args[0].full_url.split("?")[0] for r in mock_urlopen.call_args_list} == {discord_sentinel, sentinel}

        for text in (captured.out, captured.err, runner.state.read_text(encoding="utf-8"),
                     (runner.out / "eco-events.ics").read_text(encoding="utf-8"),
                     (runner.out / "events.json").read_text(encoding="utf-8")):
            assert sentinel not in text
            assert "hooks.slack.com" not in text

        args = argparse.Namespace(tracked=False, env=list(env), paths=[str(runner.out), str(runner.state)])
        assert cmd_check_leaks(args) == 0


def test_check_leaks_finds_slack_secret_value(runner):
    runner.build("baseline")
    (runner.out / "events.json").write_text("opaque-slack-secret-value", encoding="utf-8")
    args = argparse.Namespace(tracked=False, env=["ECO_EVENTS_SLACK_WEBHOOK_URL"], paths=[str(runner.out)])
    with patch.dict(os.environ, {"ECO_EVENTS_SLACK_WEBHOOK_URL": "opaque-slack-secret-value"}):
        assert cmd_check_leaks(args) == 1


def test_run_notify_does_not_mutate_callers_senders(tmp_path):
    senders = {"discord": lambda c: None}
    with patch.dict(os.environ, {}, clear=True):
        pipeline.run_notify([_e(cost="100 kr")], {"parsed_count": 1}, tmp_path / "s.json",
                            date(2026, 9, 21), "send", senders=senders, out=open(os.devnull, "w"))
    assert list(senders) == ["discord"]
