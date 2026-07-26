"""Unified event record and thread-safe event log.

Every event in the system -- a marker punched into a live board, an annotation
re-emitted by a replay, a stimulation command we dispatched ourselves -- lands
in the same table with the same shape. That is the whole point: after a
closed-loop run you want one chronological list showing what the brain did and
what we did to it, without joining two clocks.

``origin`` is what distinguishes them:

* ``marker``     -- came off the board's marker channel (live)
* ``annotation`` -- came from the replayed recording's annotations
* ``stim``       -- we dispatched it to a stimulation backend
* ``manual``     -- an agent or operator logged it by hand
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from typing import Any

ORIGINS = ("marker", "annotation", "stim", "manual")


@dataclass
class EventRecord:
    """One timestamped thing that happened."""

    event_id: int
    timestamp: float  # Unix epoch seconds, same clock as sample timestamps
    label: str
    origin: str
    value: float | None = None
    duration: float = 0.0
    # Index into the stream's monotonic sample counter, when known. Lets an
    # event be located exactly within a window even if clocks drift.
    sample_index: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "label": self.label,
            "origin": self.origin,
            "value": self.value,
            "duration": self.duration,
            "sample_index": self.sample_index,
            "payload": self.payload,
        }


class EventLog:
    """Append-only, thread-safe, capped event history for one session."""

    def __init__(self, max_events: int = 100_000) -> None:
        self._events: list[EventRecord] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._max_events = max_events

    def append(
        self,
        label: str,
        origin: str,
        timestamp: float,
        value: float | None = None,
        duration: float = 0.0,
        sample_index: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        if origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}, got '{origin}'")
        with self._lock:
            record = EventRecord(
                event_id=next(self._ids),
                timestamp=timestamp,
                label=label,
                origin=origin,
                value=value,
                duration=duration,
                sample_index=sample_index,
                payload=payload or {},
            )
            self._events.append(record)
            if len(self._events) > self._max_events:
                # Drop oldest in one slice rather than popping repeatedly.
                del self._events[: len(self._events) - self._max_events]
            return record

    def extend(self, records: list[EventRecord]) -> None:
        """Bulk-append records already built by a source (keeps their ids)."""
        if not records:
            return
        with self._lock:
            self._events.extend(records)
            if len(self._events) > self._max_events:
                del self._events[: len(self._events) - self._max_events]

    def next_id(self) -> int:
        return next(self._ids)

    def query(
        self,
        since: float | None = None,
        until: float | None = None,
        origin: str | None = None,
        label: str | None = None,
        limit: int = 500,
    ) -> list[EventRecord]:
        """Chronological slice of the log, most-recent-capped at ``limit``."""
        with self._lock:
            out = list(self._events)
        if since is not None:
            out = [e for e in out if e.timestamp >= since]
        if until is not None:
            out = [e for e in out if e.timestamp <= until]
        if origin is not None:
            out = [e for e in out if e.origin == origin]
        if label is not None:
            out = [e for e in out if e.label == label]
        return out[-limit:] if limit > 0 else out

    def counts_by_label(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for e in self._events:
                out[e.label] = out.get(e.label, 0) + 1
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
