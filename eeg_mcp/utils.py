"""Shared helpers: frequency bands and JSON-safe conversion.

Kept deliberately identical in spirit to neuro-mcp's ``utils`` so band names
mean the same thing across the offline and real-time servers.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

# Canonical EEG frequency bands (Hz). Must stay in sync with neuro-mcp.
FREQ_BANDS: dict[str, tuple[float, float]] = {
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 45.0),
}


def to_jsonable(obj: Any) -> Any:
    """Recursively convert numpy types/arrays into plain JSON-serializable Python."""
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        val = float(obj)
        return None if (np.isnan(val) or np.isinf(val)) else val
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, float):
        return None if (np.isnan(obj) or np.isinf(obj)) else obj
    return obj


def band_from_name(band: str) -> tuple[float, float]:
    """Resolve a band name (e.g. 'alpha') to its (fmin, fmax) range."""
    key = band.lower().strip()
    if key not in FREQ_BANDS:
        raise ValueError(f"Unknown band '{band}'. Choose from: {', '.join(FREQ_BANDS)}.")
    return FREQ_BANDS[key]


def now() -> float:
    """Wall-clock seconds since the epoch.

    Single call site so every timestamp the server produces -- sample stamps,
    event stamps, stimulation dispatch stamps -- comes from the same clock and
    can be subtracted without conversion. BrainFlow's timestamp channel is also
    Unix epoch seconds, so replayed and live data share one time base.
    """
    return time.time()
