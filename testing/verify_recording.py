"""Verification for recording to disk and registering metadata.

The assertions that matter here are round-trips, not "did a file appear":

* the recorded ``.fif`` reopens with the right shape, rate and channel names;
* the amplitude survives the microvolt -> volt conversion, so the signal is not
  silently scaled by a million;
* events logged during the recording come back as annotations at the right
  onsets;
* the metadata row is readable through neuro-mcp's own schema.

Run:  python testing/verify_recording.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="eegmcp_rec_")
os.environ["EEG_MCP_HOME"] = _TMP
os.environ["EEG_MCP_RECORD_DIR"] = str(Path(_TMP) / "recorded")
os.environ["EEG_MCP_DATABASE_URL"] = f"sqlite:///{Path(_TMP) / 'store.db'}"
os.environ["EEG_MCP_BUFFER_SEC"] = "30"

import mne  # noqa: E402
import numpy as np  # noqa: E402
from fastmcp import Client  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from make_synthetic import GROUND_TRUTH, write as write_synthetic  # noqa: E402

from eeg_mcp.server import mcp  # noqa: E402

mne.set_log_level("ERROR")
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


async def test_record_roundtrip(client: Client) -> dict:
    print("\n[record] live session -> .fif")
    await call(client, "start_stream", session_id="rec", board="synthetic")
    time.sleep(1.0)

    started = await call(client, "start_recording", session_id="rec",
                         subject="P01", task="rest", run="01",
                         session_label="baseline", dataset="pilot-study",
                         notes="Verification run.")
    check("recording starts", started["recording"] is True)
    check("BIDS-style filename from the entities",
          "sub-P01_ses-baseline_task-rest_run-01_eeg" in started["stem"],
          started["stem"])

    time.sleep(1.2)
    await call(client, "log_event", session_id="rec", label="cue_a", value=5)
    time.sleep(1.2)
    await call(client, "log_event", session_id="rec", label="cue_b", value=6)
    time.sleep(1.6)

    status = await call(client, "recording_status", session_id="rec")
    check("bytes accumulate while recording", status["n_samples"] > 500,
          f"{status['n_samples']} samples, {status['size_mb']:.2f} MB")
    check("status reports it is recording", status["recording"] is True)

    stopped = await call(client, "stop_recording", session_id="rec")
    check("recording finalises", stopped["written"] is True)
    check("duration is plausible", 3.0 < stopped["duration_sec"] < 8.0,
          f"{stopped['duration_sec']:.2f}s")
    return stopped


def test_file_contents(stopped: dict) -> None:
    print("\n[file] does the .fif actually contain the session?")
    path = Path(stopped["fif_path"])
    check("file exists on disk", path.exists(), path.name)

    raw = mne.io.read_raw_fif(str(path), preload=True, verbose="ERROR")
    check("channel count round-trips",
          len(raw.ch_names) == stopped["n_channels"], str(len(raw.ch_names)))
    check("sampling rate round-trips",
          abs(raw.info["sfreq"] - stopped["sfreq"]) < 1e-6, str(raw.info["sfreq"]))
    check("duration round-trips",
          abs(raw.times[-1] - stopped["duration_sec"]) < 0.1,
          f"{raw.times[-1]:.2f}s vs {stopped['duration_sec']:.2f}s")

    # The unit conversion is the classic silent bug: microvolts written as if
    # they were volts is a 1e6 error that no shape assertion would catch.
    data_uv = raw.get_data() * 1e6
    peak = float(np.max(np.abs(data_uv)))
    check("amplitude survives the uV->V conversion", 1.0 < peak < 500_000.0,
          f"peak {peak:.1f} uV")

    labels = list(raw.annotations.description)
    check("logged events became annotations",
          "cue_a" in labels and "cue_b" in labels, str(labels))
    if "cue_a" in labels and "cue_b" in labels:
        onset_a = float(raw.annotations.onset[labels.index("cue_a")])
        onset_b = float(raw.annotations.onset[labels.index("cue_b")])
        check("annotation onsets are inside the recording",
              0 <= onset_a <= raw.times[-1] and 0 <= onset_b <= raw.times[-1],
              f"a={onset_a:.2f}s b={onset_b:.2f}s")
        check("annotation order matches the order they were logged",
              onset_a < onset_b, f"{onset_a:.2f} < {onset_b:.2f}")

    check("crash-recovery sidecar cleaned up after success",
          not Path(str(path).replace("_raw.fif", ".eegraw")).exists())
    check("header sidecar retained for provenance",
          Path(stopped["header_path"]).exists())


async def test_registration(client: Client, stopped: dict) -> None:
    print("\n[db] metadata store")
    registered = stopped.get("registered")
    check("recording registered", isinstance(registered, dict)
          and registered.get("recording_id") is not None, str(registered)[:80])
    check("annotations registered alongside it",
          isinstance(registered, dict) and registered["n_annotations"] >= 2,
          str(registered.get("n_annotations") if isinstance(registered, dict) else ""))

    listed = await call(client, "list_recordings")
    check("recording appears in the catalogue", listed["n_recordings"] >= 1)
    row = listed["recordings"][0]
    for field, expected in [("subject", "P01"), ("task", "rest"), ("run", "01"),
                            ("session", "baseline"), ("dataset", "pilot-study")]:
        check(f"metadata '{field}' persisted", row.get(field) == expected,
              f"{row.get(field)!r}")
    check("recorded_by identifies the writer", row.get("recorded_by") == "eeg-mcp")


def test_neuro_mcp_compatibility() -> None:
    """The claim that matters: neuro-mcp can read what eeg-mcp wrote."""
    print("\n[compat] schema against neuro-mcp")
    from sqlalchemy import create_engine, inspect

    from eeg_mcp.db.models import Base as EegBase

    url = os.environ["EEG_MCP_DATABASE_URL"]
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    expected = {"subjects", "datasets", "recordings", "annotations", "audit_log"}
    check("expected tables created", expected <= tables, str(sorted(tables)))

    try:
        from neuro_mcp.db.models import Base as NeuroBase
    except ImportError:
        print("  SKIP  column-level schema diff (neuro-mcp not installed here)")
        print("        Install it alongside to enable this check.")
        return

    mismatches = []
    for name, neuro_table in NeuroBase.metadata.tables.items():
        eeg_table = EegBase.metadata.tables.get(name)
        if eeg_table is None:
            mismatches.append(f"{name}: missing in eeg-mcp")
            continue
        neuro_cols = {c.name: str(c.type) for c in neuro_table.columns}
        eeg_cols = {c.name: str(c.type) for c in eeg_table.columns}
        if neuro_cols != eeg_cols:
            diff = set(neuro_cols.items()) ^ set(eeg_cols.items())
            mismatches.append(f"{name}: {sorted(d[0] for d in diff)}")
    check("eeg-mcp schema matches neuro-mcp column-for-column",
          not mismatches, "; ".join(mismatches[:3]))


async def test_replay_what_we_recorded(client: Client, stopped: dict) -> None:
    """The loop closes: a recording made here can be replayed here."""
    print("\n[loop] replay the file we just recorded")
    info = await call(client, "inspect_recording", path=stopped["fif_path"])
    check("our own recording reopens for replay",
          info["n_channels"] == stopped["n_channels"])
    check("its annotations are visible to replay",
          info["n_events_in_file"] >= 2, str(info["n_events_in_file"]))

    await call(client, "start_replay", session_id="loop",
               path=stopped["fif_path"], speed=4.0)
    time.sleep(2.0)
    events = await call(client, "get_events", session_id="loop", origin="annotation")
    check("recorded events re-emit on replay", events["n_returned"] >= 1,
          str(events["n_returned"]))
    await call(client, "stop_stream", session_id="loop")


async def test_guards(client: Client) -> None:
    print("\n[guards] refusing the wrong thing")
    # BrainFlow permits only one session per board id, so the earlier synthetic
    # stream has to be released before this section opens its own.
    await call(client, "stop_stream", session_id="rec")

    for name, (tool, args), needle in [
        ("recording a session that does not exist",
         ("start_recording", {"session_id": "nope"}), "No session"),
        ("stopping a recording that never started",
         ("stop_recording", {"session_id": "guard"}), "not recording"),
        ("recovering a header that is not there",
         ("recover_recording", {"header_path": "no_such.eegmcp.json"}), "No recording"),
    ]:
        if tool == "stop_recording":
            await call(client, "start_stream", session_id="guard", board="synthetic")
        try:
            await call(client, tool, **args)
            check(f"{name} is rejected", False, "no error raised")
        except Exception as exc:
            check(f"{name} is rejected", needle in str(exc), str(exc)[:70])

    await call(client, "start_recording", session_id="guard", name="dup")
    try:
        await call(client, "start_recording", session_id="guard", name="dup2")
        check("double recording is rejected", False, "no error raised")
    except Exception as exc:
        check("double recording is rejected", "already recording" in str(exc),
              str(exc)[:70])

    # Stopping the stream must finalise rather than abandon the recording.
    # Wait for samples first: a zero-sample recording correctly writes no file.
    time.sleep(1.2)
    await call(client, "stop_stream", session_id="guard")
    written = list(Path(os.environ["EEG_MCP_RECORD_DIR"]).glob("dup_raw.fif"))
    check("stopping the stream finalises an open recording", len(written) == 1,
          str([p.name for p in written]))


async def main() -> int:
    write_synthetic(Path(_TMP) / "data")
    print(f"record dir: {os.environ['EEG_MCP_RECORD_DIR']}")
    print(f"database:   {os.environ['EEG_MCP_DATABASE_URL']}")

    async with Client(mcp) as client:
        try:
            stopped = await test_record_roundtrip(client)
            test_file_contents(stopped)
            await test_registration(client, stopped)
            test_neuro_mcp_compatibility()
            await test_replay_what_we_recorded(client, stopped)
            await test_guards(client)
        finally:
            for sid in ("rec", "loop", "guard"):
                try:
                    await call(client, "stop_stream", session_id=sid)
                except Exception:
                    pass

    print(f"\n{'=' * 56}\n  RECORDING: {PASSED} passed, {FAILED} failed\n{'=' * 56}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
