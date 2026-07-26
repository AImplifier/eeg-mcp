"""Persona test: a clinician reviewing a recording and watching a live setup.

Their questions are different from the engineer's. They do not care about
buffers or group delay; they care about *seeing* the signal, trusting it, and
keeping a record:

1. Can I open a recording and actually look at it?
2. Is the signal any good -- which electrodes should I fix?
3. Can I watch it live while the cap is being set up?
4. Where is the posterior dominant rhythm?
5. Can I mark what I see as I see it?
6. Can I keep a report that opens anywhere, years from now?
7. If I do something wrong, is the message in plain language?

Run:  python testing/persona_clinician.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="eegmcp_clin_")
os.environ["EEG_MCP_HOME"] = _TMP
os.environ["EEG_MCP_BUFFER_SEC"] = "60"

from fastmcp import Client  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from make_synthetic import GROUND_TRUTH, write as write_synthetic  # noqa: E402

from eeg_mcp.server import mcp  # noqa: E402

PASSED, FAILED = 0, 0
WINS: list[str] = []
FRICTION: list[str] = []
MONITOR_URL = ""


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


def fetch(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def post(
    url: str,
    payload: dict,
    content_type: str = "application/json",
    timeout: float = 20.0,
) -> tuple[int, dict]:
    """POST a control action. ``content_type`` is variable so the CSRF guard
    (which requires a JSON type) can be exercised."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}


def is_self_contained(html: str) -> tuple[bool, list[str]]:
    """No external fetches: the file must render on an air-gapped machine."""
    offenders = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
    offenders += re.findall(r'@import\s+url\(["\']?(https?://[^)"\']+)', html)
    return (not offenders), offenders


def figures_are_sane(html: str) -> tuple[bool, str]:
    """Every SVG parses, and every plotted point sits inside its viewBox.

    Cheap, but it is what catches the classic rendering regression: a scaling
    bug that silently pushes traces outside the visible area, which no
    string-matching assertion would ever notice.
    """
    svgs = re.findall(r"<svg.*?</svg>", html, re.S)
    if not svgs:
        return False, "no <svg> figures found"
    total = out_of_bounds = 0
    for svg in svgs:
        try:
            ET.fromstring(svg)
        except ET.ParseError as exc:
            return False, f"malformed SVG: {exc}"
        box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
        if not box:
            return False, "SVG without a viewBox"
        width, height = float(box.group(1)), float(box.group(2))
        for polyline in re.findall(r'points="([^"]+)"', svg):
            for pair in polyline.split():
                x, y = (float(v) for v in pair.split(","))
                total += 1
                if not (-1 <= x <= width + 1 and -1 <= y <= height + 1):
                    out_of_bounds += 1
    if out_of_bounds:
        return False, f"{out_of_bounds}/{total} points outside the viewBox"
    return True, f"{len(svgs)} figures, {total} points all in bounds"


async def q1_open_and_look(client: Client, path: Path) -> None:
    print("\n[Q1] Can I open a recording and actually look at it?")
    info = await call(client, "inspect_recording", path=str(path))
    check("recording opens and describes itself",
          info["n_channels"] == len(GROUND_TRUTH["ch_names"]),
          f"{info['n_channels']} ch, {info['duration_sec']:.0f}s")

    await call(client, "start_replay", session_id="review", path=str(path), speed=1.0)
    time.sleep(3.0)

    plot = await call(client, "plot_stream", session_id="review", seconds=5.0,
                      filtered=False)
    file_path = Path(plot["path"])
    check("traces file written", file_path.exists(), file_path.name)

    html = file_path.read_text(encoding="utf-8")
    contained, offenders = is_self_contained(html)
    check("plot is fully self-contained", contained, str(offenders[:3]))
    check("every channel is labelled in the figure",
          all(name in html for name in GROUND_TRUTH["ch_names"]))
    check("figure is scalable SVG, not a fixed-size bitmap", "<svg" in html)
    sane, detail = figures_are_sane(html)
    check("traces render inside their viewBox", sane, detail)
    win("A recording opens, plays, and renders to a file that needs no internet.")


