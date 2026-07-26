"""Continuous recording of a live session to disk.

**Why this cannot be "dump the ring buffer at the end".** The ring buffer holds
the last ``EEG_MCP_BUFFER_SEC`` seconds and nothing more, so by the time an hour
long session ends, 59 of every 60 minutes have already been overwritten. A
recorder has to be fed from the producer thread as chunks arrive.

**Why it writes a raw sidecar first, then converts.** Building an MNE ``Raw`` in
memory and saving at the end means a crash, a power cut, or a killed process
loses the entire session. So samples are appended to a flat binary file as they
arrive and the header is written up front; if the process dies, the data is
still on disk and :func:`recover` can finish the job. Only once the ``.fif`` is
written successfully are the intermediates removed.

What gets recorded is the **raw, unfiltered** signal. The filter chain is a
viewing and feature-extraction convenience that can be reconstructed from the
recorded parameters; the amplifier's actual output cannot be reconstructed from
anything. Filter settings are stored in the header so the view is reproducible.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# float32 halves the file size against float64 and still resolves far below the
# noise floor of any EEG amplifier (24-bit at +/-187 mV is ~22 nV per count).
_SAMPLE_DTYPE = np.float32
_HEADER_VERSION = 1


def _sanitise(value: str) -> str:
    """Make a BIDS-ish entity value safe for a filename."""
    keep = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(value))
    return keep.strip("-") or "x"


def build_stem(
    session_id: str,
    subject: str | None = None,
    session_label: str | None = None,
    task: str | None = None,
    run: str | None = None,
    name: str | None = None,
) -> str:
    """BIDS-style filename stem when entities are given, else a timestamped one."""
    if name:
        return _sanitise(name)
    parts = []
    if subject:
        parts.append(f"sub-{_sanitise(subject)}")
    if session_label:
        parts.append(f"ses-{_sanitise(session_label)}")
    if task:
        parts.append(f"task-{_sanitise(task)}")
    if run:
        parts.append(f"run-{_sanitise(run)}")
    if parts:
        parts.append("eeg")
        return "_".join(parts)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{_sanitise(session_id)}-{stamp}"


class Recorder:
    """Appends raw samples to disk, then finalises to a ``.fif`` on close.

    ``write`` is called from the producer thread and must stay cheap: it does a
    dtype cast and a file append, nothing else.
    """

    def __init__(
        self,
        directory: Path,
        stem: str,
        ch_names: list[str],
        sfreq: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stem = stem
        self.ch_names = list(ch_names)
        self.sfreq = float(sfreq)
        self.metadata = dict(metadata or {})

        self.raw_path = self.directory / f"{stem}.eegraw"
        self.ts_path = self.directory / f"{stem}.eegts"
        self.header_path = self.directory / f"{stem}.eegmcp.json"
        self.fif_path = self.directory / f"{stem}_raw.fif"

        self._raw_file = None
        self._ts_file = None
        self._lock = threading.Lock()
        self._n_samples = 0
        self._t_first: float | None = None
        self._t_last: float | None = None
        self.started_at: float | None = None
        self.closed = False

    # --- lifecycle --------------------------------------------------------

    def open(self, started_at: float) -> None:
        if self._raw_file is not None:
            return
        if self.fif_path.exists():
            raise FileExistsError(
                f"{self.fif_path} already exists. Choose a different name, or "
                "move the existing recording aside."
            )
        self.started_at = started_at
        # The header is written before any sample, so an interrupted recording
        # is still interpretable from what reached the disk.
        self.header_path.write_text(json.dumps({
            "header_version": _HEADER_VERSION,
            "stem": self.stem,
            "ch_names": self.ch_names,
            "n_channels": len(self.ch_names),
            "sfreq": self.sfreq,
            "dtype": np.dtype(_SAMPLE_DTYPE).name,
            "layout": "sample-major interleaved (n_samples, n_channels)",
            "units": "microvolts",
            "started_at": started_at,
            "started_at_iso": datetime.fromtimestamp(
                started_at, tz=timezone.utc
            ).isoformat(),
            "metadata": self.metadata,
        }, indent=2), encoding="utf-8")
        self._raw_file = open(self.raw_path, "wb")
        self._ts_file = open(self.ts_path, "wb")

    def write(self, chunk: np.ndarray, timestamps: np.ndarray) -> int:
        """Append one chunk. Called from the producer thread."""
        if self._raw_file is None or self.closed:
            return 0
        if chunk.ndim != 2 or chunk.shape[0] != len(self.ch_names):
            raise ValueError(
                f"chunk must be ({len(self.ch_names)}, n_samples), got {chunk.shape}"
            )
        n = int(chunk.shape[1])
        if n == 0:
            return 0
        with self._lock:
            # Transpose to sample-major so an interrupted file is still a whole
            # number of samples and can be truncated cleanly at any point.
            self._raw_file.write(
                np.ascontiguousarray(chunk.T, dtype=_SAMPLE_DTYPE).tobytes()
            )
            self._ts_file.write(
                np.ascontiguousarray(timestamps, dtype=np.float64).tobytes()
            )
            self._n_samples += n
            if self._t_first is None and timestamps.size:
                self._t_first = float(timestamps[0])
            if timestamps.size:
                self._t_last = float(timestamps[-1])
        return n

    def flush(self) -> None:
        with self._lock:
            if self._raw_file is not None:
                self._raw_file.flush()
            if self._ts_file is not None:
                self._ts_file.flush()

    @property
    def n_samples(self) -> int:
        with self._lock:
            return self._n_samples

    @property
    def duration_sec(self) -> float:
        return self._n_samples / self.sfreq if self.sfreq else 0.0

    @property
    def bytes_written(self) -> int:
        return self._n_samples * len(self.ch_names) * np.dtype(_SAMPLE_DTYPE).itemsize

    def status(self) -> dict[str, Any]:
        return {
            "recording": not self.closed and self._raw_file is not None,
            "stem": self.stem,
            "directory": str(self.directory),
            "fif_path": str(self.fif_path),
            "n_samples": self.n_samples,
            "duration_sec": self.duration_sec,
            "size_mb": self.bytes_written / (1024 * 1024),
            "sfreq": self.sfreq,
            "n_channels": len(self.ch_names),
            "started_at": self.started_at,
            "metadata": self.metadata,
        }

    def close(
        self, events: list[dict[str, Any]] | None = None, keep_sidecar: bool = False
    ) -> dict[str, Any]:
        """Finalise: convert the sidecar to ``.fif`` with annotations attached."""
        with self._lock:
            if self.closed:
                raise RuntimeError(f"Recorder for '{self.stem}' is already closed.")
            for handle in (self._raw_file, self._ts_file):
                if handle is not None:
                    handle.flush()
                    handle.close()
            self._raw_file = self._ts_file = None
            self.closed = True

        if self._n_samples == 0:
            # Nothing arrived. Remove the empty sidecar rather than leaving a
            # zero-sample file that looks like a real recording.
            for path in (self.raw_path, self.ts_path):
                path.unlink(missing_ok=True)
            return {
                "written": False,
                "reason": "No samples were recorded.",
                "header_path": str(self.header_path),
            }

        annotations = self._relative_events(events or [])
        raw = self._to_raw(annotations)
        raw.save(str(self.fif_path), overwrite=False, verbose="ERROR")

        header = json.loads(self.header_path.read_text(encoding="utf-8"))
        header.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "n_samples": self._n_samples,
            "duration_sec": self.duration_sec,
            "fif_path": str(self.fif_path),
            "n_annotations": len(annotations),
            "t_first": self._t_first,
            "t_last": self._t_last,
        })
        self.header_path.write_text(json.dumps(header, indent=2), encoding="utf-8")

        # Only now is it safe to drop the crash-recovery copy.
        if not keep_sidecar:
            self.raw_path.unlink(missing_ok=True)
            self.ts_path.unlink(missing_ok=True)

        return {
            "written": True,
            "fif_path": str(self.fif_path),
            "header_path": str(self.header_path),
            "sidecar_kept": keep_sidecar,
            "n_samples": self._n_samples,
            "duration_sec": self.duration_sec,
            "sfreq": self.sfreq,
            "n_channels": len(self.ch_names),
            "n_annotations": len(annotations),
            "size_mb": self.fif_path.stat().st_size / (1024 * 1024),
            "annotations": annotations,
        }

    # --- conversion -------------------------------------------------------

    def _relative_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert absolute event timestamps to onsets from the recording start.

        Events outside the recorded span are dropped rather than clamped -- an
        annotation at t=0 that actually happened before recording began would be
        a quiet falsification of the record.
        """
        if self._t_first is None:
            return []
        span = self.duration_sec
        out = []
        for event in events:
            stamp = event.get("timestamp")
            if stamp is None:
                continue
            onset = float(stamp) - self._t_first
            if onset < 0 or onset > span:
                continue
            out.append({
                "onset": onset,
                "duration": float(event.get("duration", 0.0) or 0.0),
                "label": str(event.get("label", "event")),
                "payload": {
                    "origin": event.get("origin"),
                    "value": event.get("value"),
                    **(event.get("payload") or {}),
                },
            })
        return out

    def _to_raw(self, annotations: list[dict[str, Any]]):
        import mne

        mne.set_log_level("ERROR")
        data = np.fromfile(self.raw_path, dtype=_SAMPLE_DTYPE)
        n_ch = len(self.ch_names)
        usable = (data.size // n_ch) * n_ch
        if usable != data.size:
            # A partial trailing sample means the process died mid-write.
            data = data[:usable]
        data = data.reshape(-1, n_ch).T.astype(np.float64)

        info = mne.create_info(self.ch_names, self.sfreq, ch_types="eeg")
        # Back to volts: MNE's unit convention, and what neuro-mcp expects.
        raw = mne.io.RawArray(data * 1e-6, info, verbose="ERROR")
        raw.set_meas_date(
            datetime.fromtimestamp(self.started_at or 0.0, tz=timezone.utc)
        )
        if annotations:
            raw.set_annotations(mne.Annotations(
                onset=[a["onset"] for a in annotations],
                duration=[a["duration"] for a in annotations],
                description=[a["label"] for a in annotations],
            ))
        return raw


def recover(header_path: str | Path, keep_sidecar: bool = False) -> dict[str, Any]:
    """Finish a recording whose process died before it could be closed.

    The sidecar and header are written as the session runs precisely so this is
    possible. Events cannot be recovered -- they lived in memory -- so the
    resulting file carries signal without annotations.
    """
    header_path = Path(header_path)
    header = json.loads(header_path.read_text(encoding="utf-8"))
    if header.get("fif_path") and Path(header["fif_path"]).exists():
        return {"written": False, "reason": "This recording was already finalised.",
                "fif_path": header["fif_path"]}

    directory = header_path.parent
    stem = header["stem"]
    recorder = Recorder(directory, stem, header["ch_names"], header["sfreq"],
                        header.get("metadata"))
    if not recorder.raw_path.exists():
        return {"written": False,
                "reason": f"No sidecar at {recorder.raw_path}; nothing to recover."}

    recorder.started_at = header.get("started_at")
    recorder._t_first = header.get("started_at")
    size = recorder.raw_path.stat().st_size
    itemsize = np.dtype(_SAMPLE_DTYPE).itemsize
    recorder._n_samples = size // (itemsize * len(header["ch_names"]))
    recorder.closed = True

    raw = recorder._to_raw([])
    raw.save(str(recorder.fif_path), overwrite=False, verbose="ERROR")
    header.update({"recovered": True, "fif_path": str(recorder.fif_path),
                   "n_samples": recorder._n_samples,
                   "duration_sec": recorder.duration_sec})
    header_path.write_text(json.dumps(header, indent=2), encoding="utf-8")
    if not keep_sidecar:
        recorder.raw_path.unlink(missing_ok=True)
        recorder.ts_path.unlink(missing_ok=True)
    return {"written": True, "recovered": True, "fif_path": str(recorder.fif_path),
            "n_samples": recorder._n_samples, "duration_sec": recorder.duration_sec,
            "note": "Events were not recovered; they are held in memory only."}
