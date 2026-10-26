"""A local live view of a running session, served over loopback HTTP.

A static plot cannot answer "is the signal good *right now*", which is the
question a clinician or an operator setting up a cap actually has. So this
serves a small page that polls the session and redraws, giving a real rolling
traces view without any of the machinery (websockets, a frontend build, a cloud
service) that would make it a burden to run.

The page can also *drive* the session -- play/pause, restart, skip, speed, stop,
mark an event, save a report -- so an operator watching the screen does not have
to go back to the agent for every action. That is opt-out via
``allow_control=False`` when the screen is being shown to someone who should
only watch.

Security posture, deliberately conservative because this can carry patient data
and, with controls enabled, can stop a recording:

* binds to **127.0.0.1 only** -- never reachable from the network;
* every request requires a per-monitor random token, so other local users
  cannot simply guess the port;
* control requests must be POST with a JSON content type, which forces a CORS
  preflight that another origin cannot satisfy -- so a hostile local page
  cannot drive the session even by guessing the port;
* it is opt-in per session and dies with the process.

It is still an unencrypted local service. It is not an authentication system and
must not be exposed through a tunnel or reverse proxy.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from .dsp import band_powers, quality_metrics
from .utils import FREQ_BANDS, now, to_jsonable

# Points sent per channel per refresh. Enough for a readable trace, small enough
# that the JSON stays a few hundred KB at 32 channels.
_MAX_POINTS = 700
# Largest control body we will read, so a malformed request cannot exhaust memory.
_MAX_BODY = 64 * 1024
# Safety margin added to the events query's cutoff, on top of the trace
# window itself -- see _snapshot's "events" field for why this has to be
# generous rather than exact.
_EVENT_QUERY_PADDING_SEC = 10.0


def _snapshot(session: Any, seconds: float, filtered: bool) -> dict[str, Any]:
    """Everything the page needs for one frame."""
    info = session.source.info
    try:
        data, timestamps, was_filtered = session.snapshot(seconds, filtered=filtered)
    except ValueError as exc:
        return {"ready": False, "reason": str(exc), "running": session.is_running}

    step = max(1, int(np.ceil(data.shape[1] / _MAX_POINTS)))

    payload: dict[str, Any] = {
        "ready": True,
        "running": session.is_running,
        "exhausted": session.exhausted,
        "session_id": session.session_id,
        "channels": list(info.ch_names),
        "sfreq": info.sfreq,
        "unit": info.unit,
        "filtered": bool(was_filtered),
        "n_samples": int(data.shape[1]),
        "t_start": float(timestamps[0]),
        "t_end": float(timestamps[-1]),
        "data": data[:, ::step].tolist(),
        # Deviation from each channel's own window mean, not the raw peak
        # amplitude: a signal with a large DC offset relative to its swing --
        # an unfiltered accelerometer axis sitting at ~1 g, or a raw optical
        # channel with a six-figure baseline -- would otherwise divide by a
        # peak dominated by the offset and render as a flat line even though
        # real variation is there. EEG is typically near zero-mean already
        # (especially filtered), so this changes nothing for the common case.
        "chan_mean": [float(np.mean(row)) if row.size else 0.0 for row in data],
        "peak_dev": [
            float(np.max(np.abs(row - np.mean(row)))) if row.size else 0.0
            for row in data
        ],
        # Anchored to wall-clock now(), not the buffer's own timestamps[0]:
        # an LSL source's sample clock can drift from true wall time (a
        # bridge reconnect was observed to leave it running ~1.8s ahead, on
        # this same real hardware), and a viewer positioning these events by
        # its own wall-clock "now" would then see one vanish from this list
        # well before it actually aged out of its display window -- a
        # premature disappearance with no visible cause, since the position
        # math and the inclusion cutoff were using two different clocks. The
        # padding keeps this list a superset of what any real-time view
        # needs; the view's own timestamp comparison still does the exact
        # windowing, on one consistent (wall) clock.
        "events": [
            e.as_dict()
            for e in session.events.query(
                since=now() - seconds - _EVENT_QUERY_PADDING_SEC, limit=60
            )
        ],
        "buffered_sec": session.raw_buffer.n_available / info.sfreq,
        "throughput_ratio": session.status().get("throughput_ratio"),
        "n_events": len(session.events),
    }

    # Analytics are best-effort: a window too short for a PSD must not blank the
    # whole view, since the traces are the part that matters most.
    try:
        payload["band_power"] = band_powers(
            data, info.sfreq, ch_names=list(info.ch_names),
            bands=dict(FREQ_BANDS), relative=True,
        )
    except Exception as exc:
        payload["band_power_error"] = str(exc)
    try:
        raw_data, _ts, _f = session.snapshot(seconds, filtered=False)
        payload["quality"] = quality_metrics(
            raw_data, info.sfreq, ch_names=list(info.ch_names)
        )
    except Exception as exc:
        payload["quality_error"] = str(exc)

    if hasattr(session.source, "position"):
        payload["replay"] = session.source.position()
    return to_jsonable(payload)


def _seek(session: Any, position_sec: float) -> float:
    """Reposition a replay, discarding buffered signal from before the jump.

    Clearing is not optional: without it the next window would silently straddle
    the discontinuity, and the filter chain would carry state across a seam that
    does not exist in the recording.
    """
    actual = session.source.seek(position_sec)
    session.raw_buffer.clear()
    if session.filtered_buffer is not None:
        session.filtered_buffer.clear()
    session.chain.reset()
    return actual


def _control(session: Any, action: str, body: dict[str, Any]) -> dict[str, Any]:
    """Apply one control action. Raises ValueError with a readable message."""
    source = session.source
    is_replay = hasattr(source, "position")

    def require_replay() -> None:
        if not is_replay:
            raise ValueError(
                f"Playback controls apply only to replay sessions; "
                f"'{session.session_id}' is a live {source.info.kind} stream."
            )

    if action in ("pause", "resume", "toggle", "restart", "seek", "skip", "speed"):
        require_replay()

    if action == "pause":
        source.pause()
    elif action == "resume":
        source.resume()
    elif action == "toggle":
        if source.position().get("paused"):
            source.resume()
        else:
            source.pause()
    elif action == "restart":
        _seek(session, 0.0)
        source.resume()
        session.log("restarted from the beginning")
    elif action == "seek":
        _seek(session, float(body.get("position_sec", 0.0)))
    elif action == "skip":
        position = source.position()
        _seek(session, position["position_sec"] + float(body.get("delta_sec", 0.0)))
    elif action == "speed":
        source.set_speed(float(body.get("speed", 1.0)))
    elif action == "stop":
        # Stop the producer and release the device, but keep the session in the
        # store so the buffered signal can still be reviewed and saved.
        session.stop()
        session.log("stopped from the monitor")
    elif action == "mark":
        label = str(body.get("label") or "mark").strip() or "mark"
        value = float(body.get("value", 1.0))
        marked = False
        try:
            marked = bool(source.insert_marker(value, label))
        except Exception:
            marked = False
        record = session.events.append(
            label=label, origin="manual", timestamp=now(), value=value,
            sample_index=session.raw_buffer.total_written,
            payload={"marked_in_stream": marked, "via": "monitor"},
        )
        return {"ok": True, "message": f"Marked '{label}'",
                "event": record.as_dict(), "marked_in_stream": marked}
    elif action == "clear_events":
        count = len(session.events)
        session.events.clear()
        session.log(f"cleared {count} events from the monitor")
        return {"ok": True, "message": f"Cleared {count} events"}
    elif action in ("save_report", "save_traces", "save_spectrum"):
        # Imported lazily: reports pulls in the DSP stack, and deferring keeps
        # monitor import-light for the common read-only case.
        from .reports import build_report, build_spectrum, build_traces

        seconds = float(body.get("seconds", 12.0))
        filtered = bool(body.get("filtered", True))
        if action == "save_report":
            path, summary = build_report(
                session, seconds=seconds, notes=body.get("notes") or None
            )
            what = "Report"
        elif action == "save_traces":
            path, summary = build_traces(session, seconds=seconds, filtered=filtered)
            what = "Traces"
        else:
            path, summary = build_spectrum(session, seconds=seconds, filtered=filtered)
            what = "Spectrum"
        session.log(f"{what.lower()} saved from the monitor: {path.name}")
        return {"ok": True, "message": f"{what} saved", "path": str(path),
                "summary": summary}
    else:
        raise ValueError(
            f"Unknown action '{action}'. Supported: pause, resume, toggle, "
            "restart, seek, skip, speed, stop, mark, clear_events, save_report, "
            "save_traces, save_spectrum."
        )

    result: dict[str, Any] = {"ok": True, "message": action}
    if is_replay:
        result["replay"] = to_jsonable(source.position())
    result["running"] = session.is_running
    return result


class _Handler(BaseHTTPRequestHandler):
    server_version = "eeg-mcp-monitor"
    monitor: "Monitor"  # injected via the handler subclass

    def log_message(self, *args: Any) -> None:
        """Silence per-request logging; it would pollute the MCP stdio stream."""

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # This page must never fetch anything external, and nothing external
        # should be able to embed it.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _authorised(self, params: dict[str, list[str]]) -> bool:
        token = (params.get("t") or [""])[0]
        return secrets.compare_digest(token, self.monitor.token)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not self._authorised(params):
            self._send(403, b"forbidden: missing or invalid token", "text/plain")
            return

        if parsed.path in ("/", "/index.html"):
            self._send(200, self.monitor.page_html().encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        if parsed.path == "/api/snapshot":
            try:
                seconds = float((params.get("seconds") or ["5"])[0])
                filtered = (params.get("filtered") or ["1"])[0] not in ("0", "false")
                session = self.monitor.store.get(self.monitor.session_id)
                body = json.dumps(_snapshot(session, seconds, filtered))
            except Exception as exc:
                body = json.dumps({"ready": False, "reason": str(exc)})
            self._send(200, body.encode("utf-8"), "application/json")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not self._authorised(params):
            self._json(403, {"ok": False, "error": "missing or invalid token"})
            return
        if parsed.path != "/api/control":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self.monitor.allow_control:
            self._json(403, {
                "ok": False,
                "error": "This monitor is read-only (started with allow_control=false).",
            })
            return
        # Requiring a JSON content type forces a CORS preflight for cross-origin
        # callers, which this server never answers -- so a hostile local page
        # cannot drive the session even if it guessed the port and token.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"ok": False,
                             "error": "Content-Type must be application/json"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > _MAX_BODY:
            self._json(413, {"ok": False, "error": "request body too large"})
            return

        try:
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            session = self.monitor.store.get(self.monitor.session_id)
            self._json(200, _control(session, str(body.get("action", "")), body))
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})


class Monitor:
    """A loopback HTTP server showing (and optionally driving) one session."""

    def __init__(self, session_id: str, store: Any, port: int = 0,
                 window_sec: float = 5.0, refresh_ms: int = 500,
                 allow_control: bool = True) -> None:
        self.session_id = session_id
        self.store = store
        self.window_sec = float(window_sec)
        self.refresh_ms = int(refresh_ms)
        self.allow_control = bool(allow_control)
        self.token = secrets.token_urlsafe(18)

        handler = type("BoundHandler", (_Handler,), {"monitor": self})
        # Port 0 lets the OS pick a free port, which avoids collisions when
        # several sessions are monitored at once.
        self._server = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, name=f"eeg-mcp-monitor-{session_id}",
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def stop(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()

    def page_html(self) -> str:
        from .viz import PAGE_CSS

        return (
            _PAGE_TEMPLATE
            .replace("__CSS__", PAGE_CSS)
            .replace("__CONTROLS__", _CONTROLS_HTML if self.allow_control else "")
            .replace("__TOKEN__", self.token)
            .replace("__SESSION__", self.session_id)
            .replace("__WINDOW__", str(self.window_sec))
            .replace("__REFRESH__", str(self.refresh_ms))
            .replace("__CANCONTROL__", "true" if self.allow_control else "false")
        )


_CONTROLS_HTML = """
<div class="card ctl">
  <div class="ctl-row">
    <button id="b-restart" class="btn" title="Back to the beginning">&#9198; Restart</button>
    <button id="b-toggle" class="btn primary" title="Pause or resume playback">&#9208; Pause</button>
    <button id="b-back" class="btn" title="Back 10 seconds">&#8722;10s</button>
    <button id="b-fwd" class="btn" title="Forward 10 seconds">+10s</button>
    <label class="lbl">speed
      <select id="b-speed">
        <option value="0.25">0.25&times;</option>
        <option value="0.5">0.5&times;</option>
        <option value="1" selected>1&times;</option>
        <option value="2">2&times;</option>
        <option value="4">4&times;</option>
        <option value="8">8&times;</option>
      </select></label>
    <span class="spacer"></span>
    <button id="b-stop" class="btn danger" title="Stop acquisition and release the device">&#9209; Stop</button>
  </div>
  <div class="ctl-row" id="scrub-row">
    <span class="tc" id="tc-now">0.0s</span>
    <input type="range" id="scrub" min="0" max="1000" value="0" step="1">
    <span class="tc" id="tc-end">0.0s</span>
  </div>
  <div class="ctl-row">
    <input type="text" id="mark-label" placeholder="event label" value="mark">
    <button id="b-mark" class="btn" title="Log an event at this instant">&#128278; Mark</button>
    <span class="spacer"></span>
    <button id="b-report" class="btn" title="Save a full HTML report">&#128190; Report</button>
    <button id="b-traces" class="btn" title="Save the traces">&#128190; Traces</button>
    <button id="b-spectrum" class="btn" title="Save the spectrum">&#128190; Spectrum</button>
    <button id="b-clear" class="btn" title="Clear the event log">&#128465; Events</button>
  </div>
  <div id="toast" class="toast"></div>
