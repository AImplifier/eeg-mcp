"""MCP tools for attaching custom real-time processors to a session."""

from __future__ import annotations

from typing import Any

from .app import mcp
from .config import get_config
from .processing import available, create, load_errors
from .processing.registry import ENTRY_POINT_GROUP
from .session import store
from .utils import to_jsonable


@mcp.tool()
def list_processors() -> dict[str, Any]:
    """List real-time processors available to attach, and where they came from.

    Built-ins are always present. Your own appear here once the **server**
    environment points at them -- via the ``eeg_mcp.processors`` entry point,
    ``EEG_MCP_PLUGINS``, or ``EEG_MCP_PLUGIN_DIR``. They cannot be loaded from a
    tool call, because importing a module executes it and that would make
    arbitrary code execution reachable from a model.

    Returns:
        Available processors with their parameters, plus any plugin that failed
        to load, so a typo is visible instead of silently missing.
    """
    processors = available()
    errors = load_errors()
    return to_jsonable({
        "n_available": len(processors),
        "processors": processors,
        "load_errors": errors,
        "entry_point_group": ENTRY_POINT_GROUP,
        "how_to_add": {
            "packaged": f"Declare an '{ENTRY_POINT_GROUP}' entry point and pip install it.",
            "env": "EEG_MCP_PLUGINS='mypkg.processors:MyProcessor'",
            "directory": "EEG_MCP_PLUGIN_DIR=/path/to/plugins (one .py per file)",
        },
        "docs": "See the Extending guide for a worked example.",
    })


