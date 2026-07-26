# Replay Tools

Playing an existing recording as if it were arriving live.

A replay session is the *same object* as a live stream — every acquisition, DSP,
event, visualization, and stimulation tool works on it identically. Only the
tools on this page require a replay.

---

## `inspect_recording`

Open a file and describe it without starting playback.

| Arg | Default | Meaning |
|---|---|---|
| `path` | — | Absolute, or relative to `EEG_MCP_RECORDINGS_DIR` |
| `board` | `None` | **Required for BrainFlow CSV**, which has no layout header |
| `max_events` | `100` | Cap on listed events |

**Returns:** `sfreq`, `n_channels`, `ch_names`, `duration_sec`,
`n_events_in_file`, `format`, and the `events` list with onsets in seconds.

Use it to check event labels before building a pipeline that keys on them.

---

## `start_replay`

Play a recording against a wall clock, re-emitting its annotations as events.

| Arg | Default | Meaning |
|---|---|---|
| `session_id` | — | Handle for later calls |
| `path` | — | Absolute or relative to the recordings dir |
| `speed` | `1.0` | Playback rate; 1.0 is true recording rate |
| `loop` | `false` | Restart at the end, emitting `replay_loop` |
| `start_sec` | `0.0` | Offset to begin from |
| `board` | `None` | Required for BrainFlow CSV |
| `picks` | `None` | MNE channel selection, e.g. `"eeg"` |
| `use_stim_channel` | `true` | Also extract events from a trigger channel |

Timestamps are **real Unix time**, not file time, so replayed events and
dispatched stimulations share one comparable clock.

!!! warning "Speed changes what you are testing"
    Above 1.0 you are testing logic, not latency — chunks arrive larger and less
    often. Always do a final pass at `speed=1.0`.

### Supported formats

EDF, BDF, GDF, SET, FIF, BrainVision, and anything else MNE reads, plus
BrainFlow CSV. See the [format table](../hardware.md#recording-formats-for-replay).

### `start_replay` vs `PLAYBACK_FILE_BOARD`

| | `start_replay` | `PLAYBACK_FILE_BOARD` |
|---|---|---|
| Formats | Everything MNE reads + BrainFlow CSV | BrainFlow CSV only |
| Speed control | Yes | No |
| Seek / pause | Yes | No |
| Looping | Yes | Yes (`loopback_true`) |
| Pacing | This server's media clock | BrainFlow's own |

Use the playback board when you specifically want BrainFlow's pacing; otherwise
`start_replay`.

---

## `replay_control`

Pause, resume, seek, or change speed.

| Arg | Meaning |
|---|---|
| `action` | `pause`, `resume`, `seek`, `speed`, `status` |
| `position_sec` | Required for `seek` |
| `speed` | Required for `speed` |

```text
replay_control(session_id="r1", action="pause")
replay_control(session_id="r1", action="seek", position_sec=310)
replay_control(session_id="r1", action="speed", speed=8.0)
```

**Returns:** `replay` with `position_sec`, `duration_sec`, `progress`, `speed`,
`loops_completed`, `paused`, `exhausted`.

!!! info "Seek clears the buffers"
    Both ring buffers are cleared and filter state is reset, so you never get a
    window that silently straddles the discontinuity. Absolute sample indices
    stay monotonic, so events from before the jump correctly read as having
    scrolled out rather than pointing at unrelated samples.

Calling this on a live session raises — playback controls apply only to replays.

---

## `list_recording_events`

Every event in the source file, including ones not yet reached.

!!! tip "Two different lists"
    `list_recording_events` — the **complete** list from the file. Use it to plan.

    `get_events` — what has actually been **emitted so far**. Use it to verify.
