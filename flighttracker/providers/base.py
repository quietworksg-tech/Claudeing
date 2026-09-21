"""Provider interface. A provider turns a Route into a list of priced Offers."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

from flighttracker.models import Offer, Route


class ProviderError(RuntimeError):
    """Raised when a provider cannot return offers (network, auth, quota)."""


class Provider:
    name = "base"

    #: minimum seconds between two outbound searches, enforced by the engine
    min_interval_seconds = 0

    def search(self, route: Route) -> list[Offer]:
        raise NotImplementedError

    def _search_pair(self, route: Route, depart: date, back: date | None) -> list[Offer]:
        """Search exactly one concrete date pair for one origin/destination.

        Used both for a route's own flex-date loop and, via `_search_open_jaw`,
        for each leg of an open-jaw itinerary. `route` carries everything except
        the dates actually queried (`depart`/`back`), so subclasses must use
        the explicit arguments rather than `route.depart_date`/`return_date`.
        """
        raise NotImplementedError

    def _search_open_jaw(self, route: Route) -> list[Offer]:
        """Default open-jaw handling: price each leg as a one-way and combine.

        See `flighttracker.openjaw` for what that approximation means. Any
        provider whose `search()` delegates here for open-jaw routes must
        implement `_search_pair`.
        """
        from flighttracker.openjaw import combine_legs, leg_route

        offers: list[Offer] = []
        errors: list[str] = []
        for depart, back in route.date_pairs():
            if back is None:
                continue
            try:
                out_offers = self._search_pair(leg_route(route, route.origin, route.destination), depart, None)
                in_offers = self._search_pair(
                    leg_route(route, route.return_origin, route.return_destination), back, None
                )
                offers.extend(combine_legs(out_offers, in_offers, depart, back))
            except ProviderError as exc:
                errors.append(f"{depart}/{back}: {exc}")
            if self.min_interval_seconds:
                time.sleep(self.min_interval_seconds)
        if not offers and errors:
            raise ProviderError("; ".join(errors[:3]))
        return offers

    def cheapest(self, route: Route) -> Offer | None:
        offers = [o for o in self.search(route) if o.price > 0]
        if route.max_stops is not None:
            filtered = [o for o in offers if o.stops <= route.max_stops]
            offers = filtered or offers
        return min(offers, key=lambda o: o.price) if offers else None


def http_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    timeout: int = 30,
    retries: int = 3,
) -> Any:
    """GET/POST JSON with bounded exponential backoff. Stdlib only, honours *_PROXY."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    body: bytes | None = None
    hdrs = {"Accept": "application/json", "User-Agent": "flighttracker/0.1"}
    if isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(data, bytes):
        body = data
    hdrs.update(headers or {})

    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last = ProviderError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {detail}")
            if exc.code in (400, 401, 403, 404):
                raise last from exc          # not worth retrying
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = ProviderError(f"{type(exc).__name__}: {exc}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise last or ProviderError("request failed")
