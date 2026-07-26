"""Verification for pluggable real-time processors.

The point of the plugin system is that *your* code runs on equal footing with
the built-ins, so the central test writes a plugin file to a temp directory,
points ``EEG_MCP_PLUGIN_DIR`` at it, and checks it is discovered, attachable,
executed in the producer thread, and able to emit events -- exactly what a user
following the Extending guide will do.

Run:  python testing/verify_processing.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="eegmcp_proc_")
_PLUGIN_DIR = Path(_TMP) / "plugins"
_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

# A plugin exactly as a user would write it, loaded from a directory.
(_PLUGIN_DIR / "my_plugin.py").write_text('''
"""A user-authored processor, discovered from EEG_MCP_PLUGIN_DIR."""

import numpy as np

from eeg_mcp.processing import Processor


class PeakToPeakProcessor(Processor):
    name = "my_peak_to_peak"
    description = "Mean peak-to-peak amplitude, and an event when it is large."
    streaming = True
    uses = "raw"
    params = {
        "alert_uv": (400.0, "Emit an event above this peak-to-peak amplitude."),
        "label": ("big_swing", "Event label to emit."),
    }

    def setup(self, ctx):
        self.n_alerts = 0
        self.seen_channels = len(ctx.ch_names)

    def process(self, data, timestamps, ctx):
        ptp = float(np.mean(np.ptp(data, axis=1)))
        if ptp > self.config["alert_uv"]:
            self.n_alerts += 1
            ctx.emit_event(self.config["label"], value=ptp,
                           payload={"processor": self.name})
        return {"ptp_uv": ptp, "n_alerts": self.n_alerts,
                "n_channels": self.seen_channels}


class BrokenProcessor(Processor):
    """Deliberately raises, to prove a bad plugin cannot stop acquisition."""

    name = "my_broken"
    description = "Always raises."
    streaming = True
    params = {}

    def process(self, data, timestamps, ctx):
        raise RuntimeError("this plugin is broken on purpose")
''', encoding="utf-8")

os.environ["EEG_MCP_HOME"] = _TMP
os.environ["EEG_MCP_PLUGIN_DIR"] = str(_PLUGIN_DIR)
os.environ["EEG_MCP_BUFFER_SEC"] = "30"

from fastmcp import Client  # noqa: E402

from eeg_mcp.server import mcp  # noqa: E402

PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")
    return bool(condition)


async def call(client: Client, tool: str, **args):
    result = await client.call_tool(tool, args)
    return result.data if result.data is not None else result.structured_content


async def test_discovery(client: Client) -> None:
    print("\n[discovery] built-ins and a user plugin from a directory")
    listing = await call(client, "list_processors")
    names = {p["name"] for p in listing["processors"]}

    for builtin in ("band_power", "threshold_trigger", "artifact_detector",
                    "band_tokenizer"):
        check(f"built-in '{builtin}' discovered", builtin in names)

    check("user plugin discovered from EEG_MCP_PLUGIN_DIR",
          "my_peak_to_peak" in names, str(sorted(names)))
    mine = next(p for p in listing["processors"] if p["name"] == "my_peak_to_peak")
    check("plugin source is attributed", mine["source"].startswith("dir:"),
          mine["source"])
    check("plugin parameters are advertised", "alert_uv" in mine["params"])
    check("no load errors", listing["load_errors"] == [], str(listing["load_errors"]))


async def test_user_plugin_runs(client: Client) -> None:
    print("\n[run] the user plugin executes in the producer thread")
    await call(client, "start_stream", session_id="p1", board="synthetic")

    # An agent must not be able to name an arbitrary import path: loading a
    # module executes it, which would be arbitrary code execution driven by a
    # model. Only registry entries the operator provisioned are attachable.
    for hostile in ("os.system", "eeg_mcp.session:SessionStore", "builtins:eval"):
        try:
            await call(client, "attach_processor", session_id="p1",
                       processor=hostile)
            check(f"'{hostile}' is refused", False, "no error raised")
        except Exception as exc:
            check(f"'{hostile}' is refused", "Unknown processor" in str(exc),
                  str(exc)[:60])

    await call(client, "set_filters", session_id="p1", bandpass_low=1,
               bandpass_high=40, notch_freq=50)
    attached = await call(client, "attach_processor", session_id="p1",
                          processor="my_peak_to_peak", params={"alert_uv": 1.0})
    check("user plugin attaches", attached["attached"] == "my_peak_to_peak")
    check("it is recognised as streaming",
          attached["processor"]["streaming"] is True)

    time.sleep(2.0)
    output = await call(client, "get_processor_output", session_id="p1",
                        processor="my_peak_to_peak")
    check("plugin produced outputs without being asked", output["n_total"] > 5,
          str(output["n_total"]))
    check("its return value is preserved", "ptp_uv" in (output["latest"] or {}),
          str(output["latest"])[:60])

    events = await call(client, "get_events", session_id="p1", label="big_swing")
    check("plugin emitted events into the session log",
          events["n_returned"] > 0, str(events["n_returned"]))

    status = await call(client, "processor_status", session_id="p1")
    mine = status["processors"]["my_peak_to_peak"]
    check("timing is measured", mine["mean_ms"] >= 0 and mine["n_calls"] > 5,
          f"{mine['n_calls']} calls, mean {mine['mean_ms']:.3f} ms")
    check("plugin is cheap enough for the producer loop",
          mine["max_ms"] < status["poll_interval_ms"],
          f"max {mine['max_ms']:.2f} ms vs poll {status['poll_interval_ms']:.0f} ms")

    params = await call(client, "attach_processor", session_id="p1",
                        processor="my_peak_to_peak", params={"alert_uv": 999999.0})
    check("re-attaching applies new parameters",
          params["processor"]["config"]["alert_uv"] == 999999.0)


async def test_broken_plugin_is_contained(client: Client) -> None:
    print("\n[containment] a broken plugin must not stop acquisition")
    await call(client, "attach_processor", session_id="p1", processor="my_broken")
    time.sleep(2.0)

    status = await call(client, "processor_status", session_id="p1")
    broken = status["processors"]["my_broken"]
    check("errors are counted", broken["n_errors"] > 0, str(broken["n_errors"]))
    check("the error message is retained",
          "broken on purpose" in (broken["last_error"] or ""),
          str(broken["last_error"])[:60])
    check("it is auto-disabled after repeated failures",
          broken["enabled"] is False or broken["n_errors"] >= 10,
          f"enabled={broken['enabled']} errors={broken['n_errors']}")

    stream = await call(client, "stream_status", session_id="p1")
    check("acquisition survived the broken plugin", stream["running"] is True)
    check("no samples were dropped", stream["throughput_ratio"] > 0.8,
          f"ratio={stream['throughput_ratio']:.2f}")
    check("healthy plugins keep running alongside it",
          status["processors"]["my_peak_to_peak"]["enabled"] is True)

    await call(client, "detach_processor", session_id="p1", processor="my_broken")


async def test_builtins(client: Client) -> None:
    print("\n[builtins] band power, trigger, artifacts")
    await call(client, "attach_processor", session_id="p1", processor="band_power",
               params={"window_sec": 1.0, "hop_sec": 0.25})
    await call(client, "attach_processor", session_id="p1",
               processor="threshold_trigger",
               params={"band": "alpha", "threshold": 0.0, "refractory_sec": 0.3,
                       "label": "alpha_high"})
    await call(client, "attach_processor", session_id="p1",
               processor="artifact_detector", params={"amplitude_uv": 1.0})
    time.sleep(3.0)

    power = await call(client, "get_processor_output", session_id="p1",
                       processor="band_power")
    check("band_power emits rolling output", power["n_total"] > 2,
          str(power["n_total"]))
    check("it names a dominant band",
          (power["latest"] or {}).get("dominant_band") in
          ("delta", "theta", "alpha", "beta", "gamma"),
          str((power["latest"] or {}).get("dominant_band")))

    triggers = await call(client, "get_events", session_id="p1", label="alpha_high")
    check("threshold_trigger fires without an agent round-trip",
          triggers["n_returned"] > 0, str(triggers["n_returned"]))
    if triggers["n_returned"] > 1:
        stamps = [e["timestamp"] for e in triggers["events"]]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        check("the refractory period is honoured", min(gaps) >= 0.25,
              f"min gap {min(gaps):.2f}s")

    artifacts = await call(client, "get_processor_output", session_id="p1",
                           processor="artifact_detector")
    check("artifact_detector reports channel-level detail",
          "affected_channels" in (artifacts["latest"] or {}),
          str(artifacts["latest"])[:60])


async def test_tokenizer(client: Client) -> None:
    print("\n[tokens] discretisation for sequence models")
    # Take real channel names from the board rather than assuming them.
    channels = await call(client, "get_channels", session_id="p1")
    picked = [c["name"] for c in channels["channels"][:2]]

    await call(client, "attach_processor", session_id="p1",
               processor="band_tokenizer",
               params={"window_sec": 0.5, "hop_sec": 0.25, "n_levels": 8,
                       "channels": picked, "bands": ["alpha", "beta"]})
    time.sleep(3.0)

    tokens = await call(client, "get_tokens", session_id="p1",
                        processor="band_tokenizer", limit=8)
    expected_vocab = 2 * 2 * 8
    check("vocabulary size matches channels x bands x levels",
          tokens["vocab_size"] == expected_vocab, str(tokens["vocab_size"]))
    check("token groups accumulate", tokens["n_groups"] > 1, str(tokens["n_groups"]))
    check("each group has one token per channel-band pair",
          tokens["tokens_per_group"] == 4, str(tokens["tokens_per_group"]))
    check("layout labels every token position",
          tokens["layout"] == [f"{c}:{b}" for c in picked
                               for b in ("alpha", "beta")],
          str(tokens["layout"]))

    flat = tokens["flat"]
    check("a flat sequence is provided for a sequence model",
          len(flat) == tokens["n_groups"] * 4, f"{len(flat)} tokens")
    check("every token is inside the vocabulary",
          all(0 <= t < expected_vocab for t in flat),
          f"range {min(flat)}-{max(flat)}" if flat else "empty")

    try:
        await call(client, "get_tokens", session_id="p1", processor="band_power")
        check("non-tokenizers are rejected by get_tokens", False, "no error")
    except Exception as exc:
        check("non-tokenizers are rejected by get_tokens",
              "does not produce tokens" in str(exc), str(exc)[:60])


async def test_on_demand_and_reset(client: Client) -> None:
    print("\n[control] on-demand execution and reset")
    result = await call(client, "run_processor", session_id="p1",
                        processor="band_power", seconds=2.0)
    check("run_processor executes immediately", result["result"] is not None,
          str(result.get("note", ""))[:50])

    await call(client, "reset_processors", session_id="p1")
    after = await call(client, "get_processor_output", session_id="p1",
                       processor="band_power")
    check("reset clears accumulated outputs", after["n_total"] == 0,
          str(after["n_total"]))

    await call(client, "detach_processor", session_id="p1", processor="band_power")
    status = await call(client, "processor_status", session_id="p1")
    check("detach removes it", "band_power" not in status["processors"],
          str(sorted(status["processors"])))


async def main() -> int:
    print(f"plugin dir: {_PLUGIN_DIR}")
    async with Client(mcp) as client:
        try:
            await test_discovery(client)
            await test_user_plugin_runs(client)
            await test_broken_plugin_is_contained(client)
            await test_builtins(client)
            await test_tokenizer(client)
            await test_on_demand_and_reset(client)
        finally:
            try:
                await call(client, "stop_stream", session_id="p1")
            except Exception:
                pass

    print(f"\n{'=' * 56}\n  PROCESSORS: {PASSED} passed, {FAILED} failed\n{'=' * 56}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
