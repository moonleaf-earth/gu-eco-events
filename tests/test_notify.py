import pytest
from datetime import date
from gu_eco_events.notify import plan
from gu_eco_events.model import Event

def test_notify_skipped_on_passed_deadline():
    e = Event(
        uid="1", url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
        location="L", cost=None, online=False, cancelled=False, registration_required=True, registration_url=None,
        registration_deadline="2026-09-20", description="", last_modified=None, source_id="", categories=()
    )

    # Run with today > deadline but < event start
    messages = plan([e], {}, date(2026, 9, 21))
    assert len(messages) == 0

    # Run with today = deadline
    messages = plan([e], {}, date(2026, 9, 20))
    assert len(messages) == 1

def test_notify_skipped_for_cancelled_no_registration():
    e = Event(
        uid="1", url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
        location="L", cost=None, online=False, cancelled=True, registration_required=False, registration_url=None,
        registration_deadline=None, description="", last_modified=None, source_id="", categories=()
    )

    state = {
        "events": {
            "1": {
                "seen_active": True,
                "cancellation_notified": False,
                "event": {"registration_required": False}
            }
        }
    }

    messages = plan([e], state, date(2026, 9, 21))
    assert len(messages) == 0

def test_notify_sent_for_cancelled_with_registration():
    e = Event(
        uid="1", url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
        location="L", cost=None, online=False, cancelled=True, registration_required=True, registration_url=None,
        registration_deadline=None, description="", last_modified=None, source_id="", categories=()
    )

    state = {
        "events": {
            "1": {
                "seen_active": True,
                "cancellation_notified": False,
                "event": {"registration_required": True}
            }
        }
    }

    messages = plan([e], state, date(2026, 9, 21))
    assert len(messages) == 1
    assert messages[0].kind == "cancel"


WEBHOOK = "https://discord.com/api/webhooks/123/secret-token-value"


class _Resp:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        raise self.exc


@pytest.mark.parametrize("make", [
    lambda: ConnectionResetError(54, "Connection reset by peer"),
    lambda: __import__("http.client").client.RemoteDisconnected("Remote end closed connection"),
    lambda: __import__("ssl").SSLError("bad record mac"),
])
def test_discord_sender_wraps_getresponse_errors(monkeypatch, make):
    import urllib.request
    from gu_eco_events.notify import NotifyError, discord_sender

    def boom(*a, **k):
        raise make()

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(NotifyError) as ei:
        discord_sender(WEBHOOK)("hello")
    assert "secret-token-value" not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_discord_sender_wraps_incomplete_read(monkeypatch):
    import http.client
    import urllib.request
    from gu_eco_events.notify import NotifyError, discord_sender

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(http.client.IncompleteRead(b"")))
    with pytest.raises(NotifyError) as ei:
        discord_sender(WEBHOOK)("hello")
    assert "IncompleteRead" in str(ei.value)
    assert "secret-token-value" not in str(ei.value)

def test_notify_routing_rules():
    from datetime import date
    from gu_eco_events.notify import plan
    from gu_eco_events.model import Event

    def _e(uid, req, cost):
        return Event(
            uid=uid, url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
            location="L", cost=cost, online=False, cancelled=False, registration_required=req, registration_url=None,
            registration_deadline=None, description="", last_modified=None, source_id="", categories=()
        )

    e_req_free = _e("1", True, "Free")
    e_req_paid = _e("2", True, "100 kr")
    e_open_paid = _e("3", False, "100 kr")
    e_open_free = _e("4", False, "Free")
    e_unknown = _e("5", True, "Se hemsidan")

    events = [e_req_free, e_req_paid, e_open_paid, e_open_free, e_unknown]
    state = {}
    today = date(2026, 9, 21)

    messages = plan(events, state, today)

    # 1 Discord (req_free), 1 Discord (req_paid), 1 Discord (unknown)
    assert len(messages) == 3
    uids = {m.uid: m for m in messages}

    assert "1" in uids
    assert "Kostnad:" not in uids["1"].content

    assert "2" in uids
    assert "Kostnad: 100 kr" in uids["2"].content

    assert "3" not in uids
    assert "4" not in uids

    assert "5" in uids
    assert "Kostnad:" not in uids["5"].content

def test_discord_cost_added_update():
    from datetime import date
    from gu_eco_events.notify import plan
    from gu_eco_events.model import Event

    e = Event(
        uid="1", url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
        location="L", cost="100 kr", online=False, cancelled=False, registration_required=True, registration_url=None,
        registration_deadline=None, description="", last_modified=None, source_id="", categories=()
    )

    # Previously notified without cost
    state = {
        "events": {
            "1": {
                "notified_hash": e.material_hash()
            }
        }
    }
    today = date(2026, 9, 21)

    messages = plan([e], state, today)
    assert len(messages) == 1
    assert messages[0].channel == "discord"
    assert messages[0].kind == "update"
    assert "Kostnad: 100 kr" in messages[0].content

def test_discord_cost_changed_update():
    from datetime import date
    from gu_eco_events.notify import plan
    from gu_eco_events.model import Event

    e = Event(
        uid="1", url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
        location="L", cost="200 kr", online=False, cancelled=False, registration_required=True, registration_url=None,
        registration_deadline=None, description="", last_modified=None, source_id="", categories=()
    )

    # Notified with old paid cost
    state = {
        "events": {
            "1": {
                "notified_hash": "old_hash"
            }
        }
    }
    today = date(2026, 9, 21)

    messages = plan([e], state, today)
    assert len(messages) == 1
    assert messages[0].channel == "discord"
    assert messages[0].kind == "update"
    assert "Kostnad: 200 kr" in messages[0].content
