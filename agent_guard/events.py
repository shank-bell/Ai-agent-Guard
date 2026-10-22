"""Audit events and the append-only audit log.

Trust model (this is what makes the monitor hard to fool):
  * The log, not the agent, stamps the time, so an agent cannot back-date its own events.
  * Human-approval events carry an HMAC signature made with a secret that only the approval
    service holds. An agent can write any line it likes, but it cannot sign one as the approval service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from .clock import Clock, RealClock

APPROVAL_SOURCE = "approval-service"
APPROVAL_EVENT_TYPES = ("approval_requested", "approval_decided")
# Demo default only. In real use, set AGENT_GUARD_APPROVAL_SECRET on the approval service and the monitor.
DEFAULT_SECRET = "demo-approval-secret-change-me"


def approval_secret() -> str:
    return os.environ.get("AGENT_GUARD_APPROVAL_SECRET", DEFAULT_SECRET)


@dataclass
class Event:
    type: str                      # task_start | task_end | decision | data_access | approval_requested | approval_decided
    agent_id: str
    source: str                    # who wrote the line: the agent's own id, or "approval-service"
    ts: str = ""                   # ISO-8601 UTC, stamped by the log
    task_id: str = ""
    data: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sig: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def from_json(line: str) -> "Event":
        raw = json.loads(line)
        return Event(**{k: raw.get(k, d) for k, d in (
            ("type", ""), ("agent_id", ""), ("source", ""), ("ts", ""), ("task_id", ""),
            ("data", {}), ("id", ""), ("sig", ""))})


def sign(event: Event, secret: str) -> str:
    body = asdict(event)
    body.pop("sig", None)
    msg = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def signature_valid(event: Event, secret: str) -> bool:
    return bool(event.sig) and hmac.compare_digest(event.sig, sign(event, secret))


class AuditLog:
    """JSON-lines file that agents append to and the monitor reads."""

    def __init__(self, path: str | Path, clock: Clock | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.clock = clock or RealClock()
        self._lock = threading.Lock()

    def emit(self, type: str, agent_id: str, *, source: str | None = None, task_id: str = "",
             data: dict | None = None, secret: str | None = None) -> Event:
        ev = Event(type=type, agent_id=agent_id, source=source or agent_id, task_id=task_id, data=data or {})
        ev.ts = self.clock.now().isoformat()          # stamped here, never by the agent
        if secret:
            ev.sig = sign(ev, secret)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(ev.to_json() + "\n")
        return ev

    def read_all(self) -> list[Event]:
        events, _ = self.read_from(0)
        return events

    def read_from(self, offset: int) -> tuple[list[Event], int]:
        """Return events written after byte `offset` and the new offset. Used by the live monitor."""
        events: list[Event] = []
        with self.path.open("rb") as fh:
            fh.seek(offset)
            while True:
                start = fh.tell()
                line = fh.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):          # half-written line, pick it up next poll
                    fh.seek(start)
                    break
                text = line.decode("utf-8").strip()
                if text:
                    try:
                        events.append(Event.from_json(text))
                    except (ValueError, TypeError):
                        pass                          # a corrupt line must not stop the monitor
            return events, fh.tell()

    def __iter__(self) -> Iterator[Event]:
        return iter(self.read_all())
