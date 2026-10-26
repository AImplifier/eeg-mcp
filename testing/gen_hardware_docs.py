"""Generate docs/hardware.md from BrainFlow itself.

Transcribing a 65-board table by hand guarantees it is wrong by the next
BrainFlow release. This asks the installed library what it supports and writes
the table, so regenerating after a dependency bump is one command.

Run:  python testing/gen_hardware_docs.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from brainflow.board_shim import BoardIds, BoardShim

BoardShim.disable_board_logger()

# Board-name prefix -> the vendor a user would recognise. Order matters: the
# first matching prefix wins, so longer/more specific prefixes come first.
VENDORS: list[tuple[tuple[str, ...], str]] = [
    (("CYTON", "GANGLION"), "OpenBCI"),
    (("MUSE",), "InteraXon Muse"),
    (("ANT_NEURO",), "ANT Neuro"),
    (("EXPLORE",), "Mentalab Explore"),
    (("BRAINBIT", "CALLIBRI"), "BrainBit / Neurosity Callibri"),
    (("CROWN", "NOTION"), "Neurosity"),
    (("ENOPHONE",), "Enophone"),
    (("GFORCE",), "OYMotion gForce"),
    (("UNICORN",), "g.tec Unicorn"),
    (("FREEEEG",), "FreeEEG"),
    (("PIEEG",), "PiEEG"),
    (("SYNCHRONI",), "Synchroni"),
    (("OB3000", "OB5000"), "OpenBCI OB-series"),
    (("NEUROPAWN",), "NeuroPawn"),
    (("BRAINALIVE",), "BrainAlive"),
    (("AAVAA",), "AAVAA"),
    (("GALEA",), "OpenBCI Galea"),
    (("IRONBCI",), "IronBCI"),
    (("NTL",), "NTL"),
    (("BIOLISTENER",), "BioListener"),
    (("EMOTIBIT",), "EmotiBit"),
    (("SYNTHETIC", "PLAYBACK", "STREAMING", "NO_BOARD"), "No hardware required"),
]

# Which BrainFlowInputParams field each transport needs, inferred from the
# board family. This is the question every first-time user actually has.
CONNECTION_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("SYNTHETIC", "NO_BOARD"), "none"),
    (("PLAYBACK",), "`file`, `master_board`"),
    (("STREAMING",), "`ip_address`, `ip_port`, `master_board`"),
    (("MUSE_2016_BLED", "MUSE_2_BLED", "MUSE_S_BLED"), "`serial_port` (BLED112)"),
    (("MUSE", "BRAINBIT", "CALLIBRI", "CROWN", "NOTION", "ENOPHONE", "GFORCE",
      "BRAINALIVE", "AAVAA"), "`mac_address` or `serial_number` (BLE)"),
    (("CYTON_WIFI", "GANGLION_WIFI", "CYTON_DAISY_WIFI", "NTL"),
     "`ip_address`, `ip_port`"),
    (("ANT_NEURO", "UNICORN", "EMOTIBIT", "GALEA", "EXPLORE"),
     "`ip_address` or auto-discovery"),
]


def vendor_for(name: str) -> str:
    for prefixes, vendor in VENDORS:
        if name.startswith(prefixes):
            return vendor
    return "Other"


def connection_for(name: str) -> str:
    for prefixes, hint in CONNECTION_HINTS:
        if name.startswith(prefixes):
            return hint
    return "`serial_port`"


def describe(board_id: int) -> dict:
    try:
        return json.loads(json.dumps(BoardShim.get_board_descr(board_id)))
    except Exception:
        return {}


def main() -> int:
    rows = defaultdict(list)
    total = 0
    for name, member in BoardIds.__members__.items():
        board_id = int(member.value)
        descr = describe(board_id)
        sfreq = descr.get("sampling_rate")
        eeg = descr.get("eeg_channels") or []
        rows[vendor_for(name)].append({
            "name": name,
            "board_id": board_id,
            "sfreq": sfreq,
            "n_eeg": len(eeg) if eeg else None,
            "connection": connection_for(name),
        })
        total += 1

    lines: list[str] = [
        "# Supported hardware",
        "",
        "!!! info \"Generated, not transcribed\"",
        "    This page is produced by `testing/gen_hardware_docs.py` from the "
        "installed BrainFlow. Regenerate it after a BrainFlow upgrade rather "
        "than editing it by hand.",
        "",
        "## EEG acquisition hardware",
        "",
        f"eeg-mcp acquires through [BrainFlow]"
        f"(https://brainflow.readthedocs.io/), so it supports **{total} board "
        "identifiers** across the vendors below. Pass the board by name to "
        "`start_stream` — `board=\"cyton\"`, `board=\"muse_2\"` — or by numeric id.",
        "",
        "`sfreq` and channel counts come from BrainFlow's own board "
        "description. A blank means the board has no static description "
        "(usually because it needs a `master_board`).",
        "",
    ]

    order = [v for _p, v in VENDORS] + ["Other"]
    seen: set[str] = set()
    for vendor in order:
        if vendor in seen or vendor not in rows:
            continue
        seen.add(vendor)
        entries = sorted(rows[vendor], key=lambda e: e["board_id"])
        lines.append(f"### {vendor}")
        lines.append("")
        lines.append("| Board name | id | Sampling rate | EEG ch | Connection via |")
        lines.append("|---|---:|---:|---:|---|")
        for e in entries:
            sfreq = f"{e['sfreq']} Hz" if e["sfreq"] else "—"
            n_eeg = str(e["n_eeg"]) if e["n_eeg"] else "—"
            lines.append(
                f"| `{e['name']}` | {e['board_id']} | {sfreq} | {n_eeg} | "
                f"{e['connection']} |"
            )
        lines.append("")

    lines += [
        "### Boards that need no hardware",
        "",
        "Three entries above are worth calling out, because they are how you "
        "develop and test without a rig:",
        "",
        "- **`SYNTHETIC_BOARD`** (id -1) — generates a signal immediately. The "
        "default for `start_stream`.",
        "- **`PLAYBACK_FILE_BOARD`** (id -3) — replays a BrainFlow CSV at true "
        "rate. Set `file` and `master_board`. For other formats, or if you need "
        "speed control and seeking, use `start_replay` instead.",
        "- **`STREAMING_BOARD`** (id -2) — attaches to another BrainFlow "
        "process streaming over the network.",
        "",
        "## Acquiring via LSL instead of BrainFlow",
        "",
        "Some boards are blocked from connecting through BrainFlow on some "
        "setups even when confirmed powered on and advertising. The board most "
        "often hit by this is Muse on Windows: BrainFlow's BLE scanner "
        "(SimpleBLE) has a longstanding bug in its Windows Python bindings "
        "([simpleble/simpleble#31]"
        "(https://github.com/simpleble/simpleble/issues/31), also tracked in "
        "[brainflow-dev/brainflow#393]"
        "(https://github.com/brainflow-dev/brainflow/issues/393)) that keeps "
        "`prepare_session()` from finding the device at all. Tools built on a "
        "different BLE binding — `muselsl` uses `bleak` — do not hit this bug.",
        "",
        "If a board is otherwise unreachable, `start_stream(board=\"lsl\")` "
        "attaches to an *existing* LSL outlet instead of having BrainFlow open "
        "the device itself:",
        "",
        "```python",
        "# In a separate terminal: muselsl stream   (uses bleak, not SimpleBLE)",
        "start_stream(session_id=\"s1\", board=\"lsl\", params={\"stream_type\": \"EEG\"})",
        "```",
        "",
        "Everything downstream — filtering, epoching, recording, stimulation — "
        "runs unchanged; only acquisition is rerouted. This needs a "
        "BLE-to-LSL bridge already running (`muselsl stream`, PetalStreamer, "
        "or similar) and the `lsl` extra: `pip install eeg-mcp[lsl]`. See "
        "[Acquisition tools](tools/acquisition.md#start_stream) for the full "
        "`params` reference.",
        "",
        "This path is not specific to Muse: any device that publishes an LSL "
        "outlet can be attached the same way.",
        "",
        "Bluetooth links drop, especially when several sensor streams share one "
        "connection, so the LSL source expects it: if the outlet goes quiet it "
        "marks the session disconnected, reattaches on its own once the bridge "
        "is back, and logs a `source_reconnected` event carrying the gap length "
        "rather than leaving a silent hole in the recording. See "
        "[What happens when the bridge drops]"
        "(tools/acquisition.md#what-happens-when-the-bridge-drops).",
        "",
        "## Recording formats for replay",
        "",
        "`start_replay` reads more than BrainFlow's own CSV, because MNE does "
        "the file handling:",
        "",
        "| Format | Extension | Events source |",
        "|---|---|---|",
        "| European Data Format | `.edf` | annotations + trigger channel |",
        "| BioSemi | `.bdf` | annotations + trigger channel |",
        "| General Data Format | `.gdf` | annotations + trigger channel |",
        "| EEGLAB | `.set` | annotations |",
        "| MNE / Elekta / Neuromag | `.fif` | annotations + STI channels |",
        "| BrainVision | `.vhdr` | marker file |",
        "| Nihon Kohden | `.eeg` | annotations |",
        "| Persyst | `.lay` | annotations |",
        "| BrainFlow CSV | `.csv` | marker channel (needs `board=`) |",
        "",
        "Anything else [MNE can read](https://mne.tools/stable/reading_raw_data.html) "
        "will also load; the table lists what is routinely exercised.",
        "",
        "## Stimulation hardware",
        "",
        "!!! danger \"Read this before connecting a stimulator\"",
        "    The hardware backends are **generic transports driven by command "
        "templates you supply from your device's manual — not vendor drivers**, "
        "and none has been validated against a specific commercial stimulator. "
        "Shipping a plausible-looking driver that had never been tested against "
        "the real device would be worse than shipping none, because it would "
        "fail silently while connected to something pointed at a person's head.",
        "",
        "    Use only under a protocol approved by your ethics board, on a rig "
        "whose device-level interlocks are intact, with a trained operator "
        "present. This is not a medical device.",
        "",
        "### What each backend can talk to",
        "",
        "| Backend | Hardware? | Transport | Typically used for |",
        "|---|---|---|---|",
        "| `log` | no | none | Dry runs. Accepts everything, emits nothing. |",
        "| `lsl` | no | LSL marker outlet | PsychoPy, OpenViBE, LabRecorder, "
        "any LSL consumer |",
        "| `brainflow_marker` | no | board marker channel | Sample-aligned "
        "marking inside the recording itself |",
        "| `serial_ttl` | **yes** | serial byte | Trigger boxes, stimulator "
        "trigger inputs, TTL adapters |",
        "| `template_serial` | **yes** | ASCII serial | Any device with a "
        "documented text protocol |",
        "| `tms` | **yes** | ASCII serial + trigger | TMS stimulators; "
        "intensity in %MSO |",
        "| `tes` | **yes** | ASCII serial | tDCS/tACS/tRNS; intensity in mA, "
        "default ceiling 4 mA |",
        "",
        "### Devices these transports are designed to reach",
        "",
        "Listed as *transport compatibility*, not certified support. If your "
        "device accepts TTL triggers on a serial line or exposes an ASCII "
        "command protocol, it fits one of the backends above.",
        "",
        "| Class | Examples | Backend | How it is driven |",
        "|---|---|---|---|",
        "| TMS stimulators | Magstim, MagVenture, Deymed, Nexstim | `tms` | "
        "Intensity set over serial (device protocol), pulse fired by trigger |",
        "| tES stimulators | neuroConn DC-Stimulator, Soterix, "
        "Neuroelectrics Starstim, BrainStim | `tes` | Current and ramp over "
        "serial; many also accept a TTL start |",
        "| Trigger boxes | BrainProducts TriggerBox, Cedrus StimTracker, "
        "LabJack, custom Arduino | `serial_ttl` | One byte per event code |",
        "| Presentation software | PsychoPy, OpenViBE, E-Prime, Presentation | "
        "`lsl` | Marker stream over the network |",
        "",
        "Vendor names identify the device class the transport targets. They do "
        "**not** indicate testing, endorsement, or any relationship with those "
        "manufacturers. You supply the command strings from your manual:",
        "",
        "```python",
        'attach_stim_backend(session_id="s1", backend="tms", options={',
        '    "port": "COM3",',
        '    "templates": {',
        '        "fire": "TRIG\\r\\n",',
        '        "set_intensity": "AMP {intensity:.0f}\\r\\n",',
        "    },",
        "})",
        "```",
        "",
        "### Safety gates on every hardware backend",
        "",
        "1. **Config** — refuses to open unless the server was started with "
        "`EEG_MCP_ALLOW_HARDWARE_STIM=1`.",
        "2. **Arming** — `arm_stim` grants a bounded window that expires, so a "
        "stalled agent cannot resume and fire minutes later.",
        "3. **Limits** — intensity, duration and inter-stimulus interval are "
        "clamped against configured ceilings; violations raise rather than "
        "saturate.",
        "",
        "None of this replaces the interlocks on the device itself.",
        "",
    ]

    target = Path(__file__).resolve().parent.parent / "docs" / "hardware.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {target} ({total} boards, {len(seen)} vendor groups)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
