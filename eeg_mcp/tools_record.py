"""MCP tools for recording a session to disk and registering it for analysis.

The division of labour: the **file** is the substance and the **database** is
the index. A row saying "this recording exists" is worthless without the signal,
so the file is written first and the database row only inserted once the file is
safely on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .app import mcp
from .config import get_config
from .db import list_recordings as _list_recordings, register_recording
from .recorder import Recorder, build_stem, recover
from .session import store
from .utils import to_jsonable


@mcp.tool()
def start_recording(
    session_id: str,
    subject: str | None = None,
    task: str | None = None,
    run: str | None = None,
    session_label: str | None = None,
    dataset: str | None = None,
    name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Begin writing the live signal to disk, continuously.

    Call this as soon as the session starts if you intend to keep the data. The
    ring buffer only holds the last minute or so, so anything not recorded is
    gone -- a session streamed for an hour without recording leaves nothing but
    whatever reports you exported.

    The raw, unfiltered signal is written. Filter settings are stored alongside
    it so a filtered view is reproducible; the amplifier's actual output is not
    reconstructible from anything else.

    Args:
        session_id: The running session to record.
        subject: Participant identifier, e.g. 'sub-04' or a study code. Becomes
            the subject row in the metadata store. Avoid direct identifiers.
        task: What the participant is doing, e.g. 'rest', 'motor-imagery'.
        run: Run number within the task, e.g. '01'.
        session_label: Visit or session label, e.g. '01' or 'baseline'.
        dataset: Study or collection name that groups recordings together.
        name: Explicit filename stem, overriding the BIDS-style name built from
            subject/session/task/run.
        notes: Free text kept with the recording.

    Returns:
        Where the recording is being written and the metadata attached to it.
    """
    session = store.get(session_id)
    if session.is_recording:
        raise ValueError(
            f"Session '{session_id}' is already recording to "
            f"{session.recorder.fif_path}. Call stop_recording first."
        )
    if not session.is_running:
        raise ValueError(
            f"Session '{session_id}' is not running, so there is nothing to "
            "record. Start the stream or replay first."
        )

    info = session.source.info
    metadata = {
        "subject": subject, "task": task, "run": run, "session": session_label,
        "dataset": dataset, "notes": notes,
        "source": info.as_dict(),
        "filters": session.chain.describe(),
        "mcp_session_id": session_id,
    }
    stem = build_stem(session_id, subject, session_label, task, run, name)
    recorder = Recorder(
        directory=get_config().record_dir, stem=stem,
        ch_names=list(info.ch_names), sfreq=info.sfreq, metadata=metadata,
    )
    # Anchor the recording at the moment writing begins, not at the moment the
    # session started -- otherwise annotation onsets would be offset by however
    # long the stream ran before anyone pressed record.
    from .utils import now

    recorder.open(started_at=now())
    session.recorder = recorder
    session.recording_result = None
    session.log(f"recording started: {recorder.fif_path.name}")

    return to_jsonable({
        "recording": True,
        "session_id": session_id,
        "stem": stem,
        "fif_path": str(recorder.fif_path),
        "directory": str(recorder.directory),
        "metadata": {k: v for k, v in metadata.items() if k not in ("source",)},
        "note": "Raw (unfiltered) signal is being written. Events logged from "
                "now on become annotations in the file. Call stop_recording to "
                "finalise and register it.",
    })


