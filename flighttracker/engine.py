"""Polling engine: fetch, store, evaluate alert rules, and keep every route
on its own schedule concurrently."""

from __future__ import annotations

import logging
import random
import signal
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

from flighttracker.config import Settings, load_settings
from flighttracker.db import Database
from flighttracker.models import Alert, Quote, Route, utcnow
from flighttracker.notify import Notifier
from flighttracker.providers import ProviderError, get_provider

log = logging.getLogger("flighttracker.engine")

#: an all-time low is only newsworthy once there is something to compare against
MIN_HISTORY_FOR_LOW = 6


@dataclass(slots=True)
class PollResult:
    route: Route
    quote: Quote | None = None
    alerts: list[Alert] = None            # type: ignore[assignment]
    error: str = ""

    def __post_init__(self) -> None:
        if self.alerts is None:
            self.alerts = []


class Engine:
    def __init__(self, db: Database, settings: Settings | None = None,
                 notifier: Notifier | None = None):
        self.db = db
        self.settings = settings or load_settings()
        self.notifier = notifier or Notifier(self.settings)
        self._provider_locks: dict[str, threading.Lock] = {}
        self._provider_last_call: dict[str, float] = {}

    # ------------------------------------------------------------ polling

    def _throttle(self, provider_name: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        lock = self._provider_locks.setdefault(provider_name, threading.Lock())
        with lock:
            elapsed = time.monotonic() - self._provider_last_call.get(provider_name, 0.0)
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._provider_last_call[provider_name] = time.monotonic()

    def poll(self, route: Route, notify: bool = True) -> PollResult:
        """One observation for one route: search, store the cheapest, alert."""
        assert route.id is not None
        if route.depart_date < date.today():
            self.db.set_route_enabled(route.id, False)
            self.db.log_poll(route.id, False, "departure date passed - route disabled")
            return PollResult(route=route, error="departure date has passed; route disabled")

        try:
            provider = get_provider(route.provider)
            self._throttle(provider.name, provider.min_interval_seconds)
            offer = provider.cheapest(route)
        except ProviderError as exc:
            self.db.log_poll(route.id, False, str(exc))
            log.warning("%s: %s", route.name, exc)
            return PollResult(route=route, error=str(exc))
        except Exception as exc:                          # noqa: BLE001 - keep the loop alive
            self.db.log_poll(route.id, False, f"{type(exc).__name__}: {exc}")
            log.exception("%s: unexpected provider failure", route.name)
            return PollResult(route=route, error=f"{type(exc).__name__}: {exc}")

        if offer is None:
            self.db.log_poll(route.id, False, "no offers returned")
            return PollResult(route=route, error="no offers returned")

        prior_low = self.db.min_quote(route.id)
        prior_count = self.db.quote_count(route.id)
        recent = self.db.quotes(route.id, since_epoch=int((utcnow() - timedelta(days=7)).timestamp()))

        quote = Quote.from_offer(route.id, offer, provider.name)
        self.db.add_quote(quote)
        self.db.log_poll(route.id, True, f"{quote.currency} {quote.price}")

        alerts = self._evaluate(route, quote, prior_low, prior_count, recent)
        for alert in alerts:
            self.db.add_alert(alert)
            if notify:
                sent = self.notifier.send(route, alert, quote.deep_link)
                if sent:
                    self.db.mark_alert_delivered(alert.id)
        return PollResult(route=route, quote=quote, alerts=alerts)

    # ------------------------------------------------------------- alerting

    def _on_cooldown(self, route_id: int, kind: str) -> bool:
        last = self.db.last_alert_epoch(route_id, kind)
        if last is None:
            return False
        return (time.time() - last) < self.settings.cooldown_minutes * 60

    def _evaluate(
        self,
        route: Route,
        quote: Quote,
        prior_low: Quote | None,
        prior_count: int,
        recent: list[Quote],
    ) -> list[Alert]:
        alerts: list[Alert] = []
        ccy = quote.currency

        def make(kind: str, message: str) -> Alert:
            return Alert(
                route_id=route.id, quote_id=quote.id, ts=quote.ts, kind=kind,
                price=quote.price, currency=ccy, message=message,
            )

        dates = f"{quote.depart_date}"
        if quote.return_date:
            dates += f" → {quote.return_date}"

        if route.threshold is not None and quote.price <= route.threshold:
            if not self._on_cooldown(route.id, "threshold"):
                under = route.threshold - quote.price
                alerts.append(make(
                    "threshold",
                    f"At or below your {ccy} {route.threshold:,.0f} target "
                    f"(by {ccy} {under:,.0f}) on {dates}"
                    + (f" with {quote.carrier}" if quote.carrier else ""),
                ))

        if (
            self.settings.alert_on_all_time_low
            and prior_low is not None
            and prior_count >= MIN_HISTORY_FOR_LOW
            and quote.price < prior_low.price
            and not self._on_cooldown(route.id, "all_time_low")
        ):
            delta = prior_low.price - quote.price
            alerts.append(make(
                "all_time_low",
                f"New low since tracking began - {ccy} {delta:,.0f} under the previous best "
                f"of {ccy} {prior_low.price:,.0f} (set {prior_low.ts:%Y-%m-%d %H:%M} UTC) on {dates}",
            ))

        if len(recent) >= MIN_HISTORY_FOR_LOW and not self._on_cooldown(route.id, "drop"):
            baseline = statistics.median(q.price for q in recent)
            if baseline > 0:
                drop = (baseline - quote.price) / baseline * 100
                if drop >= self.settings.drop_pct:
                    alerts.append(make(
                        "drop",
                        f"Down {drop:.1f}% against the 7-day median of {ccy} {baseline:,.0f} on {dates}",
                    ))
        return alerts

    # ------------------------------------------------------------ scheduling

    def due_routes(self, force: bool = False) -> list[Route]:
        now = time.time()
        due: list[Route] = []
        for route in self.db.list_routes(enabled_only=True):
            if force:
                due.append(route)
                continue
            last = self.db.last_poll_epoch(route.id)
            if last is None or now - last >= route.interval_minutes * 60:
                due.append(route)
        return due

    def sweep(self, force: bool = False, notify: bool = True) -> list[PollResult]:
        """Poll every route that is due, concurrently. One pass, then return."""
        from concurrent.futures import ThreadPoolExecutor

        routes = self.due_routes(force=force)
        if not routes:
            return []
        workers = max(1, min(self.settings.max_workers, len(routes)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="poll") as pool:
            return list(pool.map(lambda r: self.poll(r, notify=notify), routes))

    def watch(self, tick_seconds: int = 30, notify: bool = True) -> None:
        """Run until interrupted, polling each route on its own interval."""
        stop = threading.Event()

        def _stop(signum, _frame):
            log.info("signal %s received - finishing current sweep", signum)
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _stop)
            except ValueError:
                pass                                       # not on the main thread

        jitter = max(self.settings.jitter_pct, 0.0) / 100.0
        log.info(
            "watching %d route(s); channels: %s",
            len(self.db.list_routes(enabled_only=True)),
            ", ".join(self.settings.active_channels()),
        )
        while not stop.is_set():
            started = time.monotonic()
            try:
                for result in self.sweep(notify=notify):
                    if result.error:
                        log.warning("%s: %s", result.route.name, result.error)
                    elif result.quote:
                        log.info(
                            "%s: %s %.2f%s", result.route.name, result.quote.currency,
                            result.quote.price,
                            f"  [{len(result.alerts)} alert(s)]" if result.alerts else "",
                        )
            except Exception:                              # noqa: BLE001 - the loop outlives failures
                log.exception("sweep failed; continuing")
            delay = tick_seconds * (1 + random.uniform(-jitter, jitter))
            stop.wait(max(1.0, delay - (time.monotonic() - started)))
        log.info("stopped")

    # ------------------------------------------------------------- simulate

    def simulate(self, route: Route, days: int = 30, every_minutes: int = 60) -> int:
        """Backfill synthetic history so the analysis has something to chew on.

        Only valid for the `mock` provider - it is the one that can price a past
        instant. Existing quotes for the route are left alone.
        """
        from flighttracker.providers.mock import MockProvider

        provider = get_provider(route.provider)
        if not isinstance(provider, MockProvider):
            raise ProviderError(
                f"simulate needs the 'mock' provider (route uses {route.provider!r}); "
                "real providers cannot price the past"
            )
        assert route.id is not None
        end = utcnow().replace(second=0, microsecond=0)
        start = end - timedelta(days=days)
        written = 0
        when = start
        while when <= end:
            offers = provider.offers_at(route, when)
            if route.max_stops is not None:
                offers = [o for o in offers if o.stops <= route.max_stops] or offers
            if offers:
                best = min(offers, key=lambda o: o.price)
                self.db.add_quote(Quote.from_offer(route.id, best, provider.name, ts=when))
                written += 1
            when += timedelta(minutes=every_minutes)
        self.db.log_poll(route.id, True, f"simulated {written} observations")
        return written
