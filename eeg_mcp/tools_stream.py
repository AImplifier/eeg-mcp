"""MCP tools for live acquisition and reading from the buffer."""

from __future__ import annotations

from typing import Any

import numpy as np

from .app import mcp
from .monitor import monitors
from .session import store
from .sources.brainflow_source import BrainFlowSource, list_boards as _list_boards
from .utils import to_jsonable


@mcp.tool()
def list_boards() -> dict[str, Any]:
    """List every board BrainFlow can acquire from, with ids and sampling rates.

    Start here when you do not know what hardware is available. Two entries need
    no hardware at all: ``SYNTHETIC_BOARD`` generates a signal for development,
    and ``PLAYBACK_FILE_BOARD`` replays a BrainFlow CSV.

    Returns:
        Board names, numeric ids, sampling rates and EEG channel counts.
    """
    boards = _list_boards()
    return to_jsonable({
        "n_boards": len(boards),
        "boards": boards,
        "hint": "Use board='synthetic' to develop without hardware, or "
                "start_replay to play back an existing recording instead.",
    })


@mcp.tool()
def start_stream(
    session_id: str,
    board: str = "synthetic",
    params: dict[str, Any] | None = None,
    channels: list[int] | None = None,
) -> dict[str, Any]:
    """Open a board and begin buffering EEG in the background.

    Data flows into a ring buffer from a background thread as soon as this
    returns, so a subsequent read_window will have samples waiting. The session
    keeps running until stop_stream is called.

    Args:
        session_id: Name you will pass to every later call for this stream.
        board: Board name ('synthetic', 'cyton', 'muse_2', ...) or numeric id.
            Call list_boards to see the options.
        params: BrainFlow connection settings, e.g. {"serial_port": "COM3"} for a
            Cyton, {"mac_address": "..."} for a Muse, or
            {"file": "rec.csv", "master_board": "cyton"} for PLAYBACK_FILE_BOARD.
        channels: Optional subset of BrainFlow EEG row indices. Defaults to all.

    Returns:
        Session status including channel names, sampling rate, and buffer size.
    """
    source = BrainFlowSource(board=board, params=params, channels=channels)
    session = store.create(session_id, source)
    session.start()
    return to_jsonable({
        "started": True,
        "session_id": session_id,
        **session.status(),
        "next": "Call set_filters to add an online filter chain, then read_window "
                "or get_band_power. Allow ~1 s for the buffer to fill.",
    })


@mcp.tool()
def stop_stream(session_id: str) -> dict[str, Any]:
    """Stop a stream and release the board, serial ports, and LSL outlets.

    Always call this when finished. A board left prepared can block the next
    process from acquiring the device.

    Args:
        session_id: The stream to stop.

    Returns:
        Final status of the session before it was dropped.
    """
    session = store.get(session_id)
    final = session.status()
    # A monitor outliving its session would serve a frozen view and keep a port
    # bound, so tear it down with the stream it was watching.
    monitor_stopped = monitors.stop(session_id)
    store.drop(session_id)
    return to_jsonable({
        "stopped": True, "session_id": session_id,
        "monitor_stopped": monitor_stopped, "final_status": final,
    })


@mcp.tool()
def list_sessions() -> dict[str, Any]:
    """List all active stream and replay sessions with a one-line summary each.

    Returns:
        Session ids with their source kind, running state, and buffer fill.
    """
    sessions = []
    for session_id in store.list_ids():
        session = store.get(session_id)
        info = session.source.info
        sessions.append({
            "session_id": session_id,
            "kind": info.kind,
            "name": info.name,
            "running": session.is_running,
            "exhausted": session.exhausted,
            "sfreq": info.sfreq,
            "n_channels": info.n_channels,
            "buffered_sec": session.raw_buffer.n_available / info.sfreq,
            "n_events": len(session.events),
        })
    return to_jsonable({"n_sessions": len(sessions), "sessions": sessions})


@mcp.tool()
def stream_status(session_id: str) -> dict[str, Any]:
    """Health and timing report for a session.

    The fields that matter for closed-loop work:
    ``throughput_ratio`` below 1.0 means the producer is not keeping up and
    samples are being dropped; ``filters.group_delay_sec`` is how far behind
    real time a filtered feature is; ``last_error`` explains a stopped producer.

    Args:
        session_id: The session to report on.

    Returns:
        Source description, buffer fill, throughput, filter chain, event counts,
        attached stimulation backends, and replay position when applicable.
    """
    return to_jsonable(store.get(session_id).status())


@mcp.tool()
def read_window(
    session_id: str,
    seconds: float = 2.0,
    filtered: bool = True,
    channels: list[str] | None = None,
    max_samples: int = 1000,
    include_data: bool = True,
) -> dict[str, Any]:
    """Read the most recent samples from the buffer.

    Returns filtered signal when a filter chain is set (see set_filters),
    otherwise raw. Long windows are decimated to ``max_samples`` per channel so
    the response stays readable -- for numeric work prefer get_band_power or
    get_psd, which compute on the full-resolution data.

    Args:
        session_id: The session to read from.
        seconds: How much recent signal to return.
        filtered: Return the filtered buffer if a chain is set. False for raw.
        channels: Channel names to include. Defaults to all.
        max_samples: Decimation cap per channel. Set include_data=False to skip
            the samples entirely and get only summary statistics.
        include_data: Whether to return the sample arrays at all.

    Returns:
        Channel names, timestamps, per-channel statistics, and (optionally) the
        decimated samples in microvolts.
    """
    session = store.require_running(session_id)
    data, timestamps, was_filtered = session.snapshot(seconds, filtered=filtered)
    names = list(session.source.info.ch_names)

    if channels:
        missing = [c for c in channels if c not in names]
        if missing:
            raise ValueError(
                f"Unknown channels {missing}. Available: {', '.join(names)}"
            )
        idx = [names.index(c) for c in channels]
        data = data[idx, :]
        names = list(channels)

    n_samples = int(data.shape[1])
    step = max(1, int(np.ceil(n_samples / max(max_samples, 1))))

    out: dict[str, Any] = {
        "session_id": session_id,
        "channels": names,
        "sfreq": session.source.info.sfreq,
        "n_samples": n_samples,
        "window_sec": n_samples / session.source.info.sfreq,
        "filtered": was_filtered,
        "filter_requested": filtered,
        "decimation": step,
        "units": "microvolts",
        "t_start": float(timestamps[0]) if timestamps.size else None,
        "t_end": float(timestamps[-1]) if timestamps.size else None,
        "stats": {
            name: {
                "mean": float(np.mean(data[i])),
                "std": float(np.std(data[i])),
                "min": float(np.min(data[i])),
                "max": float(np.max(data[i])),
            }
            for i, name in enumerate(names)
        },
    }
    if include_data:
        out["data"] = data[:, ::step]
        out["timestamps"] = timestamps[::step]
    if not was_filtered and filtered:
        out["note"] = ("No filter chain is set, so this is raw signal. "
                       "Call set_filters to filter the stream continuously.")
    return to_jsonable(out)


@mcp.tool()
def get_channels(session_id: str) -> dict[str, Any]:
    """List the channels a session is producing, with their buffer index.

    Args:
        session_id: The session to inspect.

    Returns:
        Channel names in order, sampling rate, and source metadata.
    """
    info = store.get(session_id).source.info
    return to_jsonable({
        "session_id": session_id,
        "n_channels": info.n_channels,
        "channels": [{"index": i, "name": n} for i, n in enumerate(info.ch_names)],
        "sfreq": info.sfreq,
        "source": info.as_dict(),
    })
