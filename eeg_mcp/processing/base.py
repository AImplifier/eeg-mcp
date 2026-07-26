"""The contract for a pluggable real-time processor.

A processor is anything that turns a window of EEG into something else: a
feature vector, a classification, an artifact flag, a discrete token sequence
for a foundation model, a decision to stimulate. eeg-mcp ships a few, but the
point of this module is that yours plugs in on equal footing with them.

Two execution modes, and the choice matters:

* ``streaming = True`` -- called from the **producer thread** on every chunk as
  it arrives, typically every 50 ms. This is the low-latency path: a decision
  made here does not wait for an agent round-trip. The cost is that your code
  is now inside the acquisition loop, so if it takes longer than the poll
  interval it will stall acquisition and drop samples.
* ``streaming = False`` -- called only when someone asks, via ``run_processor``,
  on a window pulled from the ring buffer. Slower to react, but free to be as
  expensive as it likes.

Rule of thumb: if it must react within a chunk, stream it and keep it under a
few milliseconds. Everything else is on demand.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np


@dataclass
class ProcessorContext:
    """What a processor is told about the session it is running in.

    ``emit_event`` is the callback that makes closed-loop work possible without
    an agent in the path: a processor that detects something can log it, and
    optionally trigger stimulation, from inside the producer thread.
    """

    session_id: str
    sfreq: float
    ch_names: list[str]
    emit_event: Callable[..., Any]
    log: Callable[[str], None]
    stimulate: Callable[..., Any] | None = None

    @property
    def n_channels(self) -> int:
        return len(self.ch_names)

    def channel_index(self, name: str) -> int:
        if name not in self.ch_names:
            raise ValueError(
                f"Unknown channel '{name}'. Available: {', '.join(self.ch_names)}"
            )
        return self.ch_names.index(name)


class Processor(ABC):
    """Base class for a real-time processor.

    Subclass, set ``name``, implement :meth:`process`, and either register it
    through an entry point or point ``EEG_MCP_PLUGINS`` at it.
    """

    #: Identifier used in tool arguments. Must be unique and stable.
    name: str = ""
    #: One line shown by ``list_processors``; make it useful.
    description: str = ""
    #: Run inside the producer thread on every chunk (see the module docstring).
    streaming: bool = False
    #: Which buffer to receive: "filtered" falls back to raw when no chain is
    #: set; "raw" always gets the unfiltered signal.
    uses: str = "filtered"
    #: Declared parameters: {name: (default, "description")}. Used for
    #: validation and for telling an agent what it can tune.
    params: dict[str, tuple[Any, str]] = {}

    def __init__(self, **kwargs: Any) -> None:
        unknown = set(kwargs) - set(self.params)
        if unknown:
            raise ValueError(
                f"Processor '{self.name}' got unknown parameter(s) "
                f"{sorted(unknown)}. Accepted: {sorted(self.params) or 'none'}"
            )
        self.config: dict[str, Any] = {
            key: kwargs.get(key, default) for key, (default, _doc) in self.params.items()
        }
        # Runtime health, surfaced by processor_status.
        self.n_calls = 0
        self.n_errors = 0
        self.last_error: str | None = None
        self.total_seconds = 0.0
        self.max_seconds = 0.0
        self.enabled = True

    def setup(self, ctx: ProcessorContext) -> None:
        """Called once when attached. Allocate state here, not in ``process``."""

    @abstractmethod
    def process(
        self, data: np.ndarray, timestamps: np.ndarray, ctx: ProcessorContext
    ) -> dict[str, Any] | None:
        """Handle one chunk or window.

        Args:
            data: ``(n_channels, n_samples)`` in microvolts.
            timestamps: ``(n_samples,)`` Unix epoch seconds.
            ctx: Session context, including ``emit_event``.

        Returns:
            A JSON-serialisable dict recorded as this processor's latest
            output, or ``None`` to record nothing for this call.
        """

    def reset(self) -> None:
        """Discard accumulated state. Called on seek and on explicit reset."""

    def teardown(self) -> None:
        """Release anything acquired in :meth:`setup`."""

    # --- internals used by the session ------------------------------------

    def _invoke(
        self, data: np.ndarray, timestamps: np.ndarray, ctx: ProcessorContext
    ) -> dict[str, Any] | None:
        """Run ``process`` with timing and error containment.

        A plugin exception must never kill acquisition, so failures are counted
        and the processor is disabled after repeated errors rather than being
        retried forever inside the producer loop.
        """
        if not self.enabled:
            return None
        start = time.perf_counter()
        try:
            result = self.process(data, timestamps, ctx)
        except Exception as exc:
            self.n_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.n_errors >= 10:
                self.enabled = False
                ctx.log(
                    f"processor '{self.name}' disabled after 10 errors; "
                    f"last: {self.last_error}"
                )
            return None
        finally:
            elapsed = time.perf_counter() - start
            self.n_calls += 1
            self.total_seconds += elapsed
            self.max_seconds = max(self.max_seconds, elapsed)
        return result

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "streaming": self.streaming,
            "uses": self.uses,
            "config": self.config,
            "params": {k: {"default": d, "doc": doc}
                       for k, (d, doc) in self.params.items()},
        }

    def status(self) -> dict[str, Any]:
        mean_ms = (self.total_seconds / self.n_calls * 1000) if self.n_calls else 0.0
        return {
            **self.describe(),
            "enabled": self.enabled,
            "n_calls": self.n_calls,
            "n_errors": self.n_errors,
            "last_error": self.last_error,
            "mean_ms": mean_ms,
            "max_ms": self.max_seconds * 1000,
        }


@dataclass
class ProcessorOutput:
    """One recorded output, kept in a bounded history per processor."""

    timestamp: float
    value: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp, **self.value}
