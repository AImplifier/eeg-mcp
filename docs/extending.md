# Extending: Custom Processing & Tokenization

eeg-mcp ships a handful of processors, but the point of this page is that
**yours run on exactly the same footing as the built-ins** — same discovery,
same tools, same place in the acquisition loop. If you have a feature
extractor, a classifier, an artifact gate, or an EEG tokenizer, it plugs in
without forking anything.

---

## The 20-line version

Write a class, point the server at it, attach it:

```python title="my_plugins/rms.py"
import numpy as np
from eeg_mcp.processing import Processor


class RMSProcessor(Processor):
    name = "rms"                      # how it is referenced in tool calls
    description = "Mean RMS amplitude across channels."
    streaming = True                  # run on every chunk, in the producer thread
    uses = "filtered"                 # or "raw"
    params = {                        # {name: (default, doc)}
        "alert_uv": (100.0, "Emit an event above this RMS."),
    }

    def process(self, data, timestamps, ctx):
        rms = float(np.sqrt(np.mean(np.square(data))))
        if rms > self.config["alert_uv"]:
            ctx.emit_event("high_rms", value=rms)
        return {"rms_uv": rms}
```

```bash
export EEG_MCP_PLUGIN_DIR=/path/to/my_plugins
```

```text
list_processors()                                    # 'rms' is now listed
attach_processor(session_id="s1", processor="rms")
get_processor_output(session_id="s1", processor="rms")
```

That is the whole contract. The rest of this page is the detail that matters
when it stops being 20 lines.

---

## The two execution modes

This is the decision that shapes everything else.

| | `streaming = True` | `streaming = False` |
|---|---|---|
| Called | Every chunk (~50 ms), in the producer thread | Only via `run_processor` |
| Latency | One poll interval | Whenever someone asks |
| Budget | **Must finish in a few ms** | Free to be expensive |
| Use for | Triggers, gating, running features, tokenizing | Heavy models, batch scoring, anything offline-ish |

!!! danger "A slow streaming processor drops samples"
    Your code runs *inside the acquisition loop*. If `process` takes longer than
    the poll interval, the producer falls behind and the driver discards
    samples — the signal is gone, not delayed.

    `processor_status` reports `mean_ms` and `max_ms` per processor. Keep
    `max_ms` well under `poll_interval_ms` (50 ms by default). If you cannot,
    make it non-streaming, or have the streaming half enqueue work for a thread
    you own.

---

## What you get, and what you must return

```python
def process(self, data, timestamps, ctx):
    ...
```

- **`data`** — `(n_channels, n_samples)` float64, **microvolts**. Filtered or
  raw depending on `uses` (`"filtered"` falls back to raw when no chain is set).
- **`timestamps`** — `(n_samples,)` Unix epoch seconds, the same clock as every
  event and stimulation, so you can compare directly.
- **`ctx`** — the session context:

| Member | Use |
|---|---|
| `ctx.sfreq`, `ctx.ch_names`, `ctx.n_channels` | Geometry |
| `ctx.channel_index("Cz")` | Name → row, raising a readable error if absent |
| `ctx.emit_event(label, value=, payload=)` | Log into the unified event log |
| `ctx.stimulate(label=, value=)` | Dispatch through the session's stim backend |
| `ctx.log(message)` | Line in the session history |

Return a **JSON-serialisable dict**, recorded as the processor's latest output,
or `None` to record nothing this call (normal while a rolling window fills).

### Lifecycle hooks

```python
def setup(self, ctx): ...     # once, on attach — allocate state here
def reset(self): ...          # on reset_processors and after a replay seek
def teardown(self): ...       # on detach
```

Allocate in `setup`, not in `process` — `process` runs 20 times a second.

---

## Chunks are shorter than you think

A board hands over whatever has arrived, often **12 samples**. That is far too
short for a spectral estimate. Accumulate:

