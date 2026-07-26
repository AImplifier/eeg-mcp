# Processing Tools

Attaching custom real-time processors — yours or the built-ins.

→ To **write** one, see the [Extending guide](../extending.md). This page is the
tool reference.

---

## `list_processors`

What is available to attach, and where each came from.

```json
{
  "n_available": 5,
  "processors": [
    {"name": "band_power", "streaming": true, "uses": "filtered",
     "source": "builtin", "params": {"window_sec": {"default": 2.0, "doc": "..."}}},
    {"name": "beta_burst", "streaming": true, "source": "dir:beta_burst.py"}
  ],
  "load_errors": []
}
```

`load_errors` matters: a typo in `EEG_MCP_PLUGINS` appears there rather than as
a silently missing processor.

!!! info "Only provisioned processors are attachable"
    An agent cannot name an arbitrary import path — importing a module executes
    it. The registry is populated solely from the server environment
    (`eeg_mcp.processors` entry points, `EEG_MCP_PLUGINS`, `EEG_MCP_PLUGIN_DIR`).

---

## Built-in processors

| Name | Streaming | Uses | Does |
|---|---|---|---|
| `band_power` | yes | filtered | Rolling relative band power per channel |
| `threshold_trigger` | yes | filtered | Emits an event (and optionally stimulates) on a threshold crossing |
| `artifact_detector` | yes | **raw** | Flags amplitude and gradient artifacts |
| `band_tokenizer` | yes | filtered | Quantises log band power into discrete tokens |

`artifact_detector` runs on raw deliberately: a band-pass attenuates exactly the
large slow excursions — movement, electrode pop — it exists to catch.

---

## `attach_processor`

```text
attach_processor(session_id="s1", processor="threshold_trigger",
                 params={"band": "alpha", "threshold": 0.45,
                         "refractory_sec": 1.0, "stimulate": true})
```

A **streaming** processor starts running immediately, inside the acquisition
loop — reacting within one poll interval, with no agent round-trip. That is how
closed-loop logic gets fast.

A **non-streaming** processor does nothing until `run_processor`.

Attaching a processor whose name is already attached replaces it, which is how
you change parameters.

!!! warning "Streaming processors share the acquisition loop"
    Slow code stalls acquisition and drops samples. Check `mean_ms`/`max_ms` in
    `processor_status` against `poll_interval_ms`.

---

## `detach_processor` / `reset_processors`

`detach_processor` removes one and releases its state.

`reset_processors` clears accumulated state and outputs **without** detaching —
useful between experimental blocks, and after a replay seek so a rolling window
does not straddle the discontinuity.

---

## `processor_status`

Health and cost per processor.

```json
{
  "processors": {
    "beta_burst": {"enabled": true, "n_calls": 412, "n_errors": 0,
                   "mean_ms": 0.31, "max_ms": 1.8, "n_outputs": 96}
  },
  "poll_interval_ms": 50.0
}
```

- `max_ms` approaching `poll_interval_ms` → starving acquisition; confirm with
  `throughput_ratio` in `stream_status`.
- `enabled: false` → auto-disabled after 10 errors. `last_error` says why.

---

## `get_processor_output`

Recent outputs from a streaming processor, newest last, plus `latest`.

| Arg | Default | Meaning |
|---|---|---|
| `limit` | `20` | How many recent outputs |

History is capped at 512 outputs per processor.

---

## `run_processor`

Execute once, now, on the most recent window.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `2.0` | Window length to pass in |
| `filtered` | `None` | Override the processor's declared preference |

Works for both modes: the only way to run a non-streaming processor, and a way
to get an answer from a streaming one without waiting for its next output.

A `null` result usually means a rolling window has not filled yet — normal for
the first few calls.

---

## `get_tokens`

Discrete tokens from a tokenizing processor, for sequence models.

```text
get_tokens(session_id="s1", processor="band_tokenizer", limit=64)
```

```json
{
  "vocab_size": 320,
  "tokens_per_group": 40,
  "layout": ["Fz:delta", "Fz:theta", "Fz:alpha", "..."],
  "n_groups": 64,
  "groups": [{"timestamp": 1785038239.04, "tokens": [12, 47, 91, "..."]}],
  "flat": [12, 47, 91, "..."]
}
```

`groups` preserves window structure; `flat` is ready to feed a model. `layout`
labels what each position within a group corresponds to.

Works with **any** processor exposing `token_history(limit)` — see
[Extending](../extending.md#worked-example-2-an-eeg-tokenizer).

!!! tip "`band_tokenizer` is a starting point, not a scheme"
    It quantises log band power into `n_channels × n_bands × n_levels` tokens.
    Real EEG foundation models use learned codebooks. Its value is that the
    plumbing works end to end, so you can validate your pipeline before your
    codebook exists.
