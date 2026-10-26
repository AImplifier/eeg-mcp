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


async def verify_lsl(client: Client) -> None:
    print("\n[lsl] attaching to an existing outlet")
    try:
        import pylsl
    except ImportError:
        skip("lsl suite", "pylsl not installed (pip install eeg-mcp[lsl])")
        return

    import threading

    n_ch, sfreq = 4, 256.0
    stop_event = threading.Event()

    def publish() -> None:
        info = pylsl.StreamInfo("verify-lsl", "EEG", n_ch, sfreq, "float32", "verify1")
        chans = info.desc().append_child("channels")
        for name in ("TP9", "AF7", "AF8", "TP10"):
            chans.append_child("channel").append_child_value("label", name)
        outlet = pylsl.StreamOutlet(info)
        t = 0
        while not stop_event.is_set():
            outlet.push_sample([float(t % n_ch)] * n_ch)
            t += 1
            time.sleep(1.0 / sfreq)

    thread = threading.Thread(target=publish, daemon=True)
    thread.start()
    try:
        time.sleep(1.0)  # let the outlet become resolvable
        started = await call(client, "start_stream", session_id="lsl1", board="lsl",
                             params={"name": "verify-lsl"})
        check("lsl stream starts", started["started"] is True)
        check("lsl source reports its kind", started["source"]["kind"] == "lsl",
              started["source"]["kind"])
        check("lsl channel labels read from the outlet",
              started["source"]["ch_names"] == ["TP9", "AF7", "AF8", "TP10"],
              str(started["source"]["ch_names"]))

        time.sleep(1.5)
        window = await call(client, "read_window", session_id="lsl1", seconds=1.0)
        check("lsl samples reach the ring buffer", window["n_samples"] > 0,
              str(window["n_samples"]))
        check("lsl timestamps are Unix epoch, not the LSL clock",
              abs(window["t_end"] - time.time()) < 5.0,
              f"t_end={window['t_end']} now={time.time()}")

        stopped = await call(client, "stop_stream", session_id="lsl1")
        check("lsl stream stops cleanly", stopped["stopped"] is True)
    finally:
        stop_event.set()
        thread.join(timeout=2.0)

    try:
        await call(client, "start_stream", session_id="lsl2", board="lsl",
                   params={"name": "no-such-outlet", "resolve_timeout": 0.5})
        check("lsl rejects a missing outlet", False, "no error raised")
    except Exception as exc:
        check("lsl rejects a missing outlet", "No LSL stream found" in str(exc),
              str(exc)[:100])