```python
class _Rolling:
    def __init__(self, n_channels, sfreq, window_sec, hop_sec):
        self.capacity = max(int(window_sec * sfreq), 16)
        self.hop = max(int(hop_sec * sfreq), 1)
        self._buf = np.zeros((n_channels, self.capacity))
        self._filled = self._since = 0

    def push(self, chunk):
        n = chunk.shape[1]
        self._buf[:, :-n] = self._buf[:, n:]
        self._buf[:, -n:] = chunk
        self._filled = min(self._filled + n, self.capacity)
        self._since += n
        if self._filled >= self.capacity and self._since >= self.hop:
            self._since = 0
            return True
        return False
```

`eeg_mcp/processing/builtins.py` has this as `_RollingWindow`; the built-ins all
use it and it is a reasonable thing to copy.

---

## Worked example 1 — a closed-loop trigger

A processor that acts, rather than only reporting. Because it runs in the
producer thread, it reacts within one poll interval instead of waiting for a
tool call, a model response, and another tool call.

```python title="my_plugins/beta_burst.py"
import numpy as np
from scipy import signal

from eeg_mcp.processing import Processor


class BetaBurstProcessor(Processor):
    """Detect beta bursts over motor cortex and cue on them."""

    name = "beta_burst"
    description = "Triggers when beta envelope exceeds a running threshold."
    streaming = True
    uses = "filtered"
    params = {
        "channels": (["C3", "C4"], "Channels to average."),
        "sd_threshold": (2.0, "Envelope SDs above the running mean."),
        "refractory_sec": (0.5, "Minimum gap between triggers."),
        "stimulate": (False, "Also dispatch to the stimulation backend."),
    }

    def setup(self, ctx):
        self.idx = [ctx.channel_index(c) for c in self.config["channels"]]
        self.sos = signal.butter(4, [13, 30], btype="bandpass",
                                 fs=ctx.sfreq, output="sos")
        self.zi = None                      # carried filter state
        self.mean = self.var = None         # running statistics
        self.last_fire = 0.0
        self.n_bursts = 0

    def reset(self):
        self.zi = self.mean = self.var = None

    def process(self, data, timestamps, ctx):
        x = data[self.idx, :]
        # Stateful filtering: seed zi once, then carry it, or every chunk
        # boundary injects a transient that looks exactly like a burst.
        if self.zi is None:
            self.zi = signal.sosfilt_zi(self.sos)[:, None, :] * x[None, :, 0][..., None]
        y, self.zi = signal.sosfilt(self.sos, x, axis=-1, zi=self.zi)

        envelope = float(np.mean(np.abs(signal.hilbert(y, axis=-1))))

        # Exponential running statistics: adapts to drift without storing history.
        if self.mean is None:
            self.mean, self.var = envelope, 1.0
            return None
        alpha = 0.02
        delta = envelope - self.mean
        self.mean += alpha * delta
        self.var = (1 - alpha) * (self.var + alpha * delta * delta)
        z = delta / max(np.sqrt(self.var), 1e-9)

        now = float(timestamps[-1])
        fired = False
        if z > self.config["sd_threshold"] and \
           (now - self.last_fire) >= self.config["refractory_sec"]:
            self.last_fire, self.n_bursts, fired = now, self.n_bursts + 1, True
            ctx.emit_event("beta_burst", value=z, payload={"envelope": envelope})
            if self.config["stimulate"] and ctx.stimulate:
                try:
                    ctx.stimulate(label="beta_burst", value=7)
                except Exception as exc:
                    # A refused stimulation must not kill the producer; the
                    # attempt is already recorded in the event log.
                    ctx.log(f"beta_burst stim failed: {exc}")

        return {"envelope": envelope, "z": z, "fired": fired,
                "n_bursts": self.n_bursts}
```

!!! warning "Do not reach for `filtfilt`"
    Zero-phase filtering needs future samples. In a live stream they do not
    exist. Use `sosfilt` with a carried `zi`, as above, and accept the group
    delay — `stream_status` reports it.

---

## Worked example 2 — an EEG tokenizer

Turning EEG into discrete tokens for a transformer or a foundation model. The
plumbing — windowing, streaming, accumulation, retrieval — already exists; you
supply the quantiser.

