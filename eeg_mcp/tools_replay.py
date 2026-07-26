"""MCP tools for replaying an existing recording as if it were live."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .app import mcp
from .config import get_config
from .session import store
from .sources.replay_source import ReplaySource
from .utils import to_jsonable


def _resolve_path(path: str) -> Path:
    """Accept an absolute path, or one relative to the configured recordings dir."""
    candidate = Path(path).expanduser()
    if candidate.is_absolute() or candidate.exists():
        return candidate
    from_root = get_config().recordings_dir / path
    if from_root.exists():
        return from_root
    raise FileNotFoundError(
        f"No recording at '{path}'. Give an absolute path, or place the file in "
        f"{get_config().recordings_dir} (set EEG_MCP_RECORDINGS_DIR to change)."
    )


@mcp.tool()
def inspect_recording(
    path: str, board: str | None = None, max_events: int = 100
) -> dict[str, Any]:
    """Open a recording and describe it without starting playback.

    Use this before start_replay to see how long the file is, what channels it
    has, and which annotations will be re-emitted as events during replay.

    Args:
        path: Absolute path, or a filename inside the configured recordings dir.
            EDF/BDF/GDF/SET/FIF and anything else MNE reads; also BrainFlow CSV.
        board: Required only for BrainFlow CSV, which has no header describing
            its layout (e.g. 'cyton', 'synthetic').
        max_events: Cap on how many of the file's events to list.

    Returns:
        Duration, sampling rate, channel names, and the recording's own events.
    """
    source = ReplaySource(_resolve_path(path), board=board)
    return to_jsonable({
        **source.info.as_dict(),
        "events": source.file_events(limit=max_events),
        "next": "Call start_replay with this path to play it at recording rate.",
    })


@mcp.tool()
def start_replay(
    session_id: str,
    path: str,
    speed: float = 1.0,
    loop: bool = False,
    start_sec: float = 0.0,
    board: str | None = None,
    picks: str | None = None,
    use_stim_channel: bool = True,
) -> dict[str, Any]:
    """Replay a recording against a wall clock, re-emitting its events.

    This is the safe way to build a real-time pipeline: the session behaves
    exactly like a live stream -- same buffering, same chunked delivery, same
    tools -- so code developed here runs unchanged against hardware. The
    recording's annotations arrive in the event log at their original timings.

    Args:
        session_id: Name you will pass to every later call for this session.
        path: Absolute path, or a filename inside the configured recordings dir.
        speed: Playback rate. 1.0 is true recording rate; 2.0 is twice as fast;
            0.5 is half. Faster-than-real-time is useful for testing a pipeline
            over a long recording, but stops being a realistic latency test.
        loop: Restart from the beginning on reaching the end, emitting a
            'replay_loop' event each time. Good for long-running rehearsals.
        start_sec: Offset into the recording to begin from.
        board: Required only for BrainFlow CSV files.
        picks: MNE channel selection, e.g. 'eeg' or 'mag'. Defaults to EEG.
        use_stim_channel: Also extract events from a hardware trigger channel,
            not just annotations.

    Returns:
        Session status, including the number of events loaded from the file.
    """
    source = ReplaySource(
        path=_resolve_path(path),
        speed=speed,
        loop=loop,
        start_sec=start_sec,
        board=board,
        picks=picks,
        use_stim_channel=use_stim_channel,
    )
    session = store.create(session_id, source)
    session.start()
    return to_jsonable({
        "started": True,
        "session_id": session_id,
        **session.status(),
        "file_events_preview": source.file_events(limit=20),
        "next": "Use the same tools as a live stream: set_filters, read_window, "
                "get_band_power, get_events. Control playback with replay_control.",
    })


@mcp.tool()
def replay_control(
    session_id: str,
    action: str,
    position_sec: float | None = None,
    speed: float | None = None,
) -> dict[str, Any]:
    """Pause, resume, seek, or change the speed of a replay session.

    Args:
        session_id: A session created by start_replay.
        action: One of 'pause', 'resume', 'seek', 'speed', 'status'.
        position_sec: Target position, required for action='seek'.
        speed: New playback rate, required for action='speed'.

    Returns:
        The replay position after the action.
    """
    session = store.get(session_id)
    source = session.source
    if not isinstance(source, ReplaySource):
        raise ValueError(
            f"Session '{session_id}' is a live {source.info.kind} stream, not a "
            "replay. Playback controls apply only to sessions from start_replay."
        )

    verb = action.strip().lower()
    if verb == "pause":
        source.pause()
    elif verb == "resume":
        source.resume()
    elif verb == "seek":
        if position_sec is None:
            raise ValueError("action='seek' requires position_sec.")
        source.seek(position_sec)
        # The buffer now holds signal from before the jump; clearing avoids
        # handing back a window that silently straddles the discontinuity.
        session.raw_buffer.clear()
        if session.filtered_buffer is not None:
            session.filtered_buffer.clear()
        session.chain.reset()
    elif verb == "speed":
        if speed is None:
            raise ValueError("action='speed' requires speed.")
        source.set_speed(speed)
    elif verb != "status":
        raise ValueError(
            f"Unknown action '{action}'. Use pause, resume, seek, speed, or status."
        )

    return to_jsonable({
        "session_id": session_id,
        "action": verb,
        "replay": source.position(),
        "running": session.is_running,
    })


@mcp.tool()
def list_recording_events(
    session_id: str, max_events: int = 200
) -> dict[str, Any]:
    """List all events present in a replay session's source file.

    Distinct from get_events: this is the *complete* event list from the file,
    including events that have not been reached yet. get_events returns only
    what has actually been emitted so far.

    Args:
        session_id: A session created by start_replay.
        max_events: Cap on the number returned.

    Returns:
        Every event in the recording with its onset in seconds.
    """
    source = store.get(session_id).source
    if not isinstance(source, ReplaySource):
        raise ValueError(
            f"Session '{session_id}' is not a replay session; it has no source file."
        )
    events = source.file_events(limit=max_events)
    return to_jsonable({
        "session_id": session_id,
        "path": source.info.metadata.get("path"),
        "n_events_in_file": source.info.metadata.get("n_events_in_file"),
        "returned": len(events),
        "events": events,
    })
