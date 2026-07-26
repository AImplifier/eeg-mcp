"""Environment-driven configuration for eeg-mcp.

Everything the server needs to differ between a laptop with a synthetic board
and a lab rig with a real amplifier and a stimulator comes from environment
variables, so the same installed package serves both without code changes.

Safety-relevant defaults are deliberately conservative: hardware stimulation is
*disabled* unless ``EEG_MCP_ALLOW_HARDWARE_STIM`` is explicitly set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_data_home() -> Path:
    root = os.environ.get("EEG_MCP_HOME")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".eeg-mcp"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Resolved runtime configuration."""

    data_home: Path
    # Seconds of signal held per stream session. The ring buffer is allocated
    # up-front at n_channels x (buffer_sec * sfreq) float64, so this is the main
    # memory knob: 64 ch x 60 s x 1000 Hz ~= 30 MB.
    buffer_sec: float
    # How often the producer thread drains the source into the ring buffer.
    poll_interval_sec: float
    # Where recordings are looked up when a relative path is given to replay.
    recordings_dir: Path
    # Where newly recorded sessions are written.
    record_dir: Path
    # eeg-mcp's own metadata store, always separate from any other tool's.
    # The schema is compatible with neuro-mcp's so that neuro-mcp can *read* a
    # recording written here; that is interoperability, not a shared store.
    database_url: str
    # --- Stimulation ---
    # Master switch. While False, hardware backends refuse to open and only the
    # log/LSL/marker software backends are usable.
    allow_hardware_stim: bool
    default_stim_backend: str
    serial_port: str | None
    serial_baudrate: int
    lsl_stream_name: str
    # Hard ceilings enforced on every dispatched stimulation command, on top of
    # whatever interlocks the device itself provides.
    max_stim_intensity: float
    max_stim_duration_ms: float
    min_stim_interval_ms: float

    @classmethod
    def from_env(cls) -> "Config":
        home = _default_data_home()
        return cls(
            data_home=home,
            buffer_sec=_env_float("EEG_MCP_BUFFER_SEC", 60.0),
            poll_interval_sec=_env_float("EEG_MCP_POLL_INTERVAL_SEC", 0.05),
            recordings_dir=Path(
                os.environ.get("EEG_MCP_RECORDINGS_DIR", str(home / "recordings"))
            ).expanduser(),
            record_dir=Path(
                os.environ.get("EEG_MCP_RECORD_DIR", str(home / "recorded"))
            ).expanduser(),
            # Deliberately only the EEG_MCP_-prefixed variable. The bare
            # DATABASE_URL belongs to other applications (neuro-mcp uses it),
            # and inheriting it would make eeg-mcp write into someone else's
            # store without anyone having asked for that. eeg-mcp always owns
            # its own database; other tools read from it, they do not share it.
            database_url=(
                os.environ.get("EEG_MCP_DATABASE_URL")
                or f"sqlite:///{home / 'eeg_mcp.db'}"
            ),
            allow_hardware_stim=_env_bool("EEG_MCP_ALLOW_HARDWARE_STIM", False),
            default_stim_backend=os.environ.get("EEG_MCP_STIM_BACKEND", "log"),
            serial_port=os.environ.get("EEG_MCP_SERIAL_PORT") or None,
            serial_baudrate=int(_env_float("EEG_MCP_SERIAL_BAUDRATE", 115200)),
            lsl_stream_name=os.environ.get("EEG_MCP_LSL_STREAM_NAME", "eeg-mcp-markers"),
            max_stim_intensity=_env_float("EEG_MCP_MAX_STIM_INTENSITY", 100.0),
            max_stim_duration_ms=_env_float("EEG_MCP_MAX_STIM_DURATION_MS", 10_000.0),
            min_stim_interval_ms=_env_float("EEG_MCP_MIN_STIM_INTERVAL_MS", 0.0),
        )


_config: Config | None = None


def get_config() -> Config:
    """Return the process-wide config, resolving it from the environment once."""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def set_config(config: Config) -> None:
    """Override the config (used by tests to point at temp dirs / relax limits)."""
    global _config
    _config = config
