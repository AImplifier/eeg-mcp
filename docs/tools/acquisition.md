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