async def verify_lsl_reconnect(client: Client) -> None:
    """A dropped BLE bridge is the normal case for this source, not an edge
    case -- streaming a real Muse S Athena's EEG+ACC+GYRO+OPTICS together was
    observed to disconnect within one to three minutes on Windows. This
    reproduces that shape without hardware: kill the outlet mid-stream,
    confirm the session survives disconnected (no last_error, no crash), then
    republish under the same source_id and confirm it reattaches on its own
    and logs the gap -- rather than silently sitting on stale data forever."""
    print("\n[lsl] reconnect after the outlet drops mid-stream")
    try:
        import pylsl
    except ImportError:
        skip("lsl reconnect suite", "pylsl not installed (pip install eeg-mcp[lsl])")
        return

    import gc
    import threading

    n_ch, sfreq, source_id = 4, 256.0, "verify-reconnect-device"

    def publish(outlet_name: str, stop_event: threading.Event) -> None:
        info = pylsl.StreamInfo(outlet_name, "EEG", n_ch, sfreq, "float32", source_id)
        outlet = pylsl.StreamOutlet(info)
        t = 0
        while not stop_event.is_set():
            outlet.push_sample([float(t % n_ch)] * n_ch)
            t += 1
            time.sleep(1.0 / sfreq)
        del outlet  # let pylsl retract the stream so a fresh one can take its place

    stop_a = threading.Event()
    thread_a = threading.Thread(target=publish, args=("verify-lsl-rc", stop_a),
                                daemon=True)
    thread_a.start()
    thread_b: threading.Thread | None = None
    try:
        time.sleep(1.0)
        started = await call(
            client, "start_stream", session_id="lsl_rc", board="lsl",
            params={"name": "verify-lsl-rc", "stale_after_sec": 1.0},
        )
        check("reconnect: stream starts", started["started"] is True)

        time.sleep(1.5)
        window = await call(client, "read_window", session_id="lsl_rc", seconds=1.0)
        check("reconnect: samples flow before the drop", window["n_samples"] > 0,
              str(window["n_samples"]))

        # Kill the outlet -- simulates the BLE bridge dying, same as we saw
        # against real hardware.
        stop_a.set()
        thread_a.join(timeout=2.0)
        gc.collect()

        time.sleep(3.0)  # past stale_after_sec (1s) + a reconnect attempt cycle
        status = await call(client, "stream_status", session_id="lsl_rc")
        check("reconnect: session survives disconnected, not crashed",
              status["running"] is True and status["last_error"] is None,
              str(status["last_error"]))
        check("reconnect: connection reports disconnected",
              status["connection"]["connected"] is False, str(status["connection"]))

        # Republish under the *same* source_id -- the bridge tool restarting.
        stop_b = threading.Event()
        thread_b = threading.Thread(target=publish, args=("verify-lsl-rc", stop_b),
                                    daemon=True)
        thread_b.start()
        time.sleep(2.5)  # resolve (<=1s) + open + a couple of poll cycles

        status = await call(client, "stream_status", session_id="lsl_rc")
        check("reconnect: connection re-established",
              status["connection"]["connected"] is True, str(status["connection"]))
        check("reconnect: exactly one reconnect counted",
              status["connection"]["reconnect_count"] == 1,
              str(status["connection"]))

        events = await call(client, "get_events", session_id="lsl_rc",
                            origin="annotation")
        reconnects = [e for e in events["events"] if e["label"] == "source_reconnected"]
        check("reconnect: gap logged as an event, not left silent",
              len(reconnects) == 1, str(len(reconnects)))
        if reconnects:
            check("reconnect: logged gap is a plausible, positive duration",
                  0.0 < reconnects[0]["value"] < 30.0, str(reconnects[0]["value"]))

        window2 = await call(client, "read_window", session_id="lsl_rc", seconds=1.0)
        check("reconnect: fresh samples resume after reattaching",
              abs(window2["t_end"] - time.time()) < 5.0,
              f"t_end={window2['t_end']} now={time.time()}")

        await call(client, "stop_stream", session_id="lsl_rc")
        stop_b.set()
    finally:
        stop_a.set()
        thread_a.join(timeout=2.0)
        if thread_b is not None:
            thread_b.join(timeout=2.0)


