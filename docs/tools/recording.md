# Recording Tools

Writing a session to disk, and registering it so analysis tools can find it.

!!! danger "Nothing is kept unless you record it"
    The ring buffer holds the last `EEG_MCP_BUFFER_SEC` seconds (60 by default)
    and nothing more. Stream for an hour without recording and **every sample is
    gone** — exported HTML reports are decimated pictures, not reanalysable data.

    Call `start_recording` as soon as the session starts if you intend to keep
    the data.

---

## The division of labour

The **file** is the substance; the **database** is the index. A row saying "this
recording exists" is worthless without the signal, so the file is written first
and the metadata row inserted only once the file is safely on disk. If the
database is unreachable, the recording still succeeds and says so.

---

## `start_recording`

Begin writing the live signal continuously.

| Arg | Meaning |
|---|---|
| `session_id` | The running session to record |
| `subject` | Participant identifier, e.g. `P01`. Avoid direct identifiers |
| `task` | What they are doing, e.g. `rest`, `motor-imagery` |
| `run` | Run number within the task, e.g. `01` |
| `session_label` | Visit label, e.g. `baseline` |
| `dataset` | Study or collection grouping recordings |
| `name` | Explicit filename stem, overriding the generated one |
| `notes` | Free text kept with the recording |

Entities produce a BIDS-style filename:

```text
sub-P01_ses-baseline_task-rest_run-01_eeg_raw.fif
```

(The `_raw` suffix is MNE's convention for a continuous recording. The BIDS
entities are also stored in the database `provenance`.)

!!! info "Raw signal is what gets written"
    Not the filtered view. Filter settings are stored alongside so the view is
    reproducible; the amplifier's actual output cannot be reconstructed from
    anything else.

### Crash safety

Samples are appended to a flat sidecar as they arrive, and the header is written
before the first sample — so a crash, power cut, or killed process leaves
recoverable data rather than nothing. The sidecar is removed only once the
`.fif` is written successfully.

Recover an interrupted session with [`recover_recording`](#recover_recording).

---

## `stop_recording`

Finish, convert, and register.

| Arg | Default | Meaning |
|---|---|---|
| `register` | `true` | Insert the metadata row |
| `keep_sidecar` | `false` | Keep the crash-recovery files after conversion |

```json
{
  "written": true,
  "fif_path": "/home/you/.eeg-mcp/recorded/sub-P01_..._eeg_raw.fif",
  "duration_sec": 604.2,
  "n_annotations": 41,
  "size_mb": 74.3,
  "registered": {"recording_id": 7, "subject_id": 2, "n_annotations": 41}
}
```

The **event log becomes annotations inside the file** — board markers, replayed
annotations, dispatched stimulations, and manual notes, all with onsets relative
to the recording start. Events outside the recorded span are dropped rather than
clamped; an annotation at `t=0` that actually happened before recording began
would be a quiet falsification.

`stop_stream` also finalises an open recording, so a session stopped mid-record
still produces a readable file.

---

## `recording_status`

Whether a session is recording, how many samples and megabytes so far, or the
result of the last finished recording.

---

## `list_recordings`

The catalogue from eeg-mcp's own metadata store, newest first.

| Arg | Default | Meaning |
|---|---|---|
| `limit` | `25` | Maximum returned |
| `subject` | `None` | Filter to one participant |

---

## `recover_recording`

Finish a recording whose process died before it could be closed.

```text
recover_recording(header_path="/home/you/.eeg-mcp/recorded/sub-P01_..._eeg.eegmcp.json")
```

Events are **not** recoverable — they lived in memory — so the resulting file
carries signal without annotations. Better than losing the session.

---

## The metadata store

**eeg-mcp owns its database. Nothing else writes to it.**

```json
"env": {
  "EEG_MCP_DATABASE_URL": "sqlite:////data/eeg-mcp/store.db"
}
```

Default is `sqlite:///$EEG_MCP_HOME/eeg_mcp.db`. The bare `DATABASE_URL`
variable — used by neuro-mcp and many other tools — is **deliberately ignored**,
so eeg-mcp can never inherit another application's store by accident.

### Readable by neuro-mcp, not shared with it

The schema matches
[neuro-mcp](https://github.com/AImplifier/neuro-mcp)'s exactly — the same
`subjects`, `datasets`, `recordings`, `annotations`, and `audit_log` tables — so
neuro-mcp can **open and read** a database written here with no import or
conversion step.

That is interoperability, and it is not the same as sharing:

| | |
|---|---|
| ✅ neuro-mcp **reads** eeg-mcp's database | The schema is identical, so every row is understood |
| ✅ neuro-mcp **opens** the `.fif` directly | Standard MNE format; the database is only an index |
| ❌ The two do **not** write to one store | Separate databases, separate variables, no cross-configuration |

eeg-mcp only ever **adds** rows: a subject if new, a dataset if new, one
recording, one annotation per event, one audit entry. It never amends or voids —
those are versioned clinical operations that belong to an analysis tool, not to
an acquisition server.

!!! success "Verified, not just claimed"
    `testing/verify_recording.py` reflects both projects' schemas and fails if
    any table diverges, then queries a database eeg-mcp wrote **through
    neuro-mcp's own ORM** — checking that recordings, subject relationships,
    provenance and versioned annotations all read back correctly, and that the
    referenced `.fif` opens with its annotations intact.

    ```
    PASS  eeg-mcp schema matches neuro-mcp column-for-column
    PASS  neuro-mcp's ORM reads the recording eeg-mcp wrote  [1 row(s)]
    PASS  annotations carry neuro-mcp's versioning fields  ['cue_a' v1 active]
    PASS  neuro-mcp's reader (MNE) opens it with annotations intact  [16 ch, 4 annotations]
    ```

---

## Handing off to analysis

```text
stop_recording(session_id="s1")
```

Then in neuro-mcp:

```text
load_neuro(session_id="analysis", path="<fif_path>")
```

Or replay it back here to rehearse a pipeline against your own data:

```text
start_replay(session_id="rehearse", path="<fif_path>", speed=2.0)
```

That round trip is verified end to end in `testing/verify_recording.py`.
