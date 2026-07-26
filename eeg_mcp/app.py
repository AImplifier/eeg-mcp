"""The shared FastMCP instance.

Isolated in its own module so the ``tools_*`` modules can import ``mcp``
without importing each other -- registration is by import side-effect in
:mod:`eeg_mcp.server`, and a cycle here would break the whole server.

The ``instructions`` string is load-bearing: it is the first thing an agent
reads, and it encodes the call ordering that the tools' error messages assume.
"""

from __future__ import annotations

import mne
from fastmcp import FastMCP

# MNE chatters on stdout/stderr, which corrupts a stdio MCP transport.
mne.set_log_level("ERROR")

mcp = FastMCP(
    "eeg-realtime",
    instructions=(
        "Real-time EEG over MCP: stream from hardware, replay recordings against "
        "a wall clock, and dispatch stimulation events.\n"
        "\n"
        "ACQUIRE (pick one; both produce a session you query the same way):\n"
        "  start_stream   -> live BrainFlow board. Use board='synthetic' to work "
        "without hardware.\n"
        "  start_replay   -> an existing recording (EDF/BDF/GDF/SET/FIF via MNE, or "
        "BrainFlow CSV) played at true recording rate, re-emitting its annotations "
        "as events. Develop and test a pipeline here before touching a subject.\n"
        "\n"
        "THEN, on that session_id:\n"
        "  set_filters            -> stateful online filter chain (do this before "
        "reading features; it filters continuously in the background, not per query)\n"
        "  read_window            -> raw or filtered samples from the ring buffer\n"
        "  get_band_power / get_psd / check_signal_quality -> windowed analytics\n"
        "  get_events / log_event -> the unified event log (markers, annotations, "
        "and stimulations all land here on one clock)\n"
        "  stream_status          -> health, throughput, buffer fill, filter delay\n"
        "\n"
        "STIMULATE:\n"
        "  list_stim_backends -> what targets exist and which are hardware\n"
        "  attach_stim_backend -> bind one to the session ('log' is a safe dry run)\n"
        "  arm_stim           -> REQUIRED for hardware (TMS/tES/serial); expires\n"
        "  send_stim_event    -> deliver one command; it is logged as an event\n"
        "\n"
        "TIMING: filters are causal, so features lag by the chain's group delay "
        "(reported by stream_status). Add the poll interval and dispatch latency "
        "when budgeting a closed loop.\n"
        "\n"
        "SAFETY: hardware stimulation is disabled unless the server was started "
        "with EEG_MCP_ALLOW_HARDWARE_STIM=1, must be armed, and is clamped to "
        "configured intensity/duration/interval ceilings. Do not attempt to work "
        "around these; ask the operator to configure the server."
    ),
)
