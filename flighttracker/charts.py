"""Inline-SVG chart builders for the HTML report.

Colors come from the validated reference palette and are referenced through CSS
custom properties, so light/dark swap in one place (see report.py's stylesheet).
Diverging arms are interpolated from the neutral midpoint toward a single pole
hue each, which is why they are computed here rather than named.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Sequence

from flighttracker.analysis import WEEKDAYS, Bucket

# Diverging poles and midpoints, per mode (reference palette).
DIVERGING = {
    "light": {"cheap": (0x2A, 0x78, 0xD6), "mid": (0xF0, 0xEF, 0xEC), "dear": (0xE3, 0x49, 0x48)},
    "dark": {"cheap": (0x39, 0x87, 0xE5), "mid": (0x38, 0x38, 0x35), "dear": (0xE6, 0x67, 0x67)},
}
ARM_STEPS = 5


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> str:
    return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def diverging_step(value: float, centre: float, spread: float) -> int:
    """Quantised diverging step, -ARM_STEPS (cheap) .. +ARM_STEPS (dear)."""
    if spread <= 0:
        return 0
    t = max(-1.0, min(1.0, (value - centre) / spread))
    if abs(t) <= 1e-9:
        return 0
    step = min(ARM_STEPS, max(1, round(abs(t) * ARM_STEPS)))
    return step if t > 0 else -step


def diverging_color(step: int, mode: str) -> str:
    """The fill for a diverging step in one mode. Blue = cheaper, red = dearer."""
    poles = DIVERGING[mode]
    if step == 0:
        return "#%02x%02x%02x" % poles["mid"]
    pole = poles["dear"] if step > 0 else poles["cheap"]
    return _lerp(poles["mid"], pole, 0.30 + 0.70 * abs(step) / ARM_STEPS)


def step_class(step: int) -> str:
    """CSS class for a step; the fills themselves live in the stylesheet, per mode."""
    return f"hm{'p' if step > 0 else ('n' if step < 0 else '')}{abs(step)}"


def diverging_css() -> str:
    """Both modes' diverging fills, so the heatmap follows the active theme."""
    steps = range(-ARM_STEPS, ARM_STEPS + 1)

    def rules(mode: str, prefix: str = "") -> str:
        return "\n".join(
            f"{prefix}.{step_class(i)} {{ fill: {diverging_color(i, mode)};"
            f" background: {diverging_color(i, mode)}; }}"
            for i in steps
        )

    return "\n".join([
        rules("light"),
        '@media (prefers-color-scheme: dark) {',
        rules("dark", ':root:not([data-theme="light"]) '),
        "}",
        rules("dark", ':root[data-theme="dark"] '),
    ])


def _nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / count
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for mult in (1, 2, 2.5, 5, 10):
        step = magnitude * mult
        if raw <= step:
            break
    start = (int(lo / step)) * step
    ticks, value = [], start
    while value <= hi + step * 0.5:
        if value >= lo - step * 0.01:
            ticks.append(round(value, 2))
        value += step
    return ticks or [lo, hi]


def _money(value: float, currency: str) -> str:
    return f"{currency} {value:,.0f}"


# ---------------------------------------------------------------- line chart

