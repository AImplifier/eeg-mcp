"""Self-contained SVG/HTML rendering for EEG views.

Deliberately dependency-free: every figure is SVG built from strings, and every
page inlines its own CSS. No matplotlib (too slow to import in a request path,
and it fights with headless servers), no Plotly bundle (3 MB of JavaScript per
file), no CDN (a clinical machine may have no internet, and a plot that renders
blank because a CDN was unreachable is worse than no plot).

The result is a file of a few hundred kilobytes that opens in any browser,
survives being emailed, and renders identically in five years.
"""

from __future__ import annotations

import html
from typing import Any, Sequence

import numpy as np

# Palette chosen to stay distinguishable in both light and dark themes and under
# the most common form of colour-vision deficiency.
SERIES_COLORS = [
    "#4c8dff", "#f2994a", "#27ae60", "#eb5757", "#9b51e0",
    "#00b8d9", "#f2c94c", "#ff6b9d", "#56ccf2", "#6fcf97",
]
BAND_COLORS = {
    "delta": "#4c8dff", "theta": "#00b8d9", "alpha": "#27ae60",
    "beta": "#f2994a", "gamma": "#eb5757",
}


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _fmt(value: float, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value:.{digits}f}"


def _polyline(xs: np.ndarray, ys: np.ndarray) -> str:
    """Build an SVG points string, dropping non-finite samples."""
    finite = np.isfinite(xs) & np.isfinite(ys)
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs[finite], ys[finite]))


