"""Backend discovery and construction.

Keeping the catalogue in one table means an agent can ask what stimulation
targets exist, what options each needs, and which ones are dangerous, before it
tries to attach one.
"""

from __future__ import annotations

from typing import Any

from ..config import get_config
from .backends import (
    BrainFlowMarkerBackend,
    LogBackend,
    LSLMarkerBackend,
    SerialTTLBackend,
    TemplateSerialBackend,
    TESBackend,
    TMSBackend,
)
from .base import SafetyLimits, StimBackend

BACKEND_SPECS: dict[str, dict[str, Any]] = {
    "log": {
        "cls": LogBackend,
        "is_hardware": False,
        "options": {},
        "description": "Records commands, emits nothing. Default; safe for dry runs.",
    },
    "lsl": {
        "cls": LSLMarkerBackend,
        "is_hardware": False,
        "options": {"stream_name": "str", "source_id": "str"},
        "description": "Publishes markers on an LSL outlet for PsychoPy/OpenViBE/"
                       "LabRecorder and other network consumers.",
    },
    "brainflow_marker": {
        "cls": BrainFlowMarkerBackend,
        "is_hardware": False,
        "options": {},
        "description": "Writes into the live board's marker channel, sample-aligned. "
                       "Requires an active BrainFlow stream.",
    },
    "serial_ttl": {
        "cls": SerialTTLBackend,
        "is_hardware": True,
        "options": {"port": "str", "baudrate": "int", "pulse_ms": "float"},
        "description": "Byte triggers over a serial port to a trigger box or "
                       "stimulator input.",
    },
    "template_serial": {
        "cls": TemplateSerialBackend,
        "is_hardware": True,
        "options": {"port": "str", "baudrate": "int", "templates": "dict[str,str]",
                    "read_reply": "bool"},
        "description": "Generic ASCII-protocol serial device driven by command "
                       "templates from your device manual.",
    },
    "tms": {
        "cls": TMSBackend,
        "is_hardware": True,
        "options": {"port": "str", "baudrate": "int", "templates": "dict[str,str]",
                    "read_reply": "bool"},
        "description": "TMS stimulator over a templated serial protocol. Intensity "
                       "in %MSO. Requires arming and explicit hardware enablement.",
    },
    "tes": {
        "cls": TESBackend,
        "is_hardware": True,
        "options": {"port": "str", "baudrate": "int", "templates": "dict[str,str]",
                    "max_current_ma": "float", "read_reply": "bool"},
        "description": "tDCS/tACS/tRNS stimulator over a templated serial protocol. "
                       "Intensity in mA, default ceiling 4 mA.",
    },
}


def list_backend_specs() -> list[dict[str, Any]]:
    """Catalogue of available backends, with hardware ones clearly flagged."""
    config = get_config()
    out = []
    for name, spec in BACKEND_SPECS.items():
        out.append({
            "name": name,
            "is_hardware": spec["is_hardware"],
            "description": spec["description"],
            "options": spec["options"],
            "available": (not spec["is_hardware"]) or config.allow_hardware_stim,
            "blocked_reason": (
                None if (not spec["is_hardware"] or config.allow_hardware_stim)
                else "EEG_MCP_ALLOW_HARDWARE_STIM is not set"
            ),
        })
    return out


def default_limits() -> SafetyLimits:
    """Safety ceilings resolved from the process environment."""
    config = get_config()
    return SafetyLimits(
        max_intensity=config.max_stim_intensity,
        max_duration_ms=config.max_stim_duration_ms,
        min_interval_ms=config.min_stim_interval_ms,
    )


def create_backend(
    name: str,
    options: dict[str, Any] | None = None,
    limits: SafetyLimits | None = None,
) -> StimBackend:
    """Build a backend by name, filling unset transport options from config."""
    key = str(name).strip().lower()
    if key not in BACKEND_SPECS:
        raise ValueError(
            f"Unknown stim backend '{name}'. Available: {', '.join(BACKEND_SPECS)}"
        )
    config = get_config()
    opts = dict(options or {})

    # Fall back to environment-configured transport settings so a correctly
    # provisioned server needs no per-call wiring.
    if key in ("serial_ttl", "template_serial", "tms", "tes"):
        opts.setdefault("port", config.serial_port)
        opts.setdefault("baudrate", config.serial_baudrate)
    if key == "lsl":
        opts.setdefault("stream_name", config.lsl_stream_name)

    spec = BACKEND_SPECS[key]
    return spec["cls"](limits=limits or default_limits(), **opts)