async def verify_lsl_zombie_outlet(client: Client) -> None:
    """The bug actually found running against real hardware for an extended
    session: a bridge process exits, its LSL announcement is not retracted
    instantly, and a reconnect that only checks source_id/shape can happily
    "recover" onto that dead outlet and sit there forever -- which is exactly
    what happened to one of four simultaneous real streams (EEG/ACC/GYRO
    recovered correctly; OPTICS silently locked onto a zombie for the rest of
    the session). Reproduces it directly: an outlet that keeps resolving and
    shape-matching but has gone silent must be refused, and a *genuinely*
    fresh outlet under the same source_id, tested separately, must still be
    accepted."""
    print("\n[lsl] refuses a zombie outlet that matches but never produces")
    try:
        import pylsl
    except ImportError:
        skip("lsl zombie-outlet suite", "pylsl not installed (pip install eeg-mcp[lsl])")
        return

    import gc
    import threading

    n_ch, sfreq, source_id = 4, 256.0, "verify-zombie-device"

    def publish(stop_event: threading.Event) -> None:
        info = pylsl.StreamInfo("verify-lsl-zombie", "EEG", n_ch, sfreq, "float32",
                                source_id)
        outlet = pylsl.StreamOutlet(info)
        t = 0
        while not stop_event.is_set():
            outlet.push_sample([float(t % n_ch)] * n_ch)
            t += 1
            time.sleep(1.0 / sfreq)
        del outlet  # retract it -- phase 2 needs the name genuinely free

    # Phase 1: outlet streams briefly, then goes silent while the underlying
    # StreamOutlet object is still referenced (thread just stops pushing) --
    # still resolvable by source_id, still the right shape, producing nothing.
    # This, not a torn-down connection, is the shape of the real bug.
    stop_silent = threading.Event()
    silence_flag = threading.Event()  # set once the publisher should go quiet

    def publish_then_go_silent() -> None:
        info = pylsl.StreamInfo("verify-lsl-zombie", "EEG", n_ch, sfreq, "float32",
                                source_id)
        outlet = pylsl.StreamOutlet(info)
        t = 0
        while not stop_silent.is_set():
            if not silence_flag.is_set():
                outlet.push_sample([float(t % n_ch)] * n_ch)
                t += 1
            time.sleep(1.0 / sfreq)
        del outlet

    thread_a = threading.Thread(target=publish_then_go_silent, daemon=True)
    thread_a.start()
    try:
        time.sleep(1.0)
        started = await call(
            client, "start_stream", session_id="lsl_zombie", board="lsl",
            params={"name": "verify-lsl-zombie", "stale_after_sec": 1.0},
        )
        check("zombie: stream starts", started["started"] is True)
        time.sleep(1.5)
        window = await call(client, "read_window", session_id="lsl_zombie", seconds=1.0)
        check("zombie: samples flow before going silent", window["n_samples"] > 0,
              str(window["n_samples"]))

        silence_flag.set()  # outlet object stays alive; just stops producing
        time.sleep(4.0)  # past stale_after_sec + several reconnect attempts

        status = await call(client, "stream_status", session_id="lsl_zombie")
        check(
            "zombie: reconnect refuses the dead-but-still-resolvable outlet",
            status["connection"]["connected"] is False
            and status["connection"]["reconnect_count"] == 0,
            str(status["connection"]),
        )
    finally:
        stop_silent.set()
        thread_a.join(timeout=2.0)
        gc.collect()  # retract the zombie so phase 2 gets a clean name

    # Phase 2: a genuinely fresh, actually-producing outlet under the same
    # source_id -- the real recovery case, tested in isolation so it cannot
    # be satisfied by accidentally re-resolving phase 1's zombie.
    stop_b = threading.Event()
    thread_b = threading.Thread(target=publish, args=(stop_b,), daemon=True)
    thread_b.start()
    try:
        time.sleep(2.5)
        status = await call(client, "stream_status", session_id="lsl_zombie")
        check(
            "zombie: reconnects once a genuinely live outlet appears",
            status["connection"]["connected"] is True
            and status["connection"]["reconnect_count"] == 1,
            str(status["connection"]),
        )
        window2 = await call(client, "read_window", session_id="lsl_zombie", seconds=1.0)
        check("zombie: fresh samples resume after the real reconnect",
              window2["n_samples"] > 0, str(window2["n_samples"]))
        await call(client, "stop_stream", session_id="lsl_zombie")
    finally:
        stop_b.set()
        thread_b.join(timeout=2.0)


