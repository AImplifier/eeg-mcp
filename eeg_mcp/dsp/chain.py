"""Stateful IIR filtering for continuous streams.

**This is the piece most real-time EEG code gets wrong.** Calling a normal
filter function on each incoming chunk restarts the filter's internal state at
every chunk boundary, which injects a transient at each seam -- a few hundred
milliseconds of garbage, several times a second. It is invisible in a band-power
plot and fatal for anything phase-sensitive (ERPs, phase-locked stimulation,
connectivity).

The fix is ``scipy.signal.sosfilt`` with a persistent ``zi``: the filter's
delay-line state is carried from one chunk to the next, so filtering in chunks
gives bit-for-bit the same result as filtering the whole signal at once.
Second-order sections (``sos``) rather than ``b, a`` because transposed-direct-
form-II cascades stay numerically stable at the low corner frequencies EEG needs
(a 0.5 Hz high-pass at 1000 Hz is where ``b, a`` falls apart).

These are causal, forward-only filters. There is no ``filtfilt`` here: zero-phase
filtering needs future samples, which by definition do not exist yet in a live
stream. Expect group delay, and use :meth:`FilterChain.group_delay_sec` to know
how much.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import signal


@dataclass
class FilterSpec:
    """Declarative description of one stage in the chain."""

    kind: str  # bandpass | highpass | lowpass | bandstop | notch
    low: float | None = None
    high: float | None = None
    order: int = 4
    quality: float = 30.0  # notch only: Q = f0 / bandwidth

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "order": self.order}
        if self.low is not None:
            out["low"] = self.low
        if self.high is not None:
            out["high"] = self.high
        if self.kind == "notch":
            out["quality"] = self.quality
        return out


@dataclass
class _Stage:
    spec: FilterSpec
    sos: np.ndarray
    zi: np.ndarray | None = None  # (n_sections, n_channels, 2), lazily seeded


def _design(spec: FilterSpec, sfreq: float) -> np.ndarray:
    """Build second-order sections for one stage, validating against Nyquist."""
    nyq = sfreq / 2.0

    def _check(freq: float, name: str) -> float:
        if freq <= 0:
            raise ValueError(f"{name} must be > 0 Hz, got {freq}")
        if freq >= nyq:
            raise ValueError(
                f"{name}={freq} Hz is at or above Nyquist ({nyq} Hz) for "
                f"sfreq={sfreq} Hz. Choose a lower frequency."
            )
        return float(freq)

    kind = spec.kind.lower().strip()
    if kind == "bandpass":
        if spec.low is None or spec.high is None:
            raise ValueError("bandpass needs both low and high")
        low, high = _check(spec.low, "low"), _check(spec.high, "high")
        if low >= high:
            raise ValueError(f"bandpass requires low < high, got {low} >= {high}")
        return signal.butter(spec.order, [low, high], btype="bandpass",
                             fs=sfreq, output="sos")
    if kind == "bandstop":
        if spec.low is None or spec.high is None:
            raise ValueError("bandstop needs both low and high")
        low, high = _check(spec.low, "low"), _check(spec.high, "high")
        if low >= high:
            raise ValueError(f"bandstop requires low < high, got {low} >= {high}")
        return signal.butter(spec.order, [low, high], btype="bandstop",
                             fs=sfreq, output="sos")
    if kind == "highpass":
        if spec.low is None:
            raise ValueError("highpass needs low (the cutoff)")
        return signal.butter(spec.order, _check(spec.low, "low"),
                             btype="highpass", fs=sfreq, output="sos")
    if kind == "lowpass":
        if spec.high is None:
            raise ValueError("lowpass needs high (the cutoff)")
        return signal.butter(spec.order, _check(spec.high, "high"),
                             btype="lowpass", fs=sfreq, output="sos")
    if kind == "notch":
        freq = spec.low if spec.low is not None else spec.high
        if freq is None:
            raise ValueError("notch needs the line frequency (50 or 60) in 'low'")
        b, a = signal.iirnotch(_check(freq, "notch freq"), spec.quality, fs=sfreq)
        return signal.tf2sos(b, a)
    raise ValueError(
        f"Unknown filter kind '{spec.kind}'. Use bandpass, bandstop, "
        "highpass, lowpass, or notch."
    )


class FilterChain:
    """An ordered cascade of IIR stages that remembers its state between chunks."""

    def __init__(self, n_channels: int, sfreq: float) -> None:
        if n_channels <= 0:
            raise ValueError("n_channels must be positive")
        if sfreq <= 0:
            raise ValueError("sfreq must be positive")
        self.n_channels = int(n_channels)
        self.sfreq = float(sfreq)
        self._stages: list[_Stage] = []

    def add(self, spec: FilterSpec) -> "FilterChain":
        self._stages.append(_Stage(spec=spec, sos=_design(spec, self.sfreq)))
        return self

    def add_bandpass(self, low: float, high: float, order: int = 4) -> "FilterChain":
        return self.add(FilterSpec("bandpass", low=low, high=high, order=order))

    def add_notch(self, freq: float, quality: float = 30.0) -> "FilterChain":
        return self.add(FilterSpec("notch", low=freq, quality=quality))

    def process(self, chunk: np.ndarray) -> np.ndarray:
        """Filter a chunk, carrying delay-line state across calls.

        On the very first chunk each stage's state is seeded from the leading
        sample (``sosfilt_zi`` scaled by x[:, 0]) so the chain starts in steady
        state rather than ringing down from zero -- otherwise every stream would
        open with a large artificial transient.
        """
        if chunk.ndim != 2 or chunk.shape[0] != self.n_channels:
            raise ValueError(
                f"chunk must be ({self.n_channels}, n_samples), got {chunk.shape}"
            )
        if chunk.shape[1] == 0 or not self._stages:
            return chunk.copy()

        out = np.ascontiguousarray(chunk, dtype=np.float64)
        for stage in self._stages:
            if stage.zi is None:
                base = signal.sosfilt_zi(stage.sos)  # (n_sections, 2)
                # Broadcast to channels and scale by each channel's first sample.
                stage.zi = base[:, None, :] * out[None, :, 0][..., None]
            out, stage.zi = signal.sosfilt(stage.sos, out, axis=-1, zi=stage.zi)
        return out

    def reset(self) -> None:
        """Forget delay-line state; the next chunk re-seeds as if it were first."""
        for stage in self._stages:
            stage.zi = None

    def clear(self) -> None:
        self._stages.clear()

    def group_delay_sec(self, freq: float | None = None) -> float:
        """Approximate cascade group delay in seconds at ``freq``.

        Real-time closed-loop code needs this number: a stimulus triggered on a
        filtered feature is late by roughly this much.
        """
        if not self._stages:
            return 0.0
        if freq is None:
            # Default to the middle of the last bandpass, else 10 Hz (alpha).
            freq = 10.0
            for stage in self._stages:
                if stage.spec.kind == "bandpass" and stage.spec.low and stage.spec.high:
                    freq = (stage.spec.low + stage.spec.high) / 2.0
        total = 0.0
        w = np.array([float(freq)])
        for stage in self._stages:
            b, a = signal.sos2tf(stage.sos)
            # group_delay returns delay in samples when w is given in Hz with fs.
            _, gd = signal.group_delay((b, a), w=w, fs=self.sfreq)
            total += float(gd[0])
        return total / self.sfreq

    def describe(self) -> dict[str, Any]:
        return {
            "n_channels": self.n_channels,
            "sfreq": self.sfreq,
            "n_stages": len(self._stages),
            "stages": [s.spec.as_dict() for s in self._stages],
            "primed": any(s.zi is not None for s in self._stages),
            "group_delay_sec": self.group_delay_sec() if self._stages else 0.0,
        }

    def __len__(self) -> int:
        return len(self._stages)
