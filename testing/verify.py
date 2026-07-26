"""End-to-end verification driving the server through FastMCP's in-memory client.

Run:  python testing/verify.py

Exercises the three capabilities against real code paths -- synthetic board
streaming, replay of an actual EDF with annotations, and stimulation dispatch --
plus the numeric correctness check that matters most: that chunked stateful
filtering equals whole-signal filtering.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Point the server at a scratch home before importing it, so nothing touches
# the user's real config.
_TMP = tempfile.mkdtemp(prefix="eegmcp_verify_")
os.environ["EEG_MCP_HOME"] = _TMP
os.environ["EEG_MCP_BUFFER_SEC"] = "30"

import numpy as np  # noqa: E402
from fastmcp import Client  # noqa: E402

from eeg_mcp.server import mcp  # noqa: E402

PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def skip(name: str, why: str) -> None:
    print(f"  SKIP  {name}  ({why})")


async def call(client: Client, tool: str, **args):
    result = await client.call_tool(tool, args)
    return result.data if result.data is not None else result.structured_content


def find_edf() -> Path | None:
    """A real recording with annotations, if one is on this machine."""
    candidates = [
        Path.home() / "mne_data/MNE-eegbci-data/files/eegmmidb/1.0.0/S001/S001R03.edf",
        Path.home() / "Documents/MATLAB/eeglab2024.0/sample_data/test_data/test.edf",
    ]
    return next((p for p in candidates if p.exists()), None)


# --- numeric core (no MCP involved) --------------------------------------


def verify_filter_state() -> None:
    """The claim the whole streaming design rests on: chunking must not matter."""
    print("\n[dsp] stateful filter chain")
    from eeg_mcp.dsp import FilterChain, FilterSpec

    rng = np.random.default_rng(0)
    sfreq, n = 250.0, 2500
    signal_in = rng.standard_normal((4, n))

    whole = FilterChain(4, sfreq)
    whole.add(FilterSpec("bandpass", low=1.0, high=40.0, order=4))
    expected = whole.process(signal_in)

    chunked = FilterChain(4, sfreq)
    chunked.add(FilterSpec("bandpass", low=1.0, high=40.0, order=4))
    pieces = [chunked.process(signal_in[:, i:i + 37]) for i in range(0, n, 37)]
    actual = np.concatenate(pieces, axis=1)

    err = float(np.max(np.abs(expected - actual)))
    check("chunked filtering == whole-signal filtering", err < 1e-9, f"max err {err:.2e}")

    stateless = np.concatenate(
        [
            FilterChain(4, sfreq).add(FilterSpec("bandpass", low=1.0, high=40.0)).process(
                signal_in[:, i:i + 37]
            )
            for i in range(0, n, 37)
        ],
        axis=1,
    )
    naive_err = float(np.max(np.abs(expected - stateless)))
    check(
        "stateless per-chunk filtering is measurably wrong (control)",
        naive_err > 1e-3,
        f"max err {naive_err:.2e} -- expected a large error",
    )

    chain = FilterChain(4, sfreq)
    chain.add(FilterSpec("bandpass", low=8.0, high=12.0, order=4))
    delay = chain.group_delay_sec()
    check("group delay is reported and positive", 0.0 < delay < 1.0, f"{delay:.4f}s")

    try:
        FilterChain(4, 250.0).add(FilterSpec("lowpass", high=200.0))
        check("filter above Nyquist is rejected", False, "no error raised")
    except ValueError:
        check("filter above Nyquist is rejected", True)


def verify_ring_buffer() -> None:
    print("\n[ring] circular buffer")
    from eeg_mcp.ring import RingBuffer

    buf = RingBuffer(2, 100)
    buf.write(np.arange(20, dtype=float).reshape(2, 10), np.arange(10, dtype=float))
    data, _ = buf.read_last(10)
    check("reads back what was written", np.allclose(data[0], np.arange(10)))

    for _ in range(30):
        buf.write(np.ones((2, 10)), np.zeros(10))
    check("caps at capacity after wrap", buf.n_available == 100, f"{buf.n_available}")
    check("total_written keeps counting", buf.total_written == 310, f"{buf.total_written}")

    buf2 = RingBuffer(2, 10)
    buf2.write(np.arange(40, dtype=float).reshape(2, 20), np.arange(20, dtype=float))
    data, _ = buf2.read_last(10)
    check("oversized chunk keeps the newest tail", np.allclose(data[0], np.arange(10, 20)))


# --- MCP surface ----------------------------------------------------------


async def verify_streaming(client: Client) -> None:
    print("\n[stream] synthetic board")
    boards = await call(client, "list_boards")
    check("list_boards returns boards", boards["n_boards"] > 10, str(boards["n_boards"]))

    started = await call(client, "start_stream", session_id="s1", board="synthetic")
    check("start_stream succeeds", started["started"] is True)
    check("channels discovered", started["source"]["n_channels"] > 0)
    sfreq = started["source"]["sfreq"]

    time.sleep(2.0)
    status = await call(client, "stream_status", session_id="s1")
    check("buffer filled", status["buffered_samples"] > sfreq, str(status["buffered_samples"]))
    check("producer healthy", status["last_error"] is None, str(status["last_error"]))
    ratio = status["throughput_ratio"]
    check("throughput keeps up", ratio is not None and ratio > 0.8, f"ratio={ratio}")

    filters = await call(client, "set_filters", session_id="s1",
                         bandpass_low=1.0, bandpass_high=40.0, notch_freq=50.0)
    check("filter chain installed", filters["filters"]["n_stages"] == 2)
    check("group delay reported", filters["filters"]["group_delay_sec"] > 0)

    time.sleep(1.5)
    window = await call(client, "read_window", session_id="s1", seconds=1.0)
    check("read_window returns filtered data", window["filtered"] is True)
    check("read_window has samples", window["n_samples"] > 0, str(window["n_samples"]))

    raw = await call(client, "read_window", session_id="s1", seconds=1.0, filtered=False)
    check("raw and filtered buffers differ",
          raw["stats"] != window["stats"])

    power = await call(client, "get_band_power", session_id="s1", seconds=2.0)
    check("band power computed", "alpha" in power["mean_across_channels"])
    check("relative powers sum to ~1",
          abs(sum(v for k, v in power["mean_across_channels"].items()
                  if k.endswith("_rel")) - 1.0) < 0.01)
    check("psd backend recorded", power["psd_backend"] in ("brainflow", "scipy"),
          power["psd_backend"])

    psd = await call(client, "get_psd", session_id="s1", seconds=2.0)
    check("psd returns peak frequencies", len(psd["peak_freq"]) > 0)

    quality = await call(client, "check_signal_quality", session_id="s1")
    check("quality check runs", "channels" in quality and quality["verdict"] in
          ("ok", "attention"))

    # BrainFlow's synthetic board has a known 10 Hz component on some channels;
    # assert only that alpha is a plausible fraction, not that it dominates.
    alpha_rel = power["mean_across_channels"].get("alpha_rel", 0)
    check("alpha relative power in range", 0.0 <= alpha_rel <= 1.0, f"{alpha_rel}")


async def verify_events_and_stim(client: Client) -> None:
    print("\n[events+stim] marking and dispatch")
    logged = await call(client, "log_event", session_id="s1", label="trial_start", value=7)
    check("manual event logged", logged["logged"] is True)
    check("marker written into board stream", logged["marked_in_stream"] is True,
          "synthetic board should accept markers")

    attached = await call(client, "attach_stim_backend", session_id="s1", backend="log")
    check("log backend attaches", attached["attached"] == "log")

    sent = await call(client, "send_stim_event", session_id="s1",
                      label="cue_left", value=3, kind="marker")
    check("stim dispatched", sent["backend"] == "log")
    check("stim latency recorded", sent["dispatch_latency_ms"] >= 0)
    check("stim produced an event record", sent["event"]["origin"] == "stim")

    train = await call(client, "send_stim_train", session_id="s1", label="burst",
                       n_pulses=5, interval_ms=50)
    check("train delivered all pulses", train["n_delivered"] == 5)
    realised = train["realised_interval_ms"]["mean"]
    check("train timing near requested", realised is not None and 40 < realised < 75,
          f"{realised} ms")

    events = await call(client, "get_events", session_id="s1")
    origins = {e["origin"] for e in events["events"]}
    check("event log mixes origins", {"manual", "stim"} <= origins, str(origins))

    marker_events = await call(client, "get_events", session_id="s1", origin="marker")
    check("board markers came back on the marker channel",
          marker_events["n_returned"] > 0, str(marker_events["n_returned"]))

    time.sleep(1.0)
    epoch = await call(client, "get_epoch_around_event", session_id="s1",
                       label="cue_left", tmin=-0.2, tmax=0.5)
    check("epoch extracted around event", epoch["n_samples"] > 0)
    check("epoch centred on the right event", epoch["event"]["label"] == "cue_left")

    # Safety: hardware must be refused while EEG_MCP_ALLOW_HARDWARE_STIM is unset.
    try:
        await call(client, "attach_stim_backend", session_id="s1", backend="tms",
                   options={"port": "COM99", "templates": {"fire": "TRIG\r\n"}})
        check("hardware stim refused when disabled", False, "attach unexpectedly succeeded")
    except Exception as exc:
        check("hardware stim refused when disabled",
              "EEG_MCP_ALLOW_HARDWARE_STIM" in str(exc), str(exc)[:120])

    backends = await call(client, "list_stim_backends")
    hardware = [b for b in backends["backends"] if b["is_hardware"]]
    check("hardware backends listed but unavailable",
          len(hardware) >= 3 and all(not b["available"] for b in hardware))

    status = await call(client, "stim_status", session_id="s1")
    check("stim status counts dispatches", status["n_stim_events"] >= 6,
          str(status["n_stim_events"]))

    stopped = await call(client, "stop_stream", session_id="s1")
    check("stream stops cleanly", stopped["stopped"] is True)


async def verify_replay(client: Client) -> None:
    print("\n[replay] wall-clock resimulation")
    edf = find_edf()
    if edf is None:
        skip("replay suite", "no EDF found on this machine")
        return
    print(f"  using {edf}")

    info = await call(client, "inspect_recording", path=str(edf))
    check("recording inspected", info["n_channels"] > 0 and info["duration_sec"] > 0,
          f"{info['n_channels']}ch {info['duration_sec']:.1f}s")
    n_file_events = info["n_events_in_file"]
    check("annotations found in file", n_file_events > 0, str(n_file_events))

    started = await call(client, "start_replay", session_id="r1", path=str(edf),
                         speed=8.0)
    check("replay starts", started["started"] is True)
    check("replay reports position", "replay" in started)

    time.sleep(3.0)
    status = await call(client, "stream_status", session_id="r1")
    pos = status["replay"]
    check("replay clock advanced", pos["position_sec"] > 1.0, f"{pos['position_sec']:.2f}s")
    check("replay respects speed",
          pos["position_sec"] > 8.0, f"{pos['position_sec']:.2f}s at speed 8")
    check("replay buffered samples", status["buffered_samples"] > 0)

    events = await call(client, "get_events", session_id="r1", origin="annotation")
    check("annotations re-emitted as events during replay",
          events["n_returned"] > 0, str(events["n_returned"]))

    power = await call(client, "get_band_power", session_id="r1", seconds=1.0)
    check("band power works on replayed data", "alpha" in power["mean_across_channels"])

    paused = await call(client, "replay_control", session_id="r1", action="pause")
    at_pause = paused["replay"]["position_sec"]
    time.sleep(0.7)
    still = await call(client, "replay_control", session_id="r1", action="status")
    check("pause holds the clock",
          abs(still["replay"]["position_sec"] - at_pause) < 0.5,
          f"{at_pause:.2f} -> {still['replay']['position_sec']:.2f}")

    await call(client, "replay_control", session_id="r1", action="resume")
    sought = await call(client, "replay_control", session_id="r1", action="seek",
                        position_sec=5.0)
    check("seek repositions", abs(sought["replay"]["position_sec"] - 5.0) < 0.2,
          f"{sought['replay']['position_sec']:.2f}")

    file_events = await call(client, "list_recording_events", session_id="r1")
    check("full file event list available",
          file_events["n_events_in_file"] == n_file_events)

    stopped = await call(client, "stop_stream", session_id="r1")
    check("replay stops cleanly", stopped["stopped"] is True)


async def verify_errors(client: Client) -> None:
    print("\n[errors] actionable failure messages")
    for tool, args, needle in [
        ("stream_status", {"session_id": "nope"}, "No session"),
        ("start_stream", {"session_id": "x", "board": "not_a_board"}, "Unknown board"),
    ]:
        try:
            await call(client, tool, **args)
            check(f"{tool} rejects bad input", False, "no error raised")
        except Exception as exc:
            check(f"{tool} rejects bad input", needle in str(exc), str(exc)[:100])


async def main() -> int:
    verify_filter_state()
    verify_ring_buffer()
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print(f"\n[mcp] {len(tools)} tools registered")
        check("all tool modules registered", len(tools) >= 20, str(len(tools)))
        await verify_streaming(client)
        await verify_events_and_stim(client)
        await verify_replay(client)
        await verify_errors(client)

    print(f"\n{'=' * 52}\n  {PASSED} passed, {FAILED} failed\n{'=' * 52}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
