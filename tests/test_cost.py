"""Structured cost: parsing, classification, state compatibility, and the
Discord cost line. Also guards that no Slack integration remains."""

import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from conftest import FIXTURES, plans
from gu_eco_events import notify, pipeline
from gu_eco_events import state as state_mod
from gu_eco_events.model import Event
from gu_eco_events.normalize import registration
from gu_eco_events.notify import classify_cost, mark_sent, plan
from gu_eco_events.source.gu import parse_detail_page

ROOT = Path(__file__).resolve().parents[1]
# Frozen copy of data/state.json as committed before cost was parsed. The
# live file is rewritten by every workflow run, so tests must not pin it.
COMMITTED_STATE = FIXTURES / "legacy-state" / "state.json"
LIVE_STATE = ROOT / "data" / "state.json"
DISCORD = "https://discord.com/api/webhooks/123/abc"
WEBBKURS_UID = "gu-node-190572-1@gu-eco-events.moonleaf-earth.github.io"
TODAY = date(2026, 9, 21)


def _e(uid="1", req=True, cost=None, **kw):
    base = dict(
        uid=uid, url="https://www.gu.se/evenemang/x", title="T", all_day=False,
        start="2026-10-10T10:00:00+02:00", end="2026-10-10T11:00:00+02:00", location="L", cost=cost,
        online=False, cancelled=False, registration_required=req, registration_url=None,
        registration_deadline=None, description="", last_modified=None, source_id="", categories=(),
    )
    base.update(kw)
    return Event(**base)


def _cost_lines(content):
    return [l for l in content.splitlines() if l.startswith("Kostnad:")]


def _real(name):
    return parse_detail_page((FIXTURES / "real" / name).read_text(encoding="utf-8"))


def _from_detail(p):
    """Event carrying the real page's structured fields (the Skogaryd venue is
    outside the feed's location rule, so build_events would reject it)."""
    req, reg_url = registration(p)
    deadline = p.registration_deadline.isoformat() if p.registration_deadline else None
    return _e(cost=p.cost, req=req, title=p.title, location=p.location, registration_url=reg_url,
              registration_deadline=deadline)


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(pipeline, "SEND_DELAY_SECONDS", 0)


# --- real fixtures ----------------------------------------------------------

def test_real_fixture_free_registration_has_no_cost_line():
    p = _real("skogaryd-research-centre-platsbesok.html")
    assert p.cost == "Free"
    assert registration(p)[0] is True
    assert classify_cost(p.cost) == "free"
    (msg,) = plan([_from_detail(p)], {}, TODAY)
    assert msg.channel == "discord"
    assert _cost_lines(msg.content) == []


def test_real_fixture_paid_registration_has_exact_cost_line():
    p = _real("webbkurs-for-foretagare-hantverk-i-digital-form.html")
    assert p.cost == "950 kr plus moms"
    assert registration(p)[0] is True
    assert classify_cost(p.cost) == "paid"
    (msg,) = plan([_from_detail(p)], {}, TODAY)
    assert msg.channel == "discord"
    assert _cost_lines(msg.content) == ["Kostnad: 950 kr plus moms"]


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


# --- classification ---------------------------------------------------------

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
    "0 kr för studenter, 200 kr för övriga", "0 kr för medlemmar, 300 kr för övriga", "0-200 kr",
])
def test_classify_missing_or_ambiguous_is_unknown(cost):
    assert classify_cost(cost) == "unknown"
    assert not notify.is_paid(cost)


# --- model / state compatibility --------------------------------------------

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


def test_update_snapshot_writes_no_slack_markers():
    prior = state_mod.load(COMMITTED_STATE)
    events = [Event.from_dict(v["event"]) for v in prior["events"].values()]
    new = state_mod.update_snapshot(json.loads(json.dumps(prior)), events, date(2026, 9, 27), {"parsed_count": 1})
    assert not any(k.startswith("slack") for v in new["events"].values() for k in v)


def test_legacy_slack_markers_are_ignored():
    e = _e(cost="100 kr")
    state = {"events": {"1": {"notified_hash": e.material_hash_with_cost(), "event": e.to_dict(),
                              "slack_notified_hash": "x", "slack_cancellation_notified": True}}}
    assert plan([e], state, TODAY) == []


