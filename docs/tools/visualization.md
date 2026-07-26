# Visualization Tools

A live browser view, and self-contained HTML files you can keep.

Everything is dependency-free: figures are SVG built from strings, pages inline
their own CSS. No matplotlib (slow to import in a request path), no Plotly
bundle (3 MB of JavaScript per file), **no CDN** — a plot that renders blank
because a CDN was unreachable is worse than no plot, and a clinical machine may
have no internet at all.

---

## `start_monitor`

Open a live browser view of a running session.

| Arg | Default | Meaning |
|---|---|---|
| `window_sec` | `5.0` | Seconds shown at once; adjustable in the page |
| `refresh_ms` | `500` | Redraw interval |
| `port` | `0` | Loopback port; 0 lets the OS pick a free one |
| `allow_control` | `true` | Render the transport buttons and enable the control endpoint |

```json
{
  "url": "http://127.0.0.1:54812/?t=3Jk9_pQz...",
  "port": 54812,
  "bound_to": "127.0.0.1",
  "allow_control": true,
  "controls": ["play/pause", "restart", "skip -10s/+10s", "speed", "stop",
               "mark event", "save report", "save traces", "save spectrum"]
}
```

The page shows:

- **Rolling traces**, each channel autoscaled to its own amplitude, with the
  per-channel peak in µV printed beside it. Per-channel scaling is what keeps a
  montage readable when one electrode has ten times its neighbour's amplitude.
- **Event markers** — dashed for markers and annotations, solid red for stimulation.
- **Live relative band power** and the dominant band.
- **A per-electrode quality table** updating as you watch.
- **A filtered/raw toggle**, so you can see what the chain is actually doing.
- **Replay position** when the session is a replay.

### Driving the session from the page

With `allow_control` (the default) the page carries a control bar, so an
operator watching the screen does not have to go back to the agent for every
action:

| Control | Action | Applies to |
|---|---|---|
| ⏮ Restart | Seek to 0 and resume | Replay only |
| ⏯ Pause / Resume | Hold or continue the media clock | Replay only |
| −10s / +10s | Skip relative | Replay only |
| Scrub bar | Seek to any position | Replay only |
| Speed | 0.25× … 8× | Replay only |
| ⏹ Stop | Halt acquisition, release the device | Any session |
| 🔖 Mark | Log an event with the typed label | Any session |
| 💾 Report / Traces / Spectrum | Write a standalone HTML file | Any session |
| 🗑 Events | Clear the event log | Any session |

Keyboard: <kbd>Space</kbd> play/pause, <kbd>←</kbd>/<kbd>→</kbd> skip 10 s,
<kbd>M</kbd> mark.

Replay-only controls are disabled on a live board, and everything is disabled
once the session is stopped. Save buttons disable when nothing is buffered.

!!! info "Stop keeps the data"
    The Stop button halts the producer and releases the device but **leaves the
    session in the store**, so buffered signal can still be reviewed and saved.
    That is deliberately different from the `stop_stream` tool, which drops the
    session entirely.

    One caveat worth knowing: **seeking discards buffered signal** (necessary so
    no window straddles the discontinuity). Seek and then immediately Stop and
    there is nothing left to save — the error says so explicitly rather than
    telling you to wait for a buffer that will never refill.

### Read-only mode

```text
start_monitor(session_id="s1", allow_control=false)
```

Renders no buttons, and the control endpoint refuses **even a correctly
authenticated request**. Use it when the screen is visible to someone who should
watch but not touch.

!!! warning "Loopback only, token-gated"
    Binds to `127.0.0.1` and requires the random per-monitor token in the URL,
    so it is not reachable from the network and cannot be read by guessing the
    port. It is **not encrypted and is not an authentication system** — never
    expose it through a tunnel, reverse proxy, or port forward.

    Because the page can now *stop a recording*, control requests carry an extra
    guard: they must be `POST` with an `application/json` content type, which
    forces a CORS preflight this server never answers. A hostile local page
    therefore cannot drive the session even if it guessed the port and token.

    The page also carries a strict CSP and loads nothing external.

Only one monitor runs per session; starting again replaces it. `stop_stream`
tears it down automatically.

---

## `stop_monitor` / `list_monitors`

`stop_monitor(session_id)` closes a monitor and frees its port.
`list_monitors()` returns active monitors with their URLs.

---

## `plot_stream`

Render the recent signal as a standalone HTML file — a frozen snapshot, unlike
`start_monitor`.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `10.0` | Signal to draw |
| `channels` | `None` | Subset by name |
| `filtered` | `true` | Draw the filtered signal |
| `show_events` | `true` | Overlay event markers |

**Returns:** `path` to the written file, plus what it contains. Files go to
`$EEG_MCP_HOME/plots/` with a timestamped name.

---

## `plot_spectrum`

PSD and band composition as a standalone HTML file.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `8.0` | Window; longer gives finer resolution |
| `channels` | `None` | Subset by name |
| `fmax` | `60.0` | Highest frequency drawn |

Contains a log-power spectrum with the classical bands shaded behind it, a
stacked relative-band-power bar per channel, and a peak-frequency table.

**Returns:** `path`, `peak_freq` per channel, `dominant_band`.

---

## `export_report`

The one artefact to keep from a session.

| Arg | Default | Meaning |
|---|---|---|
| `seconds` | `10.0` | Window for traces and spectrum |
| `include_spectrum` | `true` | Add PSD and band-power sections |
| `notes` | `None` | Free text recorded at the top |

Sections: stat tiles (channels, rate, dominant band, flagged channels, event
count) → traces with event markers → spectrum and band bars → per-channel
quality table with faults called out → the full event log.

```text
export_report(session_id="s1", seconds=15,
              notes="Routine 20-minute EEG. PDR ~9.8 Hz symmetric.")
```

**Returns:** `path`, `n_events`, `bad_channels`, `dominant_band`, `sections`.

Typically ~120 KB. The test suite asserts self-containment by scanning output for
any external URL, and validates that every SVG parses and every plotted point
sits inside its viewBox — which is what catches a scaling regression that would
silently push traces out of view.

Reports carry a research-use disclaimer. They are not diagnostic documents.

---

## Choosing between them

| Want | Use |
|---|---|
| Watch it happen | `start_monitor` |
| Keep a picture of this moment | `plot_stream` |
| Look at the frequency content | `plot_spectrum` |
| One file for the record | `export_report` |
| Numbers, not pictures | `read_window`, `get_band_power`, `get_psd` |