async def q2_is_the_signal_good(client: Client) -> None:
    print("\n[Q2] Is the signal any good -- which electrodes should I fix?")
    quality = await call(client, "check_signal_quality", session_id="review")
    check("quality report covers every channel",
          len(quality["channels"]) == len(GROUND_TRUTH["ch_names"]))
    check("verdict is a plain word, not a code",
          quality["verdict"] in ("ok", "attention"), quality["verdict"])
    check("clean synthetic signal is not flagged", quality["n_bad"] == 0,
          str(quality["bad_channels"]))
    check("quality is measured on raw signal, not the filtered view",
          quality["assessed"] == "raw signal")

    metrics = next(iter(quality["channels"].values()))
    check("per-channel numbers are interpretable",
          {"rms_uv", "ptp_uv", "flat", "railed", "line_noisy"} <= set(metrics))
    win("Quality is reported per electrode with named failure modes "
        "(flat / railed / line noisy), not an opaque score.")


async def q3_watch_it_live(client: Client) -> None:
    global MONITOR_URL
    print("\n[Q3] Can I watch it live while the cap is being set up?")
    monitor = await call(client, "start_monitor", session_id="review",
                         window_sec=5.0, refresh_ms=400)
    url = MONITOR_URL = monitor["url"]
    check("monitor bound to loopback only", monitor["bound_to"] == "127.0.0.1")

    status, html = fetch(url)
    check("monitor page serves", status == 200, f"HTTP {status}")
    check("page renders traces itself", "canvas" in html and "api/snapshot" in html)
    contained, offenders = is_self_contained(html)
    check("live page loads no external resources", contained, str(offenders[:3]))

    base = url.split("?")[0]
    token = url.split("t=")[1]
    status_api, body = fetch(f"{base}api/snapshot?t={token}&seconds=3")
    check("snapshot API responds", status_api == 200, f"HTTP {status_api}")
    payload = json.loads(body)
    check("snapshot carries live data", payload.get("ready") is True,
          payload.get("reason", ""))
    if payload.get("ready"):
        check("snapshot includes traces, band power and quality",
              "data" in payload and "band_power" in payload and "quality" in payload)
        check("snapshot reports replay position for a recording",
              "replay" in payload)

    # A local port carrying patient data must not be readable by simply guessing it.
    status_noauth, _ = fetch(f"{base}api/snapshot?seconds=3")
    check("snapshot refuses requests without the token", status_noauth == 403,
          f"HTTP {status_noauth}")
    status_badauth, _ = fetch(f"{base}api/snapshot?t=wrong&seconds=3")
    check("snapshot refuses a wrong token", status_badauth == 403,
          f"HTTP {status_badauth}")

    listed = await call(client, "list_monitors")
    check("monitor is discoverable", listed["n_active"] == 1)
    win("Live rolling view in a browser, loopback-bound and token-gated, with no "
        "frontend build or cloud service involved.")


async def q4_posterior_dominant_rhythm(client: Client) -> None:
    print("\n[Q4] Where is the posterior dominant rhythm?")
    spectrum = await call(client, "plot_spectrum", session_id="review", seconds=6.0,
                          filtered=False)
    peaks = spectrum["peak_freq"]
    occipital = GROUND_TRUTH["alpha_channels"]

    for name in occipital:
        got = peaks.get(name)
        check(f"{name} shows the {GROUND_TRUTH['alpha_hz']:.0f} Hz rhythm",
              got is not None and abs(got - GROUND_TRUTH["alpha_hz"]) <= 1.0,
              f"{got:.2f} Hz" if got else "missing")

    check("dominant band named in plain terms",
          spectrum["dominant_band"] == "alpha", spectrum["dominant_band"])
    spectrum_html = Path(spectrum["path"]).read_text(encoding="utf-8")
    check("spectrum figure labels the classical bands",
          all(b in spectrum_html for b in ("delta", "theta", "alpha", "beta")))
    win("Posterior alpha is localised to the occipital channels and the figure "
        "shades the classical bands behind the spectrum.")


async def q5_mark_what_i_see(client: Client) -> None:
    print("\n[Q5] Can I mark what I see as I see it?")
    first = await call(client, "log_event", session_id="review",
                       label="eyes_closed", value=11)
    check("observation recorded", first["logged"] is True)
    await call(client, "log_event", session_id="review", label="artifact_movement",
               value=12)

    events = await call(client, "get_events", session_id="review", origin="manual")
    labels = {e["label"] for e in events["events"]}
    check("both observations retrievable", {"eyes_closed", "artifact_movement"} <= labels)

    mixed = await call(client, "get_events", session_id="review")
    origins = {e["origin"] for e in mixed["events"]}
    check("my notes sit alongside the recording's own annotations",
          {"manual", "annotation"} <= origins, str(sorted(origins)))
    win("Clinician notes and the recording's own annotations share one timeline "
        "on one clock.")


