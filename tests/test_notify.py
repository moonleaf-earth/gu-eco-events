import pytest
from datetime import date
from gu_eco_events.notify import plan
from gu_eco_events.model import Event

def test_notify_skipped_on_passed_deadline():
    e = Event(
        uid="1", url="http", title="T", all_day=False, start="2026-10-10T10:00:00", end="2026-10-10T11:00:00",
        location="L", online=False, cancelled=False, registration_required=True, registration_url=None,
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
        location="L", online=False, cancelled=True, registration_required=False, registration_url=None,
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
        location="L", online=False, cancelled=True, registration_required=True, registration_url=None,
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
