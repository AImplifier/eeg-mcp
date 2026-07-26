"""Stream sessions: the object every MCP tool operates on.

An MCP tool call is stateless, but a stream is the opposite -- it exists between
calls, produces data whether or not anyone is asking, and its filters carry
state forward. So a background thread owns the stream and tool calls query it.

**Why filtering happens in the producer thread, not at query time.** A stateful
IIR filter must see every sample exactly once, in order. If tools filtered their
own snapshots, overlapping queries would re-filter the same samples and the
carried state would be nonsense. So the producer filters each chunk once as it
arrives and writes the result to a second ring buffer; queries just read.

That is also why there are two buffers: ``raw`` (what the amplifier said) and
``filtered`` (what the chain made of it). Keeping both means you can always go
back and check whether a feature is real or a filter artifact.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .config import get_config
from .dsp import FilterChain, FilterSpec
from .events import EventLog
from .ring import RingBuffer
from .sources.base import StreamSource
from .stim.base import StimBackend, StimCommand
from .utils import now


@dataclass
class StreamSession:
    """One live or replayed stream, with its buffers, filters, and stim targets."""

    session_id: str
    source: StreamSource
    raw_buffer: RingBuffer
    events: EventLog
    chain: FilterChain
    filtered_buffer: RingBuffer | None = None
    stim_backends: dict[str, StimBackend] = field(default_factory=dict)
    default_stim_backend: str | None = None
    history: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Runtime bookkeeping, written by the producer thread.
    started_at: float | None = None
    stopped_at: float | None = None
    last_poll_at: float | None = None
    last_error: str | None = None
    poll_count: int = 0
    samples_received: int = 0
    exhausted: bool = False

    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- lifecycle --------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.history.append(f"[{stamp}] {message}")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self.exhausted = False
        self.last_error = None
        self.source.start()
        self.started_at = now()
        self.stopped_at = None
        self._thread = threading.Thread(
            target=self._run, name=f"eeg-mcp-{self.session_id}", daemon=True
        )
        self._thread.start()
        self.log(f"started {self.source.info.kind} source '{self.source.info.name}'")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            # Generous relative to the poll interval, but bounded: a wedged
            # device must not hang the whole MCP server.
            thread.join(timeout=5.0)
        self._thread = None
        try:
            self.source.stop()
        finally:
            self.stopped_at = now()
            for backend in list(self.stim_backends.values()):
                try:
                    backend.close()
                except Exception:
                    pass
            self.log("stopped")

    def _run(self) -> None:
        """Producer loop: drain the source, filter once, store, record events."""
        interval = get_config().poll_interval_sec
        while not self._stop.is_set():
            try:
                chunk = self.source.poll()
                self.last_poll_at = now()
                self.poll_count += 1

                if chunk.n_samples:
                    with self._lock:
                        before = self.raw_buffer.total_written
                        self.raw_buffer.write(chunk.data, chunk.timestamps)
                        self.samples_received += chunk.n_samples
                        if self.filtered_buffer is not None and len(self.chain):
                            filtered = self.chain.process(chunk.data)
                            self.filtered_buffer.write(filtered, chunk.timestamps)

                    for event in chunk.events:
                        offset = int(
                            np.searchsorted(chunk.timestamps, event.timestamp)
                        )
                        self.events.append(
                            label=event.label,
                            origin=event.origin,
                            timestamp=event.timestamp,
                            value=event.value,
                            duration=event.duration,
                            sample_index=before + min(offset, chunk.n_samples - 1),
                            payload=event.payload,
                        )
                elif chunk.events:
                    for event in chunk.events:
                        self.events.append(
                            label=event.label, origin=event.origin,
                            timestamp=event.timestamp, value=event.value,
                            duration=event.duration, payload=event.payload,
                        )

                if chunk.exhausted:
                    self.exhausted = True
                    self.log("source exhausted")
                    break
            except Exception:
                # A source failure must not kill the server silently; record it
                # and stop cleanly so status() can explain what happened.
                self.last_error = traceback.format_exc(limit=5)
                self.log(f"producer error: {self.last_error.splitlines()[-1]}")
                break
            self._stop.wait(interval)

    # --- filtering --------------------------------------------------------

    def set_filters(self, specs: list[FilterSpec]) -> dict[str, Any]:
        """Replace the filter chain and re-arm the filtered buffer."""
        with self._lock:
            chain = FilterChain(self.source.info.n_channels, self.source.info.sfreq)
            for spec in specs:
                chain.add(spec)
            self.chain = chain
            if specs:
                if self.filtered_buffer is None:
                    # Start its absolute counter where the raw buffer currently
                    # stands, so a sample's index means the same in both.
                    self.filtered_buffer = RingBuffer(
                        self.raw_buffer.n_channels,
                        self.raw_buffer.capacity,
                        start_index=self.raw_buffer.total_written,
                    )
                else:
                    self.filtered_buffer.clear()
            else:
                self.filtered_buffer = None
        self.log(f"filter chain set: {len(specs)} stage(s)")
        return self.chain.describe()

    # --- reading ----------------------------------------------------------

    def snapshot(
        self, seconds: float, filtered: bool = True
    ) -> tuple[np.ndarray, np.ndarray, bool]:
        """Most recent ``seconds`` of signal as (data, timestamps, was_filtered)."""
        n = int(round(seconds * self.source.info.sfreq))
        if n <= 0:
            raise ValueError(f"seconds must be > 0, got {seconds}")
        use_filtered = filtered and self.filtered_buffer is not None and len(self.chain)
        buffer = self.filtered_buffer if use_filtered else self.raw_buffer
        data, timestamps = buffer.read_last(n)
        if data.shape[1] == 0:
            # Distinguish "not yet" from "never": telling a user to wait for a
            # buffer that will never fill because the producer is stopped sends
            # them into a pointless retry loop.
            if self.is_running:
                raise ValueError(
                    f"No samples buffered yet for session '{self.session_id}'. "
                    "Give the stream a moment to fill, then try again."
                )
            detail = (
                f" Last error: {self.last_error.splitlines()[-1]}"
                if self.last_error else ""
            )
            raise ValueError(
                f"Session '{self.session_id}' is not running and its buffer is "
                "empty, so there is nothing to read. Note that seeking clears "
                "buffered signal, so seeking and then stopping leaves nothing "
                f"behind.{detail}"
            )
        return data, timestamps, bool(use_filtered)

    # --- stimulation ------------------------------------------------------

    def attach_backend(self, key: str, backend: StimBackend, make_default: bool = True):
        backend.open()
        # A BrainFlow marker backend needs the live source to write through.
        if hasattr(backend, "bind"):
            backend.bind(self.source.insert_marker)
        with self._lock:
            existing = self.stim_backends.get(key)
            if existing is not None:
                try:
                    existing.close()
                except Exception:
                    pass
            self.stim_backends[key] = backend
            if make_default or self.default_stim_backend is None:
                self.default_stim_backend = key
        self.log(f"attached stim backend '{key}' ({backend.name})")
        return backend

    def resolve_backend(self, key: str | None = None) -> StimBackend:
        name = key or self.default_stim_backend
        if name is None:
            raise ValueError(
                f"Session '{self.session_id}' has no stimulation backend attached. "
                "Call attach_stim_backend first (use backend='log' for a dry run)."
            )
        backend = self.stim_backends.get(name)
        if backend is None:
            raise KeyError(
                f"No stim backend '{name}' on session '{self.session_id}'. "
                f"Attached: {sorted(self.stim_backends) or 'none'}"
            )
        return backend

    def dispatch_stim(
        self, command: StimCommand, backend_key: str | None = None,
        also_mark_source: bool = True,
    ) -> dict[str, Any]:
        """Deliver a stimulation command and record it in the event log.

        The event is logged whether or not the transport succeeded, because a
        failed stimulation attempt is exactly as important to the record as a
        successful one.
        """
        backend = self.resolve_backend(backend_key)
        marked_in_source = False
        try:
            result = backend.send(command)
        except Exception as exc:
            self.events.append(
                label=command.label, origin="stim", timestamp=now(),
                value=command.value, duration=command.duration_ms / 1000.0,
                sample_index=self.raw_buffer.total_written,
                payload={"backend": backend.name, "error": str(exc),
                         "delivered": False, "command": command.as_dict()},
            )
            raise

        # Also punch it into the acquisition stream itself when possible, so
        # the recording carries the event even without the session log.
        if also_mark_source and backend.name != "brainflow_marker":
            try:
                marked_in_source = bool(
                    self.source.insert_marker(command.value, command.label)
                )
            except Exception:
                marked_in_source = False

        record = self.events.append(
            label=command.label, origin="stim", timestamp=result["dispatched_at"],
            value=command.value, duration=command.duration_ms / 1000.0,
            sample_index=self.raw_buffer.total_written,
            payload={"backend": backend.name, "delivered": True,
                     "marked_in_source": marked_in_source, **result["detail"]},
        )
        result["event"] = record.as_dict()
        result["marked_in_source"] = marked_in_source
        return result

    # --- introspection ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        info = self.source.info
        elapsed = (now() - self.started_at) if self.started_at else 0.0
        expected = elapsed * info.sfreq
        status: dict[str, Any] = {
            "session_id": self.session_id,
            "running": self.is_running,
            "exhausted": self.exhausted,
            "source": info.as_dict(),
            "buffered_samples": self.raw_buffer.n_available,
            "buffered_sec": self.raw_buffer.n_available / info.sfreq,
            "buffer_capacity_sec": self.raw_buffer.capacity / info.sfreq,
            "samples_received": self.samples_received,
            "poll_count": self.poll_count,
            "elapsed_sec": elapsed,
            # < 1.0 means the producer is not keeping up with the source, which
            # for a live board means samples were dropped by the driver.
            "throughput_ratio": (
                float(self.samples_received / expected) if expected > 0 else None
            ),
            "filters": self.chain.describe(),
            "n_events": len(self.events),
            "event_counts": self.events.counts_by_label(),
            "stim_backends": {
                key: backend.status() for key, backend in self.stim_backends.items()
            },
            "default_stim_backend": self.default_stim_backend,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "history": self.history[-20:],
        }
        if hasattr(self.source, "position"):
            status["replay"] = self.source.position()
        return status


class SessionStore:
    """Thread-safe registry of stream sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, StreamSession] = {}
        self._lock = threading.Lock()

    def create(self, session_id: str, source: StreamSource) -> StreamSession:
        config = get_config()
        info = source.info
        capacity = max(int(round(config.buffer_sec * info.sfreq)), 1)
        session = StreamSession(
            session_id=session_id,
            source=source,
            raw_buffer=RingBuffer(info.n_channels, capacity),
            events=EventLog(),
            chain=FilterChain(info.n_channels, info.sfreq),
        )
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None and existing.is_running:
                raise ValueError(
                    f"Session '{session_id}' is already running. Call stop_stream "
                    "first, or use a different session_id."
                )
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> StreamSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(
                f"No session '{session_id}'. Call start_stream or start_replay first. "
                f"Active: {sorted(self._sessions) or 'none'}"
            )
        return session

    def require_running(self, session_id: str) -> StreamSession:
        session = self.get(session_id)
        if not session.is_running and session.raw_buffer.n_available == 0:
            raise ValueError(
                f"Session '{session_id}' is not running and has no buffered data."
                + (f" Last error: {session.last_error}" if session.last_error else "")
            )
        return session

    def drop(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            session.stop()
        except Exception:
            pass
        return True

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)

    def stop_all(self) -> None:
        for session_id in self.list_ids():
            self.drop(session_id)


# Single shared store for the running server process.
store = SessionStore()
