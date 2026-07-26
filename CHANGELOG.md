# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-26

First release. **47 MCP tools** over stdio, **225 checks** across five suites.

### Acquisition

- `start_stream` for any of 66 BrainFlow board identifiers, plus the synthetic
  and playback boards that need no hardware.
- Fixed-capacity ring buffer filled by a background producer thread, so tool
  calls read a live view instead of blocking on a device.
- `stream_status` reports `throughput_ratio`, which exposes dropped samples
  rather than hiding them.
- BrainFlow's opaque failures (`ANOTHER_BOARD_IS_CREATED_ERROR`,
  `UNABLE_TO_OPEN_PORT_ERROR`, `BOARD_NOT_READY_ERROR`) are translated into
  messages that name the fix.

### Replay

- `start_replay` plays EDF/BDF/GDF/SET/FIF/BrainVision (via MNE) and BrainFlow
  CSV at true recording rate, re-emitting annotations and trigger-channel events
  at their original timings on a real Unix clock.
- `replay_control` adds speed, seek, pause/resume, and looping — none of which
  BrainFlow's `PLAYBACK_FILE_BOARD` provides.
- Seeking clears buffers and resets filter state so no window straddles the
  discontinuity, while absolute sample indices stay monotonic.

### Signal processing

- Stateful online filter chain using `scipy.signal.sosfilt` with persistent
  per-channel delay-line state, applied once in the producer thread. Verified
  equal to whole-signal filtering to 1e-9, with a control assertion proving the
  naive per-window approach is measurably wrong.
- `group_delay_sec` reported so closed-loop latency can be budgeted rather than
  guessed.
- `get_band_power`, `get_psd` (BrainFlow Welch with SciPy fallback), and
  `check_signal_quality` with flat / railed / line-noise detection.

### Pluggable real-time processors

- A `Processor` contract for custom real-time code, discovered through
  `eeg_mcp.processors` entry points, `EEG_MCP_PLUGINS`, or `EEG_MCP_PLUGIN_DIR`.
  Streaming processors run inside the acquisition loop, so a closed-loop
  decision needs no agent round-trip.
- Built-ins: `band_power`, `threshold_trigger`, `artifact_detector`, and
  `band_tokenizer` for sequence models.
- `list_processors`, `attach_processor`, `detach_processor`, `processor_status`,
  `get_processor_output`, `run_processor`, `reset_processors`, `get_tokens`.
- Plugin failures are contained: errors are counted and a processor is disabled
  after 10 failures rather than being retried inside the producer loop.
- Processors can only be provisioned from the server environment. An agent
  cannot name an import path, because importing a module executes it.

### Events

- One log for board markers, replayed annotations, dispatched stimulations, and
  manual notes, all on one clock with absolute sample indices.
- `get_epoch_around_event` for online epoching; verified sample-accurate against
  a synthetic recording with a spike planted at each annotation onset.

### Recording and persistence

- `start_recording` / `stop_recording` write the live session continuously to
  disk as MNE-native `.fif`, with the event log attached as annotations. Samples
  are appended to a sidecar as they arrive and the header written up front, so a
  crash leaves recoverable data; `recover_recording` finishes an interrupted
  session.
- eeg-mcp's **own** metadata store at `EEG_MCP_DATABASE_URL`, with a schema
  matching neuro-mcp's (`subjects`, `datasets`, `recordings`, `annotations`,
  `audit_log`) so an analysis tool can **read** it with no import step. The two
  projects stay separate: the bare `DATABASE_URL` is deliberately ignored so
  eeg-mcp can never inherit another application's store. Only ever adds rows;
  never amends or voids.
- `list_recordings`, `recording_status`.

### Visualization

- `start_monitor`: live rolling traces, event markers, band power, and
  per-electrode quality in a browser. Bound to `127.0.0.1`, gated by a random
  per-monitor token, strict CSP, no external resources.
- Transport controls on the monitor — play/pause, restart, skip, scrub, speed,
  stop, mark an event, and save a report/traces/spectrum — so an operator does
  not have to return to the agent for every action. Control requests require a
  JSON content type on top of the token, forcing a CORS preflight that a hostile
  local page cannot satisfy. `allow_control=false` gives a strictly viewable
  screen whose control endpoint refuses even an authenticated request.
- `plot_stream`, `plot_spectrum`, `export_report`: self-contained HTML with
  inline SVG — no matplotlib, no Plotly bundle, no CDN. Renders on an air-gapped
  machine.

### Stimulation

- One `send_stim_event` contract over `log`, `lsl`, `brainflow_marker`,
  `serial_ttl`, `template_serial`, `tms`, and `tes` backends.
- Three safety gates on hardware: `EEG_MCP_ALLOW_HARDWARE_STIM` config switch,
  expiring `arm_stim`, and clamped intensity/duration/interval ceilings that
  raise rather than saturate.
- `send_stim_train` schedules against the train's start so per-pulse cost does
  not accumulate into drift, and reports *realised* intervals.
- Commands are logged whether delivered or refused.

### Known limitations

- Real stimulation hardware has never been exercised; only the `log` backend and
  the hardware-refusal path are tested. Hardware backends are configurable
  transports, not validated vendor drivers.
- Real amplifiers are untested — only the synthetic board and file replay.
- Train timing carries a few milliseconds of jitter, adequate for cues and tES
  ramps but not for sub-millisecond rTMS.
- Long-run (multi-hour) stability is untested, as is PostgreSQL for the
  metadata store (SQLite is exercised).

[0.1.0]: https://github.com/AImplifier/eeg-mcp/releases/tag/v0.1.0