async def verify_lsl_sibling_streams(client: Client) -> None:
    """The real second bug found on hardware, distinct from the zombie-outlet
    case above: a single muselsl process publishes EEG/ACC/GYRO/OPTICS as
    four *sibling* outlets that all share one source_id ("Muse<mac>" -- see
    muselsl/lsl_outlet.py's build_outlet, which passes the same
    address-derived source_id for every sensor type), differing only in
    ``type``. A reconnect that resolves by source_id alone cannot tell them
    apart -- resolve_byprop is free to hand back a different sibling, whose
    shape then fails validation, leaving that source stuck retrying forever
    while another session's reconnect recovers by the luck of which sibling
    comes back first. This is exactly the "some streams stay frozen after a
    bridge restart, others don't" pattern observed against a real Muse S
    Athena (EEG/OPTICS recovering while ACC/GYRO stayed stale for minutes).
    Reproduces it with two simultaneous sibling outlets sharing one
    source_id (EEG-shaped and ACC-shaped). The deterministic check is on the
    mechanism itself -- connection_status()['resolves_by'] must key off the
    stream-specific discriminator (type/name), not source_id, since
    resolve_byprop's own ordering when several outlets share a source_id
    turned out to be unreliable in practice (sometimes returning only the
    first-registered sibling, sometimes both) -- an end-to-end reconnect
    could pass by luck even on the old, buggy resolve-by-source_id code.
    The reconnect-and-check-shape run below is kept as a behavioral sanity
    check on top of that, not as the sole guard."""
    print("\n[lsl] reconnects the right sibling when several outlets share one source_id")
    try:
        import pylsl
    except ImportError:
        skip("lsl sibling-stream suite", "pylsl not installed (pip install eeg-mcp[lsl])")
        return

    import gc
    import threading

    source_id = "verify-shared-device"

    def publish_pair(stop_event: threading.Event) -> None:
        eeg_info = pylsl.StreamInfo("Muse", "EEG", 4, 256.0, "float32", source_id)
        acc_info = pylsl.StreamInfo("Muse", "ACC", 3, 52.0, "float32", source_id)
        eeg_outlet = pylsl.StreamOutlet(eeg_info)
        acc_outlet = pylsl.StreamOutlet(acc_info)
        t = 0
        while not stop_event.is_set():
            eeg_outlet.push_sample([float(t % 4)] * 4)
            if t % 5 == 0:
                acc_outlet.push_sample([float(t % 3)] * 3)
            t += 1
            time.sleep(1.0 / 256.0)
        del eeg_outlet, acc_outlet  # retract both so the restart gets clean names

    stop_a = threading.Event()
    thread_a = threading.Thread(target=publish_pair, args=(stop_a,), daemon=True)
    thread_a.start()
    thread_b: threading.Thread | None = None
    stop_b = threading.Event()
    try:
        time.sleep(1.0)
        started = await call(
            client, "start_stream", session_id="lsl_sibling", board="lsl",
            params={"stream_type": "ACC", "stale_after_sec": 1.0},
        )
        check("sibling: pins to the ACC-shaped outlet, not its EEG sibling",
              started["source"]["n_channels"] == 3, str(started["source"]))

        status0 = await call(client, "stream_status", session_id="lsl_sibling")
        resolves_by = status0["connection"]["resolves_by"]
        check(
            "sibling: reconnect resolves by type/name, not the shared source_id "
            "(the actual fix -- source_id alone can't tell siblings apart)",
            resolves_by == {"type": "ACC"}, str(resolves_by),
        )
        check("sibling: source_id is still tracked, as an extra same-device check",
              status0["connection"]["source_id"] == source_id,
              str(status0["connection"]["source_id"]))

        time.sleep(1.5)
        window = await call(client, "read_window", session_id="lsl_sibling", seconds=1.0)
        check("sibling: samples flow before the drop", window["n_samples"] > 0,
              str(window["n_samples"]))

        # Simulate the bridge restart: both siblings drop together, then both
        # come back together, still sharing the same source_id -- the shape
        # a real muselsl restart takes.
        stop_a.set()
        thread_a.join(timeout=2.0)
        gc.collect()

        time.sleep(2.0)
        status = await call(client, "stream_status", session_id="lsl_sibling")
        check("sibling: connection reports disconnected",
              status["connection"]["connected"] is False, str(status["connection"]))

        thread_b = threading.Thread(target=publish_pair, args=(stop_b,), daemon=True)
        thread_b.start()
        time.sleep(2.5)  # resolve (<=1s) + open + a couple of poll cycles

        status = await call(client, "stream_status", session_id="lsl_sibling")
        check(
            "sibling: reconnects despite a same-source_id sibling also being resolvable",
            status["connection"]["connected"] is True, str(status["connection"]),
        )

        window2 = await call(client, "read_window", session_id="lsl_sibling", seconds=1.0)
        check("sibling: reattached to the ACC-shaped stream, not the EEG-shaped sibling",
              len(window2["channels"]) == 3, str(window2.get("channels")))

        await call(client, "stop_stream", session_id="lsl_sibling")
    finally:
        stop_a.set()
        stop_b.set()
        thread_a.join(timeout=2.0)
        if thread_b is not None:
            thread_b.join(timeout=2.0)


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
        await verify_lsl(client)
        await verify_lsl_reconnect(client)
        await verify_lsl_zombie_outlet(client)
        await verify_lsl_sibling_streams(client)
        await verify_errors(client)

    print(f"\n{'=' * 52}\n  {PASSED} passed, {FAILED} failed\n{'=' * 52}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
