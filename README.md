<!-- mcp-name: io.github.AImplifier/eeg-mcp -->

# eeg-mcp

**Real-time EEG over the Model Context Protocol.** Stream from hardware, replay
existing recordings against a wall clock, watch the signal live in a browser,
and dispatch stimulation events — all through one session model.

[![Docs](https://img.shields.io/badge/docs-aimplifier.github.io-teal)](https://aimplifier.github.io/eeg-mcp/)
[![PyPI](https://img.shields.io/pypi/v/eeg-mcp)](https://pypi.org/project/eeg-mcp/)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)

The offline counterpart is [neuro-mcp](https://github.com/AImplifier/neuro-mcp)
(MNE processing, source imaging, BIDS/EHR storage). This project is the
real-time half: everything that has to happen while the signal is still arriving.

---

## What it does

**Stream.** 66 BrainFlow board identifiers, plus a synthetic board that needs no
hardware. Samples land in a ring buffer filled by a background thread, so tool
calls read a live view instead of blocking on a device.

**Replay.** Play an EDF/BDF/GDF/SET/FIF or BrainFlow CSV *at the rate it was
recorded*, re-emitting its annotations as events at their original timings. The
session behaves exactly like a live stream, so a pipeline developed against a
file runs unchanged against hardware. Adds speed, seek, pause, and looping.

**Visualize.** A loopback-bound, token-gated live browser view with rolling
traces, event markers, band power and per-channel quality — plus self-contained
HTML reports that open on any machine with no internet.

**Record.** Write the session continuously to MNE-native `.fif` with the event
log attached as annotations, plus a metadata row (subject, task, run) in a store
schema-compatible with neuro-mcp — so acquisition hands off to analysis with no
import step. Crash-safe: an interrupted session is recoverable.

**Extend.** Plug in your own real-time processor — feature extractor,
classifier, artifact gate, or EEG tokenizer for sequence models — and it runs on
the same footing as the built-ins, inside the acquisition loop.
See [Extending](https://aimplifier.github.io/eeg-mcp/extending/).

**Stimulate.** One `send_stim_event` contract over pluggable backends: LSL for
software, BrainFlow's marker channel for sample-aligned embedding, and
serial/TTL or templated ASCII protocols for hardware including TMS and tES.

**One event log.** Board markers, replayed annotations, dispatched stimulations
and manual notes all land in the same table on the same clock. A closed-loop run
reconstructs afterwards with no clock join.

---

## Install

```bash
conda create -n eeg-mcp python=3.11 -y && conda activate eeg-mcp
pip install eeg-mcp
```

Optional extras: `pip install "eeg-mcp[lsl,serial]"` for LSL marker outlets and
serial trigger delivery.

Register with an MCP client (absolute path to the env's interpreter):

```json
{
  "mcpServers": {
    "eeg-realtime": {
      "command": "/path/to/envs/eeg-mcp/bin/python",
      "args": ["-m", "eeg_mcp"]
    }
  }
}
```

→ Full guide: **[Installation](https://aimplifier.github.io/eeg-mcp/installation/)**

## Quick start

No hardware required:

```text
start_stream(session_id="s1", board="synthetic")
check_signal_quality(session_id="s1")
set_filters(session_id="s1", bandpass_low=1, bandpass_high=40, notch_freq=50)
get_band_power(session_id="s1", seconds=2)
start_monitor(session_id="s1")
```

Open the returned URL to watch it live.

Replay a real recording as if it were live:

```text
inspect_recording(path="sub-04_rest.edf")
start_replay(session_id="r1", path="sub-04_rest.edf", speed=1.0)
get_events(session_id="r1", origin="annotation")
export_report(session_id="r1", notes="Routine review.")
```

→ **[Tutorial: Your First Real-Time Session](https://aimplifier.github.io/eeg-mcp/examples/tutorial-first-realtime-session/)**

---

## Documentation

| | |
|---|---|
| [Installation](https://aimplifier.github.io/eeg-mcp/installation/) | Environment, client registration, troubleshooting |
| [Tutorial](https://aimplifier.github.io/eeg-mcp/examples/tutorial-first-realtime-session/) | End-to-end, no hardware needed |
| [Replay-Driven Development](https://aimplifier.github.io/eeg-mcp/examples/replay-driven-development/) | Build against a recording, deploy live |
| [Closed-Loop Neurofeedback](https://aimplifier.github.io/eeg-mcp/examples/closed-loop-neurofeedback/) | Feature → trigger, with a measured latency budget |
| [Live Clinical Review](https://aimplifier.github.io/eeg-mcp/examples/clinical-live-review/) | Visual review and reporting |
| [Stimulation Protocols](https://aimplifier.github.io/eeg-mcp/examples/stimulation-protocols/) | TMS and tES through the safety gates |
| [Supported Hardware](https://aimplifier.github.io/eeg-mcp/hardware/) | All 66 boards, formats, stimulation transports |
| [Safety](https://aimplifier.github.io/eeg-mcp/safety/) | **Read before connecting a stimulator** |
| [Extending](https://aimplifier.github.io/eeg-mcp/extending/) | Write a custom processor or EEG tokenizer |
| [Tool Reference](https://aimplifier.github.io/eeg-mcp/tools/) | All 47 tools |

---

## The one design decision worth knowing

**Filtering happens in the producer thread, not at query time.**

A stateful IIR filter must see every sample exactly once, in order. The common
shortcut — filtering each query window independently — restarts the filter at
every window boundary and injects a transient each time. It is invisible in a
band-power plot and fatal for anything phase-sensitive.

So the producer filters each chunk once as it arrives, carrying `sosfilt` delay-
line state forward, and writes the result to a second ring buffer. Queries just
read. The test suite asserts chunked filtering matches whole-signal filtering to
`1e-9`, **and** asserts as a control that the naive approach does not.

Consequences worth internalising:

- Filters are **causal**. There is no zero-phase option, because that needs
  future samples. `stream_status` reports `group_delay_sec`.
- Both buffers are kept, so `read_window(filtered=false)` always gets you raw
  signal to check whether a feature is real or a filter artifact.
- Absolute sample indices are shared across both buffers, so an event's
  `sample_index` means the same thing whichever you read.

Budget a closed loop as **group delay + poll interval + dispatch latency** —
measured at ~71 ms in the reference configuration.

---

## ⚠️ Stimulation safety

**This software is for authorised research use. It is not a medical device and
has not been validated for clinical use.** TMS and tES can cause harm, including
seizure. Use only under a protocol approved by your ethics board, on a rig whose
device-level interlocks are intact, with a trained operator present.

Three gates apply to every hardware backend:

1. **Config** — hardware backends refuse to open unless the server was started
   with `EEG_MCP_ALLOW_HARDWARE_STIM=1`. An agent cannot set this.
2. **Arming** — `arm_stim` permits dispatch for a window that *expires*.
3. **Limits** — intensity, duration and interval are clamped; violations raise
   rather than saturate.

None of this replaces the interlocks on the device itself.

**The hardware backends are generic transports driven by command templates you
supply from your device's manual — not vendor drivers, and none has been tested
against a physical stimulator.** Shipping a plausible-looking untested driver
would be worse than shipping none, because it would fail silently while
connected to something pointed at a person's head.

Start every protocol on `backend="log"`, which accepts everything and emits
nothing.

---

## Verify

```bash
python testing/verify.py                    # core correctness
python testing/verify_recording.py          # recording + metadata store
python testing/verify_processing.py         # plugin processors
python testing/persona_bci_researcher.py    # engineer workflow
python testing/persona_clinician.py         # clinician workflow
```

All five drive the real server through FastMCP's in-memory client and assert
against planted ground truth — including that a spike planted at an annotation
onset lands at **t = 0 ± 0 ms** in the extracted epoch, which proves annotation
timing, replay clock, ring-buffer indexing and epoch extraction all agree; and
that a deliberately broken plugin cannot stop acquisition.

→ [What is and is not covered](https://aimplifier.github.io/eeg-mcp/testing/)

## License

BSD-3-Clause. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
