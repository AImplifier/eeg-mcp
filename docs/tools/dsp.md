# Signal Processing Tools

Online filtering and windowed analytics.

---

## `set_filters`

Install a **stateful** online filter chain.

| Arg | Default | Meaning |
|---|---|---|
| `bandpass_low` | `1.0` | High-pass corner, Hz. `None` to skip |
| `bandpass_high` | `40.0` | Low-pass corner, Hz. `None` to skip |
| `notch_freq` | `50.0` | Mains frequency to notch. `None` to skip |
| `order` | `4` | Butterworth order |
| `filters` | `None` | Explicit stage list, overrides the above |

```text
set_filters(session_id="s1", bandpass_low=1, bandpass_high=40, notch_freq=60)

set_filters(session_id="s1", filters=[
  {"kind": "notch",    "low": 50, "quality": 30},
  {"kind": "highpass", "low": 0.5, "order": 2},
  {"kind": "lowpass",  "high": 70, "order": 4}
])
```

Kinds: `bandpass`, `bandstop`, `highpass`, `lowpass`, `notch`.

### Why this is not applied per query

A stateful IIR filter must see every sample exactly once, in order. Filtering
each query window independently restarts the filter at every boundary and
injects a transient each time — invisible in a band-power plot, fatal for
anything phase-sensitive.

So the **producer thread** filters each chunk once as it arrives, carrying
`sosfilt` delay-line state forward, and writes to a second ring buffer. The test
suite asserts chunked filtering equals whole-signal filtering to `1e-9`, and
asserts as a control that the naive approach does not.

Consequences:

- Filters apply to samples arriving **from now on**; the filtered buffer is
  cleared on change. Allow one window before reading features.
- Raw is always retained — `filtered=false` anywhere gets it back.
- Second-order sections, not `b, a`, because a 0.5 Hz high-pass at 1000 Hz is
  where transfer-function cascades fall apart numerically.

!!! warning "Causal only"
    There is no `filtfilt`. Zero-phase filtering needs future samples, which do
    not exist yet in a live stream. `group_delay_sec` in the response is how far
    behind real time your filtered features are.

    Narrow bands cost more delay: 8–13 Hz ≈ 41 ms, 1–40 Hz ≈ 13 ms. Often better
    to filter wide and let `get_band_power` do band separation in the frequency
    domain at no latency cost.

Frequencies at or above Nyquist are rejected with the actual limit named.

---

## `clear_filters`

Remove the chain; reads return raw again.

---

## `get_band_power`

Band power over the recent window, per channel and averaged.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `2.0` | Window length |
| `filtered` | `true` | Use the filtered buffer |
| `bands` | all five | Subset of `delta`,`theta`,`alpha`,`beta`,`gamma` |
| `relative` | `true` | Also return each band as a fraction of total |
| `channels` | `None` | Subset by name |

```json
{
  "dominant_band": "alpha",
  "mean_across_channels": {"alpha": 41.2, "alpha_rel": 0.38},
  "per_channel": {"O1": {"alpha": 88.4, "alpha_rel": 0.71}},
  "psd_backend": "brainflow",
  "window_sec": 2.0
}
```

!!! tip "Threshold on `*_rel`, not absolute"
    Relative power is largely immune to impedance drift and electrode gain, so a
    threshold set at the start of a session still means the same thing an hour
    later. Absolute power will not.

Band definitions (Hz), identical to neuro-mcp: delta 1–4, theta 4–8, alpha 8–13,
beta 13–30, gamma 30–45.

Window length trades noise against responsiveness: 0.5 s is immediate but noisy,
2 s is the default, 5 s+ is stable but sluggish.

---

## `get_psd`

Power spectral density.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `4.0` | Window; longer gives finer resolution |
| `fmax` | `60.0` | Highest frequency returned |
| `max_bins` | `120` | Decimation cap |

**Returns:** `freqs`, `psd` per channel, `peak_freq` per channel,
`freq_resolution_hz`.

Use it to find a peak alpha frequency, confirm a stimulation artifact, or verify
a notch actually removed the mains.

BrainFlow's C++ Welch implementation is used when importable, with a SciPy
fallback; `psd_backend` says which ran.

---

## `check_signal_quality`

Per-channel health check. **Call this before trusting any feature.**

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `2.0` | Window to assess |
| `line_freq` | `50.0` | Mains frequency to test for |

```json
{
  "verdict": "attention",
  "n_bad": 2,
  "bad_channels": ["T6", "O2"],
  "assessed": "raw signal",
  "channels": {
    "T6": {"rms_uv": 0.04, "std_uv": 0.03, "flat": true, "railed": false,
           "line_noisy": false}
  }
}
```

| Flag | Trigger | Usual cause |
|---|---|---|
| `flat` | std < 0.1 µV | Disconnected lead |
| `railed` | amplitude > 200 mV | Saturated amplifier |
| `line_noisy` | >20% of 1–100 Hz power at mains | Poor ground/reference |

!!! info "Always assessed on raw signal"
    Even with a chain set. A notch filter would hide exactly the mains problem
    this tool exists to find.

`rms_uv` of 10–50 µV is typical for scalp EEG.