# --- Discord cost line & update semantics -----------------------------------

def test_discord_routing_matrix():
    events = [
        _e("req-paid", True, "950 kr plus moms"),
        _e("req-free", True, "Free"),
        _e("req-unknown", True, "Se hemsidan"),
        _e("req-missing", True, None),
        _e("req-zero", True, "0,00 kr"),
        _e("open-paid", False, "950 kr plus moms"),
        _e("open-free", False, "Free"),
    ]
    got = {m.uid: m for m in plan(events, {}, TODAY)}
    assert set(got) == {"req-paid", "req-free", "req-unknown", "req-missing", "req-zero"}
    assert all(m.channel == "discord" for m in got.values())
    assert _cost_lines(got["req-paid"].content) == ["Kostnad: 950 kr plus moms"]
    for uid in ("req-free", "req-unknown", "req-missing", "req-zero"):
        assert _cost_lines(got[uid].content) == []


def test_open_paid_event_never_announced_even_when_cancelled():
    e = _e("open", False, "950 kr plus moms")
    assert plan([e], {}, TODAY) == []
    state = {"events": {"open": {"seen_active": True, "event": e.to_dict()}}}
    assert plan([e], state, TODAY) == []
    assert plan([replace(e, cancelled=True)], state, TODAY) == []


def test_unchanged_repeat_is_not_duplicated():
    e = _e(cost="950 kr plus moms")
    state = {"events": {}}
    (msg,) = plan([e], state, TODAY)
    mark_sent(state, msg)
    assert plan([e], state, TODAY) == []


def test_paid_cost_change_produces_one_update():
    e = _e(cost="950 kr plus moms")
    state = {"events": {}}
    for m in plan([e], state, TODAY):
        mark_sent(state, m)
    changed = replace(e, cost="1 100 kr plus moms")
    (msg,) = plan([changed], state, TODAY)
    assert (msg.channel, msg.kind) == ("discord", "update")
    assert _cost_lines(msg.content) == ["Kostnad: 1 100 kr plus moms"]
    mark_sent(state, msg)
    assert plan([changed], state, TODAY) == []


def test_free_cost_rewording_does_not_trigger_update():
    e = _e(cost="Free")
    state = {"events": {}}
    for m in plan([e], state, TODAY):
        mark_sent(state, m)
    assert plan([replace(e, cost="Gratis")], state, TODAY) == []
    assert plan([replace(e, cost=None)], state, TODAY) == []


def test_committed_hashes_without_cost_are_stable():
    prior = state_mod.load(COMMITTED_STATE)
    entry = prior["events"][WEBBKURS_UID]
    e = Event.from_dict(entry["event"])
    assert e.cost is None
    assert entry["notified_hash"] == e.material_hash() == "463452d63346c500"
    notified = [Event.from_dict(v["event"]) for v in prior["events"].values() if v.get("notified_hash")]
    assert len(notified) == 7
    assert plan(notified, prior, date(2026, 9, 27)) == []


def test_legacy_paid_event_gets_one_cost_update_then_stable():
    state = state_mod.load(COMMITTED_STATE)
    e = replace(Event.from_dict(state["events"][WEBBKURS_UID]["event"]), cost="950 kr plus moms")
    today = date(2026, 9, 27)
    (msg,) = plan([e], state, today)
    assert (msg.channel, msg.kind) == ("discord", "update")
    assert _cost_lines(msg.content) == ["Kostnad: 950 kr plus moms"]
    mark_sent(state, msg)
    assert plan([e], state, today) == []


def test_cancelled_paid_event_cancel_keeps_cost_line():
    e = _e(cost="950 kr plus moms", registration_deadline="2026-09-30")
    state = {"events": {}}
    for m in plan([e], state, TODAY):
        mark_sent(state, m)
    state["events"]["1"].update(seen_active=True, event=e.to_dict())
    (msg,) = plan([replace(e, cancelled=True)], state, date(2026, 10, 2))
    assert (msg.channel, msg.kind) == ("discord", "cancel")
    assert _cost_lines(msg.content) == ["Kostnad: 950 kr plus moms"]
    mark_sent(state, msg)
    assert plan([replace(e, cancelled=True)], state, date(2026, 10, 2)) == []


