# Acquisition Tools

Opening a board and reading from the ring buffer.

---

## `list_boards`

Every board BrainFlow can acquire from, with ids and sampling rates.

**Returns:** `n_boards`, and per board `name`, `board_id`, `sfreq`,
`n_eeg_channels`.

Three need no hardware: `SYNTHETIC_BOARD` (-1), `PLAYBACK_FILE_BOARD` (-3),
`STREAMING_BOARD` (-2). Full table: [Supported Hardware](../hardware.md).

---

## `start_stream`

Open a board and begin buffering in the background.

| Arg | Default | Meaning |
|---|---|---|
| `session_id` | — | Handle for every later call |
| `board` | `"synthetic"` | Board name or numeric id |
| `params` | `None` | BrainFlow connection settings |
| `channels` | `None` | Subset of EEG row indices; default all |

`params` accepts `serial_port`, `mac_address`, `ip_address`, `ip_port`,
`ip_protocol`, `other_info`, `serial_number`, `file`, `master_board`, `timeout`.
Anything else is **rejected** rather than silently ignored, so a typo does not
become a mystery connection failure.

```text
start_stream(session_id="s1", board="cyton", params={"serial_port": "COM3"})
start_stream(session_id="s1", board="muse_2", params={"mac_address": "00:11:.."})
start_stream(session_id="s1", board="playback_file",
             params={"file": "rec.csv", "master_board": "cyton"})
```

Data flows immediately; allow ~1 s for the buffer to fill.

!!! note "Marker channel"
    Most boards expose one. `has_marker_channel` in the response says whether
    `log_event` and `send_stim_event` can embed codes into the stream itself.
    BrainFlow reserves value `0` for "no marker", so use nonzero codes.

### `board="lsl"` — attach to an existing outlet

Skips BrainFlow entirely and pulls from an LSL outlet that some other process
already opened — the fix when BrainFlow itself cannot reach the device. On
Windows this is most often Muse: BrainFlow's BLE scanner (SimpleBLE) has a
longstanding bug that blocks discovery even when the device is confirmed
advertising, while `muselsl` (a different BLE binding) finds it fine and
publishes it as an LSL outlet. See
[Supported hardware](../hardware.md#acquiring-via-lsl-instead-of-brainflow).

| `params` key | Default | Meaning |
|---|---|---|
| `stream_type` | `"EEG"` | Resolve the first outlet advertising this LSL type |
| `name` | — | Resolve by outlet name instead, to pick one of several |
| `resolve_timeout` | `5.0` | Seconds to wait for the outlet before failing |
| `reconnect` | `true` | Reattach automatically if the outlet drops; `false` to disable |
| `stale_after_sec` | `5.0` | How long with no samples counts as "disconnected" |

```text
# In a separate terminal: muselsl stream    (bleak, not SimpleBLE)
start_stream(session_id="s1", board="lsl", params={"stream_type": "EEG"})
```

Needs `pip install eeg-mcp[lsl]`. Channel names come from the outlet's own
metadata; `channels` selects a subset by LSL channel index the same way it
selects BrainFlow rows for other boards. `has_marker_channel` is always
`false` — an LSL source has no marker channel of its own, so `log_event` and
`send_stim_event` still land in the session's event log, just not embedded in
the stream.

This works with any device that publishes an LSL outlet, not only Muse —
`muselsl` is simply the most common case on Windows.

#### What happens when the bridge drops

Bluetooth links drop. Streaming a Muse S Athena's EEG, accelerometer,
gyroscope and optics together over one connection was measured to disconnect
within one to three minutes on Windows, and the same pattern is reported
against `muselsl` itself
([muse-lsl#55](https://github.com/alexandrebarachant/muse-lsl/issues/55),
[#110](https://github.com/alexandrebarachant/muse-lsl/issues/110)).
A bridge that goes quiet would otherwise look identical to a silent gap in a
recording, so the LSL source handles it instead:

1. **Detects it.** No samples for `stale_after_sec` marks the session
   disconnected. The session keeps running — it does not error out.
2. **Reattaches to the same stream.** Once the bridge republishes, the source
   re-resolves the stream by the `type`/`name` it was originally given, and
   checks that the channel count and sample rate still match. It does *not*
   resolve by `source_id` alone: a single bridge often publishes several
   outlets (EEG, accelerometer, gyroscope, optics) that share one
   `source_id`, so a `source_id`-only match could reattach to the wrong
   sibling.
3. **Proves the new outlet is alive.** A just-exited bridge does not always
   retract its old outlets instantly, so a stale announcement can match
   perfectly and never produce another sample. The source waits for one real
   sample before committing to a candidate.
4. **Logs the gap.** A `source_reconnected` event (origin `annotation`)
   carries the gap length, so the recording shows exactly where data is
   missing rather than hiding it.

Restarting the bridge process itself (for example `muselsl stream` exiting)
is a separate job that stays outside eeg-mcp — run it under a small
supervisor that relaunches it. Check `stream_status` →
`connection` to see the live state.

---

## `stop_stream`

Stop the stream, release the board, and tear down its live monitor.

**Returns:** `stopped`, `monitor_stopped`, `final_status`.

Always call this. A board left prepared can block the next process from
acquiring the device.

---

## `list_sessions`

All active sessions with a one-line summary each: `kind`, `running`,
`exhausted`, `sfreq`, `n_channels`, `buffered_sec`, `n_events`.

---

## `stream_status`

Health and timing report. The fields that matter:

| Field | Read it for |
|---|---|
| `throughput_ratio` | **< 1.0 means samples are being dropped.** Treat as a stop condition |
| `filters.group_delay_sec` | How far behind real time filtered features are |
| `last_error` | Why a producer thread stopped |
| `buffered_sec` / `buffer_capacity_sec` | How far back you can epoch |
| `replay` | Position, speed, progress (replay sessions only) |
| `connection` | LSL sessions only: `connected`, `reconnect_count`, `disconnected_since`, `seconds_since_last_sample`, and `resolves_by` (what a reconnect looks the stream up by) |
| `event_counts` | What has been logged, by label |

```json
{
  "running": true,
  "throughput_ratio": 1.0,
  "buffered_sec": 58.2,
  "filters": {"n_stages": 2, "group_delay_sec": 0.0128},
  "last_error": null
}
```

---

## `read_window`

Recent samples from the buffer.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `2.0` | Window length |
| `filtered` | `true` | Filtered buffer if a chain is set |
| `channels` | `None` | Subset by name |
| `max_samples` | `1000` | Decimation cap per channel |
| `include_data` | `true` | `false` returns statistics only |

**Returns:** `channels`, `stats` (mean/std/min/max per channel), `filtered`,
`decimation`, `t_start`, `t_end`, and optionally `data` and `timestamps`.

!!! tip
    For numeric work use `get_band_power` or `get_psd` — they compute on
    full-resolution data. `read_window` decimates for readability. Use
    `include_data=false` for a cheap health glance.

---

## `get_channels`

Channel names in order with their buffer index, plus source metadata. Call it
before any tool that takes a `channels` list.
