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
