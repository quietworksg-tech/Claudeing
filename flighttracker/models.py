"""Core domain objects shared by providers, storage and analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass(slots=True)
class Route:
    """A round-trip (or one-way) itinerary being watched."""

    origin: str
    destination: str
    depart_date: date
    return_date: date | None = None
    adults: int = 1
    cabin: str = "ECONOMY"
    currency: str = "USD"
    max_stops: int | None = None
    threshold: float | None = None
    interval_minutes: int = 60
    depart_flex_days: int = 0
    return_flex_days: int = 0
    provider: str = "mock"
    enabled: bool = True
    label: str | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        self.origin = self.origin.strip().upper()
        self.destination = self.destination.strip().upper()
        self.cabin = self.cabin.strip().upper()
        self.currency = self.currency.strip().upper()
        self.depart_date = parse_date(self.depart_date)
        if self.return_date is not None:
            self.return_date = parse_date(self.return_date)

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        leg = f"{self.origin}-{self.destination}"
        if self.return_date:
            return f"{leg} {self.depart_date}/{self.return_date}"
        return f"{leg} {self.depart_date}"

    def date_pairs(self) -> list[tuple[date, date | None]]:
        """Expand the flexible-date window into concrete (out, back) pairs.

        The trip length is held constant so a +/- flex window slides the whole
        round trip rather than stretching it.
        """
        from datetime import timedelta

        pairs: list[tuple[date, date | None]] = []
        for d_off in range(-self.depart_flex_days, self.depart_flex_days + 1):
            out = self.depart_date + timedelta(days=d_off)
            if self.return_date is None:
                pairs.append((out, None))
                continue
            for r_off in range(-self.return_flex_days, self.return_flex_days + 1):
                back = self.return_date + timedelta(days=r_off)
                if back < out:
                    continue
                pairs.append((out, back))
        return pairs


@dataclass(slots=True)
class Offer:
    """One priced itinerary returned by a provider."""

    price: float
    currency: str
    depart_date: date
    return_date: date | None = None
    carrier: str = ""
    stops: int = 0
    duration_minutes: int | None = None
    out_depart_time: str | None = None
    ret_depart_time: str | None = None
    deep_link: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.depart_date = parse_date(self.depart_date)
        if self.return_date is not None:
            self.return_date = parse_date(self.return_date)
        self.price = round(float(self.price), 2)


@dataclass(slots=True)
class Quote:
    """A stored observation: the cheapest offer for a route at a moment in time."""

    route_id: int
    ts: datetime
    price: float
    currency: str
    depart_date: date
    return_date: date | None
    carrier: str = ""
    stops: int = 0
    duration_minutes: int | None = None
    out_depart_time: str | None = None
    ret_depart_time: str | None = None
    deep_link: str | None = None
    provider: str = "mock"
    raw_json: str | None = None
    id: int | None = None

    @classmethod
    def from_offer(cls, route_id: int, offer: Offer, provider: str, ts: datetime | None = None) -> "Quote":
        return cls(
            route_id=route_id,
            ts=ts or utcnow(),
            price=offer.price,
            currency=offer.currency,
            depart_date=offer.depart_date,
            return_date=offer.return_date,
            carrier=offer.carrier,
            stops=offer.stops,
            duration_minutes=offer.duration_minutes,
            out_depart_time=offer.out_depart_time,
            ret_depart_time=offer.ret_depart_time,
            deep_link=offer.deep_link,
            provider=provider,
            raw_json=json.dumps(offer.raw, default=str) if offer.raw else None,
        )


@dataclass(slots=True)
class Alert:
    route_id: int
    quote_id: int | None
    ts: datetime
    kind: str          # threshold | all_time_low | drop
    price: float
    currency: str
    message: str
    delivered: bool = False
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d