def price_line_chart(
    points: Sequence[tuple[datetime, float]],
    *,
    currency: str,
    threshold: float | None,
    best: tuple[datetime, float] | None,
    chart_id: str,
    max_points: int = 400,
) -> str:
    """Price over time. One series, so no legend - the section heading names it."""
    if len(points) < 2:
        return '<p class="empty">Not enough observations yet to draw a timeline.</p>'

    # Downsample for rendering, but never drop the extremes.
    render = list(points)
    if len(render) > max_points:
        step = len(render) / max_points
        keep = {min(int(i * step), len(render) - 1) for i in range(max_points)}
        keep.add(min(range(len(render)), key=lambda i: render[i][1]))
        keep.add(max(range(len(render)), key=lambda i: render[i][1]))
        keep.update({0, len(render) - 1})
        render = [render[i] for i in sorted(keep)]

    w, h = 880, 280
    pad_l, pad_r, pad_t, pad_b = 62, 18, 18, 34
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b

    prices = [p for _, p in points]
    lo_raw, hi_raw = min(prices), max(prices)
    if threshold is not None:
        lo_raw, hi_raw = min(lo_raw, threshold), max(hi_raw, threshold)
    pad_v = (hi_raw - lo_raw) * 0.12 or max(hi_raw * 0.05, 1)
    lo, hi = lo_raw - pad_v, hi_raw + pad_v

    t0, t1 = points[0][0].timestamp(), points[-1][0].timestamp()
    span = (t1 - t0) or 1.0

    def sx(dt: datetime) -> float:
        return pad_l + (dt.timestamp() - t0) / span * plot_w

    def sy(price: float) -> float:
        return pad_t + (hi - price) / (hi - lo) * plot_h

    parts: list[str] = [
        f'<svg class="chart" id="{esc(chart_id)}" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Price history, {len(points)} observations" '
        f'data-t0="{t0}" data-t1="{t1}" data-padl="{pad_l}" data-plotw="{plot_w}">'
    ]

    for tick in _nice_ticks(lo, hi):
        y = sy(tick)
        if not (pad_t - 1 <= y <= pad_t + plot_h + 1):
            continue
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">{tick:,.0f}</text>'
        )

    coords = [(sx(dt), sy(p)) for dt, p in render]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = (f"{coords[0][0]:.1f},{pad_t + plot_h} " + line +
            f" {coords[-1][0]:.1f},{pad_t + plot_h}")
    parts.append(f'<polygon class="area" points="{area}"/>')
    parts.append(f'<polyline class="series" points="{line}"/>')

    if threshold is not None:
        ty = sy(threshold)
        parts.append(
            f'<line class="threshold" x1="{pad_l}" y1="{ty:.1f}" x2="{w - pad_r}" y2="{ty:.1f}"/>'
            f'<text class="threshold-label" x="{w - pad_r}" y="{ty - 7:.1f}" text-anchor="end">'
            f'target {_money(threshold, currency)}</text>'
        )

    if best:
        bx, by = sx(best[0]), sy(best[1])
        anchor = "start" if bx < w * 0.6 else "end"
        dx = 12 if anchor == "start" else -12
        stamp = esc(f"{best[0]:%-d %b %H:%M}")
        parts.append(
            f'<circle class="marker-ring" cx="{bx:.1f}" cy="{by:.1f}" r="7"/>'
            f'<circle class="marker" cx="{bx:.1f}" cy="{by:.1f}" r="4.5"/>'
            f'<text class="marker-label" x="{bx + dx:.1f}" y="{by - 10:.1f}" text-anchor="{anchor}">'
            f'low {_money(best[1], currency)} \u00b7 {stamp}</text>'
        )

    axis_y = pad_t + plot_h
    parts.append(f'<line class="axis" x1="{pad_l}" y1="{axis_y}" x2="{w - pad_r}" y2="{axis_y}"/>')
    label_count = min(6, len(points))
    for i in range(label_count):
        dt = points[round(i * (len(points) - 1) / max(label_count - 1, 1))][0]
        x = min(max(sx(dt), pad_l + 18), w - pad_r - 18)
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{axis_y + 18}" text-anchor="middle">'
            f'{esc(f"{dt:%-d %b}")}</text>'
        )

    parts.append(
        f'<line class="crosshair" id="{esc(chart_id)}-cross" x1="0" y1="{pad_t}" x2="0" '
        f'y2="{axis_y}" style="display:none"/>'
        f'<circle class="focus" id="{esc(chart_id)}-focus" r="5" style="display:none"/>'
        f'<rect class="hit" id="{esc(chart_id)}-hit" x="{pad_l}" y="{pad_t}" '
        f'width="{plot_w}" height="{plot_h}"/>'
    )
    parts.append("</svg>")

    series_json = ",".join(
        f'[{dt.timestamp():.0f},{p:.2f},"{dt:%a %-d %b %H:%M}"]' for dt, p in render
    )
    parts.append(
        f'<script type="application/json" id="{esc(chart_id)}-data">'
        f'{{"currency":"{esc(currency)}","lo":{lo},"hi":{hi},"padT":{pad_t},'
        f'"plotH":{plot_h},"points":[{series_json}]}}</script>'
    )
    return "".join(parts)


# ------------------------------------------------------------------ heatmap

def weekday_hour_heatmap(
    cells: dict[tuple[int, int], tuple[int, float]],
    *,
    centre: float,
    currency: str,
) -> str:
    """Weekday x hour-of-day. Diverging: how far each slot sits from typical."""
    if not cells:
        return '<p class="empty">No observations yet.</p>'

    cell_w, cell_h, gap = 32, 24, 2
    pad_l, pad_t, pad_b = 40, 22, 6
    w = pad_l + 24 * (cell_w + gap)
    h = pad_t + 7 * (cell_h + gap) + pad_b

    deltas = [med - centre for _, med in cells.values()]
    spread = max((abs(d) for d in deltas), default=1.0) or 1.0
    cheapest = min(cells.items(), key=lambda kv: kv[1][1])

    parts = [
        f'<svg class="chart heatmap" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Median price by weekday and hour of day">'
    ]
    for hour in range(0, 24, 3):
        x = pad_l + hour * (cell_w + gap) + cell_w / 2
        parts.append(f'<text class="tick" x="{x:.0f}" y="14" text-anchor="middle">{hour:02d}</text>')

    for day in range(7):
        y = pad_t + day * (cell_h + gap)
        parts.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y + cell_h / 2 + 4:.0f}" '
            f'text-anchor="end">{WEEKDAYS[day]}</text>'
        )
        for hour in range(24):
            x = pad_l + hour * (cell_w + gap)
            entry = cells.get((day, hour))
            if entry is None:
                parts.append(
                    f'<rect class="cell empty-cell" x="{x}" y="{y}" width="{cell_w}" '
                    f'height="{cell_h}" rx="3"><title>{WEEKDAYS[day]} {hour:02d}:00 '
                    f'— no observations</title></rect>'
                )
                continue
            n, median = entry
            cls = f"cell {step_class(diverging_step(median, centre, spread))}"
            if (day, hour) == cheapest[0]:
                cls += " best-cell"
            parts.append(
                f'<rect class="{cls}" x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" rx="3">'
                f'<title>{WEEKDAYS[day]} {hour:02d}:00 — median '
                f'{_money(median, currency)} from {n} observation{"s" if n != 1 else ""}</title></rect>'
            )
    parts.append("</svg>")

    (bd, bh), (bn, bmed) = cheapest
    parts.append(
        f'<p class="chart-note"><span class="swatch-ring"></span> Cheapest slot: '
        f'<strong>{WEEKDAYS[bd]} {bh:02d}:00</strong> — median {_money(bmed, currency)} '
        f'across {bn} observation{"s" if bn != 1 else ""}.</p>'
    )
    return "".join(parts)


