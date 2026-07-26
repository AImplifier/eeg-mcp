# Live Clinical Review

**The workflow.** Open a recording or a live cap, *look* at it, judge whether
the signal can be trusted, mark what you see, and keep a record that will open
on any machine years from now.

!!! danger "Research use only"
    eeg-mcp is not a medical device and is not validated for clinical
    decision-making. Nothing it produces is a diagnosis. See [Safety](../safety.md).

---

## Step 1 — Open the recording

```text
inspect_recording(path="patient_042_routine.edf")
```

```json
{
  "n_channels": 21,
  "sfreq": 256.0,
  "duration_sec": 1240.0,
  "n_events_in_file": 8,
  "ch_names": ["Fp1", "Fp2", "F3", "F4", "..."],
  "events": [{"onset_sec": 300.2, "label": "hyperventilation_start"}]
}
```

You now know the montage, the length, and what the technologist annotated —
before loading anything into a viewer.

## Step 2 — Play it

```text
start_replay(session_id="review", path="patient_042_routine.edf", speed=1.0)
```

For a first pass, review faster than real time and slow down when something
catches your eye:

```text
replay_control(session_id="review", action="speed", speed=4.0)
```

## Step 3 — Judge the signal before reading it

```text
check_signal_quality(session_id="review")
```

```json
{
  "verdict": "attention",
  "n_bad": 2,
  "bad_channels": ["T6", "O2"],
  "assessed": "raw signal",
  "channels": {
    "T6": {"rms_uv": 0.04, "ptp_uv": 0.2, "flat": true, "railed": false,
           "line_noisy": false},
    "O2": {"rms_uv": 112.4, "line_noise_ratio": 0.34, "flat": false,
           "railed": false, "line_noisy": true}
  }
}
```

Read as: **T6 is flat** (disconnected electrode) and **O2 has 34% of its power
at mains frequency** (bad ground or loose contact). Both would corrupt an
average reference, and neither is obvious from the traces at a glance.

Note `"assessed": "raw signal"` — quality is deliberately measured before any
filter, since a notch would hide exactly the mains problem you need to see.

## Step 4 — Watch it

```text
start_monitor(session_id="review", window_sec=10)
```

Open the returned URL. You get:

- **Rolling traces**, each channel autoscaled to its own amplitude — which is
  what keeps a montage readable when one electrode has ten times the amplitude
  of its neighbour. The per-channel peak in µV is printed beside each trace.
- **Event markers** as vertical lines, dashed for annotations, solid red for
  stimulation.
- **Live band power** and the dominant band.
- **A per-electrode quality table** that updates as you watch.
- **A filtered/raw toggle**, so you can see exactly what the filter is doing.
- **Transport controls** — pause, scrub, skip, speed, mark, save, stop — so you
  can review without going back to the agent for every action.
  <kbd>Space</kbd> pauses, <kbd>←</kbd>/<kbd>→</kbd> skip 10 s, <kbd>M</kbd> marks.

!!! tip "Showing the screen to someone else"
    `start_monitor(session_id="review", allow_control=false)` renders no buttons
    and refuses control requests even with a valid token — a strictly viewable
    screen.

!!! warning "Loopback only"
    The page binds to `127.0.0.1` and requires the random token in its URL. It is
    unencrypted and is not an authentication system — never expose it through a
    tunnel or proxy. It shuts down with the session.

## Step 5 — Apply a review montage filter

```text
set_filters(session_id="review", bandpass_low=0.5, bandpass_high=70, notch_freq=50)
```

Standard clinical review settings. Raw remains available — in the monitor via
the toggle, and in any tool via `filtered=false`.

## Step 6 — Find the posterior dominant rhythm

```text
plot_spectrum(session_id="review", seconds=8, channels=["O1","O2","P3","P4"])
```

```json
{
  "path": "/home/you/.eeg-mcp/plots/spectrum-review-20260726-143012.html",
  "peak_freq": {"O1": 9.77, "O2": 9.77, "P3": 9.51, "P4": 9.77},
  "dominant_band": "alpha"
}
```

A posterior dominant rhythm at ~9.8 Hz. The written figure shades the classical
bands behind the spectrum and includes a relative band-power bar per channel.

## Step 7 — Annotate as you go

```text
log_event(session_id="review", label="eyes_closed", value=11)
log_event(session_id="review", label="artifact_movement", value=12)
log_event(session_id="review", label="drowsy_onset", value=13)
```

Your notes land in the same log as the recording's own annotations:

```text
get_events(session_id="review")
```

```json
{
  "counts_by_label": {"hyperventilation_start": 1, "eyes_closed": 1,
                      "artifact_movement": 1, "drowsy_onset": 1},
  "events": [
    {"label": "hyperventilation_start", "origin": "annotation"},
    {"label": "eyes_closed", "origin": "manual"}
  ]
}
```

`origin` keeps them distinguishable — `annotation` came from the file,
`manual` is yours — while they share one timeline.

## Step 8 — Look at a specific moment

```text
get_epoch_around_event(session_id="review", label="artifact_movement",
                       tmin=-1.0, tmax=2.0, filtered=false)
```

Or freeze the replay and inspect:

```text
replay_control(session_id="review", action="pause")
plot_stream(session_id="review", seconds=10, filtered=false)
```

## Step 9 — Produce the report

```text
export_report(session_id="review", seconds=15, notes="Routine 20-minute EEG.
Awake, eyes closed at rest. PDR ~9.8 Hz symmetric. T6 flat throughout — "
"electrode fault, not cerebral. O2 mains contamination.")
```

```json
{
  "path": "/home/you/.eeg-mcp/plots/report-review-20260726-143355.html",
  "n_events": 4,
  "bad_channels": ["T6", "O2"],
  "dominant_band": "alpha",
  "sections": ["traces", "spectrum", "quality", "events"]
}
```

One HTML file, ~120 KB, containing your notes, traces with event markers, the
spectrum, the per-electrode quality table with faults called out, and the full
event log.

!!! success "Genuinely self-contained"
    No CDN, no external stylesheet, no linked images — everything is inlined as
    SVG and CSS. It opens on an air-gapped machine, survives being emailed, and
    renders identically in five years. The test suite asserts this by scanning
    the output for any external URL.

## Step 10 — Close down

```text
stop_stream(session_id="review")
```

```json
{"stopped": true, "monitor_stopped": true}
```

The monitor stops with the session, so no port is left bound serving a frozen
view of patient data.

---

## Live cap setup

The same tools work while a cap is being applied — this is where the live
monitor earns its keep:

```text
start_stream(session_id="setup", board="cyton", params={"serial_port": "COM3"})
start_monitor(session_id="setup", window_sec=5, refresh_ms=300)
```

Watch the quality table as electrodes go on. `flat` clears as contact is made;
`line_noisy` clears as the ground improves. Re-check numerically at any point:

```text
check_signal_quality(session_id="setup", line_freq=50)
```

## Reading the quality flags

| Flag | Means | Usual cause |
|---|---|---|
| `flat` | std below 0.1 µV | Disconnected lead, dead channel |
| `railed` | amplitude at the converter limit | Saturated amplifier, huge offset |
| `line_noisy` | >20% of 1–100 Hz power at mains | Poor ground/reference, loose contact |

`rms_uv` in the 10–50 µV range is typical for scalp EEG. Far below suggests poor
contact; far above suggests artifact or a bad reference.
