"""The stimulation contract, and the safety gate every command passes through.

Sending a marker to a logging sink and firing a TMS coil are the same operation
from the agent's point of view -- ``send_stim_event`` with a label and a value.
They are emphatically *not* the same operation in terms of consequence, so the
difference is handled here, once, rather than being left to each backend.

Three gates apply to hardware backends, in order:

1. **Config gate** -- ``EEG_MCP_ALLOW_HARDWARE_STIM`` must be set. A default
   install cannot drive a stimulator at all, no matter what an agent asks for.
2. **Arm gate** -- the backend must be explicitly armed, and arming expires.
   An agent that goes quiet for ten minutes cannot come back and fire a coil.
3. **Limit gate** -- intensity, duration and inter-stimulus interval are clamped
   against configured ceilings, and violations raise rather than saturate.

None of this replaces the interlocks on the device itself. It is a software
backstop for an autonomous caller, not a safety certification.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..utils import now


class SafetyError(RuntimeError):
    """Raised when a command violates a configured stimulation limit."""


@dataclass(frozen=True)
class SafetyLimits:
    """Ceilings applied to every dispatched command."""

    max_intensity: float = 100.0
    max_duration_ms: float = 10_000.0
    min_interval_ms: float = 0.0
    arm_ttl_sec: float = 300.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_intensity": self.max_intensity,
            "max_duration_ms": self.max_duration_ms,
            "min_interval_ms": self.min_interval_ms,
            "arm_ttl_sec": self.arm_ttl_sec,
        }


@dataclass
class StimCommand:
    """One stimulation action to deliver.

    ``value`` is the integer-ish code that lands in marker streams and trigger
    lines; ``intensity``/``unit`` are the physical dose for hardware backends
    (e.g. 60 "%MSO" for TMS, 2.0 "mA" for tES) and are ignored by software ones.
    """

    label: str
    value: float = 1.0
    kind: str = "marker"  # marker | pulse | train | ramp | custom
    intensity: float | None = None
    unit: str | None = None
    duration_ms: float = 0.0
    target: str | None = None  # coil / montage / channel identifier
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "kind": self.kind,
            "intensity": self.intensity,
            "unit": self.unit,
            "duration_ms": self.duration_ms,
            "target": self.target,
            "params": self.params,
        }


class StimBackend(ABC):
    """Base class handling lifecycle, arming, limits, and latency accounting."""

    #: Backend identifier used in tool arguments.
    name: str = "base"
    #: True when the backend can cause physical stimulation of a person.
    is_hardware: bool = False

    def __init__(self, limits: SafetyLimits | None = None, **options: Any) -> None:
        self.limits = limits or SafetyLimits()
        self.options = options
        self._open = False
        self._armed_until: float | None = None
        self._last_dispatch: float | None = None
        self._count = 0
        self._lock = threading.Lock()

    # --- subclass hooks ---------------------------------------------------

    def _open_impl(self) -> None:
        """Acquire the transport. Override when the backend owns a resource."""

    def _close_impl(self) -> None:
        """Release the transport. Must tolerate being called when not open."""

    @abstractmethod
    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        """Actually deliver the command. Return backend-specific detail."""

    def capabilities(self) -> dict[str, Any]:
        """What this backend supports, for discovery from the agent side."""
        return {
            "name": self.name,
            "is_hardware": self.is_hardware,
            "kinds": ["marker"],
            "supports_intensity": False,
            "requires_arm": self.is_hardware,
        }

    # --- lifecycle --------------------------------------------------------

    def open(self) -> None:
        if self._open:
            return
        if self.is_hardware:
            from ..config import get_config

            if not get_config().allow_hardware_stim:
                raise SafetyError(
                    f"Backend '{self.name}' drives physical stimulation hardware and "
                    "is disabled. Set EEG_MCP_ALLOW_HARDWARE_STIM=1 in the server "
                    "environment to enable it, and only do so on a rig with the "
                    "appropriate approvals and device-level interlocks in place."
                )
        self._open_impl()
        self._open = True

    def close(self) -> None:
        try:
            self._close_impl()
        finally:
            self._open = False
            self._armed_until = None

    @property
    def is_open(self) -> bool:
        return self._open

    # --- arming -----------------------------------------------------------

    def arm(self, ttl_sec: float | None = None) -> dict[str, Any]:
        """Permit dispatch for a bounded window. Hardware backends need this."""
        ttl = float(ttl_sec if ttl_sec is not None else self.limits.arm_ttl_sec)
        if ttl <= 0:
            raise ValueError("arm ttl must be > 0 seconds")
        with self._lock:
            self._armed_until = now() + ttl
            return {"armed": True, "expires_in_sec": ttl, "backend": self.name}

    def disarm(self) -> dict[str, Any]:
        with self._lock:
            self._armed_until = None
            return {"armed": False, "backend": self.name}

    @property
    def is_armed(self) -> bool:
        if not self.is_hardware:
            return True
        with self._lock:
            return self._armed_until is not None and now() < self._armed_until

    # --- the gate ---------------------------------------------------------

    def _check(self, command: StimCommand) -> None:
        if not self._open:
            raise RuntimeError(
                f"Backend '{self.name}' is not open. Call attach_stim_backend first."
            )
        if self.is_hardware and not self.is_armed:
            raise SafetyError(
                f"Backend '{self.name}' is not armed (or arming expired). Call "
                "arm_stim before delivering hardware stimulation."
            )
        if command.intensity is not None:
            if command.intensity < 0:
                raise SafetyError(f"Negative intensity {command.intensity} rejected.")
            if command.intensity > self.limits.max_intensity:
                raise SafetyError(
                    f"intensity={command.intensity} exceeds the configured ceiling "
                    f"{self.limits.max_intensity}. Raise EEG_MCP_MAX_STIM_INTENSITY "
                    "only if the protocol genuinely requires it."
                )
        if command.duration_ms < 0:
            raise SafetyError(f"Negative duration {command.duration_ms} ms rejected.")
        if command.duration_ms > self.limits.max_duration_ms:
            raise SafetyError(
                f"duration_ms={command.duration_ms} exceeds the configured ceiling "
                f"{self.limits.max_duration_ms} ms."
            )
        if self.limits.min_interval_ms > 0 and self._last_dispatch is not None:
            gap_ms = (now() - self._last_dispatch) * 1000.0
            if gap_ms < self.limits.min_interval_ms:
                raise SafetyError(
                    f"Only {gap_ms:.1f} ms since the last stimulation; the minimum "
                    f"safe interval is {self.limits.min_interval_ms} ms."
                )

    def send(self, command: StimCommand) -> dict[str, Any]:
        """Gate, dispatch, and time one stimulation command."""
        self._check(command)
        t_start = now()
        detail = self._dispatch(command)
        t_end = now()
        with self._lock:
            self._last_dispatch = t_end
            self._count += 1
            count = self._count
        return {
            "backend": self.name,
            "is_hardware": self.is_hardware,
            "dispatched_at": t_start,
            # Time spent inside the transport. This is the number to watch for
            # closed-loop timing: it does not include the filter group delay or
            # the poll interval, both of which are reported separately.
            "dispatch_latency_ms": (t_end - t_start) * 1000.0,
            "sequence": count,
            "command": command.as_dict(),
            "detail": detail,
        }

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "open": self._open,
            "is_hardware": self.is_hardware,
            "armed": self.is_armed,
            "armed_until": self._armed_until,
            "dispatch_count": self._count,
            "last_dispatch": self._last_dispatch,
            "limits": self.limits.as_dict(),
            "capabilities": self.capabilities(),
        }
