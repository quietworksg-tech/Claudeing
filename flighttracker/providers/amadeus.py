"""Amadeus Self-Service 'Flight Offers Search'.

Credentials (free test tier at developers.amadeus.com):
    AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET
    AMADEUS_HOST=production   # optional; defaults to the test host
"""

from __future__ import annotations

import os
import re
import time
from datetime import date

from flighttracker.models import Offer, Route
from flighttracker.providers.base import Provider, ProviderError, http_json

TEST_HOST = "https://test.api.amadeus.com"
PROD_HOST = "https://api.amadeus.com"
_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def _iso_duration_minutes(value: str | None) -> int | None:
    if not value:
        return None
    m = _DURATION.fullmatch(value)
    if not m:
        return None
    days, hours, mins = (int(g) if g else 0 for g in m.groups())
    return days * 1440 + hours * 60 + mins


class AmadeusProvider(Provider):
    name = "amadeus"
    min_interval_seconds = 1

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 host: str | None = None):
        self.client_id = client_id or os.environ.get("AMADEUS_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("AMADEUS_CLIENT_SECRET", "")
        env_host = host or os.environ.get("AMADEUS_HOST", "test")
        self.host = PROD_HOST if env_host.lower() in ("production", "prod", PROD_HOST) else TEST_HOST
        self._token = ""
        self._token_expires = 0.0

    def _access_token(self) -> str:
        if not self.client_id or not self.client_secret:
            raise ProviderError(
                "Amadeus credentials missing - set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET"
            )
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        payload = http_json(
            f"{self.host}/v1/security/oauth2/token",
            method="POST",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        self._token = payload.get("access_token", "")
        self._token_expires = time.time() + float(payload.get("expires_in", 1799))
        if not self._token:
            raise ProviderError("Amadeus returned no access_token")
        return self._token

    def _search_pair(self, route: Route, depart: date, back: date | None) -> list[Offer]:
        params: dict[str, object] = {
            "originLocationCode": route.origin,
            "destinationLocationCode": route.destination,
            "departureDate": depart.isoformat(),
            "adults": route.adults,
            "currencyCode": route.currency,
            "travelClass": route.cabin,
            "max": 20,
        }
        if back is not None:
            params["returnDate"] = back.isoformat()
        if route.max_stops == 0:
            params["nonStop"] = "true"

        payload = http_json(
            f"{self.host}/v2/shopping/flight-offers",
            params=params,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        offers: list[Offer] = []
        for item in payload.get("data", []) or []:
            itineraries = item.get("itineraries", []) or []
            if not itineraries:
                continue
            segs_out = itineraries[0].get("segments", []) or []
            segs_back = itineraries[1].get("segments", []) if len(itineraries) > 1 else []
            stops = max(len(segs_out) - 1, len(segs_back) - 1 if segs_back else 0)
            duration = sum(
                _iso_duration_minutes(it.get("duration")) or 0 for it in itineraries
            ) or None
            carriers = item.get("validatingAirlineCodes") or []
            offers.append(
                Offer(
                    price=float(item["price"]["grandTotal"]),
                    currency=item["price"].get("currency", route.currency),
                    depart_date=depart,
                    return_date=back,
                    carrier=carriers[0] if carriers else "",
                    stops=stops,
                    duration_minutes=duration,
                    out_depart_time=(segs_out[0]["departure"]["at"][11:16] if segs_out else None),
                    ret_depart_time=(segs_back[0]["departure"]["at"][11:16] if segs_back else None),
                    deep_link=None,
                    raw={"id": item.get("id"), "source": item.get("source")},
                )
            )
        return offers

    def search(self, route: Route) -> list[Offer]:
        if route.is_open_jaw:
            # Amadeus's GET search is one origin/destination pair; an open-jaw
            # itinerary is priced as two one-way legs summed (see openjaw.py).
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
