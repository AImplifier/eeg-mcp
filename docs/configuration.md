# Configuration

Everything is environment-driven, so one installed package serves a laptop with
a synthetic board and a lab rig with an amplifier and a stimulator. Set these in
the `env` block of your MCP client config (see [Installation](installation.md)).

## Variables

### Storage

| Variable | Default | Purpose |
|---|---|---|
| `EEG_MCP_HOME` | `~/.eeg-mcp` | Data home; plots and reports are written under it |
| `EEG_MCP_RECORDINGS_DIR` | `$EEG_MCP_HOME/recordings` | Root for relative paths given to `start_replay` |
| `EEG_MCP_RECORD_DIR` | `$EEG_MCP_HOME/recorded` | Where `start_recording` writes new sessions |
| `DATABASE_URL` | `sqlite:///$EEG_MCP_HOME/eeg_mcp.db` | Recording metadata store. **Set the same value as neuro-mcp to share one store** |
| `EEG_MCP_DATABASE_URL` | *unset* | Overrides `DATABASE_URL` when you want the two servers kept separate |

!!! tip "Sharing a store with neuro-mcp"
    `DATABASE_URL` is neuro-mcp's variable, honoured here deliberately: a rig
    that already configured neuro-mcp shares its store with no extra setup, and
    a session recorded here appears there with no import step. See
    [Recording](tools/recording.md#the-metadata-store).

### Custom processors

| Variable | Default | Purpose |
|---|---|---|
| `EEG_MCP_PLUGIN_DIR` | *unset* | Directory of `.py` files; every `Processor` in them is registered |
| `EEG_MCP_PLUGINS` | *unset* | Comma-separated `module:ClassName` list |

Processors can only be provisioned through the **server's** environment — never
from a tool call, because importing a module executes it. See
[Extending](extending.md#making-it-discoverable).

### Streaming

| Variable | Default | Purpose |
|---|---|---|
| `EEG_MCP_BUFFER_SEC` | `60` | Seconds of signal held per session |
| `EEG_MCP_POLL_INTERVAL_SEC` | `0.05` | How often the producer drains the source |

!!! tip "Sizing the buffer"
    Memory is `n_channels × buffer_sec × sfreq × 8 bytes`, and it **doubles**
    once a filter chain is set, because raw and filtered are both kept.

    64 ch × 60 s × 1000 Hz ≈ 30 MB per buffer, so ~60 MB for that session.

    The buffer is also the limit on how far back `get_epoch_around_event` can
    reach. If you epoch around events minutes after they occur, raise it.

`EEG_MCP_POLL_INTERVAL_SEC` is a direct term in your closed-loop latency budget:
a 50 ms poll adds up to 50 ms of delay before a sample is even visible. Lowering
it tightens the loop at the cost of more wake-ups; raising it helps a machine
that cannot keep up (watch `throughput_ratio` in `stream_status`).

### Stimulation

| Variable | Default | Purpose |
|---|---|---|
| `EEG_MCP_ALLOW_HARDWARE_STIM` | *unset* | **Master switch.** Hardware backends refuse to open while unset |
| `EEG_MCP_STIM_BACKEND` | `log` | Default backend name |
| `EEG_MCP_SERIAL_PORT` | *unset* | Default serial port for serial/TMS/tES backends |
| `EEG_MCP_SERIAL_BAUDRATE` | `115200` | Default serial baud rate |
| `EEG_MCP_LSL_STREAM_NAME` | `eeg-mcp-markers` | Name of the LSL marker outlet |
| `EEG_MCP_MAX_STIM_INTENSITY` | `100` | Intensity ceiling (%MSO for TMS, mA for tES) |
| `EEG_MCP_MAX_STIM_DURATION_MS` | `10000` | Duration ceiling per command |
| `EEG_MCP_MIN_STIM_INTERVAL_MS` | `0` | Minimum interval between commands |

!!! danger "The ceilings are the point"
    `EEG_MCP_MAX_STIM_INTENSITY` defaults to 100, which is meaningful for TMS
    (%MSO) but far above any sane tES current. **If you are running tES, set it
    to your protocol's approved limit** — e.g. `2.0` for a 2 mA montage. The
    `tes` backend additionally enforces its own `max_current_ma`, default 4 mA.

    Raise a ceiling only when the protocol genuinely requires it, and never to
    make a failing call succeed.

## Worked examples

### Development laptop, no hardware

Nothing to set. Defaults give you the synthetic board, a 60 s buffer, and the
`log` stimulation backend that emits nothing.

### Replay-driven development

```json
"env": {
  "EEG_MCP_RECORDINGS_DIR": "/data/eeg-archive",
  "EEG_MCP_BUFFER_SEC": "300"
}
```

A long buffer lets you epoch around events well after they scrolled past.

### High-density live rig

```json
"env": {
  "EEG_MCP_BUFFER_SEC": "30",
  "EEG_MCP_POLL_INTERVAL_SEC": "0.02"
}
```

128 ch × 30 s × 1000 Hz ≈ 30 MB per buffer. The 20 ms poll trims the latency
budget; confirm `throughput_ratio` stays at 1.0.

### tES rig, 2 mA protocol

```json
"env": {
  "EEG_MCP_ALLOW_HARDWARE_STIM": "1",
  "EEG_MCP_SERIAL_PORT": "COM3",
  "EEG_MCP_MAX_STIM_INTENSITY": "2.0",
  "EEG_MCP_MAX_STIM_DURATION_MS": "1200000",
  "EEG_MCP_MIN_STIM_INTERVAL_MS": "1000"
}
```

### rTMS rig, 10 Hz protocol at 60% MSO

```json
"env": {
  "EEG_MCP_ALLOW_HARDWARE_STIM": "1",
  "EEG_MCP_SERIAL_PORT": "COM4",
  "EEG_MCP_MAX_STIM_INTENSITY": "60",
  "EEG_MCP_MAX_STIM_DURATION_MS": "50",
  "EEG_MCP_MIN_STIM_INTERVAL_MS": "95"
}
```

The interval floor of 95 ms is what stops a miscalculated loop from delivering a
10 Hz train at 100 Hz.

## Inspecting the running configuration

```text
list_stim_backends()
```

returns the resolved ceilings and whether hardware is enabled, so you can verify
the server picked up your `env` block without restarting anything.
