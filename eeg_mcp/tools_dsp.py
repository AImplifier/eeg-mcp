"""MCP tools for online filtering and windowed analytics."""

from __future__ import annotations

from typing import Any

from .app import mcp
from .dsp import FilterSpec, band_powers, quality_metrics, welch_psd
from .session import store
from .utils import FREQ_BANDS, band_from_name, to_jsonable


@mcp.tool()
def set_filters(
    session_id: str,
    bandpass_low: float | None = 1.0,
    bandpass_high: float | None = 40.0,
    notch_freq: float | None = 50.0,
    order: int = 4,
    filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Install a stateful online filter chain on a running stream.

    The chain filters continuously in the background as samples arrive, carrying
    filter state across chunk boundaries. This matters: filtering each query
    window independently would inject a transient at every boundary and corrupt
    any phase-sensitive measure. Filtered output goes to a second buffer, so raw
    signal remains available via read_window(filtered=False).

    These are causal filters -- there is no zero-phase option, because that needs
    future samples. Check ``group_delay_sec`` in the response to know how far
    behind real time your filtered features are.

    Args:
        session_id: The session to filter.
        bandpass_low: High-pass corner in Hz. None to skip the bandpass.
        bandpass_high: Low-pass corner in Hz. None to skip the bandpass.
        notch_freq: Mains frequency to notch out (50 in most of the world, 60 in
            the Americas). None to skip.
        order: Butterworth order for the bandpass. 4 is a sensible default.
        filters: Advanced -- an explicit stage list that overrides the arguments
            above, e.g. [{"kind": "highpass", "low": 0.5},
            {"kind": "notch", "low": 60, "quality": 30}].
            Kinds: bandpass, bandstop, highpass, lowpass, notch.

    Returns:
        The resulting chain description including its group delay.
    """
    session = store.get(session_id)

    specs: list[FilterSpec] = []
    if filters is not None:
        for entry in filters:
            specs.append(FilterSpec(
                kind=str(entry.get("kind", "")),
                low=entry.get("low"),
                high=entry.get("high"),
                order=int(entry.get("order", order)),
                quality=float(entry.get("quality", 30.0)),
            ))
    else:
        if notch_freq:
            specs.append(FilterSpec("notch", low=float(notch_freq)))
        if bandpass_low and bandpass_high:
            specs.append(FilterSpec(
                "bandpass", low=float(bandpass_low),
                high=float(bandpass_high), order=int(order),
            ))
        elif bandpass_low:
            specs.append(FilterSpec("highpass", low=float(bandpass_low), order=order))
        elif bandpass_high:
            specs.append(FilterSpec("lowpass", high=float(bandpass_high), order=order))

    described = session.set_filters(specs)
    return to_jsonable({
        "session_id": session_id,
        "filters": described,
        "note": "Applies to samples arriving from now on; the filtered buffer was "
                "cleared. Allow one window to refill before reading features.",
    })


@mcp.tool()
def clear_filters(session_id: str) -> dict[str, Any]:
    """Remove the filter chain; reads return raw signal again.

    Args:
        session_id: The session to clear filters on.

    Returns:
        The now-empty chain description.
    """
    session = store.get(session_id)
    return to_jsonable({
        "session_id": session_id, "filters": session.set_filters([]), "cleared": True,
    })


@mcp.tool()
def get_band_power(
    session_id: str,
    seconds: float = 2.0,
    filtered: bool = True,
    bands: list[str] | None = None,
    relative: bool = True,
    channels: list[str] | None = None,
) -> dict[str, Any]:
    """Band power over the most recent window, per channel and averaged.

    The workhorse feature for neurofeedback and closed-loop triggering. Prefer
    the ``_rel`` (relative) values for thresholds: they are largely immune to
    impedance drift, so a threshold set at the start of a session still means
    the same thing an hour later.

    Args:
        session_id: The session to analyse.
        seconds: Window length. 2 s is a reasonable default; shorter windows are
            noisier, longer ones respond more slowly.
        filtered: Use the filtered buffer when a chain is set.
        bands: Band names to compute. Defaults to delta, theta, alpha, beta, gamma.
        relative: Also return each band as a fraction of total power.
        channels: Channel names to include. Defaults to all.

    Returns:
        Absolute and relative power per channel, the across-channel mean, and
        the dominant band.
    """
    session = store.require_running(session_id)
    data, _timestamps, was_filtered = session.snapshot(seconds, filtered=filtered)
    names = list(session.source.info.ch_names)

    if channels:
        missing = [c for c in channels if c not in names]
        if missing:
            raise ValueError(f"Unknown channels {missing}. Available: {', '.join(names)}")
        idx = [names.index(c) for c in channels]
        data, names = data[idx, :], list(channels)

    selected = (
        {b: band_from_name(b) for b in bands} if bands else dict(FREQ_BANDS)
    )
    result = band_powers(
        data, session.source.info.sfreq, ch_names=names,
        bands=selected, relative=relative,
    )
    return to_jsonable({
        "session_id": session_id, "filtered": was_filtered, "units": "uV^2/Hz",
        **result,
    })


@mcp.tool()
def get_psd(
    session_id: str,
    seconds: float = 4.0,
    filtered: bool = True,
    fmax: float = 60.0,
    channels: list[str] | None = None,
    max_bins: int = 120,
) -> dict[str, Any]:
    """Power spectral density over the most recent window.

    Use when band power is too coarse -- to find a peak alpha frequency, confirm
    a stimulation artifact, or check whether a notch actually removed the mains.

    Args:
        session_id: The session to analyse.
        seconds: Window length. Longer gives finer frequency resolution.
        filtered: Use the filtered buffer when a chain is set.
        fmax: Highest frequency to return.
        channels: Channel names to include. Defaults to all.
        max_bins: Cap on returned frequency bins, to keep the response readable.

    Returns:
        Frequencies, per-channel PSD, and each channel's peak frequency.
    """
    import numpy as np

    session = store.require_running(session_id)
    data, _timestamps, was_filtered = session.snapshot(seconds, filtered=filtered)
    names = list(session.source.info.ch_names)

    if channels:
        missing = [c for c in channels if c not in names]
        if missing:
            raise ValueError(f"Unknown channels {missing}. Available: {', '.join(names)}")
        idx = [names.index(c) for c in channels]
        data, names = data[idx, :], list(channels)

    freqs, psd, backend = welch_psd(data, session.source.info.sfreq)
    mask = freqs <= fmax
    freqs, psd = freqs[mask], psd[:, mask]

    step = max(1, int(np.ceil(freqs.size / max(max_bins, 1))))
    peaks = {
        name: float(freqs[int(np.argmax(psd[i]))]) if freqs.size else None
        for i, name in enumerate(names)
    }
    return to_jsonable({
        "session_id": session_id,
        "filtered": was_filtered,
        "psd_backend": backend,
        "freqs": freqs[::step],
        "psd": {name: psd[i, ::step] for i, name in enumerate(names)},
        "peak_freq": peaks,
        "freq_resolution_hz": float(freqs[1] - freqs[0]) if freqs.size > 1 else None,
        "window_sec": float(data.shape[1] / session.source.info.sfreq),
        "units": "uV^2/Hz",
    })


@mcp.tool()
def check_signal_quality(
    session_id: str, seconds: float = 2.0, line_freq: float = 50.0
) -> dict[str, Any]:
    """Per-channel health check: amplitude, flat/railed detection, line noise.

    Call this before trusting any real-time feature, and definitely before
    driving stimulation from one. A flat or railed electrode poisons an
    average reference and a band-power threshold without changing anything you
    would notice in the feature value itself.

    Args:
        session_id: The session to check.
        seconds: Window length to assess.
        line_freq: Mains frequency to test for (50 or 60).

    Returns:
        Per-channel metrics and a list of channels that look bad.
    """
    session = store.require_running(session_id)
    # Always assess the raw signal: a filter chain would hide exactly the
    # problems this tool exists to find.
    data, _timestamps, _ = session.snapshot(seconds, filtered=False)
    result = quality_metrics(
        data, session.source.info.sfreq,
        ch_names=list(session.source.info.ch_names), line_freq=line_freq,
    )
    verdict = "ok" if result["n_bad"] == 0 else "attention"
    return to_jsonable({
        "session_id": session_id, "assessed": "raw signal", "verdict": verdict,
        **result,
    })
