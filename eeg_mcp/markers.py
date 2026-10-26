"""Ingest an external LSL marker stream into a session's event log.

eeg-mcp already *publishes* markers over LSL -- ``attach_stim_backend(backend=
"lsl")`` opens an outlet PsychoPy, OpenViBE or LabRecorder can subscribe to.
This is the other direction: PsychoPy (or any other LSL marker producer)
fires a marker at stimulus onset or a subject's response, and it needs to
land in *this* session's own event log, on the buffer's own sample index --
not just recorded with a timestamp and left to line up later by hand. That is
what "one event log, one clock, no clock join" means in practice, and until
this module existed it only held for markers that arrived through a board's
own marker channel or a replayed file's annotations.

A marker outlet is irregular-rate by LSL convention -- there is no fixed
sampling rate for "an event happened" -- which is exactly what
``sources.lsl_source.LSLSource`` refuses to open as acquisition, correctly,
for that job. ``LSLMarkerInlet`` is a separate, much smaller listener built
for that shape: non-blocking pulls, string- or numeric-valued samples, no
signal buffer of its own. It is driven from inside the owning session's
existing producer thread (see ``StreamSession._run``), not a thread of its
own -- this project's filtering and buffer-writing are deliberately kept to
one writer per session, and a second thread appending to the same event log
concurrently would be a needless, unreviewed exception to that.

**Known limitation.** Unlike ``LSLSource``, this does not currently
auto-reconnect if the marker outlet drops -- it was scoped for the common
case (a same-machine or LAN link to presentation software, not BLE) rather
than built out fully on spec. If that turns out to matter in practice, the
reconnect logic in ``LSLSource`` is a direct template: pin by source_id,
validate shape, log the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sources import _lsl_clock

_DEFAULT_RESOLVE_TIMEOUT = 5.0
_PULL_TIMEOUT = 0.0
_MAX_PULL_SAMPLES = 256


@dataclass
class RawMarker:
    """One marker as reported by the outlet, before it enters the event log."""

    timestamp: float
    label: str
    value: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class LSLMarkerInlet:
    """Pulls discrete markers from an existing LSL outlet.

    Use for presentation software that timestamps its own events over LSL --
    PsychoPy is the common case. Attach with
    ``attach_marker_stream(session_id, ...)``; each pulled marker is folded
    into that session's event log with ``origin="marker"``, on the sample
    index nearest its own LSL timestamp in the *raw* buffer (see
    ``StreamSession._locate_sample_index``) -- the same "which sample was
    this at" question a board's own marker channel answers for free, answered
    here by searching the timestamps this server already recorded.
    """

    def __init__(
        self,
        stream_type: str = "Markers",
        name: str | None = None,
        resolve_timeout: float = _DEFAULT_RESOLVE_TIMEOUT,
    ) -> None:
        try:
            from pylsl import StreamInlet, resolve_byprop
        except ImportError as exc:
            raise RuntimeError(
                "Marker ingestion needs pylsl. Install it with: "
                "pip install eeg-mcp[lsl]"
            ) from exc

        prop, value = ("name", name) if name else ("type", stream_type)
        streams = resolve_byprop(prop, str(value), timeout=float(resolve_timeout))
        if not streams:
            raise RuntimeError(
                f"No LSL marker stream found with {prop}='{value}' within "
                f"{float(resolve_timeout):.0f}s. Start the producer first -- "
                "e.g. a PsychoPy experiment with an LSL marker outlet -- and "
                "confirm it is visible before retrying. If more than one "
                "outlet is running, pass name=... to pick one."
            )

        self._inlet = StreamInlet(streams[0], max_buflen=360)
        info = self._inlet.info()
        self._candidate_offset = _lsl_clock.candidate_offset()
        self._epoch_offset: float | None = None
        self._started = False

        self.stream_name = info.name()
        self.stream_type = info.type()
        self.source_id = info.source_id()

    def open(self) -> None:
        if self._started:
            return
        self._inlet.open_stream()
        self._started = True

    def poll(self) -> list[RawMarker]:
        """Non-blocking: whatever markers have arrived since the last call."""
        if not self._started:
            return []
        samples, timestamps = self._inlet.pull_chunk(
            timeout=_PULL_TIMEOUT, max_samples=_MAX_PULL_SAMPLES
        )
        if not samples:
            return []

        if self._epoch_offset is None:
            self._epoch_offset = _lsl_clock.resolve_offset(
                float(timestamps[0]), self._candidate_offset
            )

        out: list[RawMarker] = []
        for sample, raw_ts in zip(samples, timestamps):
            label = str(sample[0]) if sample else "marker"
            value: float | None = None
            if len(sample) > 1:
                try:
                    value = float(sample[1])
                except (TypeError, ValueError):
                    value = None
            out.append(RawMarker(
                timestamp=float(raw_ts) + self._epoch_offset,
                label=label, value=value,
                payload={"stream_name": self.stream_name, "raw_sample": list(sample)},
            ))
        return out

    def close(self) -> None:
        if not self._started:
            return
        try:
            self._inlet.close_stream()
        finally:
            self._started = False

    def describe(self) -> dict[str, Any]:
        return {
            "stream_name": self.stream_name,
            "stream_type": self.stream_type,
            "source_id": self.source_id,
        }
