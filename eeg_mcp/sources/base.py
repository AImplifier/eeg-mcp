"""The one interface every signal source implements.

A live BrainFlow board and a replayed EDF file look identical from here: both
are polled, both hand back a chunk of samples with wall-clock timestamps and
whatever events occurred inside that chunk. Everything downstream -- ring
buffer, filter chain, band power, closed-loop stimulation -- is written once
against this interface and therefore works in both modes.

That symmetry is the design goal: an agent pipeline developed against a
recording runs unchanged against hardware.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class RawEvent:
    """An event as reported by a source, before it enters the session log."""

    timestamp: float
    label: str
    origin: str
    value: float | None = None
    duration: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceChunk:
    """Samples produced since the previous poll, plus events inside them."""

    data: np.ndarray  # (n_channels, n_samples), microvolts
    timestamps: np.ndarray  # (n_samples,), Unix epoch seconds
    events: list[RawEvent] = field(default_factory=list)
    # Set by a finite source (a replay) once it has emitted its last sample.
    exhausted: bool = False

    @property
    def n_samples(self) -> int:
        return int(self.data.shape[1]) if self.data.ndim == 2 else 0


@dataclass
class SourceInfo:
    """Static description of what a source produces."""

    kind: str  # "brainflow" | "replay" | "lsl"
    name: str
    sfreq: float
    ch_names: list[str]
    # BrainFlow and replay sources always convert to microvolts (see
    # replay_source's MNE loader); an LSL source has no such guarantee and
    # reports whatever unit the outlet itself declares, or "unknown" if it
    # declares none. Tools that display or scale a signal should read this
    # rather than assuming microvolts.
    unit: str = "microvolts"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def n_channels(self) -> int:
        return len(self.ch_names)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "sfreq": self.sfreq,
            "n_channels": self.n_channels,
            "ch_names": self.ch_names,
            "unit": self.unit,
            **self.metadata,
        }


class StreamSource(ABC):
    """Polled producer of EEG chunks.

    Implementations must be safe to ``poll`` from a single background thread
    while ``stop`` may be called from another.
    """

    info: SourceInfo

    @abstractmethod
    def start(self) -> None:
        """Acquire the device / arm the replay clock. Idempotent."""

    @abstractmethod
    def poll(self) -> SourceChunk:
        """Return everything produced since the last call. Never blocks long."""

    @abstractmethod
    def stop(self) -> None:
        """Release the device / halt the clock. Idempotent and safe after error."""

    def insert_marker(self, value: float, label: str) -> bool:
        """Punch a marker into the source's own stream, if it supports one.

        Returns False when the source has no marker channel, in which case the
        caller should still record the event in the session log -- it just will
        not be embedded in the device's own recording.
        """
        return False

    def describe(self) -> dict[str, Any]:
        return self.info.as_dict()
