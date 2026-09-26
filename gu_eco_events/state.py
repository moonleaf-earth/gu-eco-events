"""Durable JSON state projection (committed as data/state.json).

Per UID: last event snapshot, material/content hash, notification markers.
Plus `last_success` counts used by the severe-drop guard. Contains no
secrets and nothing that is not already public on gu.se.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import config
from .model import Event

SCHEMA = 1


def empty() -> dict:
    return {"schema": SCHEMA, "last_success": None, "events": {}}


def load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return empty()
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"unsupported state schema {data.get('schema')!r} in {p}")
    data.setdefault("events", {})
    return data


def dumps(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write(path: str | Path, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save(path: str | Path, state: dict) -> None:
    atomic_write(path, dumps(state).encode("utf-8"))


def update_snapshot(state: dict, events: list[Event], today: date, counts: dict) -> dict:
    """Record the current events and success counts. Notification markers
    are left untouched here; see notify.mark_sent."""
    evs = state.setdefault("events", {})
    for e in events:
        entry = evs.setdefault(e.uid, {"first_seen": today.isoformat()})
        entry["event"] = e.to_dict()
        entry["material_hash"] = e.material_hash()
        entry["content_hash"] = e.content_hash()
        entry["last_seen"] = today.isoformat()
        if not e.cancelled:
            entry["seen_active"] = True
        entry.setdefault("notified_hash", None)
        entry.setdefault("cancellation_notified", False)
    cutoff = today - timedelta(days=config.STATE_RETENTION_DAYS)
    for uid in list(evs):
        ev = evs[uid].get("event")
        if ev and Event.from_dict(ev).end_local_date() < cutoff:
            del evs[uid]
    state["last_success"] = {"date": today.isoformat(), **counts}
    return state