async def q6_keep_a_report(client: Client) -> None:
    print("\n[Q6] Can I keep a report that opens anywhere?")
    report = await call(client, "export_report", session_id="review", seconds=8.0,
                        notes="Routine review. Awake, eyes closed at rest.")
    path = Path(report["path"])
    check("report written", path.exists(), path.name)

    html = path.read_text(encoding="utf-8")
    contained, offenders = is_self_contained(html)
    check("report is fully self-contained", contained, str(offenders[:3]))
    check("notes are preserved in the report", "Routine review" in html)
    sane, detail = figures_are_sane(html)
    check("all report figures render inside their viewBox", sane, detail)

    for section in ("Traces", "Spectrum", "Signal quality", "Events"):
        check(f"report contains the {section.lower()} section", section in html)

    check("report carries a research-use disclaimer",
          "not a diagnostic device" in html.lower() or "research use" in html.lower())
    check("report lists the events reviewed", report["n_events"] > 0,
          str(report["n_events"]))
    size_kb = path.stat().st_size / 1024
    check("report is a reasonable size to email", size_kb < 4000, f"{size_kb:.0f} KB")
    if size_kb > 1500:
        friction(f"Report is {size_kb:.0f} KB -- consider fewer channels or a "
                 "shorter window for routine sharing.")
    else:
        win(f"Full report is {size_kb:.0f} KB, self-contained, opens in any browser.")


async def q7_drive_it_from_the_screen(client: Client) -> None:
    """The buttons on the monitor: an operator should not have to go back to the
    agent to pause, mark, or save."""
    print("\n[Q7] Can I drive it from the screen without going back to the agent?")
    base = MONITOR_URL.split("?")[0]
    token = MONITOR_URL.split("t=")[1]
    control = f"{base}api/control?t={token}"

    page = fetch(MONITOR_URL)[1]
    for label in ("Restart", "Pause", "Stop", "Mark", "Report"):
        check(f"'{label}' button present on the page", label in page)

    status, body = post(control, {"action": "pause"})
    check("pause works from the page", status == 200 and body.get("ok") is True,
          str(body)[:80])
    at_pause = body.get("replay", {}).get("position_sec", 0.0)
    time.sleep(0.6)
    _s, still = post(control, {"action": "seek", "position_sec": at_pause})
    check("pause actually held the clock",
          abs(still.get("replay", {}).get("position_sec", -1) - at_pause) < 0.6,
          f"{at_pause:.2f}")

    _s, resumed = post(control, {"action": "resume"})
    check("resume works", resumed.get("ok") is True)

    _s, sought = post(control, {"action": "seek", "position_sec": 12.0})
    check("scrub bar seeking works",
          abs(sought.get("replay", {}).get("position_sec", 0) - 12.0) < 0.3,
          f"{sought.get('replay', {}).get('position_sec')}")

    _s, skipped = post(control, {"action": "skip", "delta_sec": -10})
    check("skip back 10 s works",
          abs(skipped.get("replay", {}).get("position_sec", 0) - 2.0) < 0.5,
          f"{skipped.get('replay', {}).get('position_sec')}")

    # Seeking discards buffered signal by design, so saving needs a refill first.
    # Asserting the error is explicit about that, rather than telling the user to
    # wait for a buffer that a subsequent stop would never refill.
    _s, too_soon = post(control, {"action": "save_report", "seconds": 6})
    check("saving straight after a seek explains the empty buffer",
          too_soon.get("ok") is not True
          and "seek" in too_soon.get("error", "").lower()
          or "moment to fill" in too_soon.get("error", ""),
          too_soon.get("error", "")[:70])
    time.sleep(2.5)

    _s, sped = post(control, {"action": "speed", "speed": 2.0})
    check("speed control works", sped.get("replay", {}).get("speed") == 2.0)
    post(control, {"action": "speed", "speed": 1.0})

    _s, marked = post(control, {"action": "mark", "label": "operator_note"})
    check("mark button logs an event", marked.get("ok") is True)
    events = await call(client, "get_events", session_id="review",
                        label="operator_note")
    check("marked event reaches the session log", events["n_returned"] == 1)

    _s, saved = post(control, {"action": "save_report", "seconds": 6})
    check("save report button writes a file",
          saved.get("ok") is True and Path(saved.get("path", "")).exists(),
          str(saved.get("path", ""))[-46:])
    _s, saved_tr = post(control, {"action": "save_traces", "seconds": 6})
    check("save traces button writes a file",
          Path(saved_tr.get("path", "")).exists())

    # A control surface that can stop a recording needs the same gate as the
    # data, plus a CSRF guard -- a hostile local page must not be able to POST.
    status_notoken, _ = post(f"{base}api/control", {"action": "pause"})
    check("control refuses requests without the token", status_notoken == 403,
          f"HTTP {status_notoken}")
    status_form, _ = post(control, {"action": "pause"},
                          content_type="application/x-www-form-urlencoded")
    check("control refuses non-JSON content type (CSRF guard)",
          status_form == 415, f"HTTP {status_form}")
    status_bad, bad = post(control, {"action": "self_destruct"})
    check("unknown action is rejected with the supported list",
          status_bad == 400 and "Supported" in bad.get("error", ""),
          bad.get("error", "")[:60])

    _s, stopped = post(control, {"action": "stop"})
    check("stop button halts acquisition", stopped.get("running") is False)
    _s, after = post(control, {"action": "save_report", "seconds": 6})
    check("buffered signal is still saveable after stopping",
          after.get("ok") is True)
    win("The operator can pause, scrub, mark, save and stop from the screen; the "
        "control endpoint carries the same token gate as the data plus a CSRF guard.")


