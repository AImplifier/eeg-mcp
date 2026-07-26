"""MCP tools for dispatching stimulation events to software and hardware."""

from __future__ import annotations

import time
from typing import Any

from .app import mcp
from .config import get_config
from .session import store
from .stim import create_backend, list_backend_specs
from .stim.base import SafetyError, StimCommand
from .utils import to_jsonable

# A train holds the tool call open for its whole duration, so it is capped.
_MAX_TRAIN_PULSES = 1000
_MAX_TRAIN_SEC = 60.0


@mcp.tool()
def list_stim_backends() -> dict[str, Any]:
    """List stimulation targets, their options, and which ones drive hardware.

    Software backends ('log', 'lsl', 'brainflow_marker') are always available.
    Hardware backends ('serial_ttl', 'tms', 'tes', 'template_serial') require
    the server to have been started with EEG_MCP_ALLOW_HARDWARE_STIM=1, and must
    be armed before each session of use.

    Returns:
        The backend catalogue and the currently configured safety ceilings.
    """
    config = get_config()
    return to_jsonable({
        "backends": list_backend_specs(),
        "hardware_enabled": config.allow_hardware_stim,
        "limits": {
            "max_intensity": config.max_stim_intensity,
            "max_duration_ms": config.max_stim_duration_ms,
            "min_interval_ms": config.min_stim_interval_ms,
        },
        "note": "Hardware backends are generic transports driven by command "
                "templates from your device's manual -- not vendor drivers. They "
                "do not replace the device's own safety interlocks.",
    })


@mcp.tool()
def attach_stim_backend(
    session_id: str,
    backend: str = "log",
    options: dict[str, Any] | None = None,
    make_default: bool = True,
) -> dict[str, Any]:
    """Bind a stimulation target to a session and open its transport.

    Start with backend='log' to rehearse a protocol with no output at all, then
    switch once the timing looks right.

    Args:
        session_id: The session to attach to.
        backend: One of the names from list_stim_backends.
        options: Backend-specific settings. For serial backends: {"port": "COM3"}.
            For templated devices (tms, tes, template_serial), the commands from
            your device manual, e.g.
            {"port": "COM3", "templates": {"fire": "TRIG\\r\\n",
             "set_intensity": "AMP {intensity:.0f}\\r\\n"}}.
        make_default: Use this backend when send_stim_event omits one.

    Returns:
        Backend status, including whether it still needs arming.
    """
    session = store.get(session_id)
    instance = create_backend(backend, options)
    session.attach_backend(backend, instance, make_default=make_default)
    status = instance.status()
    return to_jsonable({
        "session_id": session_id,
        "attached": backend,
        "default": session.default_stim_backend,
        "status": status,
        "next": ("Call arm_stim before sending -- this backend drives hardware."
                 if instance.is_hardware else "Ready; call send_stim_event."),
    })


@mcp.tool()
def detach_stim_backend(session_id: str, backend: str) -> dict[str, Any]:
    """Close and remove a stimulation backend from a session.

    Args:
        session_id: The session to detach from.
        backend: The attached backend's name.

    Returns:
        Confirmation and the remaining attached backends.
    """
    session = store.get(session_id)
    instance = session.stim_backends.pop(backend, None)
    if instance is None:
        raise KeyError(
            f"No stim backend '{backend}' on session '{session_id}'. "
            f"Attached: {sorted(session.stim_backends) or 'none'}"
        )
    instance.close()
    if session.default_stim_backend == backend:
        session.default_stim_backend = next(iter(session.stim_backends), None)
    return to_jsonable({
        "session_id": session_id, "detached": backend,
        "remaining": sorted(session.stim_backends),
        "default": session.default_stim_backend,
    })


@mcp.tool()
def arm_stim(
    session_id: str, backend: str | None = None, ttl_sec: float = 300.0
) -> dict[str, Any]:
    """Permit hardware stimulation for a bounded window.

    Arming expires deliberately: an agent that stalls mid-protocol should not be
    able to resume and fire a stimulator minutes later without a fresh, explicit
    decision. Re-arm as needed, or disarm the moment a block ends.

    Args:
        session_id: The session whose backend to arm.
        backend: Which attached backend. Defaults to the session default.
        ttl_sec: How long the arming lasts, in seconds.

    Returns:
        Arming confirmation and expiry.
    """
    session = store.get(session_id)
    instance = session.resolve_backend(backend)
    result = instance.arm(ttl_sec)
    session.log(f"armed '{instance.name}' for {ttl_sec}s")
    return to_jsonable({"session_id": session_id, **result, "status": instance.status()})


@mcp.tool()
def disarm_stim(session_id: str, backend: str | None = None) -> dict[str, Any]:
    """Revoke permission to stimulate. Takes effect immediately.

    Args:
        session_id: The session whose backend to disarm.
        backend: Which attached backend. Defaults to the session default.

    Returns:
        Disarm confirmation.
    """
    session = store.get(session_id)
    instance = session.resolve_backend(backend)
    result = instance.disarm()
    session.log(f"disarmed '{instance.name}'")
    return to_jsonable({"session_id": session_id, **result})