# ------------------------------------------------------- booking-window curve

def days_out_curve(buckets: Sequence[Bucket], *, currency: str) -> str:
    """Median fare by days-before-departure, with each bucket's observed low.

    Position-encoded (dots on a connecting line, not bars) so the axis can be
    cropped to the data without overstating small differences.
    """
    if not buckets:
        return '<p class="empty">No observations yet.</p>'

    w, h = 880, 250
    pad_l, pad_r, pad_t, pad_b = 62, 18, 26, 44
    plot_w, plot_h = w - pad_l - pad_r, h - pad_t - pad_b
    slot = plot_w / max(len(buckets), 1)

    values = [v for b in buckets for v in (b.minimum, b.median)]
    lo_raw, hi_raw = min(values), max(values)
    pad_v = (hi_raw - lo_raw) * 0.16 or max(hi_raw * 0.04, 1)
    lo, hi = lo_raw - pad_v, hi_raw + pad_v
    cheapest = min(buckets, key=lambda b: b.median)

    def sx(i: int) -> float:
        return pad_l + slot * (i + 0.5)

    def sy(v: float) -> float:
        return pad_t + (hi - v) / (hi - lo) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Median and lowest price by days before departure">'
    ]
    for tick in _nice_ticks(lo, hi):
        y = sy(tick)
        if not (pad_t - 1 <= y <= pad_t + plot_h + 1):
            continue
        parts.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">{tick:,.0f}</text>'
        )

    median_path = " ".join(f"{sx(i):.1f},{sy(b.median):.1f}" for i, b in enumerate(buckets))
    if len(buckets) > 1:
        parts.append(f'<polyline class="series" points="{median_path}"/>')

    base = pad_t + plot_h
    for i, bucket in enumerate(buckets):
        x, y_med, y_min = sx(i), sy(bucket.median), sy(bucket.minimum)
        tip = (f'{esc(bucket.key)} before departure \u2014 median {_money(bucket.median, currency)}, '
               f'low {_money(bucket.minimum, currency)}, '
               f'{bucket.n} observation{"s" if bucket.n != 1 else ""}')
        parts.append(
            f'<line class="range" x1="{x:.1f}" y1="{y_med:.1f}" x2="{x:.1f}" y2="{y_min:.1f}"/>'
            f'<circle class="dot-low" cx="{x:.1f}" cy="{y_min:.1f}" r="3.5"><title>{tip}</title></circle>'
            f'<circle class="dot-ring" cx="{x:.1f}" cy="{y_med:.1f}" r="7"/>'
            f'<circle class="dot{" best" if bucket is cheapest else ""}" cx="{x:.1f}" '
            f'cy="{y_med:.1f}" r="5"><title>{tip}</title></circle>'
        )
        parts.append(
            f'<text class="tick" x="{x:.1f}" y="{base + 20}" text-anchor="middle">'
            f'{esc(bucket.key)}</text>'
        )
        if bucket.thin:
            parts.append(
                f'<text class="thin-flag" x="{x:.1f}" y="{base + 34}" text-anchor="middle">'
                f'n={bucket.n}</text>'
            )
        if bucket is cheapest:
            parts.append(
                f'<text class="marker-label" x="{x:.1f}" y="{y_med - 14:.1f}" '
                f'text-anchor="middle">{_money(bucket.median, currency)}</text>'
            )
    parts.append(f'<line class="axis" x1="{pad_l}" y1="{base}" x2="{w - pad_r}" y2="{base}"/>')
    parts.append("</svg>")
    parts.append(
        '<div class="legend"><span class="key-dot"></span><span>median for the window</span>'
        '<span class="key-low"></span><span>cheapest seen in that window</span></div>'
    )
    return "".join(parts)
