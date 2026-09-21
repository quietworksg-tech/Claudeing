"""Rendering: a terminal summary and a self-contained static HTML report."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from flighttracker.analysis import Summary, heatmap, sparkline, summarise, timeline
from flighttracker.charts import (
    ARM_STEPS, days_out_curve, diverging_css, esc, price_line_chart, step_class,
    weekday_hour_heatmap,
)
from flighttracker.db import Database
from flighttracker.models import Quote, Route

BAR_WIDTH = 28


def money(value: float | None, currency: str) -> str:
    return "n/a" if value is None else f"{currency} {value:,.2f}"


# ------------------------------------------------------------------ terminal

def render_terminal(summary: Summary, quotes: Sequence[Quote], width: int = 74) -> str:
    s, ccy = summary, summary.currency
    out: list[str] = []
    rule = "─" * width
    out.append(rule)
    out.append(f"  {s.route.name}   [{s.route.provider}] {s.route.cabin.title()}  x{s.route.adults}")
    out.append(rule)

    if not s.n:
        out.append("  No observations yet.")
        for note in s.notes:
            out.append(f"  ! {note}")
        return "\n".join(out)

    out.append(f"  Now          {money(s.current, ccy)}   {s.verdict}")
    out.append(f"  Range        {money(s.low, ccy)}  →  {money(s.high, ccy)}"
               f"   (median {money(s.median, ccy)})")

    best_local = s.best_moment_local
    if best_local and s.best_quote:
        bq = s.best_quote
        dates = str(bq.depart_date) + (f" → {bq.return_date}" if bq.return_date else "")
        out.append(f"  Cheapest at  {best_local:%a %d %b %Y %H:%M} {s.tz}"
                   f"   {money(bq.price, ccy)}")
        out.append(f"               {dates}"
                   + (f"  {bq.carrier}" if bq.carrier else "")
                   + (f"  {bq.stops} stop(s)" if bq.stops else "  nonstop"))

    if s.threshold is not None:
        state = "MET" if s.threshold_met else f"{money(s.current - s.threshold, ccy)} above"
        out.append(f"  Target       {money(s.threshold, ccy)}   [{state}]")

    deltas = []
    if s.change_24h is not None:
        deltas.append(f"24h {s.change_24h:+.1f}%")
    if s.change_7d is not None:
        deltas.append(f"7d {s.change_7d:+.1f}%")
    score = f"deal score {s.deal_score}/100" if s.deal_score is not None else ""
    if deltas or score:
        out.append(f"  Movement     {'   '.join(deltas)}{'   ' if deltas and score else ''}{score}")

    out.append(f"  Samples      {s.n} observations"
               + (f" over {(s.last_seen - s.first_seen).days}d" if s.first_seen and s.last_seen else ""))
    prices = [q.price for q in sorted(quotes, key=lambda q: q.ts)]
    if len(prices) > 1:
        out.append(f"  Timeline     {sparkline(prices, width=width - 15)}")
        out.append(f"               {s.first_seen:%d %b}"
                   + " " * max(width - 28 - 12, 1) + f"{s.last_seen:%d %b}")

    out.append("")
    out.append(f"  BEST TIME TO CHECK  (local time, {s.tz})")
    for title, buckets in (("hour", s.by_hour), ("weekday", s.by_weekday)):
        best = s.cheapest_hour if title == "hour" else s.cheapest_weekday
        if not best:
            continue
        label = f"{best.key}:00" if title == "hour" else best.key
        flag = "  (thin sample)" if best.thin else ""
        out.append(f"    cheapest {title:<8} {label:<6} median {money(best.median, ccy)}"
                   f"  n={best.n}{flag}")

    if s.by_days_out:
        out.append("")
        out.append("  BOOKING WINDOW  (median by days before departure)")
        hi = max(b.median for b in s.by_days_out) or 1.0
        best = min(b.median for b in s.by_days_out) or 1.0
        for b in s.by_days_out:
            filled = max(1, round(b.median / hi * BAR_WIDTH))   # bars start at zero
            delta = (b.median - best) / best * 100
            tag = " best" if b is s.cheapest_days_out else f"{delta:+5.1f}%"
            flag = " (thin)" if b.thin else ""
            out.append(f"    {b.key:>9}  {'█' * filled:<{BAR_WIDTH}} "
                       f"{money(b.median, ccy):>12} {tag}  n={b.n}{flag}")

    if s.notes:
        out.append("")
        for note in s.notes:
            out.append(f"  ! {note}")
    return "\n".join(out)


# ---------------------------------------------------------------------- HTML

STYLE = """
:root {
  color-scheme: light;
  --plane: #f9f9f7; --surface-1: #fcfcfb;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-1-soft: rgba(42,120,214,0.12);
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --up: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --plane: #0d0d0d; --surface-1: #1a1a19;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-1-soft: rgba(57,135,229,0.16);
    --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --up: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane: #0d0d0d; --surface-1: #1a1a19;
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-1-soft: rgba(57,135,229,0.16);
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --up: #0ca30c;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--plane); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 15px; line-height: 1.5;
}
.wrap { max-width: 1000px; margin: 0 auto; padding: 32px 16px 64px; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline;
         justify-content: space-between; margin-bottom: 8px; }
