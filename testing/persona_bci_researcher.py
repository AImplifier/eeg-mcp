"""Persona test: a BCI engineer building a closed-loop pipeline.

Their questions, in the order they would actually ask them:

1. Can I develop against a recording and deploy the same code live?
2. Do the numbers mean what I think -- is power on the channel I asked for?
3. Are my markers actually aligned to the samples, to within a sample?
4. What is my end-to-end latency budget, concretely?
5. How much jitter does a pulse train have?
6. Can I run a live board and a replay side by side to compare?
7. When I get it wrong, does the error tell me what to fix?

Run:  python testing/persona_bci_researcher.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="eegmcp_bci_")
os.environ["EEG_MCP_HOME"] = _TMP
os.environ["EEG_MCP_BUFFER_SEC"] = "60"

import numpy as np  # noqa: E402
from fastmcp import Client  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from make_synthetic import GROUND_TRUTH, write as write_synthetic  # noqa: E402

from eeg_mcp.server import mcp  # noqa: E402

PASSED, FAILED = 0, 0
WINS: list[str] = []
FRICTION: list[str] = []

# Shared CI runners are not real-time systems: they are virtualised, noisy, and
# oversubscribed, so Python's sleep resolution and thread scheduling are far
# worse than on a workstation. The timing bounds below describe real hardware,
# which is the number a BCI engineer actually cares about -- so on CI they are
# widened rather than skipped, keeping the assertion meaningful in both places.
ON_CI = bool(os.environ.get("CI"))


def tol(local: float, ci: float) -> float:
    return ci if ON_CI else local


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")
    return bool(condition)


def win(text: str) -> None:
    WINS.append(text)
    print(f"   +  {text}")


def friction(text: str) -> None:
    FRICTION.append(text)
    print(f"   -  {text}")


async def call(client: Client, tool: str, **args):
    result = await client.call_tool(tool, args)
    return result.data if result.data is not None else result.structured_content


async def q1_develop_offline_deploy_live(client: Client, path: Path) -> None:
    print("\n[Q1] Can I develop against a recording and deploy the same code live?")

    # The exact same call sequence, once against a file and once against a board.
    async def pipeline(session_id: str) -> dict:
        await call(client, "set_filters", session_id=session_id,
                   bandpass_low=1.0, bandpass_high=40.0, notch_freq=50.0)
        time.sleep(2.0)
        return await call(client, "get_band_power", session_id=session_id, seconds=1.5)

    await call(client, "start_replay", session_id="dev", path=str(path), speed=1.0)
    dev = await pipeline("dev")

    await call(client, "start_stream", session_id="live", board="synthetic")
    live = await pipeline("live")

    check("identical tool sequence works on replay and live",
          set(dev["mean_across_channels"]) == set(live["mean_across_channels"]))
    check("both report a dominant band",
          bool(dev["dominant_band"]) and bool(live["dominant_band"]),
          f"replay={dev['dominant_band']} live={live['dominant_band']}")
    win("A pipeline written against a file needs zero changes to run on hardware.")


async def q2_numbers_mean_what_i_think(client: Client) -> None:
    print("\n[Q2] Is the power really on the channel I asked for?")
    alpha_ch = GROUND_TRUTH["alpha_channels"]
    quiet_ch = "Fz"

    psd = await call(client, "get_psd", session_id="dev", seconds=4.0,
                     filtered=False, fmax=45.0)
    peaks = psd["peak_freq"]

    for name in alpha_ch:
        got = peaks.get(name)
        check(f"{name} peaks at the planted {GROUND_TRUTH['alpha_hz']} Hz",
              got is not None and abs(got - GROUND_TRUTH["alpha_hz"]) <= 1.0,
              f"{got} Hz")

    power = await call(client, "get_band_power", session_id="dev", seconds=4.0,
                       filtered=False)
    per_ch = power["per_channel"]
    alpha_rel = np.mean([per_ch[c]["alpha_rel"] for c in alpha_ch])
    quiet_rel = per_ch[quiet_ch]["alpha_rel"]
    check("alpha is concentrated on the channels that carry it",
          alpha_rel > 3 * quiet_rel, f"{alpha_rel:.3f} vs {quiet_rel:.3f} on {quiet_ch}")
    win("Per-channel band power is correctly localised, not smeared across the montage.")


async def q3_marker_sample_alignment(client: Client) -> None:
    """The assertion that covers the whole timing chain at once."""
    print("\n[Q3] Are markers aligned to samples, to within a sample?")
    spike_ch = GROUND_TRUTH["spike_channel"]

    events = await call(client, "get_events", session_id="dev", origin="annotation")
    if not check("annotations arrived during replay", events["n_returned"] >= 2,
                 str(events["n_returned"])):
        return

    # Use the second-most-recent so the whole post-event window is recorded.
    epoch = await call(client, "get_epoch_around_event", session_id="dev",
                       label="stim", tmin=-0.1, tmax=0.1, filtered=False,
                       occurrence=-2, max_samples=10_000)

    channels = epoch["channels"]
    times = np.asarray(epoch["times"], dtype=float)
    data = np.asarray(epoch["data"], dtype=float)
    trace = data[channels.index(spike_ch)]

    peak_time = float(times[int(np.argmax(np.abs(trace)))])
    tolerance = 2.0 / GROUND_TRUTH["sfreq"]  # two samples
    check("planted spike lands at t=0 in the epoch",
          abs(peak_time) <= tolerance,
          f"peak at {peak_time * 1000:+.1f} ms, tolerance +/-{tolerance * 1000:.1f} ms")

    others = [i for i, c in enumerate(channels) if c != spike_ch]
    check("spike is confined to the channel it was planted on",
          np.max(np.abs(trace)) > 5 * np.max(np.abs(data[others])),
          f"{np.max(np.abs(trace)):.0f} uV vs {np.max(np.abs(data[others])):.0f} uV")
    win("Annotation timing -> replay clock -> ring-buffer index -> epoch cut is "
        "sample-accurate end to end.")


async def q4_latency_budget(client: Client) -> None:
    print("\n[Q4] What is my closed-loop latency budget?")
    status = await call(client, "stream_status", session_id="live")
    group_delay_ms = status["filters"]["group_delay_sec"] * 1000.0

    await call(client, "attach_stim_backend", session_id="live", backend="log")

    # Time the decision half of the loop: read a feature, decide, dispatch.
    samples = []
    for _ in range(5):
        t0 = time.perf_counter()
        await call(client, "get_band_power", session_id="live", seconds=1.0)
        sent = await call(client, "send_stim_event", session_id="live",
                          label="closed_loop", value=1)
        samples.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(0.05)

    decide_ms = float(np.median(samples))
    poll_ms = 50.0  # EEG_MCP_POLL_INTERVAL_SEC default
    total = group_delay_ms + poll_ms + decide_ms

    print(f"        filter group delay : {group_delay_ms:7.1f} ms")
    print(f"        producer poll      : {poll_ms:7.1f} ms")
    print(f"        feature + dispatch : {decide_ms:7.1f} ms (median of 5)")
    print(f"        ---------------------------------")
    print(f"        end-to-end budget  : {total:7.1f} ms")

    limit = tol(250.0, 2000.0)
    check("group delay is reported, not hidden", group_delay_ms > 0,
          f"{group_delay_ms:.1f} ms")
    check(f"feature+dispatch stays under {limit:.0f} ms"
          + (" (CI-relaxed)" if ON_CI else ""),
          decide_ms < limit, f"{decide_ms:.1f} ms")
    check("dispatch latency is measured per call",
          sent["dispatch_latency_ms"] >= 0, f"{sent['dispatch_latency_ms']:.3f} ms")
    if total > 500:
        friction(f"End-to-end budget is {total:.0f} ms -- too slow for phase-locked "
                 "stimulation, fine for band-power neurofeedback.")
    else:
        win(f"End-to-end budget {total:.0f} ms is workable for neurofeedback loops.")


async def q5_train_jitter(client: Client) -> None:
    print("\n[Q5] How much jitter does a pulse train have?")
    train = await call(client, "send_stim_train", session_id="live", label="rtms_10hz",
                       n_pulses=20, interval_ms=100)
    realised = train["realised_interval_ms"]
    spread = realised["max"] - realised["min"]

    print(f"        requested {train['requested_interval_ms']:.1f} ms, "
          f"realised mean {realised['mean']:.2f} ms "
          f"(min {realised['min']:.2f}, max {realised['max']:.2f})")

    mean_tol = tol(5.0, 40.0)
    drift_tol = tol(0.1, 0.8)
    check("all pulses delivered", train["n_delivered"] == 20)
    check(f"mean interval within {mean_tol:.0f} ms of requested"
          + (" (CI-relaxed)" if ON_CI else ""),
          abs(realised["mean"] - 100.0) < mean_tol, f"{realised['mean']:.2f} ms")
    # Scheduling against the train's start rather than the previous pulse is
    # what keeps this bounded; without it the error would compound per pulse.
    check("no cumulative drift over the train",
          abs(train["total_sec"] - 1.9) < drift_tol,
          f"{train['total_sec']:.3f}s over 19 gaps")

    if spread > tol(20, 200):
        friction(f"Train jitter spread is {spread:.1f} ms -- Python sleep resolution. "
                 "For sub-ms rTMS timing, drive the train from device hardware.")
    else:
        win(f"Train jitter spread {spread:.1f} ms, scheduled against train start so "
            "error does not accumulate.")


async def q6_concurrent_sessions(client: Client, path: Path) -> None:
    print("\n[Q6] Can I run a live board and a replay side by side?")
    await call(client, "start_replay", session_id="compare", path=str(path),
               speed=2.0)
    time.sleep(1.5)

    sessions = await call(client, "list_sessions")
    ids = {s["session_id"] for s in sessions["sessions"]}
    check("three sessions coexist", {"dev", "live", "compare"} <= ids, str(sorted(ids)))

    running = [s for s in sessions["sessions"] if s["running"]]
    check("all sessions independently producing", len(running) >= 3, str(len(running)))

    live_status = await call(client, "stream_status", session_id="live")
    throughput_floor = tol(0.8, 0.5)
    check("live throughput unaffected by concurrent replays",
          live_status["throughput_ratio"] > throughput_floor,
          f"ratio={live_status['throughput_ratio']:.2f}")
    check("sessions keep separate event logs",
          live_status["n_events"] != (
              await call(client, "stream_status", session_id="compare"))["n_events"])
    win("Independent sessions, independent buffers, independent event logs.")


async def q7_error_ergonomics(client: Client) -> None:
    print("\n[Q7] When I get it wrong, does the error tell me what to fix?")
    cases = [
        ("filter above Nyquist",
         ("set_filters", {"session_id": "dev", "bandpass_low": 1, "bandpass_high": 200}),
         "Nyquist"),
        ("replay control on a live session",
         ("replay_control", {"session_id": "live", "action": "pause"}), "not a replay"),
        ("seek without a position",
         ("replay_control", {"session_id": "dev", "action": "seek"}), "position_sec"),
        ("unknown channel name",
         ("get_band_power", {"session_id": "dev", "channels": ["Xx9"]}), "Available"),
        ("stim before attaching a backend",
         ("send_stim_event", {"session_id": "compare", "label": "x"}),
         "attach_stim_backend"),
        ("train over the pulse cap",
         ("send_stim_train", {"session_id": "live", "label": "x",
                              "n_pulses": 5000, "interval_ms": 10}), "cap"),
    ]
    for name, (tool, args), needle in cases:
        try:
            await call(client, tool, **args)
            check(f"{name} is rejected", False, "no error raised")
        except Exception as exc:
            check(f"{name} is rejected with guidance", needle in str(exc),
                  f"...{str(exc)[-80:].strip()}")
    win("Errors name the offending value and the tool that fixes it.")


async def main() -> int:
    data_dir = Path(_TMP) / "data"
    path = write_synthetic(data_dir)
    print(f"synthetic ground truth: {path.name} "
          f"({GROUND_TRUTH['n_events']} events, {GROUND_TRUTH['alpha_hz']} Hz alpha "
          f"on {GROUND_TRUTH['alpha_channels']}, spike on "
          f"{GROUND_TRUTH['spike_channel']})")

    async with Client(mcp) as client:
        try:
            await q1_develop_offline_deploy_live(client, path)
            await q2_numbers_mean_what_i_think(client)
            await q3_marker_sample_alignment(client)
            await q4_latency_budget(client)
            await q5_train_jitter(client)
            await q6_concurrent_sessions(client, path)
            await q7_error_ergonomics(client)
        finally:
            for session_id in ("dev", "live", "compare"):
                try:
                    await call(client, "stop_stream", session_id=session_id)
                except Exception:
                    pass

    print(f"\n{'=' * 60}")
    print(f"  BCI ENGINEER: {PASSED} passed, {FAILED} failed")
    print(f"{'=' * 60}")
    if WINS:
        print("\n  Wins:")
        for item in WINS:
            print(f"    + {item}")
    if FRICTION:
        print("\n  Friction:")
        for item in FRICTION:
            print(f"    - {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
