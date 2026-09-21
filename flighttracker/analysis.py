"""Turn a route's price history into the answer: how good is today, and when
has this fare actually been cheapest?

Everything here is descriptive statistics over observed quotes. Where a result
is thin (too few samples in a bucket) it says so rather than pretending.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from flighttracker.models import Quote, Route

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MIN_BUCKET_SAMPLES = 3


@dataclass(slots=True)
class Bucket:
    key: str
    n: int
    minimum: float
    median: float
    maximum: float = 0.0

    @property
    def thin(self) -> bool:
        return self.n < MIN_BUCKET_SAMPLES


@dataclass(slots=True)
class Summary:
    route: Route
    tz: str
    n: int
    window_days: int
    currency: str = "USD"
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    current: float | None = None
    low: float | None = None
    high: float | None = None
    median: float | None = None
    mean: float | None = None
    stdev: float | None = None
    best_quote: Quote | None = None
    percentile: float | None = None      # where `current` sits in the observed range, 0 = cheapest
    deal_score: int | None = None        # 0-100, higher = better time to buy
    verdict: str = "no data"
    change_24h: float | None = None
    change_7d: float | None = None
    threshold: float | None = None
    threshold_met: bool = False
    by_hour: list[Bucket] = field(default_factory=list)
    by_weekday: list[Bucket] = field(default_factory=list)
    by_days_out: list[Bucket] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best_moment_local(self) -> datetime | None:
        if not self.best_quote:
            return None
        return self.best_quote.ts.astimezone(ZoneInfo(self.tz))

    @property
    def cheapest_hour(self) -> Bucket | None:
        return _pick_cheapest(self.by_hour)

    @property
    def cheapest_weekday(self) -> Bucket | None:
        return _pick_cheapest(self.by_weekday)

    @property
    def cheapest_days_out(self) -> Bucket | None:
        return _pick_cheapest(self.by_days_out)


def _pick_cheapest(buckets: Sequence[Bucket]) -> Bucket | None:
    solid = [b for b in buckets if not b.thin]
    pool = solid or list(buckets)
    return min(pool, key=lambda b: b.median) if pool else None


def percentile_rank(values: Sequence[float], target: float) -> float:
    """Fraction of observations at or below `target`, 0.0 - 1.0."""
    if not values:
        return 0.0
    return sum(1 for v in values if v <= target) / len(values)


def _bucketise(pairs: Iterable[tuple[str, float]]) -> list[Bucket]:
    grouped: dict[str, list[float]] = {}
    for key, price in pairs:
        grouped.setdefault(key, []).append(price)
    return [
        Bucket(key=k, n=len(v), minimum=min(v), median=statistics.median(v), maximum=max(v))
        for k, v in grouped.items()
    ]


def _change(quotes: Sequence[Quote], current: float, hours: int) -> float | None:
    """Percent change vs the last quote at least `hours` old. None if history is short."""
    cutoff = quotes[-1].ts - timedelta(hours=hours)
    past = [q for q in quotes if q.ts <= cutoff]
    if not past or past[-1].price == 0:
        return None
    return round((current - past[-1].price) / past[-1].price * 100, 1)


def summarise(
    route: Route,
    quotes: Sequence[Quote],
    *,
    tz: str = "UTC",
    window_days: int = 30,
) -> Summary:
    zone = ZoneInfo(tz)
    summary = Summary(
        route=route, tz=tz, n=len(quotes), window_days=window_days,
        currency=route.currency, threshold=route.threshold,
    )
    if not quotes:
        summary.notes.append("No observations yet - run `flighttracker check` or `watch`.")
        return summary

    quotes = sorted(quotes, key=lambda q: q.ts)
    prices = [q.price for q in quotes]
    latest = quotes[-1]

    summary.currency = latest.currency or route.currency
    summary.first_seen = quotes[0].ts
    summary.last_seen = latest.ts
    summary.current = latest.price
    summary.low = min(prices)
    summary.high = max(prices)
    summary.median = round(statistics.median(prices), 2)
    summary.mean = round(statistics.fmean(prices), 2)
    summary.stdev = round(statistics.pstdev(prices), 2) if len(prices) > 1 else 0.0
    summary.best_quote = min(quotes, key=lambda q: (q.price, q.ts))
    summary.percentile = round(percentile_rank(prices, latest.price), 3)
    summary.deal_score = int(round((1 - summary.percentile) * 100))
    summary.change_24h = _change(quotes, latest.price, 24)
    summary.change_7d = _change(quotes, latest.price, 24 * 7)

    if route.threshold is not None:
        summary.threshold_met = latest.price <= route.threshold

    if summary.threshold_met and summary.percentile <= 0.25:
        summary.verdict = "BOOK NOW - under your threshold and near the observed floor"
    elif summary.threshold_met:
        summary.verdict = "AT TARGET - under your threshold, though not the cheapest seen"
    elif summary.percentile <= 0.10:
        summary.verdict = "STRONG - cheapest 10% of everything observed"
    elif summary.percentile <= 0.30:
        summary.verdict = "GOOD - in the cheaper third of the range"
    elif summary.percentile >= 0.80:
        summary.verdict = "WAIT - near the top of the observed range"
    else:
        summary.verdict = "TYPICAL - middle of the observed range"

    local = [(q.ts.astimezone(zone), q.price) for q in quotes]
    summary.by_hour = sorted(
        _bucketise((f"{dt.hour:02d}", p) for dt, p in local), key=lambda b: b.key
    )
    summary.by_weekday = sorted(
        _bucketise((WEEKDAYS[dt.weekday()], p) for dt, p in local),
        key=lambda b: WEEKDAYS.index(b.key),
    )
    summary.by_days_out = sorted(
        _bucketise(
            (_days_out_bucket((route.depart_date - q.ts.astimezone(zone).date()).days), q.price)
            for q in quotes
        ),
        key=lambda b: -_bucket_sort_key(b.key),
    )

    span_hours = (summary.last_seen - summary.first_seen).total_seconds() / 3600
    if span_hours < 48:
        summary.notes.append(
            f"Only {span_hours:.0f}h of history - time-of-day and weekday patterns "
            "are not meaningful yet. Let it run at least a week."
        )
    if len(prices) < 24:
        summary.notes.append(
            f"{len(prices)} observations so far; a 30-day picture at hourly polling is ~720."
        )
    if summary.stdev and summary.median and summary.stdev / summary.median < 0.01:
        summary.notes.append("This fare has barely moved - the airline is not discounting it.")
    return summary


def _days_out_bucket(days: int) -> str:
    for lo, hi in ((0, 7), (7, 14), (14, 21), (21, 30), (30, 45), (45, 60), (60, 90), (90, 180)):
        if lo <= days < hi:
            return f"{lo}-{hi}d"
    return "180d+" if days >= 180 else "departed"


def _bucket_sort_key(key: str) -> int:
    if key == "departed":
        return -1
    if key == "180d+":
        return 180
    return int(key.split("-")[0])


def timeline(quotes: Sequence[Quote], tz: str = "UTC") -> list[tuple[datetime, float]]:
    zone = ZoneInfo(tz)
    return [(q.ts.astimezone(zone), q.price) for q in sorted(quotes, key=lambda q: q.ts)]


def heatmap(quotes: Sequence[Quote], tz: str = "UTC") -> dict[tuple[int, int], tuple[int, float]]:
    """(weekday, hour) -> (sample count, median price), in local time."""
    zone = ZoneInfo(tz)
    grouped: dict[tuple[int, int], list[float]] = {}
    for q in quotes:
        dt = q.ts.astimezone(zone)
        grouped.setdefault((dt.weekday(), dt.hour), []).append(q.price)
    return {k: (len(v), statistics.median(v)) for k, v in grouped.items()}


def sparkline(values: Sequence[float], width: int = 48) -> str:
    """Braille-free unicode sparkline for terminal output."""
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    if len(values) > width:
        step = len(values) / width
        values = [values[min(int(i * step), len(values) - 1)] for i in range(width)]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return "".join(blocks[min(int((v - lo) / span * len(blocks)), len(blocks) - 1)] for v in values)