</div>
"""


_PAGE_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>eeg-mcp live monitor</title><style>__CSS__
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.bar label,.lbl{color:var(--muted);font-size:12px}
.pill{padding:3px 9px;border-radius:999px;font-size:11px;font-weight:600;
  border:1px solid var(--line)}
.live{background:#27ae6022;color:#27ae60}.stale{background:#eb575722;color:#eb5757}
canvas{width:100%;display:block;border-radius:8px}
select,input[type=text]{background:var(--card);color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px}
input[type=text]{min-width:150px}
#bands{display:flex;flex-wrap:wrap;gap:6px}
.bp{font-size:11px;padding:3px 8px;border-radius:6px;background:var(--card);
  border:1px solid var(--line);font-variant-numeric:tabular-nums}
.ctl{position:sticky;top:0;z-index:5;backdrop-filter:blur(6px)}
.ctl-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.ctl-row:last-of-type{margin-bottom:0}
.spacer{flex:1}
.btn{background:var(--card);color:var(--fg);border:1px solid var(--line);
  border-radius:7px;padding:6px 12px;font-size:12px;font-weight:500;cursor:pointer;
  font-family:inherit;line-height:1.4}
.btn:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.btn:active:not(:disabled){transform:translateY(1px)}
.btn:disabled{opacity:.38;cursor:not-allowed}
.btn.primary{border-color:var(--accent);color:var(--accent)}
.btn.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
#scrub{flex:1;min-width:120px;accent-color:var(--accent)}
.tc{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;min-width:52px}
.tc:last-child{text-align:right}
.toast{font-size:11.5px;color:var(--muted);min-height:16px;word-break:break-all}
.toast.err{color:var(--bad)}
.toast.ok{color:#27ae60}
</style></head><body><div class="wrap">
<h1>Live monitor &mdash; __SESSION__</h1>
<p class="sub" id="sub">connecting&hellip;</p>
<div class="bar">
  <span id="state" class="pill stale">connecting</span>
  <label>window <select id="win">
    <option value="2">2 s</option><option value="5" selected>5 s</option>
    <option value="10">10 s</option><option value="20">20 s</option></select></label>
  <label>signal <select id="filt">
    <option value="1" selected>filtered</option><option value="0">raw</option>
  </select></label>
  <label><input type="checkbox" id="run" checked> auto-refresh</label>
</div>
__CONTROLS__
<div class="card"><canvas id="tr" height="420"></canvas></div>
<div class="card"><div id="bands"></div></div>
<div class="card"><div id="quality"></div></div>
<div class="foot">Loopback only (127.0.0.1). Not encrypted, not an authentication
system &mdash; do not expose through a tunnel or proxy.</div>
</div><script>
const TOKEN="__TOKEN__", REFRESH=__REFRESH__, CANCONTROL=__CANCONTROL__;
const cv=document.getElementById("tr"), ctx=cv.getContext("2d");
const COLORS=["#4c8dff","#f2994a","#27ae60","#eb5757","#9b51e0","#00b8d9",
  "#f2c94c","#ff6b9d","#56ccf2","#6fcf97"];
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const $=id=>document.getElementById(id);
// A fixed decimal count reads fine for microvolt-scale EEG but rounds a
// small-magnitude signal (gyroscope deviations under 1 dps) to "0" -- scale
// the precision to the value instead.
function fmtNum(v){
  const a=Math.abs(v);
  return v.toFixed(a<10?2:a<100?1:0);
}
let failures=0, isReplay=false, scrubbing=false, lastData=null;

function draw(d){
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth,
        rows=d.channels.length, H=Math.max(220, rows*38);
  cv.style.height=H+"px"; cv.width=W*dpr; cv.height=H*dpr;
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);
  const padL=64, padR=54, plotW=W-padL-padR, rh=H/rows;
  const muted=css("--muted"), line=css("--line");
  ctx.font="11px ui-sans-serif,system-ui,sans-serif";

  const unit=d.unit||"microvolts";
  for(let i=0;i<rows;i++){
    const mid=i*rh+rh/2, series=d.data[i], mean=d.chan_mean[i]||0;
    ctx.strokeStyle=line; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(padL,mid); ctx.lineTo(padL+plotW,mid); ctx.stroke();
    ctx.fillStyle=muted; ctx.textAlign="right";
    ctx.fillText(d.channels[i], padL-8, mid+4);
    // Per-channel autoscale, centred on the channel's own window mean: the
    // only way a montage stays readable when one electrode has ten times the
    // amplitude of its neighbour, and the only way a signal with a large DC
    // offset (accelerometer gravity, raw optical baseline) doesn't render as
    // a flat line -- see the peak_dev comment in _snapshot.
    const peak=d.peak_dev[i]||1, amp=(rh/2)*0.84;
    ctx.strokeStyle=COLORS[i%COLORS.length]; ctx.lineWidth=1.1; ctx.beginPath();
    for(let k=0;k<series.length;k++){
      const x=padL+(k/(series.length-1||1))*plotW, y=mid-((series[k]-mean)/peak)*amp;
      k?ctx.lineTo(x,y):ctx.moveTo(x,y);
    }
    ctx.stroke();
    ctx.fillStyle=muted; ctx.textAlign="left";
    ctx.fillText("±"+fmtNum(peak)+" "+unit, padL+plotW+6, mid+4);
  }

  const span=(d.t_end-d.t_start)||1;
  (d.events||[]).forEach(e=>{
    const f=(e.timestamp-d.t_start)/span;
    if(f<0||f>1) return;
    const x=padL+f*plotW;
    ctx.strokeStyle=e.origin==="stim"?"#eb5757":css("--accent");
    ctx.lineWidth=e.origin==="stim"?1.6:1.2;
    ctx.setLineDash(e.origin==="stim"?[]:[4,3]);
    ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,H); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle=muted; ctx.textAlign="left"; ctx.fillText(e.label, x+3, 11);
  });
  ctx.fillStyle=muted; ctx.textAlign="center";
  ctx.fillText(span.toFixed(1)+" s window", padL+plotW/2, H-2);
}

function bands(d){
  const host=$("bands");
  if(!d.band_power){ host.textContent=d.band_power_error||"no band power yet";
    return; }
  const m=d.band_power.mean_across_channels;
  host.innerHTML=["delta","theta","alpha","beta","gamma"].map(b=>{
    const v=m[b+"_rel"];
    return "<span class='bp'><b>"+b+"</b> "+(v==null?"-":(v*100).toFixed(1)+"%")+
      "</span>";
  }).join("")+"<span class='bp'>dominant <b>"+
    (d.band_power.dominant_band||"-")+"</b></span>";
}

function quality(d){
  const host=$("quality");
  if(!d.quality){ host.textContent=d.quality_error||"no quality data yet"; return; }
  const ch=d.quality.channels, bad=d.quality.bad_channels||[], unit=d.unit||"microvolts";
  // The underlying metrics are always named *_uv (they were written for EEG,
  // in microvolts); the header names the unit actually in play so the table
  // doesn't claim "uV" for an accelerometer or optics stream.
  let h="<table><thead><tr><th>Channel</th><th>RMS ("+unit+")</th>"+
    "<th>Peak-peak ("+unit+")</th><th>Line noise</th><th>Status</th></tr></thead><tbody>";
  for(const k in ch){ const m=ch[k];
    const flags=["flat","railed","line_noisy"].filter(f=>m[f]);
    h+="<tr class='"+(flags.length?"bad":"ok")+"'><td>"+k+"</td><td>"+
      fmtNum(m.rms_uv)+"</td><td>"+fmtNum(m.ptp_uv)+"</td><td>"+
      m.line_noise_ratio.toFixed(3)+"</td><td>"+(flags.join(", ")||"ok")+
      "</td></tr>";
  }
  host.innerHTML=h+"</tbody></table>"+(bad.length?
    "<p class='sub' style='margin:8px 0 0'>Attention: "+bad.join(", ")+"</p>":"");
}

function toast(msg, kind){
  const t=$("toast"); if(!t) return;
  t.className="toast"+(kind?" "+kind:""); t.textContent=msg;
}

async function ctl(action, extra){
  if(!CANCONTROL) return null;
  try{
    const r=await fetch("/api/control?t="+TOKEN, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(Object.assign({action:action}, extra||{}))
    });
    const d=await r.json();
    if(!d.ok){ toast(d.error||"failed", "err"); return null; }
    toast(d.path ? d.message+": "+d.path : d.message, "ok");
    tick();
    return d;
  }catch(e){ toast("request failed: "+e, "err"); return null; }
}

function syncControls(d){
  if(!CANCONTROL) return;
  isReplay = !!d.replay;
  const paused = isReplay && d.replay.paused;
  const stopped = !d.running;
  $("b-toggle").innerHTML = paused ? "&#9654; Resume" : "&#9208; Pause";
  // Playback controls are meaningless on a live board and on a stopped session.
  [["b-restart",1],["b-toggle",1],["b-back",1],["b-fwd",1],["b-speed",1]]
    .forEach(([id])=>{ $(id).disabled = !isReplay || stopped; });
  $("b-stop").disabled = stopped;
  $("b-mark").disabled = stopped;
  // Nothing buffered means nothing to save. Disabling is kinder than letting
  // the click fail -- and the buffer really can be empty, because a seek
  // discards it and stopping means it will never refill.
  const noData = !(d.buffered_sec > 0);
  ["b-report","b-traces","b-spectrum"].forEach(id=>{
    $(id).disabled = noData;
    $(id).title = noData ? "Nothing buffered to save yet" : "";
  });
  $("scrub-row").style.display = isReplay ? "flex" : "none";
  if(isReplay && !scrubbing){
    const p=d.replay.position_sec, dur=d.replay.duration_sec||1;
    $("scrub").value = Math.round((p/dur)*1000);
    $("tc-now").textContent = p.toFixed(1)+"s";
    $("tc-end").textContent = dur.toFixed(1)+"s";
    if(String(d.replay.speed) !== $("b-speed").value){
      const opt=[...$("b-speed").options].find(o=>+o.value===d.replay.speed);
      if(opt) $("b-speed").value=opt.value;
    }
  }
}

function wireControls(){
  if(!CANCONTROL) return;
  $("b-restart").onclick = ()=>ctl("restart");
  $("b-toggle").onclick  = ()=>ctl("toggle");
  $("b-back").onclick    = ()=>ctl("skip", {delta_sec:-10});
  $("b-fwd").onclick     = ()=>ctl("skip", {delta_sec:10});
  $("b-speed").onchange  = e=>ctl("speed", {speed:parseFloat(e.target.value)});
  $("b-stop").onclick    = ()=>{
    if(confirm("Stop acquisition and release the device?\\n\\n"+
               "Buffered signal stays available to save.")) ctl("stop");
  };
  $("b-mark").onclick    = ()=>ctl("mark", {label:$("mark-label").value||"mark"});
  $("b-report").onclick  = ()=>{ toast("saving report\\u2026"); ctl("save_report",
    {seconds:parseFloat($("win").value)}); };
  $("b-traces").onclick  = ()=>{ toast("saving traces\\u2026"); ctl("save_traces",
    {seconds:parseFloat($("win").value), filtered:$("filt").value==="1"}); };
  $("b-spectrum").onclick= ()=>{ toast("saving spectrum\\u2026"); ctl("save_spectrum",
    {seconds:Math.max(8,parseFloat($("win").value)),
     filtered:$("filt").value==="1"}); };
  $("b-clear").onclick   = ()=>{
    if(confirm("Clear the event log? The signal buffer is untouched."))
      ctl("clear_events");
  };
  const s=$("scrub");
  s.oninput = ()=>{ scrubbing=true;
    if(lastData&&lastData.replay)
      $("tc-now").textContent=((s.value/1000)*lastData.replay.duration_sec)
        .toFixed(1)+"s"; };
  s.onchange = ()=>{
    const dur = lastData&&lastData.replay ? lastData.replay.duration_sec : 0;
    scrubbing=false; ctl("seek", {position_sec:(s.value/1000)*dur});
  };
  document.addEventListener("keydown", e=>{
    if(e.target.tagName==="INPUT"||e.target.tagName==="SELECT") return;
    if(e.code==="Space"){ e.preventDefault(); if(isReplay) ctl("toggle"); }
    if(e.code==="ArrowLeft"&&isReplay)  ctl("skip",{delta_sec:-10});
    if(e.code==="ArrowRight"&&isReplay) ctl("skip",{delta_sec:10});
    if(e.key==="m"||e.key==="M") ctl("mark",{label:$("mark-label").value||"mark"});
  });
}

async function tick(){
  if(!$("run").checked) return;
  const secs=$("win").value, filt=$("filt").value;
  try{
    const r=await fetch("/api/snapshot?t="+TOKEN+"&seconds="+secs+"&filtered="+filt);
    const d=await r.json();
    lastData=d;
    const st=$("state"), sub=$("sub");
    if(!d.ready){ st.className="pill stale"; st.textContent="waiting";
      sub.textContent=d.reason||"no data yet"; return; }
    failures=0;
    st.className="pill "+(d.running?"live":"stale");
    st.textContent=d.running?(d.replay&&d.replay.paused?"paused":"live")
      :(d.exhausted?"finished":"stopped");
    let s=d.channels.length+" ch @ "+d.sfreq+" Hz  |  "+
      (d.filtered?"filtered":"raw")+"  |  buffered "+
      d.buffered_sec.toFixed(1)+" s  |  "+d.n_events+" events";
    if(d.replay) s+="  |  replay "+d.replay.position_sec.toFixed(1)+"/"+
      d.replay.duration_sec.toFixed(1)+" s  x"+d.replay.speed;
    sub.textContent=s;
    draw(d); bands(d); quality(d); syncControls(d);
  }catch(e){
    if(++failures>3){
      const st=$("state");
      st.className="pill stale"; st.textContent="disconnected";
      $("sub").textContent=
        "Server unreachable - the session may have been stopped.";
    }
  }
}
wireControls(); tick(); setInterval(tick, REFRESH);
window.addEventListener("resize", ()=>tick());
</script></body></html>"""