# --- end to end ---------------------------------------------------------------

def _send(runner, fixture, sent, env=None):
    env = {"ECO_EVENTS_DISCORD_WEBHOOK_URL": DISCORD} if env is None else env
    with patch.dict(os.environ, env, clear=True), \
         patch("gu_eco_events.notify.discord_sender", return_value=sent.append):
        return runner.run(fixture, mode="send")


def test_paid_fixture_sent_once_with_cost(runner):
    sent = []
    code, _, _ = _send(runner, "notify/paid", sent)
    assert code == 0
    assert len(sent) == 1
    assert _cost_lines(sent[0]) == ["Kostnad: 950 kr plus moms"]
    for _ in range(2):
        code, lines, _ = _send(runner, "notify/paid", sent)
        assert code == 0 and plans(lines) == []
    assert len(sent) == 1


def test_cost_learned_after_announcement_sends_one_update(runner):
    sent = []
    _send(runner, "notify/base", sent)
    assert _cost_lines(sent[0]) == []
    code, lines, _ = _send(runner, "notify/paid", sent)
    assert code == 0
    assert [p["plan"] for p in plans(lines)] == ["update"]
    assert _cost_lines(sent[-1]) == ["Kostnad: 950 kr plus moms"]
    code, lines, _ = _send(runner, "notify/paid", sent)
    assert plans(lines) == [] and len(sent) == 2


def test_cost_change_end_to_end_one_update(runner):
    sent = []
    _send(runner, "notify/paid", sent)
    code, lines, _ = _send(runner, "notify/paid-price-changed", sent)
    assert code == 0
    assert [(p["channel"], p["plan"]) for p in plans(lines)] == [("discord", "update")]
    assert _cost_lines(sent[-1]) == ["Kostnad: 1 100 kr plus moms"]
    code, lines, _ = _send(runner, "notify/paid-price-changed", sent)
    assert plans(lines) == [] and len(sent) == 2


# --- no Slack integration -----------------------------------------------------

class _RecordingEnv(dict):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.read = set()

    def __getitem__(self, k):
        self.read.add(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.read.add(k)
        return super().get(k, default)

    def __contains__(self, k):
        self.read.add(k)
        return super().__contains__(k)


def test_no_slack_webhook_read_or_sent(runner, monkeypatch):
    slack = "https://hooks.slack.com/services/" + "T0SENTINEL/B0SENTINEL/" + "neverReadThisSlackSentinel"
    env = _RecordingEnv({"ECO_EVENTS_DISCORD_WEBHOOK_URL": DISCORD, "ECO_EVENTS_SLACK_WEBHOOK_URL": slack})
    monkeypatch.setattr(os, "environ", env)
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = HTTPError(url=DISCORD, code=403, msg="Forbidden", hdrs=None, fp=None)
        code, lines, captured = runner.run("notify/paid", mode="send")
    assert code == 4
    assert "ECO_EVENTS_DISCORD_WEBHOOK_URL" in env.read
    assert not any("SLACK" in k for k in env.read)
    urls = {c.args[0].full_url for c in mock_urlopen.call_args_list}
    assert urls and all(u.startswith(DISCORD) for u in urls)
    assert "hooks.slack.com" not in captured.out + captured.err


def test_no_slack_code_paths():
    assert pipeline.CHANNELS == ("discord",)
    for mod in (notify, pipeline, state_mod):
        assert not [n for n in dir(mod) if "slack" in n.lower()], mod.__name__
    assert {m.channel for m in plan([_e(cost="950 kr plus moms")], {}, TODAY)} == {"discord"}


def test_workflow_and_readme_have_no_slack_secret():
    for path in (ROOT / ".github/workflows/publish.yml", ROOT / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert "ECO_EVENTS_SLACK_WEBHOOK_URL" not in text, path
        assert "slack" not in text.lower(), path
