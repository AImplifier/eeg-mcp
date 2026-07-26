"""Generate a synthetic recording with ground truth for the persona tests.

Everything asserted downstream is planted here deliberately:

* a 10 Hz sine on the two occipital channels, and nowhere else -- so a PSD peak
  at 10 Hz on O1/O2 but not on Fz is evidence the spectral path is wired to the
  right channels;
* a one-sample 500 uV spike on Fp1 at the exact onset of every annotation -- so
  an epoch cut around an annotation must show its maximum at t=0. That single
  assertion covers the whole chain: annotation timing, replay clock, ring-buffer
  absolute indexing, and epoch extraction.

Run:  python testing/make_synthetic.py [outdir]
"""

from __future__ import annotations

import sys
from pathlib import Path

import mne
import numpy as np

SFREQ = 250.0
DURATION_SEC = 40.0
CH_NAMES = ["Fp1", "Fp2", "Fz", "Cz", "Pz", "C3", "O1", "O2"]
ALPHA_CHANNELS = ["O1", "O2"]
SPIKE_CHANNEL = "Fp1"
EVENT_ONSETS = np.arange(1.0, DURATION_SEC - 1.0, 2.0)  # seconds
ALPHA_HZ = 10.0
ALPHA_UV = 25.0
SPIKE_UV = 500.0
NOISE_UV = 5.0


def build(seed: int = 42) -> mne.io.RawArray:
    rng = np.random.default_rng(seed)
    n_times = int(DURATION_SEC * SFREQ)
    times = np.arange(n_times) / SFREQ

    # Volts internally: MNE's unit. The server converts to microvolts on read.
    data = rng.standard_normal((len(CH_NAMES), n_times)) * NOISE_UV * 1e-6

    alpha = np.sin(2 * np.pi * ALPHA_HZ * times) * ALPHA_UV * 1e-6
    for name in ALPHA_CHANNELS:
        data[CH_NAMES.index(name)] += alpha

    spike_row = CH_NAMES.index(SPIKE_CHANNEL)
    for onset in EVENT_ONSETS:
        data[spike_row, int(round(onset * SFREQ))] += SPIKE_UV * 1e-6

    info = mne.create_info(CH_NAMES, SFREQ, ch_types="eeg")
    raw = mne.io.RawArray(data, info, verbose="ERROR")
    raw.set_annotations(
        mne.Annotations(
            onset=EVENT_ONSETS,
            duration=np.zeros_like(EVENT_ONSETS),
            description=["stim"] * len(EVENT_ONSETS),
        )
    )
    return raw


def write(outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "synthetic_raw.fif"
    build().save(str(path), overwrite=True, verbose="ERROR")
    return path


GROUND_TRUTH = {
    "sfreq": SFREQ,
    "duration_sec": DURATION_SEC,
    "ch_names": CH_NAMES,
    "alpha_channels": ALPHA_CHANNELS,
    "alpha_hz": ALPHA_HZ,
    "spike_channel": SPIKE_CHANNEL,
    "event_onsets": EVENT_ONSETS.tolist(),
    "n_events": len(EVENT_ONSETS),
}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    written = write(target)
    print(f"wrote {written}")
    print(f"  {len(CH_NAMES)} ch @ {SFREQ} Hz, {DURATION_SEC}s")
    print(f"  {len(EVENT_ONSETS)} annotations, {ALPHA_HZ} Hz alpha on {ALPHA_CHANNELS}")
    print(f"  {SPIKE_UV} uV spike on {SPIKE_CHANNEL} at each annotation onset")