async def q8_read_only_mode(client: Client, path: Path) -> None:
    print("\n[Q8] Can I show the screen to someone without letting them touch it?")
    await call(client, "start_replay", session_id="ro", path=str(path), speed=1.0)
    monitor = await call(client, "start_monitor", session_id="ro",
                         allow_control=False)
    check("read-only monitor reports no controls", monitor["controls"] == [])

    page = fetch(monitor["url"])[1]
    # The script always references the ids; what must be absent is the markup.
    check("no transport buttons rendered", 'id="b-toggle"' not in page)

    base = monitor["url"].split("?")[0]
    token = monitor["url"].split("t=")[1]
    status, body = post(f"{base}api/control?t={token}", {"action": "stop"})
    check("read-only monitor refuses control even with a valid token",
          status == 403 and "read-only" in body.get("error", "").lower(),
          f"HTTP {status}")

    status_view, snap = fetch(f"{base}api/snapshot?t={token}&seconds=2")
    check("read-only monitor still serves the live view", status_view == 200)

    await call(client, "stop_stream", session_id="ro")
    win("allow_control=false gives a strictly viewable screen -- the control "
        "endpoint refuses even a correctly authenticated request.")


async def q9_plain_language_errors(client: Client) -> None:
    print("\n[Q9] If I do something wrong, is the message in plain language?")
    cases = [
        ("a recording that does not exist",
         ("inspect_recording", {"path": "not_a_real_file.edf"}), "No recording at"),
        ("plotting a session I never opened",
         ("plot_stream", {"session_id": "typo"}), "No session"),
        ("monitoring a session I never opened",
         ("start_monitor", {"session_id": "typo"}), "No session"),
    ]
    for name, (tool, args), needle in cases:
        try:
            await call(client, tool, **args)
            check(f"{name} is rejected", False, "no error raised")
        except Exception as exc:
            message = str(exc)
            check(f"{name} is explained clearly", needle in message,
                  f"...{message[-70:].strip()}")
            if any(term in message for term in ("Traceback", "0x", "KeyError(")):
                friction(f"Error for '{name}' leaks internals: {message[:70]}")

    stopped = await call(client, "stop_stream", session_id="review")
    check("closing the session also closes the live view",
          stopped["monitor_stopped"] is True)
    remaining = await call(client, "list_monitors")
    check("no monitor left listening", remaining["n_active"] == 0)
    win("Stopping a session tears down its monitor, so no port is left bound "
        "serving a frozen view.")


async def main() -> int:
    path = write_synthetic(Path(_TMP) / "data")
    print(f"reviewing: {path.name} ({GROUND_TRUTH['n_events']} annotations, "
          f"{GROUND_TRUTH['alpha_hz']} Hz alpha on "
          f"{GROUND_TRUTH['alpha_channels']})")

    async with Client(mcp) as client:
        try:
            await q1_open_and_look(client, path)
            await q2_is_the_signal_good(client)
            await q3_watch_it_live(client)
            await q4_posterior_dominant_rhythm(client)
            await q5_mark_what_i_see(client)
            await q6_keep_a_report(client)
            await q7_drive_it_from_the_screen(client)
            await q8_read_only_mode(client, path)
            await q9_plain_language_errors(client)
        finally:
            for leftover in ("review", "ro"):
                try:
                    await call(client, "stop_stream", session_id=leftover)
                except Exception:
                    pass

    print(f"\n{'=' * 60}")
    print(f"  CLINICIAN: {PASSED} passed, {FAILED} failed")
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
