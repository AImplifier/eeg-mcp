"""Windowed analytics: PSD, band power, and signal-quality checks.

Unlike :mod:`eeg_mcp.dsp.chain`, nothing here is stateful -- these run on a
snapshot pulled from the ring buffer and answer "what does the signal look like
right now". BrainFlow's C++ ``DataFilter`` does the heavy lifting when it is
importable (it is a hard dependency, but the SciPy path keeps the module usable
in a stripped test environment and lets results be cross-checked).
"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy import signal

from ..utils import FREQ_BANDS


def _next_pow2(n: int) -> int:
    return 1 << max(int(n - 1).bit_length(), 1)


def welch_psd(
    data: np.ndarray,
    sfreq: float,
    nfft: int | None = None,
    backend: str = "auto",
) -> tuple[np.ndarray, np.ndarray, str]:
    """Welch PSD per channel; returns (freqs, psd (n_channels, n_freqs), backend).

    ``nfft`` defaults to the largest power of two that fits the window, capped
    at ~1 s of data -- a compromise between frequency resolution and having
    enough segments to average away variance.
    """
    if data.ndim != 2:
        raise ValueError(f"data must be 2-D (n_channels, n_samples), got {data.shape}")
    n_samples = data.shape[1]
    if n_samples < 16:
        raise ValueError(
            f"Need at least 16 samples for a PSD, got {n_samples}. "
            "Request a longer window."
        )
    if nfft is None:
        nfft = min(_next_pow2(n_samples) // 2, _next_pow2(int(sfreq)))
        nfft = max(min(nfft, n_samples), 16)
    nfft = int(min(nfft, n_samples))

    if backend in ("auto", "brainflow"):
        try:
            from brainflow.data_filter import DataFilter, WindowOperations

            overlap = nfft // 2
            rows = []
            freqs: np.ndarray | None = None
            for ch in range(data.shape[0]):
                series = np.ascontiguousarray(data[ch], dtype=np.float64)
                ampl, frq = DataFilter.get_psd_welch(
                    series, nfft, overlap, int(round(sfreq)),
                    WindowOperations.HANNING.value,
                )
                rows.append(np.asarray(ampl, dtype=np.float64))
                freqs = np.asarray(frq, dtype=np.float64)
            if freqs is not None:
                return freqs, np.vstack(rows), "brainflow"
        except Exception:
            if backend == "brainflow":
                raise
            # Fall through to SciPy.

    freqs, psd = signal.welch(
        data, fs=sfreq, nperseg=nfft, noverlap=nfft // 2,
        window="hann", axis=-1, scaling="density",
    )
    return np.asarray(freqs), np.atleast_2d(psd), "scipy"


def band_powers(
    data: np.ndarray,
    sfreq: float,
    ch_names: list[str] | None = None,
    bands: dict[str, tuple[float, float]] | None = None,
    relative: bool = True,
    backend: str = "auto",
) -> dict[str, Any]:
    """Absolute (and optionally relative) power per band, per channel.

    Relative power is each band divided by total power across all requested
    bands. It is the more useful number in practice: it is largely immune to
    impedance drift and electrode gain, so an alpha-neurofeedback threshold set
    on relative power survives a session that absolute power would not.
    """
    bands = bands or FREQ_BANDS
    freqs, psd, used = welch_psd(data, sfreq, backend=backend)

    per_channel: dict[str, dict[str, float]] = {}
    names = ch_names or [f"ch{i}" for i in range(psd.shape[0])]
    if len(names) != psd.shape[0]:
        names = [f"ch{i}" for i in range(psd.shape[0])]

    integrate = getattr(np, "trapezoid", None) or np.trapz
    abs_matrix = np.zeros((psd.shape[0], len(bands)), dtype=np.float64)
    band_items = list(bands.items())

    for bi, (_band, (fmin, fmax)) in enumerate(band_items):
        mask = (freqs >= fmin) & (freqs <= fmax)
        if not np.any(mask):
            continue
        abs_matrix[:, bi] = integrate(psd[:, mask], x=freqs[mask], axis=-1)

    totals = abs_matrix.sum(axis=1)
    for ci, name in enumerate(names):
        entry: dict[str, float] = {}
        for bi, (band, _rng) in enumerate(band_items):
            entry[band] = float(abs_matrix[ci, bi])
            if relative:
                total = totals[ci]
                entry[f"{band}_rel"] = (
                    float(abs_matrix[ci, bi] / total) if total > 0 else 0.0
                )
        per_channel[name] = entry

    mean_abs = abs_matrix.mean(axis=0)
    summary = {band: float(mean_abs[bi]) for bi, (band, _r) in enumerate(band_items)}
    grand_total = float(mean_abs.sum())
    if relative and grand_total > 0:
        for bi, (band, _r) in enumerate(band_items):
            summary[f"{band}_rel"] = float(mean_abs[bi] / grand_total)

    dominant = max(band_items, key=lambda kv: summary.get(kv[0], 0.0))[0]
    return {
        "bands": {b: list(r) for b, r in band_items},
        "per_channel": per_channel,
        "mean_across_channels": summary,
        "dominant_band": dominant,
        "n_samples": int(data.shape[1]),
        "window_sec": float(data.shape[1] / sfreq),
        "psd_backend": used,
    }


def quality_metrics(
    data: np.ndarray,
    sfreq: float,
    ch_names: list[str] | None = None,
    line_freq: float = 50.0,
    flat_threshold_uv: float = 0.1,
    railed_threshold_uv: float = 200_000.0,
) -> dict[str, Any]:
    """Per-channel health check: amplitude, flatness, railing, line noise.

    Worth calling before trusting any real-time feature. A flat or railed
    channel silently poisons an average-referenced montage and a band-power
    threshold, and neither shows up in the feature value itself.
    """
    names = ch_names or [f"ch{i}" for i in range(data.shape[0])]
    if len(names) != data.shape[0]:
        names = [f"ch{i}" for i in range(data.shape[0])]

    rms = np.sqrt(np.mean(np.square(data), axis=1))
    ptp = np.ptp(data, axis=1)
    std = np.std(data, axis=1)

    # Line-noise ratio: power in a narrow band around the mains frequency
    # relative to total 1-100 Hz power. > ~0.2 usually means a bad ground.
    line_ratio = np.zeros(data.shape[0])
    try:
        freqs, psd, _ = welch_psd(data, sfreq)
        line_mask = (freqs >= line_freq - 2) & (freqs <= line_freq + 2)
        broad_mask = (freqs >= 1) & (freqs <= min(100.0, sfreq / 2 - 1))
        if np.any(line_mask) and np.any(broad_mask):
            broad = psd[:, broad_mask].sum(axis=1)
            line_ratio = np.divide(
                psd[:, line_mask].sum(axis=1), broad,
                out=np.zeros_like(broad), where=broad > 0,
            )
    except ValueError:
        pass  # Window too short for a PSD; amplitude metrics still stand.

    channels = {}
    bad: list[str] = []
    for i, name in enumerate(names):
        is_flat = bool(std[i] < flat_threshold_uv)
        is_railed = bool(np.max(np.abs(data[i])) > railed_threshold_uv)
        noisy = bool(line_ratio[i] > 0.2)
        if is_flat or is_railed or noisy:
            bad.append(name)
        channels[name] = {
            "rms_uv": float(rms[i]),
            "ptp_uv": float(ptp[i]),
            "std_uv": float(std[i]),
            "line_noise_ratio": float(line_ratio[i]),
            "flat": is_flat,
            "railed": is_railed,
            "line_noisy": noisy,
        }
    return {
        "channels": channels,
        "bad_channels": bad,
        "n_bad": len(bad),
        "line_freq": line_freq,
        "window_sec": float(data.shape[1] / sfreq),
    }
