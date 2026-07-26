# Stimulation Tools

!!! danger "Read [Safety](../safety.md) first"
    Hardware backends can drive TMS and tES devices, which can cause harm
    including seizure. They are **generic transports you configure from your
    device's manual — not validated vendor drivers**. Research use only, under an
    approved protocol, with a trained operator present.

---

## The three gates

Every hardware command passes, in order:

1. **Config** — refuses to open unless the server was started with
   `EEG_MCP_ALLOW_HARDWARE_STIM=1`. An agent cannot set this.
2. **Arm** — must be explicitly armed; arming **expires** (default 300 s).
3. **Limits** — intensity, duration and interval clamped against ceilings;
   violations **raise** rather than silently saturate.

Software backends (`log`, `lsl`, `brainflow_marker`) skip gates 1 and 2.

---

## `list_stim_backends`

The catalogue, with hardware clearly flagged and the resolved ceilings.

| Backend | Hardware | Transport |
|---|---|---|
| `log` | no | none — accepts everything, emits nothing |
| `lsl` | no | LSL marker outlet |
| `brainflow_marker` | no | the board's own marker channel |
| `serial_ttl` | **yes** | serial byte |
| `template_serial` | **yes** | ASCII serial, templated |
| `tms` | **yes** | ASCII serial + trigger, %MSO |
| `tes` | **yes** | ASCII serial, mA, 4 mA default ceiling |

Also returns `hardware_enabled` and `limits`, so you can verify the server
picked up your config without restarting.

---

## `attach_stim_backend`

Bind a target to a session and open its transport.

| Arg | Default | Meaning |
|---|---|---|
| `backend` | `"log"` | Name from the catalogue |
| `options` | `None` | Backend-specific settings |
| `make_default` | `true` | Use when `send_stim_event` omits a backend |

```text
attach_stim_backend(session_id="s1", backend="log")

attach_stim_backend(session_id="s1", backend="lsl",
                    options={"stream_name": "experiment-markers"})

attach_stim_backend(session_id="s1", backend="tms", options={
  "port": "COM3",
  "templates": {"set_intensity": "AMP {intensity:.0f}\r\n", "fire": "TRIG\r\n"}
})
```

Serial options default from `EEG_MCP_SERIAL_PORT` / `EEG_MCP_SERIAL_BAUDRATE`.

### Templates

`template_serial`, `tms` and `tes` are driven by command strings **from your
device's manual**. Placeholders are filled with `str.format` from the command
fields — `{intensity}`, `{value}`, `{duration_ms}`, `{label}`, plus anything in
`params`. There is no expression evaluation, so a template cannot execute code.

Recognised keys: `fire` (required), `set_intensity`, and one per `kind`
(`pulse`, `train`, `ramp`, `custom`). Intensity is always sent **before** firing.

---

## `detach_stim_backend`

Close and remove a backend. Returns the remaining attached ones.

---

## `arm_stim` / `disarm_stim`

```text
arm_stim(session_id="s1", backend="tms", ttl_sec=120)
disarm_stim(session_id="s1", backend="tms")
```

Keep the window as short as the block. Expiry is the safety property: an agent
that stalls mid-protocol cannot resume and fire minutes later on a stale
decision.

---

## `send_stim_event`

Deliver one command and log it against the EEG clock.

| Arg | Default | Meaning |
|---|---|---|
| `label` | — | Event name |
| `value` | `1.0` | Code for marker streams and trigger lines; nonzero |
| `kind` | `"marker"` | `marker`, `pulse`, `train`, `ramp`, `custom` |
| `intensity` | `None` | %MSO for TMS, mA for tES; clamped |
| `unit` | `None` | Unit label, for the record |
| `duration_ms` | `0.0` | Pulse width or stimulation duration |
| `target` | `None` | Coil / montage / channel, for the record |
| `backend` | `None` | Defaults to the session default |
| `also_mark_source` | `true` | Also punch into the acquisition stream |
| `params` | `None` | Extra fields for templates |

```json
{
  "backend": "tms",
  "dispatch_latency_ms": 4.21,
  "marked_in_source": true,
  "detail": {"delivered": true, "commands_sent": ["AMP 60\r\n", "TRIG\r\n"]},
  "event": {"label": "tms_single", "origin": "stim", "sample_index": 128400}
}
```

`dispatch_latency_ms` is time inside the transport only. Add the filter group
delay and poll interval for the full closed-loop budget.

!!! info "Failures are logged too"
    A refused or failed command is written to the event log with the error
    attached, then raises. A failed stimulation attempt matters as much to the
    record as a successful one.

---

## `send_stim_train`

A regular train of pulses. **Blocks until it completes.**

| Arg | Meaning |
|---|---|
| `n_pulses` | How many; capped at 1000 |
| `interval_ms` | Inter-pulse interval; for rTMS, `1000 / frequency` |
| `intensity` | Dose per pulse |

Capped at 1000 pulses and 60 seconds total, so longer protocols are built from
several calls with decision points between them.

```json
{
  "n_delivered": 20,
  "requested_interval_ms": 100.0,
  "realised_interval_ms": {"mean": 100.02, "min": 97.16, "max": 103.20},
  "total_sec": 1.900
}
```

Pulses are scheduled against the **train's start**, not the previous pulse, so
per-pulse dispatch cost does not accumulate into drift. Every pulse is logged
individually, so the log reconstructs delivered timing, not intended timing.

!!! warning "Not a real-time system"
    A few ms of jitter is fine for cues and tES ramps, **not** for
    sub-millisecond rTMS. Program those into the stimulator's own sequencer and
    use eeg-mcp to arm, gate, and record.

If a safety gate refuses mid-train, the train stops and the error reports how
many pulses were delivered.

---

## `stim_status`

Attached backends, arming state, dispatch counts, and whether hardware is
enabled process-wide.