```python title="my_plugins/vq_tokenizer.py"
import numpy as np

from eeg_mcp.processing import Processor


class VQTokenizer(Processor):
    """Tokenise windows against a learned codebook (VQ-VAE style)."""

    name = "vq_tokenizer"
    description = "Nearest-codebook-entry tokens from a learned embedding."
    streaming = True
    uses = "filtered"
    params = {
        "codebook_path": (None, "Path to an .npy codebook, (n_codes, n_features)."),
        "window_sec": (1.0, "Window that becomes one token."),
        "hop_sec": (0.5, "Stride between tokens."),
        "max_history": (1024, "Tokens retained for get_tokens."),
    }

    def setup(self, ctx):
        path = self.config["codebook_path"]
        if not path:
            raise ValueError(
                "vq_tokenizer needs codebook_path pointing at an .npy of shape "
                "(n_codes, n_features)."
            )
        self.codebook = np.load(path)          # (n_codes, n_features)
        self.vocab_size = int(self.codebook.shape[0])
        self.capacity = max(int(self.config["window_sec"] * ctx.sfreq), 16)
        self.hop = max(int(self.config["hop_sec"] * ctx.sfreq), 1)
        self.buf = np.zeros((ctx.n_channels, self.capacity))
        self.filled = self.since = 0
        self.history: list[dict] = []

    def reset(self):
        self.filled = self.since = 0
        self.history = []

    def _embed(self, window):
        """Your feature map. Must match how the codebook was trained."""
        spectrum = np.abs(np.fft.rfft(window, axis=-1))
        return np.log1p(spectrum).mean(axis=0)

    def process(self, data, timestamps, ctx):
        n = data.shape[1]
        self.buf[:, :-n] = self.buf[:, n:]
        self.buf[:, -n:] = data
        self.filled = min(self.filled + n, self.capacity)
        self.since += n
        if self.filled < self.capacity or self.since < self.hop:
            return None
        self.since = 0

        vector = self._embed(self.buf)
        vector = vector[: self.codebook.shape[1]]
        token = int(np.argmin(np.linalg.norm(self.codebook - vector, axis=1)))

        self.history.append({"timestamp": float(timestamps[-1]),
                             "tokens": [token]})
        if len(self.history) > self.config["max_history"]:
            del self.history[: len(self.history) - self.config["max_history"]]
        return {"tokens": [token], "vocab_size": self.vocab_size}

    # Implementing this makes the get_tokens tool work with your processor.
    def token_history(self, limit: int = 64):
        groups = self.history[-max(1, limit):]
        return {
            "vocab_size": self.vocab_size,
            "tokens_per_group": 1,
            "layout": ["vq"],
            "n_groups": len(groups),
            "groups": groups,
            "flat": [t for g in groups for t in g["tokens"]],
        }
```

Any processor exposing `token_history(limit)` works with:

```text
get_tokens(session_id="s1", processor="vq_tokenizer", limit=64)
```

```json
{
  "vocab_size": 512,
  "tokens_per_group": 1,
  "n_groups": 64,
  "flat": [187, 187, 42, 199, 187, ...],
  "groups": [{"timestamp": 1785038239.04, "tokens": [187]}]
}
```

Feed `flat` straight into your sequence model.

!!! tip "Start from `band_tokenizer`"
    The built-in quantises log band power into `n_channels × n_bands × n_levels`
    tokens. It is a **starting point, not a published scheme** — real EEG
    foundation models use learned codebooks. But it proves the pipeline
    end-to-end, so you can validate your plumbing before your codebook exists,
    then swap the quantiser.

---

## Making it discoverable

Three routes. All are **operator-controlled, in the server's environment**.

=== "Plugin directory (quickest)"

    ```bash
    export EEG_MCP_PLUGIN_DIR=/path/to/my_plugins
    ```

    Every non-underscore `.py` in the directory is imported and any `Processor`
    subclass registered. Restart the server to pick up changes.

=== "Environment list"

    ```bash
    export EEG_MCP_PLUGINS="mypkg.processors:BetaBurstProcessor,mypkg.tok:VQTokenizer"
    ```

    `module:ClassName`, comma-separated. Use `module:` with an empty class name
    to register every processor in the module.