def render_traces(
    data: np.ndarray,
    times: np.ndarray,
    ch_names: Sequence[str],
    events: Sequence[dict[str, Any]] | None = None,
    width: int = 1000,
    row_height: int = 46,
    title: str = "",
    per_channel_scale: bool = True,
) -> str:
    """Stacked multichannel traces with event markers -- the standard EEG view.

    Each channel is drawn in its own horizontal band. ``per_channel_scale``
    normalises each channel to its own amplitude, which is what makes a montage
    readable when one electrode has ten times the amplitude of its neighbour;
    turn it off to compare absolute amplitudes across channels.
    """
    n_ch = int(data.shape[0])
    if n_ch == 0 or data.shape[1] == 0:
        return '<p class="empty">No samples to plot.</p>'

    pad_l, pad_r, pad_t, pad_b = 74, 16, 24 if title else 10, 28
    plot_w = width - pad_l - pad_r
    plot_h = n_ch * row_height
    height = plot_h + pad_t + pad_b

    t0, t1 = float(times[0]), float(times[-1])
    span = (t1 - t0) or 1.0
    # Plot against elapsed seconds; absolute Unix stamps are unreadable on an axis.
    rel = (times - t0).astype(float)
    xs = pad_l + (rel / span) * plot_w

    if per_channel_scale:
        scales = np.max(np.abs(data), axis=1)
        scales[scales == 0] = 1.0
    else:
        peak = float(np.max(np.abs(data))) or 1.0
        scales = np.full(n_ch, peak)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="fig" '
        f'preserveAspectRatio="xMidYMid meet" role="img">'
    ]
    if title:
        parts.append(f'<text x="{pad_l}" y="16" class="fig-title">{_esc(title)}</text>')

    for i, name in enumerate(ch_names):
        mid = pad_t + i * row_height + row_height / 2
        parts.append(
            f'<line x1="{pad_l}" y1="{mid:.1f}" x2="{pad_l + plot_w}" y2="{mid:.1f}" '
            f'class="baseline"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{mid + 4:.1f}" class="ch-label">{_esc(name)}</text>'
        )
        amp = (row_height / 2) * 0.86
        ys = mid - (data[i] / scales[i]) * amp
        color = SERIES_COLORS[i % len(SERIES_COLORS)]
        parts.append(
            f'<polyline points="{_polyline(xs, ys)}" fill="none" '
            f'stroke="{color}" stroke-width="1.1"/>'
        )
        parts.append(
            f'<text x="{pad_l + plot_w + 4}" y="{mid + 3:.1f}" class="scale-label">'
            f'{_fmt(scales[i], 0)}</text>'
        )

    for event in events or []:
        stamp = event.get("timestamp")
        if stamp is None or not (t0 <= stamp <= t1):
            continue
        x = pad_l + ((stamp - t0) / span) * plot_w
        origin = event.get("origin", "manual")
        cls = "ev-stim" if origin == "stim" else "ev-mark"
        parts.append(
            f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" '
            f'y2="{pad_t + plot_h}" class="{cls}"/>'
        )
        parts.append(
            f'<text x="{x + 3:.1f}" y="{pad_t + 10}" class="ev-label">'
            f'{_esc(event.get("label", ""))}</text>'
        )

    axis_y = pad_t + plot_h + 16
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = pad_l + frac * plot_w
        parts.append(
            f'<text x="{x:.1f}" y="{axis_y}" class="axis-label" '
            f'text-anchor="middle">{_fmt(frac * span, 1)}s</text>'
        )
    parts.append(
        f'<text x="{pad_l + plot_w}" y="{axis_y}" class="axis-note" '
        f'text-anchor="end">uV (peak per channel, right)</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def render_psd(
    freqs: np.ndarray,
    psd: np.ndarray,
    ch_names: Sequence[str],
    width: int = 1000,
    height: int = 320,
    title: str = "Power spectral density",
    bands: dict[str, tuple[float, float]] | None = None,
) -> str:
    """Log-power spectrum per channel, with the classical bands shaded behind."""
    if freqs.size == 0 or psd.shape[1] == 0:
        return '<p class="empty">No spectrum to plot.</p>'

    pad_l, pad_r, pad_t, pad_b = 58, 108, 26, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    fmin, fmax = float(freqs[0]), float(freqs[-1])
    span = (fmax - fmin) or 1.0
    # dB keeps four orders of magnitude of EEG power on one readable axis.
    with np.errstate(divide="ignore"):
        db = 10.0 * np.log10(np.maximum(psd, 1e-20))
    lo, hi = float(np.percentile(db, 1)), float(np.max(db))
    rng = (hi - lo) or 1.0

    xs = pad_l + ((freqs - fmin) / span) * plot_w
    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="fig" '
        f'preserveAspectRatio="xMidYMid meet" role="img">',
        f'<text x="{pad_l}" y="16" class="fig-title">{_esc(title)}</text>',
    ]

    for band, (blo, bhi) in (bands or {}).items():
        if bhi < fmin or blo > fmax:
            continue
        x0 = pad_l + ((max(blo, fmin) - fmin) / span) * plot_w
        x1 = pad_l + ((min(bhi, fmax) - fmin) / span) * plot_w
        parts.append(
            f'<rect x="{x0:.1f}" y="{pad_t}" width="{max(x1 - x0, 0):.1f}" '
            f'height="{plot_h}" fill="{BAND_COLORS.get(band, "#888")}" '
            f'opacity="0.07"/>'
        )
        parts.append(
            f'<text x="{(x0 + x1) / 2:.1f}" y="{pad_t + plot_h + 22}" '
            f'class="band-label" text-anchor="middle">{_esc(band)}</text>'
        )

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + plot_h - frac * plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
            f'class="grid"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis-label" '
            f'text-anchor="end">{_fmt(lo + frac * rng, 0)}</text>'
        )

    for i, name in enumerate(ch_names):
        ys = pad_t + plot_h - ((db[i] - lo) / rng) * plot_h
        ys = np.clip(ys, pad_t, pad_t + plot_h)
        color = SERIES_COLORS[i % len(SERIES_COLORS)]
        parts.append(
            f'<polyline points="{_polyline(xs, ys)}" fill="none" '
            f'stroke="{color}" stroke-width="1.4" opacity="0.9"/>'
        )
        ly = pad_t + 12 + i * 15
        if ly < pad_t + plot_h:
            parts.append(
                f'<line x1="{pad_l + plot_w + 12}" y1="{ly - 4}" '
                f'x2="{pad_l + plot_w + 28}" y2="{ly - 4}" stroke="{color}" '
                f'stroke-width="2.4"/>'
            )
            parts.append(
                f'<text x="{pad_l + plot_w + 33}" y="{ly}" class="legend">'
                f'{_esc(name)}</text>'
            )

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = pad_l + frac * plot_w
        parts.append(
            f'<text x="{x:.1f}" y="{pad_t + plot_h + 16}" class="axis-label" '
            f'text-anchor="middle">{_fmt(fmin + frac * span, 0)}</text>'
        )
    parts.append(
        f'<text x="{pad_l - 44}" y="{pad_t + plot_h / 2}" class="axis-note" '
        f'transform="rotate(-90 {pad_l - 44} {pad_t + plot_h / 2})" '
        f'text-anchor="middle">dB uV^2/Hz</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def render_band_bars(
    per_channel: dict[str, dict[str, float]],
    bands: Sequence[str],
    width: int = 1000,
    row_height: int = 26,
    title: str = "Relative band power",
) -> str:
    """Stacked relative-band-power bar per channel -- the topography at a glance."""
    names = list(per_channel)
    if not names:
        return '<p class="empty">No band power to plot.</p>'

    pad_l, pad_r, pad_t, pad_b = 74, 130, 26, 12
    plot_w = width - pad_l - pad_r
    height = pad_t + len(names) * row_height + pad_b

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="fig" '
        f'preserveAspectRatio="xMidYMid meet" role="img">',
        f'<text x="{pad_l}" y="16" class="fig-title">{_esc(title)}</text>',
    ]
    for i, name in enumerate(names):
        y = pad_t + i * row_height
        entry = per_channel[name]
        x = float(pad_l)
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + row_height * 0.65:.1f}" '
            f'class="ch-label">{_esc(name)}</text>'
        )
        for band in bands:
            frac = float(entry.get(f"{band}_rel", 0.0) or 0.0)
            w = frac * plot_w
            if w <= 0.4:
                continue
            parts.append(
                f'<rect x="{x:.1f}" y="{y + 4:.1f}" width="{w:.1f}" '
                f'height="{row_height - 9}" fill="{BAND_COLORS.get(band, "#888")}" '
                f'opacity="0.85"><title>{_esc(name)} {_esc(band)}: '
                f'{frac * 100:.1f}%</title></rect>'
            )
            x += w

    for j, band in enumerate(bands):
        ly = pad_t + 10 + j * 16
        parts.append(
            f'<rect x="{pad_l + plot_w + 14}" y="{ly - 9}" width="11" height="11" '
            f'fill="{BAND_COLORS.get(band, "#888")}"/>'
        )
        parts.append(
            f'<text x="{pad_l + plot_w + 31}" y="{ly}" class="legend">'
            f'{_esc(band)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def render_quality_table(quality: dict[str, Any]) -> str:
    """Per-channel health as a table, with bad channels called out."""
    channels = quality.get("channels", {})
    if not channels:
        return '<p class="empty">No quality data.</p>'
    rows = [
        "<table class='qt'><thead><tr><th>Channel</th><th>RMS (uV)</th>"
        "<th>Peak-peak (uV)</th><th>Line noise</th><th>Status</th></tr></thead><tbody>"
    ]
    for name, metrics in channels.items():
        flags = [
            key for key in ("flat", "railed", "line_noisy") if metrics.get(key)
        ]
        status = ", ".join(flags) if flags else "ok"
        cls = "bad" if flags else "ok"
        rows.append(
            f"<tr class='{cls}'><td>{_esc(name)}</td>"
            f"<td>{_fmt(metrics.get('rms_uv'), 1)}</td>"
            f"<td>{_fmt(metrics.get('ptp_uv'), 1)}</td>"
            f"<td>{_fmt(metrics.get('line_noise_ratio'), 3)}</td>"
            f"<td>{_esc(status)}</td></tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


PAGE_CSS = """
:root { color-scheme: light dark;
  --bg:#ffffff; --fg:#16191d; --muted:#5c6570; --line:#dfe3e8; --card:#f7f8fa;
  --accent:#4c8dff; --bad:#eb5757; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#14171b; --fg:#e8ebef; --muted:#98a2ad; --line:#2a2f36; --card:#1b1f25;
  --accent:#6ba4ff; --bad:#ff7676; } }
* { box-sizing:border-box; }
body { margin:0; padding:28px; background:var(--bg); color:var(--fg);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif; }
.wrap { max-width:1080px; margin:0 auto; }
h1 { font-size:21px; margin:0 0 4px; letter-spacing:-.01em; }
h2 { font-size:15px; margin:26px 0 8px; font-weight:600; }
.sub { color:var(--muted); font-size:13px; margin:0 0 18px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px; margin-bottom:16px; overflow-x:auto; }
.fig { width:100%; height:auto; display:block; }
.fig-title { fill:var(--fg); font-size:12px; font-weight:600; }
.ch-label { fill:var(--muted); font-size:11px; text-anchor:end; }
.scale-label,.axis-label,.legend,.band-label { fill:var(--muted); font-size:10px; }
.legend { font-size:11px; }
.axis-note { fill:var(--muted); font-size:10px; opacity:.8; }
.baseline { stroke:var(--line); stroke-width:1; }
.grid { stroke:var(--line); stroke-width:1; stroke-dasharray:3 3; opacity:.6; }
.ev-mark { stroke:var(--accent); stroke-width:1.2; stroke-dasharray:4 3; opacity:.85; }
.ev-stim { stroke:var(--bad); stroke-width:1.6; opacity:.9; }
.ev-label { fill:var(--muted); font-size:9px; }
.grid-2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
  gap:10px; margin-bottom:16px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:11px 13px; }
.stat .k { color:var(--muted); font-size:11px; text-transform:uppercase;
  letter-spacing:.04em; }
.stat .v { font-size:19px; font-weight:600; margin-top:3px;
  font-variant-numeric:tabular-nums; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { text-align:left; padding:6px 9px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase;
  letter-spacing:.04em; }
td { font-variant-numeric:tabular-nums; }
tr.bad td { color:var(--bad); }
.empty { color:var(--muted); font-style:italic; padding:12px; }
.foot { color:var(--muted); font-size:11px; margin-top:22px;
  border-top:1px solid var(--line); padding-top:12px; }
"""


def stat_tiles(tiles: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        f"<div class='stat'><div class='k'>{_esc(k)}</div>"
        f"<div class='v'>{_esc(v)}</div></div>"
        for k, v in tiles
    )
    return f"<div class='grid-2'>{cells}</div>"


def page(title: str, subtitle: str, body: str, footer: str = "") -> str:
    """Wrap rendered sections into one standalone HTML document."""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{PAGE_CSS}</style></head><body>"
        f"<div class='wrap'><h1>{_esc(title)}</h1>"
        f"<p class='sub'>{_esc(subtitle)}</p>{body}"
        f"<div class='foot'>{_esc(footer)}</div></div></body></html>"
    )
