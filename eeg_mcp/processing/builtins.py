"""Processors that ship with eeg-mcp.

These are useful in their own right, but they are also the reference for writing
your own: each one is a small, complete example of a different shape --
rolling feature extraction, a closed-loop trigger, a quality gate, and
discretisation for sequence models.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..utils import FREQ_BANDS
from .base import Processor, ProcessorContext


class _RollingWindow:
    """Accumulates chunks until a full analysis window is available.

    Streaming processors receive whatever the board hands over -- often 12
    samples at a time -- which is far too short for a spectral estimate. This
    keeps the last ``window_sec`` and reports when it is worth recomputing.
    """

    def __init__(self, n_channels: int, sfreq: float, window_sec: float,
                 hop_sec: float) -> None:
        self.capacity = max(int(round(window_sec * sfreq)), 16)
        self.hop = max(int(round(hop_sec * sfreq)), 1)
        self._buffer = np.zeros((n_channels, self.capacity), dtype=np.float64)
        self._filled = 0
        self._since_emit = 0

    def push(self, chunk: np.ndarray) -> bool:
        n = chunk.shape[1]
        if n == 0:
            return False
        if n >= self.capacity:
            self._buffer[:] = chunk[:, -self.capacity:]
            self._filled = self.capacity
        else:
            self._buffer[:, :-n] = self._buffer[:, n:]
            self._buffer[:, -n:] = chunk
            self._filled = min(self._filled + n, self.capacity)
        self._since_emit += n
        if self._filled >= self.capacity and self._since_emit >= self.hop:
            self._since_emit = 0
            return True
        return False

    @property
    def data(self) -> np.ndarray:
        return self._buffer

    def reset(self) -> None:
        self._buffer.fill(0.0)
        self._filled = 0
        self._since_emit = 0


class BandPowerProcessor(Processor):
    """Rolling band power, recomputed every ``hop_sec``."""

    name = "band_power"
    description = "Rolling relative band power per channel, computed continuously."
    streaming = True
    uses = "filtered"
    params = {
        "window_sec": (2.0, "Analysis window length in seconds."),
        "hop_sec": (0.25, "How often to recompute, in seconds."),
        "bands": (None, "Band names to compute; null means all five."),
        "channels": (None, "Channel names to include; null means all."),
    }

    def setup(self, ctx: ProcessorContext) -> None:
        names = self.config["channels"] or ctx.ch_names
        self._indices = [ctx.channel_index(n) for n in names]
        self._names = list(names)
        self._bands = {
            b: FREQ_BANDS[b] for b in (self.config["bands"] or list(FREQ_BANDS))
        }
        self._window = _RollingWindow(
            len(self._indices), ctx.sfreq,
            self.config["window_sec"], self.config["hop_sec"],
        )

    def reset(self) -> None:
        if hasattr(self, "_window"):
            self._window.reset()

    def process(self, data, timestamps, ctx):
        if not self._window.push(data[self._indices, :]):
            return None
        from ..dsp import band_powers

        result = band_powers(
            self._window.data, ctx.sfreq, ch_names=self._names,
            bands=self._bands, relative=True,
        )
        return {
            "dominant_band": result["dominant_band"],
            "mean": result["mean_across_channels"],
            "per_channel": result["per_channel"],
        }


class ThresholdTriggerProcessor(Processor):
    """Fires an event when a band's relative power crosses a threshold.

    This is the closed loop with the agent taken out of the path. Running in the
    producer thread, it reacts within one poll interval instead of waiting for a
    tool call, a model response, and another tool call. Set ``stimulate`` to
    dispatch through the session's stimulation backend as well.
    """

    name = "threshold_trigger"
    description = ("Emits an event (and optionally stimulates) when relative "
                   "band power crosses a threshold.")
    streaming = True
    uses = "filtered"
    params = {
        "band": ("alpha", "Band to watch."),
        "threshold": (0.5, "Relative-power threshold, 0-1."),
        "direction": ("above", "'above' or 'below'."),
        "channels": (None, "Channels to average; null means all."),
        "window_sec": (2.0, "Analysis window length."),
        "hop_sec": (0.25, "How often to test the threshold."),
        "refractory_sec": (1.0, "Minimum gap between triggers."),
        "label": ("threshold_trigger", "Event label to emit."),
        "stimulate": (False, "Also dispatch a stimulation command."),
        "stim_value": (1.0, "Marker value when stimulating."),
    }

    def setup(self, ctx: ProcessorContext) -> None:
        band = self.config["band"]
        if band not in FREQ_BANDS:
            raise ValueError(
                f"Unknown band '{band}'. Choose from: {', '.join(FREQ_BANDS)}"
            )
        if self.config["direction"] not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")
        names = self.config["channels"] or ctx.ch_names
        self._indices = [ctx.channel_index(n) for n in names]
        self._names = list(names)
        self._window = _RollingWindow(
            len(self._indices), ctx.sfreq,
            self.config["window_sec"], self.config["hop_sec"],
        )
        self._last_trigger = 0.0
        self._n_triggers = 0

    def reset(self) -> None:
        if hasattr(self, "_window"):
            self._window.reset()
        self._last_trigger = 0.0

    def process(self, data, timestamps, ctx):
        if not self._window.push(data[self._indices, :]):
            return None
        from ..dsp import band_powers

        band = self.config["band"]
        result = band_powers(
            self._window.data, ctx.sfreq, ch_names=self._names,
            bands={band: FREQ_BANDS[band]}, relative=True,
        )
        value = float(result["mean_across_channels"].get(f"{band}_rel", 0.0))
        threshold = float(self.config["threshold"])
        crossed = (value > threshold if self.config["direction"] == "above"
                   else value < threshold)

        now_ts = float(timestamps[-1]) if timestamps.size else 0.0
        fired = False
        if crossed and (now_ts - self._last_trigger) >= self.config["refractory_sec"]:
            self._last_trigger = now_ts
            self._n_triggers += 1
            fired = True
            payload = {"band": band, "value": value, "threshold": threshold,
                       "direction": self.config["direction"],
                       "processor": self.name}
            ctx.emit_event(self.config["label"], value=value, payload=payload)
            if self.config["stimulate"] and ctx.stimulate is not None:
                try:
                    ctx.stimulate(label=self.config["label"],
                                  value=float(self.config["stim_value"]))
                except Exception as exc:
                    # A refused or failed stimulation must not take down the
                    # producer; the attempt is already in the event log.
                    ctx.log(f"threshold_trigger stimulation failed: {exc}")
        return {f"{band}_rel": value, "crossed": crossed, "fired": fired,
                "n_triggers": self._n_triggers}


class ArtifactDetectorProcessor(Processor):
    """Flags windows containing amplitude or gradient artifacts.

    Deliberately runs on **raw** signal: a band-pass would attenuate exactly the
    large slow excursions (movement, electrode pop) this exists to catch.
    """

    name = "artifact_detector"
    description = "Flags amplitude and gradient artifacts on the raw signal."
    streaming = True
    uses = "raw"
    params = {
        "amplitude_uv": (150.0, "Peak amplitude above this is an artifact."),
        "gradient_uv": (50.0, "Sample-to-sample jump above this is an artifact."),
        "window_sec": (1.0, "Analysis window length."),
        "hop_sec": (0.25, "How often to test."),
        "emit_events": (True, "Log an event when an artifact is detected."),
        "label": ("artifact", "Event label to emit."),
    }

    def setup(self, ctx: ProcessorContext) -> None:
        self._names = list(ctx.ch_names)
        self._window = _RollingWindow(
            len(self._names), ctx.sfreq,
            self.config["window_sec"], self.config["hop_sec"],
        )
        self._n_artifacts = 0
        self._was_clean = True

    def reset(self) -> None:
        if hasattr(self, "_window"):
            self._window.reset()
        self._was_clean = True

    def process(self, data, timestamps, ctx):
        if not self._window.push(data):
            return None
        window = self._window.data
        peak = np.max(np.abs(window), axis=1)
        gradient = np.max(np.abs(np.diff(window, axis=1)), axis=1)

        bad_amp = peak > self.config["amplitude_uv"]
        bad_grad = gradient > self.config["gradient_uv"]
        bad = bad_amp | bad_grad
        affected = [n for n, flag in zip(self._names, bad) if flag]

        if affected and self._was_clean:
            # Edge-triggered: log the onset, not every window of a long artifact.
            self._n_artifacts += 1
            if self.config["emit_events"]:
                ctx.emit_event(self.config["label"], value=float(len(affected)),
                               payload={"channels": affected,
                                        "processor": self.name})
        self._was_clean = not affected

        return {"clean": not affected, "affected_channels": affected,
                "max_amplitude_uv": float(np.max(peak)),
                "max_gradient_uv": float(np.max(gradient)),
                "n_artifacts": self._n_artifacts}


class BandTokenizerProcessor(Processor):
    """Turns a window into discrete tokens for a sequence model.

    A minimal, honest EEG tokenizer: compute log relative band power per
    (channel, band), quantise each into ``n_levels`` bins, and flatten to a
    token id. One window becomes ``n_channels * n_bands`` tokens drawn from a
    vocabulary of ``n_channels * n_bands * n_levels``.

    This is a *starting point*, not a published scheme. Real EEG tokenizers for
    foundation models use learned codebooks (VQ-VAE and similar); the value here
    is that the plumbing -- windowing, streaming, token accumulation, retrieval
    through an MCP tool -- is already done, so swapping the quantiser for your
    own model is a small change to :meth:`process`.
    """

    name = "band_tokenizer"
    description = ("Quantises log band power into discrete tokens for sequence "
                   "models. Replace the quantiser with your own codebook.")
    streaming = True
    uses = "filtered"
    params = {
        "window_sec": (1.0, "Window that becomes one token group."),
        "hop_sec": (0.5, "Stride between token groups."),
        "n_levels": (8, "Quantisation levels per (channel, band)."),
        "bands": (None, "Bands to tokenise; null means all five."),
        "channels": (None, "Channels to tokenise; null means all."),
        "db_floor": (-20.0, "Lower edge of the quantised range, in dB."),
        "db_ceiling": (20.0, "Upper edge of the quantised range, in dB."),
        "max_history": (512, "Token groups kept for get_tokens."),
    }

    def setup(self, ctx: ProcessorContext) -> None:
        names = self.config["channels"] or ctx.ch_names
        self._indices = [ctx.channel_index(n) for n in names]
        self._names = list(names)
        self._bands = list(self.config["bands"] or list(FREQ_BANDS))
        for band in self._bands:
            if band not in FREQ_BANDS:
                raise ValueError(
                    f"Unknown band '{band}'. Choose from: {', '.join(FREQ_BANDS)}"
                )
        self._levels = int(self.config["n_levels"])
        if self._levels < 2:
            raise ValueError("n_levels must be at least 2")
        self._window = _RollingWindow(
            len(self._indices), ctx.sfreq,
            self.config["window_sec"], self.config["hop_sec"],
        )
        self._history: list[dict[str, Any]] = []
        self.vocab_size = len(self._names) * len(self._bands) * self._levels

    def reset(self) -> None:
        if hasattr(self, "_window"):
            self._window.reset()
        self._history = []

    def _quantise(self, db: float) -> int:
        floor, ceiling = self.config["db_floor"], self.config["db_ceiling"]
        frac = (db - floor) / max(ceiling - floor, 1e-9)
        return int(np.clip(int(frac * self._levels), 0, self._levels - 1))

    def process(self, data, timestamps, ctx):
        if not self._window.push(data[self._indices, :]):
            return None
        from ..dsp import band_powers

        result = band_powers(
            self._window.data, ctx.sfreq, ch_names=self._names,
            bands={b: FREQ_BANDS[b] for b in self._bands}, relative=True,
        )

        tokens: list[int] = []
        levels: list[int] = []
        for ch_i, channel in enumerate(self._names):
            entry = result["per_channel"][channel]
            for band_i, band in enumerate(self._bands):
                power = float(entry.get(band, 0.0))
                db = 10.0 * np.log10(max(power, 1e-12))
                level = self._quantise(db)
                levels.append(level)
                slot = (ch_i * len(self._bands)) + band_i
                tokens.append(slot * self._levels + level)

        group = {
            "timestamp": float(timestamps[-1]) if timestamps.size else 0.0,
            "tokens": tokens,
            "levels": levels,
        }
        self._history.append(group)
        if len(self._history) > int(self.config["max_history"]):
            del self._history[: len(self._history) - int(self.config["max_history"])]

        return {
            "tokens": tokens,
            "vocab_size": self.vocab_size,
            "n_tokens": len(tokens),
            "layout": [f"{c}:{b}" for c in self._names for b in self._bands],
            "n_groups": len(self._history),
        }

    def token_history(self, limit: int = 64) -> dict[str, Any]:
        """Recent token groups, for ``get_tokens``."""
        groups = self._history[-max(1, limit):]
        return {
            "vocab_size": self.vocab_size,
            "tokens_per_group": len(self._names) * len(self._bands),
            "layout": [f"{c}:{b}" for c in self._names for b in self._bands],
            "n_groups": len(groups),
            "groups": groups,
            "flat": [t for g in groups for t in g["tokens"]],
        }
