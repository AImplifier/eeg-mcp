"""MCP tools for looking at the signal: live monitor and standalone HTML views.

The HTML builders live in :mod:`eeg_mcp.reports` because the live monitor's
Save buttons produce the same files; these tools are a thin MCP wrapper.
"""

from __future__ import annotations

from typing import Any

from .app import mcp
from .monitor import monitors
from .reports import build_report, build_spectrum, build_traces
from .session import store
from .utils import to_jsonable


@mcp.tool()
def start_monitor(
    session_id: str,
    window_sec: float = 5.0,
    refresh_ms: int = 500,
    port: int = 0,
    allow_control: bool = True,
) -> dict[str, Any]:
    """Open a live browser view of a running session and return its URL.

    Serves a rolling traces view with event markers, band power and per-channel
    quality, refreshing continuously. This is the tool for "let me watch the
    recording" -- a static plot cannot answer whether the signal is good *now*.

    With ``allow_control`` (the default) the page also carries transport
    buttons: play/pause, restart, skip, speed, stop, mark an event, and save a
    report. Pass ``allow_control=false`` for a strictly read-only view, e.g.
    when showing the screen to someone who should not be able to alter the run.

    The page is bound to 127.0.0.1 only and requires a random per-monitor token,
    so it is not reachable from the network. It is still unencrypted: do not
    expose it through a tunnel or reverse proxy.

    Args:
        session_id: The running session to display.
        window_sec: Seconds of signal shown at once. Adjustable in the page too.
        refresh_ms: Redraw interval. 500 ms is smooth without loading the server.
        port: TCP port on loopback. 0 lets the OS choose a free one.
        allow_control: Enable the transport buttons and the control endpoint.

    Returns:
        The URL to open, including the access token.
    """
    session = store.get(session_id)
    monitor = monitors.start(session_id, store, port=port, window_sec=window_sec,
                             refresh_ms=refresh_ms, allow_control=allow_control)
    session.log(
        f"live monitor started on port {monitor.port}"
        f"{' (read-only)' if not allow_control else ''}"
    )
    return to_jsonable({
        "session_id": session_id,
        "url": monitor.url,
        "port": monitor.port,
        "window_sec": window_sec,
        "refresh_ms": refresh_ms,
        "allow_control": allow_control,
        "bound_to": "127.0.0.1",
        "controls": (
            ["play/pause", "restart", "skip -10s/+10s", "speed", "stop",
             "mark event", "save report", "save traces", "save spectrum"]
            if allow_control else []
        ),
        "note": "Open this URL in a browser. The token in the URL is required; "
                "the page is loopback-only and unencrypted.",
    })


@mcp.tool()
def stop_monitor(session_id: str) -> dict[str, Any]:
    """Close a session's live monitor and free its port.

    Args:
        session_id: The session whose monitor to stop.

    Returns:
        Whether a monitor was running.
    """
    stopped = monitors.stop(session_id)
    return to_jsonable({"session_id": session_id, "stopped": stopped})


@mcp.tool()
def list_monitors() -> dict[str, Any]:
    """List live monitors currently serving, with their URLs.

    Returns:
        Active monitors and the URL for each.
    """
    active = monitors.list_active()
    return to_jsonable({"n_active": len(active), "monitors": active})


@mcp.tool()
def plot_stream(
    session_id: str,
    seconds: float = 10.0,
    channels: list[str] | None = None,
    filtered: bool = True,
    show_events: bool = True,
) -> dict[str, Any]:
    """Render the recent signal as a standalone HTML file and return its path.

    A frozen snapshot, unlike start_monitor -- use it to keep a record, attach to
    a note, or share. The file inlines everything and needs no internet.

    Args:
        session_id: The session to plot.
        seconds: How much recent signal to draw.
        channels: Channel names to include. Defaults to all.
        filtered: Draw the filtered signal when a chain is set.
        show_events: Overlay event markers on the traces.

    Returns:
        Path to the written HTML file and what it contains.
    """
    session = store.require_running(session_id)
    _path, summary = build_traces(
        session, seconds=seconds, channels=channels, filtered=filtered,
        show_events=show_events,
    )
    return to_jsonable({
        "session_id": session_id, **summary,
        "note": "Open this file in a browser. It is fully self-contained.",
    })


@mcp.tool()
def plot_spectrum(
    session_id: str,
    seconds: float = 8.0,
    channels: list[str] | None = None,
    filtered: bool = True,
    fmax: float = 60.0,
) -> dict[str, Any]:
    """Render the power spectrum and band composition as a standalone HTML file.

    Args:
        session_id: The session to analyse.
        seconds: Window length. Longer gives finer frequency resolution.
        channels: Channel names to include. Defaults to all.
        filtered: Use the filtered signal when a chain is set.
        fmax: Highest frequency to draw.

    Returns:
        Path to the written HTML file, plus the peak frequency per channel.
    """
    session = store.require_running(session_id)
    _path, summary = build_spectrum(
        session, seconds=seconds, channels=channels, filtered=filtered, fmax=fmax,
    )
    return to_jsonable({"session_id": session_id, **summary})


@mcp.tool()
def export_report(
    session_id: str,
    seconds: float = 10.0,
    include_spectrum: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    """Write a full review page: traces, spectrum, quality, and the event log.

    The one artefact to keep from a session. Everything is inlined, so it opens
    on any machine, needs no internet, and still renders years from now.

    Args:
        session_id: The session to report on.
        seconds: Window of signal to include in the traces and spectrum.
        include_spectrum: Add the PSD and band-power sections.
        notes: Free text to record at the top -- indication, montage, observations.

    Returns:
        Path to the written HTML file and a summary of what it found.
    """
    session = store.get(session_id)
    _path, summary = build_report(
        session, seconds=seconds, include_spectrum=include_spectrum, notes=notes,
    )
    return to_jsonable({"session_id": session_id, **summary})