@mcp.tool()
def send_stim_event(
    session_id: str,
    label: str,
    value: float = 1.0,
    kind: str = "marker",
    intensity: float | None = None,
    unit: str | None = None,
    duration_ms: float = 0.0,
    target: str | None = None,
    backend: str | None = None,
    also_mark_source: bool = True,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deliver one stimulation command, and log it against the EEG clock.

    The event is written to the session log whether the transport succeeds or
    fails -- a failed stimulation attempt matters just as much to the record.
    When the acquisition source supports markers, the same event is also punched
    into the stream itself so the recording is self-describing.

    Args:
        session_id: The session to stimulate within.
        label: Event name, e.g. 'tms_pulse' or 'cue_left'.
        value: Numeric code for marker streams and trigger lines. Nonzero.
        kind: 'marker' (event code only), 'pulse' (code then clear after
            duration_ms), 'train', 'ramp', or 'custom'. Backends advertise which
            kinds they support via list_stim_backends.
        intensity: Physical dose for hardware backends -- %MSO for TMS, mA for
            tES. Clamped against the configured ceiling; ignored by software
            backends.
        unit: Unit label for intensity, for the record.
        duration_ms: Pulse width or stimulation duration.
        target: Coil, montage or channel identifier, for the record.
        backend: Which attached backend to use. Defaults to the session default.
        also_mark_source: Also write the code into the acquisition stream's own
            marker channel.
        params: Extra fields made available to command templates.

    Returns:
        Dispatch result with latency, and the resulting event record.
    """
    session = store.get(session_id)
    command = StimCommand(
        label=label, value=float(value), kind=kind, intensity=intensity,
        unit=unit, duration_ms=float(duration_ms), target=target,
        params=params or {},
    )
    try:
        return to_jsonable({
            "session_id": session_id,
            **session.dispatch_stim(
                command, backend_key=backend, also_mark_source=also_mark_source
            ),
        })
    except SafetyError as exc:
        # Surface safety refusals as a clear, non-retryable answer rather than a
        # generic error an agent might loop on.
        raise ValueError(f"Stimulation refused by safety gate: {exc}") from exc


@mcp.tool()
def send_stim_train(
    session_id: str,
    label: str,
    n_pulses: int,
    interval_ms: float,
    value: float = 1.0,
    intensity: float | None = None,
    duration_ms: float = 0.0,
    backend: str | None = None,
) -> dict[str, Any]:
    """Deliver a regular train of pulses, blocking until it completes.

    For rTMS bursts and repeated cues. The call holds until the train finishes,
    so it is capped at 1000 pulses and 60 seconds total; build longer protocols
    from several calls so there are decision points in between.

    Every pulse is logged individually, so the event log reconstructs the exact
    delivered timing rather than the intended timing.

    Args:
        session_id: The session to stimulate within.
        label: Base event name; pulses are logged as '<label>_<n>'.
        n_pulses: How many pulses to deliver.
        interval_ms: Inter-pulse interval. For rTMS this is 1000/frequency.
        value: Numeric code for each pulse.
        intensity: Physical dose per pulse, clamped against the ceiling.
        duration_ms: Pulse width.
        backend: Which attached backend. Defaults to the session default.

    Returns:
        Per-pulse dispatch times and the realised interval statistics.
    """
    if n_pulses < 1:
        raise ValueError(f"n_pulses must be at least 1, got {n_pulses}")
    if n_pulses > _MAX_TRAIN_PULSES:
        raise ValueError(
            f"n_pulses={n_pulses} exceeds the {_MAX_TRAIN_PULSES}-pulse cap. "
            "Split the protocol across several calls."
        )
    if interval_ms <= 0:
        raise ValueError(f"interval_ms must be > 0, got {interval_ms}")
    total_sec = n_pulses * interval_ms / 1000.0
    if total_sec > _MAX_TRAIN_SEC:
        raise ValueError(
            f"This train would run {total_sec:.1f} s, over the {_MAX_TRAIN_SEC} s cap. "
            "Split it across several calls."
        )

    session = store.get(session_id)
    dispatched: list[float] = []
    try:
        for i in range(n_pulses):
            command = StimCommand(
                label=f"{label}_{i + 1}", value=float(value), kind="pulse",
                intensity=intensity, duration_ms=float(duration_ms),
                params={"train_label": label, "pulse_index": i + 1,
                        "train_size": n_pulses},
            )
            result = session.dispatch_stim(command, backend_key=backend)
            dispatched.append(result["dispatched_at"])
            if i < n_pulses - 1:
                # Schedule against the train's start, not the previous pulse, so
                # per-pulse dispatch cost does not accumulate into drift.
                target = dispatched[0] + (i + 1) * interval_ms / 1000.0
                remaining = target - time.time()
                if remaining > 0:
                    time.sleep(remaining)
    except SafetyError as exc:
        raise ValueError(
            f"Train stopped after {len(dispatched)} of {n_pulses} pulses -- "
            f"refused by safety gate: {exc}"
        ) from exc

    intervals = [
        (dispatched[i + 1] - dispatched[i]) * 1000.0 for i in range(len(dispatched) - 1)
    ]
    return to_jsonable({
        "session_id": session_id,
        "label": label,
        "n_delivered": len(dispatched),
        "requested_interval_ms": interval_ms,
        "realised_interval_ms": {
            "mean": sum(intervals) / len(intervals) if intervals else None,
            "min": min(intervals) if intervals else None,
            "max": max(intervals) if intervals else None,
        },
        "total_sec": (dispatched[-1] - dispatched[0]) if len(dispatched) > 1 else 0.0,
        "first_dispatch": dispatched[0] if dispatched else None,
    })


@mcp.tool()
def stim_status(session_id: str) -> dict[str, Any]:
    """Report attached stimulation backends, arming state, and dispatch counts.

    Args:
        session_id: The session to report on.

    Returns:
        Status of every attached backend and the count of stimulation events.
    """
    session = store.get(session_id)
    return to_jsonable({
        "session_id": session_id,
        "default_stim_backend": session.default_stim_backend,
        "backends": {k: b.status() for k, b in session.stim_backends.items()},
        "n_stim_events": len(session.events.query(origin="stim", limit=0)),
        "hardware_enabled": get_config().allow_hardware_stim,
    })
