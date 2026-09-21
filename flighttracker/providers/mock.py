"""Deterministic synthetic fare market.

No API key, no network. Prices are a pure function of (route, observation time),
so the same minute always yields the same fare - which makes the tracker's
analysis reproducible and lets `simulate` backfill a month of history instantly.

The model layers the effects real fares actually show:

  base x booking-curve x weekday x hour-of-day x flash-sale x noise

It is a *simulation*, not a forecast. Point it at a real provider before
spending money on the answer.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timezone

from flighttracker.models import Offer, Route
from flighttracker.providers.base import Provider

CARRIERS = ["BA", "VS", "AA", "DL", "UA", "AF", "KL", "LH", "EK", "QR"]


def _unit(*parts: object) -> float:
    """Deterministic pseudo-random float in [0, 1) from the given parts."""
    digest = hashlib.blake2b("|".join(str(p) for p in parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def _booking_curve(days_out: int) -> float:
    """Fare multiplier by days before departure.

    Cheapest in the ~40-80 day 'prime booking window'; a steep last-minute
    climb inside three weeks; mildly elevated when schedules first load.
    """
    d = max(days_out, 0)
    if d <= 21:
        return 1.28 + 0.55 * ((21 - d) / 21) ** 1.7
    if d <= 90:
        return 0.96 + 0.32 * ((90 - d) / 69) ** 2.2
    return 0.96 + 0.20 * min((d - 90) / 180, 1.0)


def _weekday_factor(dt: datetime) -> float:
    """Fare sales load Monday night and are matched through Wednesday."""
    return [1.010, 0.978, 0.972, 0.992, 1.018, 1.012, 1.004][dt.weekday()]


def _hour_factor(dt: datetime) -> float:
    """Shallow diurnal swing; revenue systems re-price overnight."""
    return 1.0 + 0.022 * math.sin((dt.hour + dt.minute / 60 - 4) / 24 * 2 * math.pi)


def _sale_factor(key: str, dt: datetime) -> float:
    """Occasional flash sale, held for a six-hour bucket."""
    bucket = dt.replace(minute=0, second=0, microsecond=0).hour // 6
    roll = _unit(key, "sale", dt.date(), bucket)
    if roll < 0.030:
        return 0.86
    if roll < 0.075:
        return 0.94
    return 1.0


class MockProvider(Provider):
    name = "mock"

    def route_key(self, route: Route) -> str:
        return f"{route.origin}{route.destination}{route.cabin}{route.adults}"

    def base_price(self, route: Route, depart: date, back: date | None) -> float:
        key = self.route_key(route)
        base = 180 + 940 * _unit(key, "base")
        if back is not None:
            nights = (back - depart).days
            base *= 1.9 + 0.006 * max(nights - 7, 0)          # round trip premium
            if nights < 4:
                base *= 1.06                                   # short stays price worse
            if 5 <= nights <= 14:
                base *= 0.97                                   # sweet-spot stay length
        if route.cabin in ("BUSINESS", "FIRST"):
            base *= 3.4 if route.cabin == "BUSINESS" else 5.8
        return base * route.adults

    def price_at(self, route: Route, when: datetime, depart: date, back: date | None) -> float:
        """The fare this simulated market shows for one date pair at one instant."""
        key = self.route_key(route)
        when = when.astimezone(timezone.utc)
        days_out = (depart - when.date()).days
        minute_bucket = when.replace(second=0, microsecond=0).isoformat()

        price = self.base_price(route, depart, back)
        price *= _booking_curve(days_out)
        price *= _weekday_factor(when)
        price *= _hour_factor(when)
        price *= _sale_factor(key, when)
        price *= 0.985 + 0.030 * _unit(key, depart, back, minute_bucket)
        # slow multi-day drift so the month-long series has real structure
        price *= 1.0 + 0.05 * math.sin(
            (when.timestamp() / 86400 + 40 * _unit(key, "phase")) / 9.0 * 2 * math.pi
        )
        # date-pair offset: neighbouring departure dates are not equally priced
        price *= 0.90 + 0.22 * _unit(key, "pair", depart, back)
        return round(price, 2)

    def offers_at(self, route: Route, when: datetime) -> list[Offer]:
        key = self.route_key(route)
        offers: list[Offer] = []
        for depart, back in route.date_pairs():
            price = self.price_at(route, when, depart, back)
            for rank in range(3):                              # a few carriers per date pair
                seed = _unit(key, depart, back, "carrier", rank)
                stops = 0 if rank == 0 else (1 if seed < 0.7 else 2)
                markup = 1.0 + rank * (0.04 + 0.10 * seed) - (0.06 if stops >= 1 else 0.0)
                carrier = CARRIERS[int(seed * len(CARRIERS))]
                offers.append(
                    Offer(
                        price=round(price * markup, 2),
                        currency=route.currency,
                        depart_date=depart,
                        return_date=back,
                        carrier=carrier,
                        stops=stops,
                        duration_minutes=int(420 + 600 * _unit(key, "dur") + stops * 180),
                        out_depart_time=f"{int(_unit(key, depart, 'oh', rank) * 24):02d}:"
                                        f"{int(_unit(key, depart, 'om', rank) * 12) * 5:02d}",
                        ret_depart_time=(
                            f"{int(_unit(key, back, 'rh', rank) * 24):02d}:"
                            f"{int(_unit(key, back, 'rm', rank) * 12) * 5:02d}"
                            if back else None
                        ),
                        deep_link=None,
                        raw={"simulated": True, "rank": rank},
                    )
                )
        return offers

    def search(self, route: Route) -> list[Offer]:
        return self.offers_at(route, datetime.now(timezone.utc))
