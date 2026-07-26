# Replay-Driven Development

**The problem.** You are building a real-time pipeline that will eventually run
on a person. Every debugging cycle on hardware costs setup time, subject time,
and gel. And the interesting cases — the artifact, the drowsy epoch, the rare
event — happen when they happen, not when you are ready.

**The approach.** Develop against a recording that plays back *at the rate it
was recorded*, with its annotations arriving as live events. Your pipeline
experiences the same chunking, latency, and buffering it will face on hardware,
so when you switch to a board, nothing about the code changes.

---

## Step 1 — Look before you play

```text
inspect_recording(path="sub-04_task-rest_eeg.edf")
```

```json
{
  "kind": "replay",
  "name": "sub-04_task-rest_eeg.edf",
  "sfreq": 500.0,
  "n_channels": 64,
  "duration_sec": 612.4,
  "n_events_in_file": 240,
  "format": "edf",
  "events": [
    {"sample": 2500, "onset_sec": 5.0, "label": "T1", "duration": 0.0},
    {"sample": 5000, "onset_sec": 10.0, "label": "T2", "duration": 0.0}
  ]
}
```

This opens and describes the file without starting playback. Check the event
labels now — they are what will arrive as events, and they are what your
pipeline will key on.

!!! tip "Relative paths"
    A bare filename is resolved against `EEG_MCP_RECORDINGS_DIR`. Set it once in
    your MCP config and refer to recordings by name.

## Step 2 — Play it as if it were live

```text
start_replay(session_id="dev", path="sub-04_task-rest_eeg.edf", speed=1.0)
```

```json
{
  "started": true,
  "source": {"kind": "replay", "sfreq": 500.0, "n_channels": 64,
             "duration_sec": 612.4, "speed": 1.0},
  "replay": {"position_sec": 0.0, "progress": 0.0},
  "next": "Use the same tools as a live stream: set_filters, read_window ..."
}
```

The session is now indistinguishable from a live stream to every other tool.

### Which formats work

EDF, BDF, GDF, SET, FIF, BrainVision, and anything else MNE reads — plus
BrainFlow CSV (which needs `board=` since it has no header describing its
layout). See the [format table](../hardware.md#recording-formats-for-replay).

### Why not `PLAYBACK_FILE_BOARD`?

BrainFlow's playback board is real and useful, but it only reads BrainFlow CSV
and gives no speed control or seeking. `start_replay` reads everything MNE
reads and adds `speed`, `seek`, `pause`, and looping. Use the playback board
when you specifically want BrainFlow's own pacing:

```text
start_stream(session_id="bf", board="playback_file",
             params={"file": "rec.csv", "master_board": "cyton"})
```

## Step 3 — Build the pipeline

Exactly what you would run live:

```text
set_filters(session_id="dev", bandpass_low=1, bandpass_high=40, notch_freq=50)
get_band_power(session_id="dev", seconds=2)
```

## Step 4 — Confirm the file's events arrive as live events

```text
get_events(session_id="dev", origin="annotation")
```

```json
{
  "n_returned": 12,
  "counts_by_label": {"T1": 6, "T2": 6},
  "events": [
    {"event_id": 1, "label": "T1", "origin": "annotation",
     "timestamp": 1785062412.51, "sample_index": 2500,
     "payload": {"source_sample": 2500, "loop": 0}}
  ]
}
```

Note `timestamp` is real Unix time, not file time. Replayed events and any
stimulation you dispatch share one comparable clock — which is what lets you
measure real response latencies during development.

!!! info "Two different event lists"
    `get_events` returns what has actually been *emitted so far*.
    `list_recording_events` returns the **complete** list from the file,
    including events not yet reached. Use the latter to plan, the former to
    verify.

## Step 5 — Epoch around an event

```text
get_epoch_around_event(session_id="dev", label="T1", tmin=-0.2, tmax=0.8)
```

This is the check that proves your timing is right end to end. In the project's
own test suite, a recording with a spike planted at each annotation onset shows
that spike at **t = 0 ± 0 ms** in the extracted epoch — annotation timing,
replay clock, ring-buffer indexing, and epoch extraction all agree.

## Step 6 — Use the controls a file gives you that hardware cannot

### Jump to the interesting part

```text
replay_control(session_id="dev", action="seek", position_sec=310)
```

Seeking clears the buffers and resets filter state, so you never get a window
that silently straddles the discontinuity.

### Freeze to inspect

```text
replay_control(session_id="dev", action="pause")
replay_control(session_id="dev", action="resume")
```

### Run faster than real time

```text
replay_control(session_id="dev", action="speed", speed=8.0)
```

Ten minutes of recording in seventy-five seconds. Excellent for exercising a
pipeline over a long file.

!!! warning "Speed changes what you are testing"
    Above 1.0 you are testing **logic**, not latency. Chunks arrive larger and
    less often, so a timing bug that only appears at true rate can hide. Always
    do a final pass at `speed=1.0`.

### Loop for a long rehearsal

```text
start_replay(session_id="rehearse", path="rec.edf", loop=true)
```

Each wrap emits a `replay_loop` event so your pipeline can tell the difference
between a new pass and a discontinuity.

## Step 7 — Compare replay against live, side by side

Sessions are fully independent, so run both at once:

```text
start_replay(session_id="ref",  path="rec.edf", speed=1.0)
start_stream(session_id="live", board="cyton", params={"serial_port": "COM3"})
```

Then apply the same filters to both and compare `get_band_power` output. Any
difference is your hardware or your montage — not your code.

## Step 8 — Go live

```diff
- start_replay(session_id="dev", path="sub-04_task-rest_eeg.edf", speed=1.0)
+ start_stream(session_id="dev", board="cyton", params={"serial_port": "COM3"})
```

Nothing else changes.

---

## What carries over, and what does not

| Carries over | Does not |
|---|---|
| Tool sequence and arguments | Absolute signal quality — a file has no impedance drift |
| Filter design and group delay | Electrode contact, which only a real cap has |
| Event labels and epoch logic | Board-specific channel counts and sampling rates |
| Latency budget at `speed=1.0` | Latency measured at `speed > 1.0` |
| Buffer sizing | Dropped samples from a flaky USB link |

Run [`check_signal_quality`](../tools/dsp.md) as your first call on real
hardware. It is the one thing replay can never rehearse.