@mcp.tool()
def attach_processor(
    session_id: str,
    processor: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach a processor to a session.

    A ``streaming`` processor runs inside the acquisition loop on every chunk,
    so it reacts within one poll interval without an agent round-trip -- that is
    how closed-loop logic gets fast. The cost is that slow code stalls
    acquisition; ``processor_status`` reports each one's mean and worst-case
    time so you can see whether yours is affordable.

    Non-streaming processors do nothing until you call ``run_processor``.

    Args:
        session_id: The session to attach to.
        processor: Name from list_processors.
        params: Processor-specific settings; see its ``params`` in the listing.

    Returns:
        The attached processor's configuration and execution mode.
    """
    session = store.get(session_id)
    instance = create(processor, params)
    session.attach_processor(instance)
    return to_jsonable({
        "session_id": session_id,
        "attached": instance.name,
        "processor": instance.describe(),
        "next": (
            "It is running now; read results with get_processor_output."
            if instance.streaming else
            "Call run_processor to execute it on a window."
        ),
    })


@mcp.tool()
def detach_processor(session_id: str, processor: str) -> dict[str, Any]:
    """Remove a processor from a session and release its state.

    Args:
        session_id: The session to detach from.
        processor: The attached processor's name.

    Returns:
        Confirmation and the processors still attached.
    """
    session = store.get(session_id)
    if not session.detach_processor(processor):
        raise KeyError(
            f"No processor '{processor}' on session '{session_id}'. "
            f"Attached: {sorted(session.processors) or 'none'}"
        )
    return to_jsonable({
        "session_id": session_id, "detached": processor,
        "remaining": sorted(session.processors),
    })


@mcp.tool()
def processor_status(session_id: str) -> dict[str, Any]:
    """Report attached processors, their health, and their cost.

    ``mean_ms`` and ``max_ms`` are the numbers to watch for a streaming
    processor: if ``max_ms`` approaches the poll interval (50 ms by default) it
    is starving acquisition, which shows up as ``throughput_ratio`` below 1.0 in
    stream_status. A processor that raises 10 times is disabled automatically
    rather than being retried forever inside the producer loop.

    Args:
        session_id: The session to report on.

    Returns:
        Per-processor configuration, call counts, errors, and timing.
    """
    session = store.get(session_id)
    return to_jsonable({
        "session_id": session_id,
        "n_attached": len(session.processors),
        "processors": {
            name: {
                **processor.status(),
                "n_outputs": len(session.processor_outputs.get(name, ())),
            }
            for name, processor in session.processors.items()
        },
        "poll_interval_ms": 1000.0 * get_config().poll_interval_sec,
    })


@mcp.tool()
def get_processor_output(
    session_id: str, processor: str, limit: int = 20
) -> dict[str, Any]:
    """Read a streaming processor's recent outputs.

    Args:
        session_id: The session to read from.
        processor: The attached processor's name.
        limit: How many recent outputs to return, newest last.

    Returns:
        The recorded outputs with their timestamps, plus the latest one.
    """
    session = store.get(session_id)
    if processor not in session.processors:
        raise KeyError(
            f"No processor '{processor}' on session '{session_id}'. "
            f"Attached: {sorted(session.processors) or 'none'}"
        )
    history = list(session.processor_outputs.get(processor, ()))
    recent = history[-max(1, limit):]
    return to_jsonable({
        "session_id": session_id,
        "processor": processor,
        "n_total": len(history),
        "n_returned": len(recent),
        "latest": recent[-1].as_dict() if recent else None,
        "outputs": [o.as_dict() for o in recent],
    })


@mcp.tool()
def run_processor(
    session_id: str,
    processor: str,
    seconds: float = 2.0,
    filtered: bool | None = None,
) -> dict[str, Any]:
    """Run a processor once on the most recent window, on demand.

    Works for both modes: for a non-streaming processor this is the only way to
    execute it, and for a streaming one it is a way to get an answer right now
    rather than waiting for its next scheduled output.

    Args:
        session_id: The session to read from.
        processor: The attached processor's name.
        seconds: Window length to pass in.
        filtered: Override the processor's declared preference for filtered or
            raw signal.

    Returns:
        Whatever the processor returned for this window.
    """
    session = store.require_running(session_id)
    instance = session.processors.get(processor)
    if instance is None:
        raise KeyError(
            f"No processor '{processor}' on session '{session_id}'. "
            f"Attach it first with attach_processor."
        )
    want_filtered = (
        instance.uses == "filtered" if filtered is None else bool(filtered)
    )
    data, timestamps, was_filtered = session.snapshot(seconds, filtered=want_filtered)
    context = session.make_processor_context()
    result = instance._invoke(data, timestamps, context)
    return to_jsonable({
        "session_id": session_id,
        "processor": processor,
        "filtered": was_filtered,
        "window_sec": float(data.shape[1] / session.source.info.sfreq),
        "result": result,
        "note": (None if result is not None else
                 "The processor returned nothing for this window. Streaming "
                 "processors often need several chunks before their internal "
                 "window fills."),
    })


@mcp.tool()
def reset_processors(session_id: str) -> dict[str, Any]:
    """Clear accumulated processor state and outputs, without detaching them.

    Useful between experimental blocks, and after a replay seek, so a rolling
    window does not straddle the discontinuity.

    Args:
        session_id: The session whose processors to reset.

    Returns:
        Which processors were reset.
    """
    session = store.get(session_id)
    session.reset_processors()
    return to_jsonable({
        "session_id": session_id, "reset": sorted(session.processors),
    })


@mcp.tool()
def get_tokens(
    session_id: str, processor: str = "band_tokenizer", limit: int = 32
) -> dict[str, Any]:
    """Read discrete tokens from a tokenizing processor.

    For feeding EEG into sequence models. Returns both the grouped tokens (one
    group per window) and a flat sequence, plus the vocabulary size and the
    channel/band layout each position corresponds to.

    Works with any attached processor exposing ``token_history`` -- the built-in
    ``band_tokenizer`` is a starting point, and your own tokenizer plugs in the
    same way.

    Args:
        session_id: The session to read from.
        processor: The tokenizing processor's name.
        limit: How many recent token groups to return.

    Returns:
        Token groups, a flattened sequence, the vocabulary size, and the layout.
    """
    session = store.get(session_id)
    instance = session.processors.get(processor)
    if instance is None:
        raise KeyError(
            f"No processor '{processor}' on session '{session_id}'. "
            f"Attached: {sorted(session.processors) or 'none'}"
        )
    if not hasattr(instance, "token_history"):
        raise ValueError(
            f"Processor '{processor}' does not produce tokens. A tokenizer must "
            "expose a token_history(limit) method; see the Extending guide."
        )
    return to_jsonable({
        "session_id": session_id, "processor": processor,
        **instance.token_history(limit=limit),
    })