class MonitorRegistry:
    """Process-wide registry of live monitors, one per session at most."""

    def __init__(self) -> None:
        self._monitors: dict[str, Monitor] = {}
        self._lock = threading.Lock()

    def start(self, session_id: str, store: Any, port: int = 0,
              window_sec: float = 5.0, refresh_ms: int = 500,
              allow_control: bool = True) -> Monitor:
        with self._lock:
            existing = self._monitors.pop(session_id, None)
        if existing is not None:
            existing.stop()
        monitor = Monitor(session_id, store, port, window_sec, refresh_ms,
                          allow_control)
        with self._lock:
            self._monitors[session_id] = monitor
        return monitor

    def get(self, session_id: str) -> Monitor | None:
        with self._lock:
            return self._monitors.get(session_id)

    def stop(self, session_id: str) -> bool:
        with self._lock:
            monitor = self._monitors.pop(session_id, None)
        if monitor is None:
            return False
        monitor.stop()
        return True

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"session_id": sid, "url": m.url, "port": m.port,
                 "window_sec": m.window_sec, "refresh_ms": m.refresh_ms,
                 "allow_control": m.allow_control}
                for sid, m in self._monitors.items()
            ]

    def stop_all(self) -> None:
        for session_id in list(self._monitors):
            self.stop(session_id)


monitors = MonitorRegistry()
