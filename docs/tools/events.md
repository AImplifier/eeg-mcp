# Event Tools

One log, one clock. Board markers, replayed annotations, dispatched
stimulations and manual notes all land in the same table — which is what lets a
closed-loop session be reconstructed afterwards without joining two clocks.

## Event origins

| `origin` | Comes from |
|---|---|
| `marker` | The board's marker channel (live) |
| `annotation` | A replayed recording's annotations or trigger channel |
| `stim` | A command we dispatched, delivered **or refused** |
| `manual` | Logged by hand with `log_event` |

Every record carries `event_id`, `timestamp` (Unix seconds), `label`, `origin`,
`value`, `duration`, `sample_index`, and a free-form `payload`.

`sample_index` is an **absolute** index into the session's monotonic sample
counter, shared by the raw and filtered buffers, so an event locates exactly the
same samples in either.

---

## `log_event`

Record something the server did not generate itself — a task onset, a subject
report, an experimenter note.

| Arg | Default | Meaning |
|---|---|---|
| `label` | — | Event name |
| `value` | `1.0` | Numeric code; nonzero if marking the stream |
| `duration` | `0.0` | Seconds; 0 for instantaneous |
| `mark_in_stream` | `true` | Also punch into the board's marker channel |
| `payload` | `None` | Extra structured detail |

```text
log_event(session_id="s1", label="eyes_closed", value=11)
```

`marked_in_stream: true` in the response means the code went into the
acquisition stream itself, so the recording carries the event independently of
this session's log.

Do **not** use this for stimulations — `send_stim_event` logs those itself, with
the backend detail and delivery outcome attached.

---

## `get_events`

Read the log.

| Arg | Default | Meaning |
|---|---|---|
| `last_seconds` | `None` | Only the last N seconds |
| `origin` | `None` | Filter by origin |
| `label` | `None` | Filter to one label |
| `limit` | `200` | Max returned, most-recent-truncated |

```text
get_events(session_id="s1", origin="stim", last_seconds=60)
```

**Returns:** `events` in chronological order, `n_total`, and `counts_by_label`.

---

## `get_epoch_around_event`

Online epoching: the signal window around an event that already occurred.

| Arg | Default | Meaning |
|---|---|---|
| `label` | `None` | Event label; `None` takes the most recent of any kind |
| `tmin` | `-0.2` | Window start relative to the event (negative = before) |
| `tmax` | `0.8` | Window end relative to the event |
| `filtered` | `true` | Use the filtered buffer |
| `occurrence` | `-1` | `-1` most recent, `-2` the one before, `0` earliest |
| `max_samples` | `500` | Decimation cap |

```text
get_epoch_around_event(session_id="s1", label="cue_left", tmin=-0.2, tmax=0.8)
```

**Returns:** `event`, `times` (relative to the event), `data`, `channels`,
`n_matching_events`.

!!! info "Verified sample-accurate"
    In the project's test suite, a synthetic recording with a spike planted at
    each annotation onset shows that spike at **t = 0 ± 0 ms** in the extracted
    epoch — annotation timing, replay clock, ring-buffer indexing and epoch
    extraction all agree.

### Two failure modes worth knowing

**The event scrolled out.**

```text
That event is 12.4 s older than the 60 s buffer holds. Increase
EEG_MCP_BUFFER_SEC, request the epoch sooner, or pass filtered=false if the
filter chain was set after the event occurred.
```

The last clause matters: the filtered buffer only starts filling when
`set_filters` is called, so it can be shorter than the raw one.

**The window is not recorded yet.**

```text
The window extends 0.80 s past the event but only 0.31 s have been recorded
since. Wait, or lower tmax.
```

Use `occurrence=-2` for the second-most-recent event when you need the full
post-event window immediately.

---

## `clear_events`

Empty the log without stopping the stream — useful between experimental blocks.
The signal buffer is untouched.