h1 { font-size: 1.5rem; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 1.15rem; margin: 0 0 2px; letter-spacing: -0.01em; }
h3 { font-size: 0.80rem; margin: 26px 0 8px; text-transform: uppercase;
     letter-spacing: 0.07em; color: var(--text-secondary); font-weight: 600; }
.sub { color: var(--text-secondary); font-size: 0.875rem; margin: 0; }
button.theme { font: inherit; font-size: 0.8125rem; cursor: pointer; padding: 5px 12px;
  border-radius: 999px; border: 1px solid var(--border); background: var(--surface-1);
  color: var(--text-secondary); }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px;
        padding: 22px; margin: 18px 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
         gap: 2px; margin-top: 18px; background: var(--border); border: 1px solid var(--border);
         border-radius: 10px; overflow: hidden; }
.tile { background: var(--surface-1); padding: 12px 14px; }
.tile .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
               color: var(--muted); }
.tile .value { font-size: 1.35rem; font-weight: 600; letter-spacing: -0.02em; margin-top: 2px; }
.tile .value.small { font-size: 1.0rem; font-weight: 600; }
.tile .foot { font-size: 0.78rem; color: var(--text-secondary); }
.verdict { display: inline-flex; align-items: center; gap: 8px; margin-top: 14px;
  padding: 7px 13px; border-radius: 999px; font-size: 0.86rem; font-weight: 600;
  border: 1px solid var(--border); color: var(--text-primary); }
.verdict .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.meter { height: 6px; border-radius: 3px; background: var(--grid); margin-top: 7px;
         overflow: hidden; }
