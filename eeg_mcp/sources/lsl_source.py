"""Acquisition from an existing LSL outlet, instead of owning the device.

BrainFlow's native BLE scanning (SimpleBLE) has a longstanding bug in its
Windows Python bindings (simpleble/simpleble#31, also tracked in
brainflow-dev/brainflow#393) that keeps some devices -- Muse boards among them
-- from being discovered during ``prepare_session()``, even when the device is
confirmed powered on, advertising, and unpaired. Tools built on a different BLE
binding (``muselsl`` uses ``bleak``) do not hit this bug, and typically publish
the device as an LSL outlet once connected.

This source does no BLE work of its own. It attaches to whatever EEG outlet is
already running -- ``muselsl stream``, PetalStreamer, OpenViBE, or any other
BLE-to-LSL bridge -- so acquisition can route around BrainFlow-specific
connectivity failures while every downstream eeg_mcp tool (filtering,
epoching, recording, stimulation) keeps working unchanged. See
docs/installation.md's troubleshooting table for the Windows/Muse case this
was written for.

**On reconnection.** BLE links drop -- this is not hypothetical: streaming a
Muse S Athena's EEG+ACC+GYRO+OPTICS together over one connection was measured
to disconnect within one to three minutes on Windows/bleak, and the same
pattern is reported against upstream muselsl independently of this project
(alexandrebarachant/muse-lsl#55, #110). The bridge tool (e.g. muselsl) usually
exits when this happens; something outside eeg-mcp has to relaunch it. What
*is* this module's job is what happens on the eeg-mcp side while that bridge
is down and after it comes back: a source that simply stopped producing
samples looks identical to a silent recording gap, which is exactly what this
server's design elsewhere (throughput_ratio, the honest event log) exists to
avoid. So this source detects staleness, re-resolves the *same stream*
(by the type/name originally given, cross-checked against source_id when one
was declared -- important if more than one headset is running), validates the
new outlet still matches the channel count and sample rate the session was
built around, and logs a ``source_reconnected`` event carrying the gap
duration rather than leaving a silent hole.

Two distinct failure modes showed up against real multi-hour sessions, both
producing the same symptom -- one of four simultaneous streams
(EEG/ACC/GYRO/OPTICS) silently stuck after a bridge restart while the others
recovered:

1. A still-registered but dead outlet from the *previous* bridge process --
   its LSL announcement had not yet been retracted, so resolve_byprop kept
   matching it and handing back a description that looked perfect and
   produced nothing. Fixed by actually waiting for one real sample from a
   reconnect candidate before committing to it, not just checking that its
   metadata matches (see ``_LIVENESS_PROBE_TIMEOUT``).
2. Reconnecting by source_id *alone*: muselsl gives all four of a device's
   simultaneous outlets (EEG/ACC/GYRO/OPTICS) the same source_id --
   "Muse<mac>" -- differing only in ``type``. A reconnect that resolves by
   source_id alone can't tell its own stream apart from a sibling, so
   resolve_byprop is free to hand back a *different* sensor's outlet; that
   candidate then fails the channel/rate check and the real match is never
   found, while another session's reconnect recovers by the luck of which
   sibling resolve_byprop returns first. Fixed by resolving reconnects by the
   original (type or name) discriminator, using source_id only as an
   additional same-device check rather than the primary key.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..utils import now
from . import _lsl_clock
from .base import RawEvent, SourceChunk, SourceInfo, StreamSource

# First connect: the caller is waiting synchronously and can set this as high
# as they like via params={'resolve_timeout': ...}.
_DEFAULT_RESOLVE_TIMEOUT = 5.0
# Each *reconnect* attempt, in contrast, runs inside poll() on the session's
# producer thread -- StreamSource.poll() promises never to block long, so this
# is deliberately short. It doubles as the retry cadence: resolve_byprop
# blocks for up to this long whether it succeeds or times out, so callers
# don't need a separate cooldown timer to avoid hammering the LSL resolver.
_RECONNECT_RESOLVE_TIMEOUT = 1.0
# No samples for this long is treated as "the link is down," not "briefly
# busy." Must comfortably exceed normal chunking/scheduling jitter; 5s is
# generous relative to the sub-second poll interval this server otherwise
# uses, so it won't fire on ordinary jitter, but is still short enough that a
# recording's gap stays small.
_DEFAULT_STALE_AFTER_SEC = 5.0
# Chunk pulls must not block the producer thread, which already polls on a
# fixed interval; a zero timeout returns immediately with whatever is queued.
_PULL_TIMEOUT = 0.0
_MAX_PULL_SAMPLES = 4096
# A candidate reconnect outlet is proven live by actually waiting this long
# for a real sample, not just by its source_id matching -- a bridge process
# that just exited does not always retract its LSL announcement instantly,
# so resolve_byprop can hand back a description that matches perfectly but
# produces nothing. This is the one place in this class allowed to block for
# a while: it only runs once per reconnect attempt, already gated behind the
# resolve timeout above.
_LIVENESS_PROBE_TIMEOUT = 0.5


def _channel_metadata(stream_info: Any, n_channels: int) -> tuple[list[str], str]:
    """Read channel labels and unit out of the inlet's XML description.

    Both come from the same ``<channel>`` elements LSL producers are expected
    to fill in (muselsl among them: label, unit, type per channel) -- read in
    one walk rather than two. Unit is reported only when every channel agrees
    on one; a source with mixed units per channel would make a single
    "unit" field on SourceInfo misleading, so that case reports "unknown"
    rather than guessing which channel's unit to surface.
    """
    names: list[str] = []
    units: set[str] = set()
    try:
        ch = stream_info.desc().child("channels").child("channel")
        while not ch.empty():
            label = ch.child_value("label")
            names.append(label if label else f"ch{len(names)}")
            unit = ch.child_value("unit")
            if unit:
                units.add(unit)
            ch = ch.next_sibling()
    except Exception:
        pass
    if len(names) != n_channels:
        names = [f"ch{i}" for i in range(n_channels)]
    unit = units.pop() if len(units) == 1 else "unknown"
    return names, unit


class LSLSource(StreamSource):
    """Pulls EEG from an existing LSL outlet rather than opening a board.

    Use this when BrainFlow cannot open a device directly -- most often the
    Windows/SimpleBLE bug that blocks Muse discovery -- by pointing an
    LSL-publishing tool at the device first (e.g. ``muselsl stream``), then
    attaching here with ``start_stream(board="lsl")``.
    """

    def __init__(
        self,
        stream_type: str = "EEG",
        name: str | None = None,
        resolve_timeout: float = _DEFAULT_RESOLVE_TIMEOUT,
        channels: list[int] | None = None,
        reconnect: bool = True,
        stale_after_sec: float = _DEFAULT_STALE_AFTER_SEC,
    ) -> None:
        try:
            from pylsl import StreamInlet, local_clock, resolve_byprop  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "board='lsl' needs pylsl. Install it with: pip install eeg-mcp[lsl]"
            ) from exc

        prop, value = ("name", name) if name else ("type", stream_type)
        inlet, stream_info = self._open(prop, str(value), float(resolve_timeout))
        if inlet is None:
            raise RuntimeError(
                f"No LSL stream found with {prop}='{value}' within "
                f"{float(resolve_timeout):.0f}s. Start the outlet first -- e.g. "
                "`muselsl stream` for a Muse, or PetalStreamer -- and confirm it "
                "is visible before retrying. If more than one outlet is running, "
                "pass params={'name': '...'} to pick one."
            )
        sfreq = float(stream_info.nominal_srate())
        if sfreq <= 0:
            inlet.close_stream()
            raise ValueError(
                f"LSL stream '{stream_info.name()}' reports an irregular "
                "sampling rate (0 Hz), so it cannot be treated as a continuous "
                "EEG signal -- it is likely a marker/event stream rather than "
                "the EEG data stream. Pass params={'name': '...'} to select the "
                "right outlet."
            )

        n_total = int(stream_info.channel_count())
        all_names, stream_unit = _channel_metadata(stream_info, n_total)
        self._eeg_rows = list(channels) if channels else list(range(n_total))
        invalid = [c for c in self._eeg_rows if not (0 <= c < n_total)]
        if invalid:
            inlet.close_stream()
            raise ValueError(
                f"Channels {invalid} are out of range for stream "
                f"'{stream_info.name()}', which has {n_total} channel(s)."
            )
        ch_names = [all_names[i] for i in self._eeg_rows]

        self._inlet = inlet
        self._n_total = n_total
        self._sfreq = sfreq
        self._started = False

        # What a reconnect re-resolves by: the same (prop, value) used for the
        # initial connect -- e.g. type='ACC' -- not source_id alone. A single
        # muselsl process publishes EEG/ACC/GYRO/OPTICS as four *sibling*
        # outlets that all share one source_id ("Muse<mac>": see
        # muselsl/lsl_outlet.py -- every StreamInfo it builds passes the same
        # address-derived source_id regardless of sensor type). Resolving a
        # reconnect by source_id alone can't tell those siblings apart, so
        # resolve_byprop is free to hand back a *different* sensor's outlet;
        # that candidate then fails the channel/rate check below and the
        # real match never gets picked up, while whichever sibling
        # resolve_byprop happens to return first for another session's
        # reconnect attempt recovers fine -- which is exactly the "some
        # streams stay frozen after a bridge restart, others don't" pattern
        # observed against real hardware. Pinning to (prop, value) keeps
        # reconnect specific to this stream; source_id is instead used below
        # as an extra same-device check, for when more than one headset
        # could be advertising nearby.
        source_id = stream_info.source_id()
        self._reconnect_prop, self._reconnect_value = prop, str(value)
        self._source_id = source_id

        self._reconnect_enabled = bool(reconnect)
        self._stale_after_sec = float(stale_after_sec)
        self._last_sample_at: float | None = None
        self._disconnected_since: float | None = None
        self._reconnect_count = 0

        # See sources._lsl_clock for why this is decided empirically
        # per-source rather than assumed. Re-detected after each reconnect,
        # since a replacement outlet is a fresh process with its own clock.
        self._candidate_offset = _lsl_clock.candidate_offset()
        self._epoch_offset: float | None = None

        self.info = SourceInfo(
            kind="lsl",
            name=f"lsl:{stream_info.name()}",
            sfreq=sfreq,
            ch_names=ch_names,
            unit=stream_unit,
            metadata={
                "stream_name": stream_info.name(),
                "stream_type": stream_info.type(),
                "source_id": source_id,
                "hostname": stream_info.hostname(),
                "channels_total": n_total,
                "channels_used": self._eeg_rows,
                "has_marker_channel": False,
                "reconnect_enabled": self._reconnect_enabled,
            },
        )

    @staticmethod
    def _open(prop: str, value: str, timeout: float):
        """Resolve one outlet and open an inlet on it, or ``(None, None)``.

        Never raises on "not found" -- that is the expected, common outcome
        of a reconnect attempt, not an error. Only import/API failures
        propagate.
        """
        from pylsl import StreamInlet, resolve_byprop

        streams = resolve_byprop(prop, value, timeout=timeout)
        if not streams:
            return None, None
        inlet = StreamInlet(streams[0], max_buflen=360, recover=True)
        return inlet, inlet.info()

    def start(self) -> None:
        if self._started:
            return
        self._inlet.open_stream()
        self._started = True

    def poll(self) -> SourceChunk:
        empty = SourceChunk(
            data=np.zeros((len(self._eeg_rows), 0)), timestamps=np.zeros(0)
        )
        if not self._started:
            return empty

        if self._inlet is None:
            return self._attempt_reconnect() or empty

        try:
            samples, lsl_timestamps = self._inlet.pull_chunk(
                timeout=_PULL_TIMEOUT, max_samples=_MAX_PULL_SAMPLES
            )
        except Exception as exc:
            # pylsl.LostError (or similar): the library already knows the
            # link is gone. Don't wait for the staleness timer -- drop the
            # dead inlet now so the next poll starts reconnecting immediately.
            self._drop_inlet(str(exc))
            return self._attempt_reconnect() or empty

        if not samples:
            if (
                self._reconnect_enabled
                and self._last_sample_at is not None
                and now() - self._last_sample_at > self._stale_after_sec
            ):
                self._drop_inlet(
                    f"no samples for over {self._stale_after_sec:.0f}s"
                )
                return self._attempt_reconnect() or empty
            return empty

        self._last_sample_at = now()
        data, timestamps = self._process_samples(samples, lsl_timestamps)
        return SourceChunk(data=data, timestamps=timestamps)

    def _process_samples(
        self, samples: list, lsl_timestamps: list
    ) -> tuple[np.ndarray, np.ndarray]:
        """Shared by the normal poll path and a reconnect's liveness probe,
        so the epoch-offset detection runs through one code path rather than
        two copies that could quietly drift apart."""
        data = np.asarray(samples, dtype=np.float64).T[self._eeg_rows, :]
        raw_ts = np.asarray(lsl_timestamps, dtype=np.float64)
        if self._epoch_offset is None:
            self._epoch_offset = _lsl_clock.resolve_offset(
                raw_ts[0], self._candidate_offset
            )
        return data, raw_ts + self._epoch_offset

    def _drop_inlet(self, reason: str) -> None:
        if self._inlet is not None:
            try:
                self._inlet.close_stream()
            except Exception:
                pass
        self._inlet = None
        if self._disconnected_since is None:
            self._disconnected_since = now()

    def _attempt_reconnect(self) -> SourceChunk | None:
        """One bounded, self-throttling reconnect attempt.

        Returns a chunk carrying a ``source_reconnected`` event on success
        (plus, when the liveness probe below catches real data, the samples
        it caught rather than discarding them), or ``None`` when there is
        nothing to report this call. Never raises: a source that cannot
        currently reach its device is a status to report (throughput_ratio
        already does this), not a reason to kill the whole session -- the
        headband may come back the moment someone notices and wakes it.
        """
        if not self._reconnect_enabled or self._disconnected_since is None:
            return None

        inlet, stream_info = self._open(
            self._reconnect_prop, self._reconnect_value, _RECONNECT_RESOLVE_TIMEOUT
        )
        if inlet is None:
            return None

        # A name/type-based reconnect could in principle attach to a
        # differently configured outlet (a bridge restarted with different
        # flags, a second device answering to the same generic name/type).
        # Refuse rather than hand the ring buffer a shape it wasn't built
        # for, and -- when the original outlet declared a source_id -- refuse
        # a candidate from a *different* device too, even if its shape
        # happens to match.
        if (
            int(stream_info.channel_count()) != self._n_total
            or abs(float(stream_info.nominal_srate()) - self._sfreq) > 0.5
            or (self._source_id and stream_info.source_id() != self._source_id)
        ):
            inlet.close_stream()
            return None

        # Matching metadata is not the same as being alive: observed against
        # real hardware, a bridge process that just exited does not always
        # retract its old outlets' LSL announcements immediately, so
        # resolve_byprop can hand back a perfectly-matching description of a
        # stream that will never produce another sample. Prove it before
        # committing -- actually wait for one real chunk, not just a
        # metadata match.
        inlet.open_stream()
        try:
            probe_samples, probe_timestamps = inlet.pull_chunk(
                timeout=_LIVENESS_PROBE_TIMEOUT, max_samples=_MAX_PULL_SAMPLES
            )
        except Exception:
            probe_samples = []
        if not probe_samples:
            inlet.close_stream()
            return None

        gap_sec = now() - self._disconnected_since
        self._inlet = inlet
        self._reconnect_count += 1
        self._disconnected_since = None
        # A replacement outlet is a fresh process with its own clock
        # convention -- re-detect on its first real chunk rather than trust
        # the old source's finding.
        self._candidate_offset = _lsl_clock.candidate_offset()
        self._epoch_offset = None
        self._last_sample_at = now()
        data, timestamps = self._process_samples(probe_samples, probe_timestamps)

        event = RawEvent(
            timestamp=now(),
            label="source_reconnected",
            origin="annotation",
            value=round(gap_sec, 3),
            payload={
                "reconnect_count": self._reconnect_count,
                "gap_sec": round(gap_sec, 3),
                "stream_name": stream_info.name(),
            },
        )
        return SourceChunk(data=data, timestamps=timestamps, events=[event])

    def connection_status(self) -> dict[str, Any]:
        """Live reconnection state, surfaced through StreamSession.status()."""
        return {
            "connected": self._inlet is not None,
            "reconnect_enabled": self._reconnect_enabled,
            "reconnect_count": self._reconnect_count,
            "disconnected_since": self._disconnected_since,
            "seconds_since_last_sample": (
                now() - self._last_sample_at if self._last_sample_at else None
            ),
            # What a reconnect resolves by (see the module docstring's
            # "two distinct failure modes") -- exposed so a stuck reconnect
            # against real hardware can be diagnosed from stream_status
            # alone, without adding print statements.
            "resolves_by": {self._reconnect_prop: self._reconnect_value},
            "source_id": self._source_id or None,
        }

    def stop(self) -> None:
        if not self._started:
            return
        if self._inlet is not None:
            try:
                self._inlet.close_stream()
            except Exception:
                pass
        self._started = False
