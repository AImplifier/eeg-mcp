# Safety

!!! danger "Read this before connecting anything to a person"
    **eeg-mcp is not a medical device.** It has not been validated, certified,
    or cleared for clinical use by any regulator. Nothing it outputs is a
    diagnosis.

    **Transcranial stimulation can cause harm, including seizure.** TMS and tES
    must be used only under a protocol approved by your ethics board or IRB, on
    a rig whose device-level interlocks are intact, with a trained operator
    physically present and able to stop stimulation.

## Why this needs more care than a normal library

An MCP server is driven by an autonomous agent. The usual safety assumption —
that a human read each command before it reached the device — does not hold. An
agent can retry, loop, misread a threshold, or act on a hallucinated value at
machine speed.

Every design decision below follows from that.

## The three gates

Every hardware stimulation command passes three checks, in order. All are in
[`stim/base.py`](https://github.com/AImplifier/eeg-mcp/blob/main/eeg_mcp/stim/base.py).

### 1. Config gate

Hardware backends refuse to open unless the server process was started with
`EEG_MCP_ALLOW_HARDWARE_STIM=1`.

```text
SafetyError: Backend 'tms' drives physical stimulation hardware and is disabled.
Set EEG_MCP_ALLOW_HARDWARE_STIM=1 in the server environment to enable it ...
```

This variable lives in the **server's** environment, set by the operator in the
MCP client config. An agent cannot set it, and cannot work around it. A default
install cannot drive a stimulator at all.

### 2. Arm gate

Even with hardware enabled, a backend must be explicitly armed, and **arming
expires** (default 300 s).

```text
arm_stim(session_id="s1", ttl_sec=120)
```

Expiry is the point. An agent that stalls mid-protocol — waiting on a user,
stuck in a retry loop, or simply slow — cannot come back ten minutes later and
fire a coil on a decision that is no longer current. `disarm_stim` revokes
immediately.

### 3. Limit gate

Intensity, duration, and inter-stimulus interval are clamped against configured
ceilings. Violations **raise** rather than silently saturate, because a command
quietly reduced to a legal value is a command that did not do what the protocol
said.

```text
SafetyError: intensity=85 exceeds the configured ceiling 60. Raise
EEG_MCP_MAX_STIM_INTENSITY only if the protocol genuinely requires it.
```

The `tes` backend additionally enforces `max_current_ma` (default 4 mA, the
conventional upper bound in published human tES safety guidance), and `tms`
rejects intensities above 100 since %MSO has no meaning beyond that.

!!! warning "Set your ceilings deliberately"
    `EEG_MCP_MAX_STIM_INTENSITY` defaults to 100 — sensible for TMS %MSO,
    absurd for tES mA. If you run tES, set it to your approved current. See
    [Configuration](configuration.md#stimulation).

## What these gates are not

They are a **software backstop against an autonomous caller**. They are not:

- a replacement for the interlocks, dose limits, or emergency stop on the device;
- a safety certification, or evidence of one;
- protection against a wrong command that is *within* your configured limits.

The device's own safety systems remain the primary control. If your stimulator
has a hardware abort, keep it within reach of the operator.

## No fabricated vendor drivers

The hardware backends are **generic transports driven by command templates you
supply from your device's manual** — not reverse-engineered vendor drivers, and
none has been validated against a specific commercial stimulator.

This is deliberate. A module called `magstim.py` that had never been tested
against a Magstim would look authoritative in a code review while silently doing
the wrong thing to a device pointed at someone's head. The dangerous,
device-specific knowledge belongs with the operator who has the manual and the
approvals.

```python
attach_stim_backend(session_id="s1", backend="tms", options={
    "port": "COM3",
    "templates": {
        "fire": "TRIG\r\n",
        "set_intensity": "AMP {intensity:.0f}\r\n",
    },
})
```

Templates are filled with `str.format` — no expression evaluation, so a template
cannot execute code.

## Rehearse on `log` first

The `log` backend accepts every command and emits nothing. It is not a stub to
be replaced later; it is the correct target while designing a protocol, and it
makes the entire stimulation path — timing, event logging, safety refusals —
testable with no device and no risk.

```text
attach_stim_backend(session_id="s1", backend="log")
send_stim_train(session_id="s1", label="rtms_10hz", n_pulses=20, interval_ms=100)
```

Check the realised intervals in the response before you ever attach hardware.

## Timing honesty

Every stimulation is logged as an event **whether or not the transport
succeeded**, because a failed attempt matters as much to the record as a
successful one. `send_stim_train` reports *realised* intervals, not intended
ones.

!!! warning "Python is not a real-time system"
    Measured train jitter is a few milliseconds. That is fine for cues and
    neurofeedback. It is **not** adequate for sub-millisecond rTMS timing or
    phase-locked stimulation — drive those from the stimulator's own hardware
    sequencer and use eeg-mcp to arm, gate, and record.

## Patient data

`start_monitor` opens a local HTTP server that can carry patient signal. It
binds to `127.0.0.1` only and requires a random per-monitor token, so it is not
reachable from the network and cannot be read by guessing the port.

It is **not encrypted and is not an authentication system**. Do not expose it
through a tunnel, reverse proxy, or port forward. Stopping the session tears the
monitor down with it.

## Reporting a safety issue

Open an issue at
[github.com/AImplifier/eeg-mcp/issues](https://github.com/AImplifier/eeg-mcp/issues).
If it concerns unsafe stimulation behaviour, say so in the title so it can be
triaged first.
