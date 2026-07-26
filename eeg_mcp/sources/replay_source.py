"""Wall-clock replay of an existing recording, with its events.

This is the "resimulation" half of the server. A file on disk is turned into a
source that emits samples *at the rate they were originally recorded*, so
downstream code experiences the same latency, chunking and jitter it will face
against live hardware -- and the recording's annotations come back out as
events at the right moments.

Why not just use BrainFlow's ``PLAYBACK_FILE_BOARD``? Because it only reads
BrainFlow CSV and gives no speed control or seeking. This source reads anything
MNE can open (EDF/BDF/GDF/SET/FIF/...) *and* BrainFlow CSV, and adds
``speed``, ``seek``, ``pause``, and looping. Use the playback board when you
want BrainFlow's own pacing; use this for everything else.

The clock is a *media clock*: sample N is emitted when
``(N - start) / sfreq / speed`` seconds of wall time have passed since start.
Emitted timestamps are real Unix time, so replayed events and live stimulation
commands share a single comparable time base.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np

from ..utils import now
from .base import RawEvent, SourceChunk, SourceInfo, StreamSource

# Extensions we hand to BrainFlow's CSV reader rather than MNE.
_BRAINFLOW_SUFFIXES = {".csv", ".tsv"}


class _Timeline:
    """Events pinned to sample indices in the loaded recording."""

    def __init__(self) -> None:
        self.samples: list[int] = []
        self.labels: list[str] = []
        self.values: list[float | None] = []
        self.durations: list[float] = []

    def add(self, sample: int, label: str, value: float | None, duration: float) -> None:
        self.samples.append(int(sample))
        self.labels.append(str(label))
        self.values.append(value)
        self.durations.append(float(duration))

    def sort(self) -> None:
        if not self.samples:
            return
        order = np.argsort(np.asarray(self.samples, dtype=np.int64), kind="stable")
        self.samples = [self.samples[i] for i in order]
        self.labels = [self.labels[i] for i in order]
        self.values = [self.values[i] for i in order]
        self.durations = [self.durations[i] for i in order]

    def between(self, lo: int, hi: int) -> list[tuple[int, str, float | None, float]]:
        """Events with ``lo <= sample < hi``, in order."""
        arr = np.asarray(self.samples, dtype=np.int64)
        if arr.size == 0:
            return []
        start = int(np.searchsorted(arr, lo, side="left"))
        end = int(np.searchsorted(arr, hi, side="left"))
        return [
            (self.samples[i], self.labels[i], self.values[i], self.durations[i])
            for i in range(start, end)
        ]

    def __len__(self) -> int:
        return len(self.samples)


def _load_with_mne(
    path: Path, picks: str | list[str] | None, use_stim_channel: bool
) -> tuple[np.ndarray, float, list[str], _Timeline]:
    """Read any MNE-supported format into microvolts + a sample-indexed timeline."""
    import mne

    mne.set_log_level("ERROR")
    raw = mne.io.read_raw(str(path), preload=True)
    sfreq = float(raw.info["sfreq"])
    timeline = _Timeline()

    # Pull events off a hardware trigger channel *before* picking EEG, since
    # picking would drop the stim channel we need to read.
    if use_stim_channel:
        try:
            found = mne.find_events(raw, verbose=False)
            for sample, _prev, code in found:
                timeline.add(int(sample) - int(raw.first_samp), f"stim_{int(code)}",
                             float(code), 0.0)
        except Exception:
            # No stim channel, or nothing on it. Annotations below still apply.
            pass

    for ann in raw.annotations:
        onset_sample = int(round((float(ann["onset"]) - raw.first_time) * sfreq))
        timeline.add(onset_sample, str(ann["description"]), None,
                     float(ann["duration"]))
    timeline.sort()

    if picks is not None:
        raw.pick(picks)
    elif len(mne.pick_types(raw.info, eeg=True, exclude=[])) > 0:
        raw.pick("eeg")

    # MNE stores EEG in volts; the rest of this server speaks microvolts so that
    # replayed and BrainFlow-sourced amplitudes are directly comparable.
    data = raw.get_data() * 1e6
    return np.ascontiguousarray(data, dtype=np.float64), sfreq, list(raw.ch_names), timeline


def _load_brainflow_csv(
    path: Path, board: str | int
) -> tuple[np.ndarray, float, list[str], _Timeline]:
    """Read a BrainFlow-format CSV using the board layout that produced it."""
    from brainflow.data_filter import DataFilter

    from .brainflow_source import BoardShimSafe, resolve_board_id

    board_id = resolve_board_id(board)
    raw = DataFilter.read_file(str(path))
    if raw.ndim != 2:
        raise ValueError(f"Expected a 2-D BrainFlow CSV, got shape {raw.shape}")

    eeg_rows = BoardShimSafe.eeg_channels(board_id)
    sfreq = BoardShimSafe.sampling_rate(board_id)
    max_row = max(eeg_rows)
    if raw.shape[0] <= max_row:
        raise ValueError(
            f"File has {raw.shape[0]} rows but board {board_id} expects EEG on rows "
            f"up to {max_row}. Pass the board that recorded this file."
        )
    data = raw[eeg_rows, :].astype(np.float64, copy=True)
    ch_names = BoardShimSafe.channel_names(board_id, len(eeg_rows))

    timeline = _Timeline()
    try:
        from brainflow.board_shim import BoardShim

        marker_row = int(BoardShim.get_marker_channel(board_id))
        if raw.shape[0] > marker_row:
            markers = raw[marker_row, :]
            for idx in np.flatnonzero(markers):
                timeline.add(int(idx), f"marker_{int(markers[idx])}",
                             float(markers[idx]), 0.0)
    except Exception:
        pass
    timeline.sort()
    return data, sfreq, ch_names, timeline


class ReplaySource(StreamSource):
    """Emits a loaded recording sample-by-sample against a real-time clock."""

    def __init__(
        self,
        path: str | Path,
        speed: float = 1.0,
        loop: bool = False,
        start_sec: float = 0.0,
        board: str | int | None = None,
        picks: str | list[str] | None = None,
        use_stim_channel: bool = True,
    ) -> None:
        if speed <= 0:
            raise ValueError("speed must be > 0 (use pause() to hold the clock)")
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"No recording at {resolved}")

        self.path = resolved
        self.speed = float(speed)
        self.loop = bool(loop)
        self._lock = threading.Lock()

        if resolved.suffix.lower() in _BRAINFLOW_SUFFIXES:
            if board is None:
                raise ValueError(
                    "A BrainFlow CSV has no header describing its layout. Pass "
                    "board=... (e.g. 'cyton', 'synthetic') so rows can be mapped."
                )
            data, sfreq, ch_names, timeline = _load_brainflow_csv(resolved, board)
            fmt = "brainflow_csv"
        else:
            data, sfreq, ch_names, timeline = _load_with_mne(
                resolved, picks, use_stim_channel
            )
            fmt = resolved.suffix.lstrip(".").lower() or "mne"

        self._data = data
        self._timeline = timeline
        self._n_times = int(data.shape[1])
        self._sfreq = sfreq

        self._start_sample = self._clamp_sample(int(round(start_sec * sfreq)))
        self._cursor = self._start_sample
        self._t0 = 0.0
        self._started = False
        self._paused = False
        self._paused_media_offset = 0.0
        self._exhausted = False
        self._loops = 0

        self.info = SourceInfo(
            kind="replay",
            name=resolved.name,
            sfreq=sfreq,
            ch_names=ch_names,
            metadata={
                "path": str(resolved),
                "format": fmt,
                "duration_sec": self._n_times / sfreq if sfreq else 0.0,
                "n_times": self._n_times,
                "n_events_in_file": len(timeline),
                "speed": self.speed,
                "loop": self.loop,
            },
        )

    # --- clock ------------------------------------------------------------

    def _clamp_sample(self, sample: int) -> int:
        return int(max(0, min(sample, max(self._n_times - 1, 0))))

    def _media_elapsed(self) -> float:
        """Seconds of recording that should have played by now."""
        if self._paused:
            return self._paused_media_offset
        return (now() - self._t0) * self.speed

    def _wall_time_for(self, sample: int) -> float:
        """Unix time at which ``sample`` is (or was) due to be emitted."""
        return self._t0 + (sample - self._start_sample) / (self._sfreq * self.speed)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._t0 = now()
            self._cursor = self._start_sample
            self._paused = False
            self._paused_media_offset = 0.0
            self._exhausted = False
            self._started = True

    def poll(self) -> SourceChunk:
        with self._lock:
            empty = SourceChunk(
                data=np.zeros((self._data.shape[0], 0)), timestamps=np.zeros(0)
            )
            if not self._started or self._paused:
                return empty
            if self._exhausted:
                empty.exhausted = True
                return empty

            target = self._start_sample + int(self._media_elapsed() * self._sfreq)
            target = min(target, self._n_times)
            if target <= self._cursor:
                return empty

            lo, hi = self._cursor, target
            data = self._data[:, lo:hi].copy()
            idx = np.arange(lo, hi, dtype=np.float64)
            timestamps = self._t0 + (idx - self._start_sample) / (
                self._sfreq * self.speed
            )

            events = [
                RawEvent(
                    timestamp=self._wall_time_for(sample),
                    label=label,
                    origin="annotation",
                    value=value,
                    duration=duration,
                    payload={"source_sample": sample, "loop": self._loops},
                )
                for sample, label, value, duration in self._timeline.between(lo, hi)
            ]
            self._cursor = hi

            exhausted = False
            if self._cursor >= self._n_times:
                if self.loop:
                    self._loops += 1
                    self._cursor = self._start_sample
                    self._t0 = now()
                    events.append(
                        RawEvent(
                            timestamp=self._t0,
                            label="replay_loop",
                            origin="annotation",
                            value=float(self._loops),
                            payload={"loop": self._loops},
                        )
                    )
                else:
                    self._exhausted = True
                    exhausted = True
                    events.append(
                        RawEvent(
                            timestamp=self._wall_time_for(self._n_times),
                            label="replay_end",
                            origin="annotation",
                            payload={"n_times": self._n_times},
                        )
                    )
            return SourceChunk(
                data=data, timestamps=timestamps, events=events, exhausted=exhausted
            )

    def stop(self) -> None:
        with self._lock:
            self._started = False

    # --- transport controls ----------------------------------------------

    def pause(self) -> None:
        with self._lock:
            if self._started and not self._paused:
                self._paused_media_offset = (now() - self._t0) * self.speed
                self._paused = True

    def resume(self) -> None:
        with self._lock:
            if self._started and self._paused:
                # Rebase t0 so the media clock continues from where it stopped.
                self._t0 = now() - self._paused_media_offset / self.speed
                self._paused = False

    def seek(self, position_sec: float) -> float:
        """Jump to ``position_sec`` in the recording; returns the actual position."""
        with self._lock:
            sample = self._clamp_sample(int(round(position_sec * self._sfreq)))
            self._start_sample = sample
            self._cursor = sample
            self._t0 = now()
            self._paused_media_offset = 0.0
            self._exhausted = False
            return sample / self._sfreq

    def set_speed(self, speed: float) -> float:
        """Change playback rate without losing the current position."""
        if speed <= 0:
            raise ValueError("speed must be > 0")
        with self._lock:
            # Re-anchor the clock at the current cursor so the change takes
            # effect from here rather than retroactively rescaling the past.
            self._start_sample = self._cursor
            self._t0 = now()
            self._paused_media_offset = 0.0
            self.speed = float(speed)
            self.info.metadata["speed"] = self.speed
            return self.speed

    def position(self) -> dict[str, Any]:
        with self._lock:
            return {
                "position_sec": self._cursor / self._sfreq,
                "duration_sec": self._n_times / self._sfreq,
                "cursor_sample": self._cursor,
                "n_times": self._n_times,
                "speed": self.speed,
                "loop": self.loop,
                "loops_completed": self._loops,
                "paused": self._paused,
                "exhausted": self._exhausted,
                "progress": (
                    self._cursor / self._n_times if self._n_times else 1.0
                ),
            }

    def file_events(self, limit: int = 500) -> list[dict[str, Any]]:
        """The recording's own events, as loaded -- available before playing."""
        return [
            {
                "sample": self._timeline.samples[i],
                "onset_sec": self._timeline.samples[i] / self._sfreq,
                "label": self._timeline.labels[i],
                "value": self._timeline.values[i],
                "duration": self._timeline.durations[i],
            }
            for i in range(min(len(self._timeline), limit))
        ]