@mcp.tool()
def stop_recording(
    session_id: str,
    register: bool = True,
    keep_sidecar: bool = False,
) -> dict[str, Any]:
    """Finish the recording, write the ``.fif``, and register it for analysis.

    The event log becomes annotations inside the file, and -- unless you pass
    ``register=false`` -- a row is inserted into the metadata store with the
    subject, task, run and provenance, so neuro-mcp can find and open it.

    Args:
        session_id: The session whose recording to finish.
        register: Insert the recording into the metadata database.
        keep_sidecar: Keep the raw crash-recovery files after conversion.
            Normally removed once the .fif is safely written.

    Returns:
        The written file, its annotations, and the database ids if registered.
    """
    session = store.get(session_id)
    if session.recorder is None:
        raise ValueError(
            f"Session '{session_id}' is not recording. Call start_recording first."
        )

    result = session.finish_recording(keep_sidecar=keep_sidecar)
    if not result or not result.get("written"):
        return to_jsonable({
            "session_id": session_id, "written": False,
            "reason": (result or {}).get("reason", "nothing recorded"),
        })

    payload: dict[str, Any] = {"session_id": session_id, **{
        k: v for k, v in result.items() if k != "annotations"
    }, "n_annotations": result.get("n_annotations", 0)}

    if register:
        metadata = session.recorder.metadata
        try:
            registration = register_recording(
                source_path=result["fif_path"],
                sfreq=result["sfreq"],
                n_channels=result["n_channels"],
                duration_sec=result["duration_sec"],
                events=result.get("annotations"),
                subject=metadata.get("subject"),
                dataset=metadata.get("dataset"),
                task=metadata.get("task"),
                run=metadata.get("run"),
                session_label=metadata.get("session"),
                notes=metadata.get("notes"),
                provenance={
                    "source": metadata.get("source"),
                    "filters": metadata.get("filters"),
                    "mcp_session_id": session_id,
                },
            )
            payload["registered"] = registration
        except Exception as exc:
            # The file is the thing that matters; a database problem must not
            # look like a failed recording.
            payload["registered"] = False
            payload["registration_error"] = str(exc)
            payload["warning"] = (
                "The recording was written successfully but could not be "
                "registered in the metadata store. The .fif is intact and can "
                "be registered later or opened directly."
            )
    else:
        payload["registered"] = False

    payload["next"] = (
        "Open it in neuro-mcp with load_neuro, or replay it here with "
        "start_replay to rehearse against your own data."
    )
    return to_jsonable(payload)


@mcp.tool()
def recording_status(session_id: str) -> dict[str, Any]:
    """Report whether a session is recording, and how much has been written.

    Args:
        session_id: The session to check.

    Returns:
        Current recorder state, or the result of the last finished recording.
    """
    session = store.get(session_id)
    if session.recorder is None:
        return to_jsonable({
            "session_id": session_id, "recording": False,
            "last_recording": session.recording_result and {
                k: v for k, v in session.recording_result.items()
                if k != "annotations"
            },
            "hint": "Call start_recording to begin writing this session to disk.",
        })
    return to_jsonable({
        "session_id": session_id,
        **session.recorder.status(),
        "n_events_so_far": len(session.events),
    })


@mcp.tool()
def list_recordings(limit: int = 25, subject: str | None = None) -> dict[str, Any]:
    """List recordings in the metadata store, newest first.

    Includes recordings written by neuro-mcp if both servers share a
    ``DATABASE_URL``, so this is the catalogue across both tools.

    Args:
        limit: Maximum number to return.
        subject: Only recordings for this participant identifier.

    Returns:
        Recordings with their subject, task, duration and annotation count.
    """
    config = get_config()
    rows = _list_recordings(limit=limit, subject=subject)
    return to_jsonable({
        "n_recordings": len(rows),
        "database_url": config.database_url,
        "record_dir": str(config.record_dir),
        "recordings": rows,
        "note": "Point neuro-mcp at the same DATABASE_URL to analyse these "
                "recordings there without an import step.",
    })


@mcp.tool()
def recover_recording(header_path: str, keep_sidecar: bool = False) -> dict[str, Any]:
    """Finish a recording whose process died before it could be closed.

    Samples are appended to disk as they arrive and the header is written up
    front, precisely so an interrupted session is not lost. Point this at the
    orphaned ``.eegmcp.json`` header to convert what was captured into a ``.fif``.

    Events are not recoverable -- they lived in memory -- so the resulting file
    carries signal without annotations.

    Args:
        header_path: Path to the ``<stem>.eegmcp.json`` left behind.
        keep_sidecar: Keep the raw files after conversion.

    Returns:
        The recovered file and how much signal it contains.
    """
    target = Path(header_path).expanduser()
    if not target.exists():
        raise FileNotFoundError(
            f"No recording header at {target}. Look for '*.eegmcp.json' in "
            f"{get_config().record_dir}."
        )
    return to_jsonable(recover(target, keep_sidecar=keep_sidecar))