=== "Entry point (for distribution)"

    ```toml title="pyproject.toml"
    [project.entry-points."eeg_mcp.processors"]
    beta_burst = "mypkg.processors:BetaBurstProcessor"
    vq_tokenizer = "mypkg.tok:VQTokenizer"
    ```

    `pip install` your package and the processors appear automatically. This is
    how to publish a processor for other people.

In an MCP client config:

```json
{
  "mcpServers": {
    "eeg-realtime": {
      "command": "/path/to/envs/eeg-mcp/bin/python",
      "args": ["-m", "eeg_mcp"],
      "env": { "EEG_MCP_PLUGIN_DIR": "/home/you/eeg-plugins" }
    }
  }
}
```

!!! info "Why an agent cannot just name an import path"
    Importing a module executes it. A tool that accepted a dotted path from a
    model would be arbitrary code execution driven by a language model — so
    `attach_processor` only accepts names already in the registry, and the
    registry is populated solely from the server's environment.

    `list_processors` also returns `load_errors`, so a typo in
    `EEG_MCP_PLUGINS` shows up as a message rather than a silently missing
    processor.

---

## Failure containment

A plugin exception cannot stop acquisition. Errors are counted, the message is
kept in `last_error`, and after **10 failures the processor is disabled** rather
than retried forever inside the producer loop. Healthy processors keep running.

```text
processor_status(session_id="s1")
```

```json
{
  "processors": {
    "beta_burst": {"enabled": true, "n_calls": 412, "n_errors": 0,
                   "mean_ms": 0.31, "max_ms": 1.8},
    "my_broken":  {"enabled": false, "n_errors": 10,
                   "last_error": "RuntimeError: this plugin is broken on purpose"}
  },
  "poll_interval_ms": 50.0
}
```

This is asserted in `testing/verify_processing.py`: a deliberately broken plugin
is attached and acquisition must continue at `throughput_ratio = 1.0`.

---

## Testing your processor

You do not need hardware. Use the synthetic board, or better, replay a recording
so the input is reproducible:

```python
import asyncio, os
os.environ["EEG_MCP_PLUGIN_DIR"] = "/path/to/my_plugins"

from fastmcp import Client
from eeg_mcp.server import mcp

async def main():
    async with Client(mcp) as c:
        async def call(t, **a):
            r = await c.call_tool(t, a)
            return r.data if r.data is not None else r.structured_content

        await call("start_replay", session_id="t", path="rec.edf", speed=4.0)
        await call("set_filters", session_id="t", bandpass_low=1, bandpass_high=40)
        await call("attach_processor", session_id="t", processor="beta_burst")
        await asyncio.sleep(5)
        print(await call("get_processor_output", session_id="t",
                         processor="beta_burst"))
        print(await call("processor_status", session_id="t"))
        await call("stop_stream", session_id="t")

asyncio.run(main())
```

Replay gives you a fixed input, so a change in your processor's output is a
change in *your code* rather than in the signal.

`testing/verify_processing.py` is a fuller example — it writes a plugin to a
temp directory, points the environment at it, and asserts discovery, execution,
event emission, timing, and containment.

---

## Checklist

- [ ] `name` is unique and stable — it is the public identifier
- [ ] `description` says what it does in one line
- [ ] `params` declares every setting with a default and a doc string
- [ ] State allocated in `setup`, cleared in `reset`
- [ ] `process` returns a JSON-serialisable dict, or `None`
- [ ] Streaming: `max_ms` measured and well under the poll interval
- [ ] Filters are causal with carried state — no `filtfilt`
- [ ] Tokenizers implement `token_history(limit)`
- [ ] Exercised against a replayed recording before hardware

---

## Also pluggable

Processing is not the only extension point:

- **[Stimulation backends](tools/stimulation.md)** — `template_serial`, `tms` and
  `tes` are configured with command templates from your device manual, so a new
  device usually needs no code at all.
- **[Recording](tools/recording.md)** — output is MNE-native `.fif` plus a
  metadata row in a neuro-mcp-compatible store, so downstream analysis is
  someone else's tool by design.
