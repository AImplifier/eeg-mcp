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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .config import get_config
from .dsp import FilterChain, FilterSpec
from .events import EventLog
from .processing.base import Processor, ProcessorContext, ProcessorOutput
from .ring import RingBuffer
from .sources.base import StreamSource
from .stim.base import StimBackend, StimCommand
from .utils import now

# Outputs kept per processor. Enough to see a trend without unbounded growth.
_PROCESSOR_HISTORY = 512


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
    recorder: Any = None  # Recorder | None; set by start_recording
    recording_result: dict[str, Any] | None = None
    processors: dict[str, Processor] = field(default_factory=dict)
    processor_outputs: dict[str, deque] = field(default_factory=dict)
    marker_listener: Any = None  # markers.LSLMarkerInlet | None
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
    # Built once and reused: rebuilding the callback closures on every chunk
    # would allocate needlessly inside the acquisition loop.
    _processor_context: ProcessorContext | None = field(default=None, repr=False)

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
        # Finalise before releasing the device: a session stopped mid-recording
        # must still produce a readable file rather than leaving a sidecar
        # behind that someone has to know to recover.
        try:
            self.finish_recording()
        except Exception as exc:
            self.log(f"recording finalise failed: {exc}")
        try:
            self.source.stop()
        finally:
            self.stopped_at = now()
            for backend in list(self.stim_backends.values()):
                try:
                    backend.close()
                except Exception:
                    pass
            if self.marker_listener is not None:
                try:
                    self.marker_listener.close()
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
                    filtered = None
                    with self._lock:
                        before = self.raw_buffer.total_written
                        self.raw_buffer.write(chunk.data, chunk.timestamps)
                        self.samples_received += chunk.n_samples
                        if self.filtered_buffer is not None and len(self.chain):
                            filtered = self.chain.process(chunk.data)
                            self.filtered_buffer.write(filtered, chunk.timestamps)

                    # Outside the lock: this touches the disk, and holding the
                    # session lock across a write would stall every reader.
                    # The recorder gets the *raw* chunk -- the amplifier's
                    # output is the thing that cannot be reconstructed later.
                    if self.recorder is not None and not self.recorder.closed:
                        self.recorder.write(chunk.data, chunk.timestamps)

                    if self.processors:
                        self._run_processors(chunk.data, filtered, chunk.timestamps)

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

                if self.marker_listener is not None:
                    self._ingest_markers()

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

    # --- processors -------------------------------------------------------

    def make_processor_context(self) -> ProcessorContext:
        """Bind session callbacks so a processor can act, not just report."""

        def emit_event(label: str, value: float | None = None,
                       payload: dict[str, Any] | None = None,
                       duration: float = 0.0):
            return self.events.append(
                label=label, origin="manual", timestamp=now(), value=value,
                duration=duration, sample_index=self.raw_buffer.total_written,
                payload={**(payload or {}), "via": "processor"},
            )

        def stimulate(label: str, value: float = 1.0, **kwargs: Any):
            return self.dispatch_stim(
                StimCommand(label=label, value=float(value), **kwargs)
            )

        info = self.source.info
        return ProcessorContext(
            session_id=self.session_id, sfreq=info.sfreq,
            ch_names=list(info.ch_names), emit_event=emit_event,
            log=self.log, stimulate=stimulate,
        )

    def attach_processor(self, processor: Processor) -> Processor:
        context = self.make_processor_context()
        processor.setup(context)
        with self._lock:
            existing = self.processors.get(processor.name)
            if existing is not None:
                try:
                    existing.teardown()
                except Exception:
                    pass
            self.processors[processor.name] = processor
            self.processor_outputs[processor.name] = deque(maxlen=_PROCESSOR_HISTORY)
        self.log(f"attached processor '{processor.name}'")
        return processor

    def detach_processor(self, name: str) -> bool:
        with self._lock:
            processor = self.processors.pop(name, None)
            self.processor_outputs.pop(name, None)
        if processor is None:
            return False
        try:
            processor.teardown()
        except Exception:
            pass
        self.log(f"detached processor '{name}'")
        return True

    def _run_processors(
        self, raw: np.ndarray, filtered: np.ndarray | None, timestamps: np.ndarray
    ) -> None:
        """Feed streaming processors. Runs in the producer thread, so it is
        wrapped in error containment -- a plugin must not stop acquisition."""
        context = self._processor_context
        if context is None:
            context = self._processor_context = self.make_processor_context()
        for name, processor in list(self.processors.items()):
            if not processor.streaming or not processor.enabled:
                continue
            data = filtered if (processor.uses == "filtered" and filtered is not None) \
                else raw
            result = processor._invoke(data, timestamps, context)
            if result is not None:
                history = self.processor_outputs.get(name)
                if history is not None:
                    history.append(ProcessorOutput(
                        timestamp=float(timestamps[-1]) if timestamps.size else now(),
                        value=result,
                    ))

    def reset_processors(self) -> None:
        for processor in self.processors.values():
            try:
                processor.reset()
            except Exception:
                pass
        for history in self.processor_outputs.values():
            history.clear()

    # --- external markers (e.g. PsychoPy over LSL) -------------------------

    def attach_marker_listener(self, listener: Any) -> Any:
        listener.open()
        with self._lock:
            existing = self.marker_listener
            self.marker_listener = listener
        if existing is not None:
            try:
                existing.close()
            except Exception:
                pass
        self.log(f"attached marker listener '{listener.stream_name}'")
        return listener

    def detach_marker_listener(self) -> bool:
        with self._lock:
            listener = self.marker_listener
            self.marker_listener = None
        if listener is None:
            return False
        try:
            listener.close()
        except Exception:
            pass
        self.log("detached marker listener")
        return True

    def _locate_sample_index(self, timestamp: float) -> int | None:
        """Absolute raw-buffer index of the sample nearest ``timestamp``.

        For an event that arrived as part of the chunk currently being
        processed, its index is cheap to compute directly (see the
        ``chunk.events`` handling in ``_run``, which searches just that
        chunk). A marker from an independent, asynchronously polled listener
        has no such host chunk to search -- this instead searches a recent
        window of the buffer's own recorded timestamps, which is the raw
        signal's real arrival time regardless of when the marker happened to
        be picked up.
        """
        sfreq = self.source.info.sfreq
        if sfreq <= 0:
            return None
        window_n = min(self.raw_buffer.capacity, max(int(sfreq * 5), 1))
        _data, ts = self.raw_buffer.read_last(window_n)
        if ts.size == 0:
            return None
        total = self.raw_buffer.total_written
        start_abs = total - ts.size
        offset = int(np.searchsorted(ts, timestamp))
        return start_abs + min(offset, ts.size - 1)

    def _ingest_markers(self) -> None:
        """Poll the attached marker listener and fold its output into the
        event log. Wrapped in its own error containment, same reasoning as
        processors: a marker source hiccup must not take acquisition down
        with it."""
        try:
            markers = self.marker_listener.poll()
        except Exception as exc:
            self.log(f"marker listener error: {exc}")
            return
        for marker in markers:
            self.events.append(
                label=marker.label, origin="marker", timestamp=marker.timestamp,
                value=marker.value,
                sample_index=self._locate_sample_index(marker.timestamp),
                payload=marker.payload,
            )

    # --- recording --------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self.recorder is not None and not self.recorder.closed

    def finish_recording(self, keep_sidecar: bool = False) -> dict[str, Any] | None:
        """Close the recorder, writing the event log in as annotations.

        Idempotent: calling it again returns the stored result rather than
        raising, so ``stop_recording`` and ``stop_stream`` can both call it.
        """
        if self.recorder is None:
            return None
        if self.recorder.closed:
            return self.recording_result
        events = [e.as_dict() for e in self.events.query(limit=0)]
        result = self.recorder.close(events, keep_sidecar=keep_sidecar)
        self.recording_result = result
        self.log(
            f"recording finished: {result.get('fif_path') or result.get('reason')}"
        )
        return result

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
        if hasattr(self.source, "connection_status"):
            status["connection"] = self.source.connection_status()
        if self.marker_listener is not None:
            status["marker_listener"] = self.marker_listener.describe()
        status["recording"] = (
            self.recorder.status() if self.recorder is not None else {"recording": False}
        )
        if self.recording_result is not None:
            status["last_recording"] = {
                k: v for k, v in self.recording_result.items() if k != "annotations"
            }
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
