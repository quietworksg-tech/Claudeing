"""Open-jaw pricing: fly into one city, home from another.

No provider here prices a genuine combined open-jaw ticket. Instead, an
open-jaw itinerary is priced as **two separate one-way legs, summed** - which
is a close, slightly pessimistic approximation of real fares. Airlines
occasionally undercut the sum of two one-ways with a true combined ticket;
treat this as an upper bound on what you'll actually pay, not a guarantee.
"""

from __future__ import annotations

from datetime import date

from flighttracker.models import Offer, Route


def leg_route(route: Route, origin: str, destination: str) -> Route:
    """A one-way Route standing in for a single leg of an open-jaw itinerary.

    Only origin/destination vary between legs; the actual date is supplied
    separately to whichever search call uses this (see `Provider._search_pair`
    and each provider's own per-pair search), so `depart_date` here is a
    placeholder required by the constructor, not the value actually queried.
    """
    return Route(
        origin=origin, destination=destination, depart_date=route.depart_date,
        return_date=None, adults=route.adults, cabin=route.cabin,
        currency=route.currency, max_stops=route.max_stops, provider=route.provider,
    )


def combine_legs(
    out_offers: list[Offer], in_offers: list[Offer], depart: date, back: date, cap: int = 5,
) -> list[Offer]:
    """Cross the cheapest few offers on each leg into combined itineraries.

    Capped both sides so a large multi-carrier result set doesn't explode
    combinatorially - the cheapest handful of each leg is what matters for
    picking the overall best price.
    """
    combined: list[Offer] = []
    for out in sorted(out_offers, key=lambda o: o.price)[:cap]:
        for inbound in sorted(in_offers, key=lambda o: o.price)[:cap]:
            carrier = "/".join(c for c in (out.carrier, inbound.carrier) if c)
            duration = (out.duration_minutes or 0) + (inbound.duration_minutes or 0)
            combined.append(Offer(
                price=round(out.price + inbound.price, 2),
                currency=out.currency or inbound.currency,
                depart_date=depart, return_date=back,
                carrier=carrier, stops=out.stops + inbound.stops,
                duration_minutes=duration or None,
                out_depart_time=out.out_depart_time, ret_depart_time=inbound.out_depart_time,
                deep_link=out.deep_link or inbound.deep_link,
                raw={"open_jaw": True, "outbound": out.raw, "inbound": inbound.raw},
            ))
    return combined
