"""Clocks. Scenarios use SimClock so "5 minutes of work" runs instantly but is stamped as 5 minutes."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ScaledClock(Clock):
    """Real time sped up by `speed`: a 300 s sleep takes 300/speed real seconds, and timestamps show the full 300 s.
    Used by the live demo so a second window can watch the agents misbehave as it happens."""

    def __init__(self, start: datetime, speed: float):
        self._start, self._t0, self.speed = start, time.monotonic(), speed

    def now(self) -> datetime:
        return self._start + timedelta(seconds=(time.monotonic() - self._t0) * self.speed)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds / self.speed)


class SimClock(Clock):
    """Virtual time: sleep() just moves the clock forward."""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        self._now = when
