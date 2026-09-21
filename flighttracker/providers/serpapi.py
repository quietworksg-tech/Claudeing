"""Google Flights prices via SerpAPI's `google_flights` engine.

Credentials: SERPAPI_KEY (serpapi.com). Each date pair costs one search, so keep
flex windows small on a metered plan.
"""

from __future__ import annotations

import os
import time
from datetime import date

from flighttracker.models import Offer, Route
from flighttracker.providers.base import Provider, ProviderError, http_json

CABIN_CODES = {"ECONOMY": 1, "PREMIUM_ECONOMY": 2, "BUSINESS": 3, "FIRST": 4}


class SerpApiProvider(Provider):
    name = "serpapi"
    min_interval_seconds = 1

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SERPAPI_KEY", "")

    def _search_pair(self, route: Route, depart: date, back: date | None) -> list[Offer]:
        if not self.api_key:
            raise ProviderError("SerpAPI key missing - set SERPAPI_KEY")
        params: dict[str, object] = {
            "engine": "google_flights",
            "departure_id": route.origin,
            "arrival_id": route.destination,
            "outbound_date": depart.isoformat(),
            "currency": route.currency,
            "adults": route.adults,
            "travel_class": CABIN_CODES.get(route.cabin, 1),
            "type": 1 if back else 2,
            "api_key": self.api_key,
            "hl": "en",
        }
        if back is not None:
            params["return_date"] = back.isoformat()
        if route.max_stops == 0:
            params["stops"] = 1          # SerpAPI: 1 = nonstop only

        payload = http_json("https://serpapi.com/search.json", params=params)
        if payload.get("error"):
            raise ProviderError(str(payload["error"])[:200])

        offers: list[Offer] = []
        for group in ("best_flights", "other_flights"):
            for item in payload.get(group, []) or []:
                price = item.get("price")
                if not price:
                    continue
                legs = item.get("flights", []) or []
                layovers = item.get("layovers", []) or []
                offers.append(
                    Offer(
                        price=float(price),
                        currency=route.currency,
                        depart_date=depart,
                        return_date=back,
                        carrier=(legs[0].get("airline", "") if legs else ""),
                        stops=len(layovers),
                        duration_minutes=item.get("total_duration"),
                        out_depart_time=(
                            (legs[0].get("departure_airport", {}).get("time", "") or "")[-5:]
                            if legs else None
                        ),
                        ret_depart_time=None,
                        deep_link=payload.get("search_metadata", {}).get("google_flights_url"),
                        raw={"type": group, "carbon": item.get("carbon_emissions", {}).get("this_flight")},
                    )
                )
        return offers

    def search(self, route: Route) -> list[Offer]:
        if route.is_open_jaw:
            # Google Flights' one-search-per-itinerary model doesn't expose a
            # combined open-jaw fare here; priced as two one-ways (openjaw.py).
            return self._search_open_jaw(route)
        offers: list[Offer] = []
        errors: list[str] = []
        for depart, back in route.date_pairs():
            try:
                offers.extend(self._search_pair(route, depart, back))
            except ProviderError as exc:
                errors.append(f"{depart}/{back}: {exc}")
            if self.min_interval_seconds:
                time.sleep(self.min_interval_seconds)
        if not offers and errors:
            raise ProviderError("; ".join(errors[:3]))
        return offers
