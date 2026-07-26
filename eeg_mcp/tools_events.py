"""MCP tools for the unified event log."""

from __future__ import annotations

from typing import Any

import numpy as np

from .app import mcp
from .session import store
from .utils import now, to_jsonable


@mcp.tool()
def log_event(
    session_id: str,
    label: str,
    value: float = 1.0,
    duration: float = 0.0,
    mark_in_stream: bool = True,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an event against the stream's clock.

    For anything the server did not generate itself: a task onset, a subject
    report, an experimenter note. Stimulation commands are logged automatically
    by send_stim_event, so do not log those by hand.

    Args:
        session_id: The session to annotate.
        label: Event name, e.g. 'trial_start' or 'eyes_closed'.
        value: Numeric code. Also written to the board's marker channel when
            mark_in_stream is set; BrainFlow reserves 0, so use a nonzero value.
        duration: Event duration in seconds. 0 for an instantaneous event.
        mark_in_stream: Also punch the value into the acquisition stream's own
            marker channel, so the recording carries the event independently.
        payload: Any extra structured detail to keep with the event.

    Returns:
        The stored event record.
    """
    session = store.get(session_id)
    marked = False
    if mark_in_stream:
        try:
            marked = bool(session.source.insert_marker(float(value), label))
        except Exception:
            marked = False

    record = session.events.append(
        label=label, origin="manual", timestamp=now(), value=float(value),
        duration=float(duration), sample_index=session.raw_buffer.total_written,
        payload={**(payload or {}), "marked_in_stream": marked},
    )
    return to_jsonable({
        "session_id": session_id, "logged": True, "marked_in_stream": marked,
        "event": record.as_dict(),
    })


@mcp.tool()
def get_events(
    session_id: str,
    last_seconds: float | None = None,
    origin: str | None = None,
    label: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Read the session's event log.

    Everything lands here on one clock: board markers, annotations re-emitted by
    a replay, stimulation commands we dispatched, and manual notes. That is what
    makes a closed-loop run reconstructible afterwards without joining clocks.

    Args:
        session_id: The session to read.
        last_seconds: Only events from the last N seconds. None for all.
        origin: Filter by 'marker' (live board), 'annotation' (from a replayed
            file), 'stim' (dispatched by us), or 'manual' (logged by hand).
        label: Filter to one event label.
        limit: Maximum events to return, most recent first-truncated.

    Returns:
        Matching events in chronological order, plus counts by label.
    """
    session = store.get(session_id)
    since = (now() - last_seconds) if last_seconds else None
    events = session.events.query(
        since=since, origin=origin, label=label, limit=limit
    )
    return to_jsonable({
        "session_id": session_id,
        "n_returned": len(events),
        "n_total": len(session.events),
        "filters": {"last_seconds": last_seconds, "origin": origin, "label": label},
        "counts_by_label": session.events.counts_by_label(),
        "events": [e.as_dict() for e in events],
    })


@mcp.tool()
def get_epoch_around_event(
    session_id: str,
    label: str | None = None,
    tmin: float = -0.2,
    tmax: float = 0.8,
    filtered: bool = True,
    occurrence: int = -1,
    max_samples: int = 500,
) -> dict[str, Any]:
    """Extract the signal window around an event that already occurred.

    Online epoching: use it to inspect the response to the most recent stimulus,
    or to verify that a stimulation artifact lands where you expect. The event
    must still be inside the ring buffer -- for a default 60 s buffer, that means
    within the last minute.

    Args:
        session_id: The session to read from.
        label: Event label to find. None takes the most recent event of any kind.
        tmin: Window start relative to the event, in seconds (negative = before).
        tmax: Window end relative to the event, in seconds.
        filtered: Use the filtered buffer when a chain is set.
        occurrence: Which matching event; -1 is the most recent, -2 the one
            before it, 0 the earliest still in the log.
        max_samples: Decimation cap per channel.

    Returns:
        The epoch data, its channel names, and the event it is centred on.
    """
    session = store.get(session_id)
    if tmax <= tmin:
        raise ValueError(f"tmax must be greater than tmin, got {tmin} >= {tmax}")

    candidates = session.events.query(label=label, limit=0)
    candidates = [e for e in candidates if e.sample_index is not None]
    if not candidates:
        raise ValueError(
            f"No events{f' labelled {label!r}' if label else ''} with a sample "
            f"index in session '{session_id}'. Call get_events to see what exists."
        )
    try:
        event = candidates[occurrence]
    except IndexError:
        raise ValueError(
            f"occurrence={occurrence} is out of range; only {len(candidates)} "
            "matching events are in the log."
        ) from None

    sfreq = session.source.info.sfreq
    buffer = (
        session.filtered_buffer
        if (filtered and session.filtered_buffer is not None and len(session.chain))
        else session.raw_buffer
    )
    was_filtered = buffer is session.filtered_buffer

    start_abs = int(event.sample_index + round(tmin * sfreq))
    n_wanted = int(round((tmax - tmin) * sfreq))
    total = buffer.total_written
    oldest_abs = buffer.oldest_index

    if start_abs < oldest_abs:
        behind_sec = (oldest_abs - start_abs) / sfreq
        raise ValueError(
            f"That event is {behind_sec:.1f} s older than the "
            f"{buffer.capacity / sfreq:.0f} s "
            + ("filtered " if was_filtered else "")
            + "buffer holds. Increase EEG_MCP_BUFFER_SEC, request the epoch "
            "sooner, or pass filtered=false if the filter chain was set after "
            "the event occurred."
        )
    if start_abs + n_wanted > total:
        available_sec = (total - start_abs) / sfreq
        raise ValueError(
            f"The window extends {tmax:.2f} s past the event but only "
            f"{available_sec:.2f} s have been recorded since. Wait, or lower tmax."
        )

    data, timestamps = buffer.read_last(total - start_abs)
    data, timestamps = data[:, :n_wanted], timestamps[:n_wanted]
    step = max(1, int(np.ceil(data.shape[1] / max(max_samples, 1))))

    return to_jsonable({
        "session_id": session_id,
        "event": event.as_dict(),
        "channels": list(session.source.info.ch_names),
        "sfreq": sfreq,
        "tmin": tmin,
        "tmax": tmax,
        "n_samples": int(data.shape[1]),
        "filtered": was_filtered,
        "decimation": step,
        "units": "microvolts",
        "times": (np.arange(data.shape[1]) / sfreq + tmin)[::step],
        "data": data[:, ::step],
        "timestamps": timestamps[::step],
        "n_matching_events": len(candidates),
    })


@mcp.tool()
def clear_events(session_id: str) -> dict[str, Any]:
    """Empty the session's event log without stopping the stream.

    Useful between blocks of an experiment. The signal buffer is untouched.

    Args:
        session_id: The session whose log to clear.

    Returns:
        How many events were discarded.
    """
    session = store.get(session_id)
    count = len(session.events)
    session.events.clear()
    session.log(f"cleared {count} events")
    return to_jsonable({"session_id": session_id, "cleared": count})