.meter > span { display: block; height: 100%; background: var(--series-1); border-radius: 3px; }
.chart { width: 100%; height: auto; display: block; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.series { fill: none; stroke: var(--series-1); stroke-width: 2; stroke-linejoin: round;
          stroke-linecap: round; }
.area { fill: var(--series-1-soft); stroke: none; }
.threshold { stroke: var(--critical); stroke-width: 2; stroke-dasharray: 5 4; }
.threshold-label, .marker-label {
  paint-order: stroke; stroke: var(--surface-1); stroke-width: 3.5px;
  stroke-linejoin: round; font-size: 11px; font-weight: 600;
}
.threshold-label { fill: var(--critical); }
.marker { fill: var(--series-1); }
.marker-ring { fill: var(--surface-1); }
.marker-label { fill: var(--text-primary); }
.range { stroke: var(--series-1); stroke-width: 2; opacity: 0.35; }
.dot { fill: var(--series-1); }
.dot-ring { fill: var(--surface-1); }
.dot-low { fill: var(--series-1); opacity: 0.45; }
.key-dot, .key-low { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
  background: var(--series-1); margin-right: -8px; }
.key-low { opacity: 0.45; width: 8px; height: 8px; }
.thin-flag { fill: var(--muted); font-size: 9px; }
.cell { stroke: var(--surface-1); stroke-width: 0; }
.cell.best-cell { stroke: var(--text-primary); }
.empty-cell { fill: var(--grid); }
.crosshair { stroke: var(--axis); stroke-width: 1; stroke-dasharray: 3 3; }
.focus { fill: var(--series-1); stroke: var(--surface-1); stroke-width: 2; }
.hit { fill: transparent; cursor: crosshair; }
.tooltip { position: fixed; pointer-events: none; z-index: 20; display: none;
  background: var(--surface-1); color: var(--text-primary); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 10px; font-size: 0.8125rem; line-height: 1.35;
  box-shadow: 0 6px 20px rgba(0,0,0,0.16); }
.tooltip b { font-variant-numeric: tabular-nums; }
.chart-note { font-size: 0.85rem; color: var(--text-secondary); margin: 10px 0 0; }
.swatch-ring { display: inline-block; width: 11px; height: 11px; border-radius: 3px;
  border: 2px solid var(--text-primary); vertical-align: -1px; margin-right: 4px; }
.legend { display: flex; gap: 14px; align-items: center; font-size: 0.8rem;
          color: var(--text-secondary); margin-top: 10px; flex-wrap: wrap; }
.legend .ramp { display: inline-flex; gap: 2px; }
.legend .ramp i { width: 16px; height: 11px; border-radius: 2px; display: block;
                  border: 1px solid var(--border); }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
th { color: var(--text-secondary); font-weight: 600; font-size: 0.75rem;
     text-transform: uppercase; letter-spacing: 0.05em; }
tbody tr:last-child td { border-bottom: none; }
details { margin-top: 18px; }
summary { cursor: pointer; font-size: 0.85rem; color: var(--text-secondary); }
.notes { margin: 16px 0 0; padding: 0; list-style: none; }
.notes li { font-size: 0.85rem; color: var(--text-secondary); padding-left: 18px;
            position: relative; margin-top: 5px; }
.notes li::before { content: "!"; position: absolute; left: 4px; color: var(--warning);
                    font-weight: 700; }
.empty { color: var(--muted); font-size: 0.875rem; }
footer { margin-top: 36px; font-size: 0.8rem; color: var(--muted); }
@media (max-width: 640px) { .wrap { padding: 20px 16px 48px; } h1 { font-size: 1.25rem; } }
"""

SCRIPT = """
(function () {
  var root = document.documentElement, tip = document.getElementById('tip');
  var btn = document.getElementById('theme');
  if (btn) btn.addEventListener('click', function () {
    var dark = getComputedStyle(root).getPropertyValue('--plane').trim() === '#0d0d0d';
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });

  function show(html, x, y) {
    tip.innerHTML = html; tip.style.display = 'block';
    var r = tip.getBoundingClientRect();
    tip.style.left = Math.min(x + 14, window.innerWidth - r.width - 8) + 'px';
    tip.style.top = Math.max(y - r.height - 12, 8) + 'px';
  }
  function hide() { tip.style.display = 'none'; }

  document.querySelectorAll('svg.chart[data-t0]').forEach(function (svg) {
    var cfg = JSON.parse(document.getElementById(svg.id + '-data').textContent);
    var hit = document.getElementById(svg.id + '-hit');
    var cross = document.getElementById(svg.id + '-cross');
    var focus = document.getElementById(svg.id + '-focus');
    var t0 = +svg.dataset.t0, t1 = +svg.dataset.t1;
    var padL = +svg.dataset.padl, plotW = +svg.dataset.plotw;

    function move(ev) {
      var box = svg.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var sx = (ev.clientX - box.left) / box.width * vb.width;
      var t = t0 + (sx - padL) / plotW * (t1 - t0);
      var best = cfg.points[0], bd = Infinity;
      for (var i = 0; i < cfg.points.length; i++) {
        var d = Math.abs(cfg.points[i][0] - t);
        if (d < bd) { bd = d; best = cfg.points[i]; }
      }
      var px = padL + (best[0] - t0) / (t1 - t0) * plotW;
      var py = cfg.padT + (cfg.hi - best[1]) / (cfg.hi - cfg.lo) * cfg.plotH;
      cross.setAttribute('x1', px); cross.setAttribute('x2', px);
      cross.style.display = ''; focus.setAttribute('cx', px);
      focus.setAttribute('cy', py); focus.style.display = '';
      show('<b>' + cfg.currency + ' ' + best[1].toLocaleString(undefined,
           {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</b><br>' + best[2],
           ev.clientX, ev.clientY);
    }
    hit.addEventListener('mousemove', move);
    hit.addEventListener('mouseleave', function () {
      hide(); cross.style.display = 'none'; focus.style.display = 'none';
    });
  });

  document.querySelectorAll('svg.heatmap rect, svg .dot, svg .dot-low').forEach(function (el) {
    var t = el.querySelector('title');
    if (!t) return;
    var text = t.textContent;
    el.addEventListener('mousemove', function (ev) { show(text, ev.clientX, ev.clientY); });
    el.addEventListener('mouseleave', hide);
  });
})();
"""

VERDICT_COLOR = {"BOOK": "var(--good)", "AT T": "var(--good)", "STRO": "var(--good)",
                 "GOOD": "var(--series-1)", "WAIT": "var(--critical)", "TYPI": "var(--warning)"}


def _verdict_color(verdict: str) -> str:
    return VERDICT_COLOR.get(verdict[:4].upper(), "var(--muted)")


def _tile(label: str, value: str, foot: str = "", small: bool = False, meter: int | None = None) -> str:
    cls = "value small" if small else "value"
    inner = f'<div class="label">{esc(label)}</div><div class="{cls}">{value}</div>'
    if meter is not None:
        inner += f'<div class="meter"><span style="width:{max(0, min(100, meter))}%"></span></div>'
    if foot:
        inner += f'<div class="foot">{foot}</div>'
    return f'<div class="tile">{inner}</div>'


def _delta(value: float | None) -> str:
    if value is None:
        return '<span style="color:var(--muted)">n/a</span>'
    colour = "var(--up)" if value < 0 else ("var(--critical)" if value > 0 else "var(--muted)")
    return f'<span style="color:{colour}">{value:+.1f}%</span>'


def _route_section(summary: Summary, quotes: Sequence[Quote], index: int) -> str:
    s, ccy = summary, summary.currency
    parts = [f'<section class="card" id="route-{s.route.id}">']
    itinerary = f"{s.route.origin} \u2192 {s.route.destination}  {s.route.depart_date}"
    if s.route.return_date:
        itinerary += f" \u2192 {s.route.return_date}"
    meta = [itinerary, s.route.provider, s.route.cabin.title(), f"{s.route.adults} pax",
            f"every {s.route.interval_minutes} min"]
    if s.route.depart_flex_days or s.route.return_flex_days:
        meta.append(f"flex ±{s.route.depart_flex_days}/{s.route.return_flex_days}d")
    parts.append(f'<h2>{esc(s.route.name)}</h2>')
    parts.append(f'<p class="sub">{esc("  ·  ".join(meta))}</p>')

    if not s.n:
        parts.append('<p class="empty">No observations recorded yet.</p></section>')
        return "".join(parts)

    parts.append(
        f'<div class="verdict"><span class="dot" style="background:{_verdict_color(s.verdict)}">'
        f'</span>{esc(s.verdict)}</div>'
    )

    best_local = s.best_moment_local
    tiles = [
        _tile("Price now", f"{ccy} {s.current:,.0f}",
              f"last checked {s.last_seen:%d %b %H:%M} UTC"),
        _tile("All-time low", f"{ccy} {s.low:,.0f}",
              f"{best_local:%a %d %b, %H:%M} {esc(s.tz)}" if best_local else ""),
        _tile("Typical", f"{ccy} {s.median:,.0f}",
              f"spread {ccy} {s.low:,.0f}–{s.high:,.0f}"),
        _tile("Deal score", f"{s.deal_score}/100",
              f"cheaper than {100 - int(s.percentile * 100)}% of observations",
              meter=s.deal_score),
        _tile("Movement", f"{_delta(s.change_24h)} <span style=\"color:var(--muted)\">24h</span>",
              f"7 days {_delta(s.change_7d)}", small=True),
    ]
    if s.threshold is not None:
        state = "target met" if s.threshold_met else f"{ccy} {s.current - s.threshold:,.0f} to go"
        tiles.append(_tile("Your target", f"{ccy} {s.threshold:,.0f}", state))
    parts.append(f'<div class="tiles">{"".join(tiles)}</div>')

    parts.append("<h3>Price timeline</h3>")
    points = timeline(quotes, s.tz)
    best = (best_local, s.low) if best_local else None
    parts.append(price_line_chart(points, currency=ccy, threshold=s.threshold,
                                  best=best, chart_id=f"line{index}"))

    parts.append(f"<h3>When it is cheapest — weekday × hour ({esc(s.tz)})</h3>")
    cells = heatmap(quotes, s.tz)
    parts.append(weekday_hour_heatmap(cells, centre=s.median, currency=ccy))
    parts.append(
        '<div class="legend"><span>cheaper than typical</span><span class="ramp">'
        + "".join(f'<i class="{step_class(i)}"></i>' for i in range(-ARM_STEPS, ARM_STEPS + 1))
        + "</span><span>dearer</span></div>"
    )

    parts.append("<h3>Booking window — median by days before departure</h3>")
    parts.append(days_out_curve(s.by_days_out, currency=ccy))

    if s.notes:
        parts.append('<ul class="notes">'
                     + "".join(f"<li>{esc(n)}</li>" for n in s.notes) + "</ul>")

    parts.append(_data_table(s, quotes))
    parts.append("</section>")
    return "".join(parts)


def _data_table(summary: Summary, quotes: Sequence[Quote], rows: int = 40) -> str:
    zone = ZoneInfo(summary.tz)
    recent = sorted(quotes, key=lambda q: q.ts, reverse=True)[:rows]
    body = "".join(
        f"<tr><td>{q.ts.astimezone(zone):%Y-%m-%d %H:%M}</td>"
        f'<td class="num">{q.price:,.2f}</td>'
        f"<td>{esc(q.depart_date)}{esc(' → ' + str(q.return_date)) if q.return_date else ''}</td>"
        f"<td>{esc(q.carrier or '—')}</td>"
        f'<td class="num">{q.stops}</td></tr>'
        for q in recent
    )
    return (
        f"<details><summary>Data table — most recent {len(recent)} of {summary.n} "
        f"observations</summary><table><thead><tr><th>Time ({esc(summary.tz)})</th>"
        f'<th class="num">{esc(summary.currency)}</th><th>Dates</th><th>Carrier</th>'
        f'<th class="num">Stops</th></tr></thead><tbody>{body}</tbody></table></details>'
    )


def _overview(summaries: Sequence[Summary]) -> str:
    rows = []
    for s in summaries:
        if not s.n:
            rows.append(f"<tr><td>{esc(s.route.name)}</td><td colspan='5' class='empty'>"
                        f"no data yet</td></tr>")
            continue
        rows.append(
            f'<tr><td><a href="#route-{s.route.id}">{esc(s.route.name)}</a></td>'
            f'<td class="num">{s.currency} {s.current:,.0f}</td>'
            f'<td class="num">{s.currency} {s.low:,.0f}</td>'
            f'<td class="num">{s.currency} {s.median:,.0f}</td>'
            f'<td class="num">{s.deal_score}</td>'
            f'<td><span class="dot" style="display:inline-block;width:8px;height:8px;'
            f'border-radius:50%;background:{_verdict_color(s.verdict)};margin-right:6px">'
            f'</span>{esc(s.verdict.split(" - ")[0])}</td></tr>'
        )
    return (
        '<section class="card"><h2>All routes</h2>'
        '<table><thead><tr><th>Route</th><th class="num">Now</th><th class="num">Low</th>'
        '<th class="num">Typical</th><th class="num">Score</th><th>Call</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def render_html(
    db: Database,
    routes: Sequence[Route],
    *,
    tz: str = "UTC",
    window_days: int = 30,
) -> str:
    since = int((datetime.now(timezone.utc).timestamp()) - window_days * 86400)
    summaries, quote_sets = [], []
    for route in routes:
        quotes = db.quotes(route.id, since_epoch=since)
        summaries.append(summarise(route, quotes, tz=tz, window_days=window_days))
        quote_sets.append(quotes)

    sections = "".join(
        _route_section(s, q, i) for i, (s, q) in enumerate(zip(summaries, quote_sets))
    )
    generated = datetime.now(ZoneInfo(tz))
    overview = _overview(summaries) if len(summaries) > 1 else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight price tracker</title>
<style>{STYLE}\n{diverging_css()}</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <h1>Flight price tracker</h1>
    <p class="sub">{len(summaries)} route{'s' if len(summaries) != 1 else ''} · last
       {window_days} days · generated {generated:%d %b %Y %H:%M} {esc(tz)}</p>
  </div>
  <button class="theme" id="theme" type="button">Toggle theme</button>
</header>
{overview}
{sections}
<footer>Prices are observations from the configured provider, not offers. Verify on the
airline's own site before booking.</footer>
</div>
<div class="tooltip" id="tip" role="status" aria-live="polite"></div>
<script>{SCRIPT}</script>
</body>
</html>"""


def write_html(
    db: Database,
    routes: Sequence[Route],
    path: str | Path,
    *,
    tz: str = "UTC",
    window_days: int = 30,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(db, routes, tz=tz, window_days=window_days), encoding="utf-8")
    return target
