"""Command line interface.

    flighttracker add JFK-LHR --depart 2026-12-10 --return 2026-12-20 --threshold 650
    flighttracker watch
    flighttracker report --html reports/flights.html
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flighttracker import __version__
from flighttracker.analysis import summarise
from flighttracker.config import load_settings
from flighttracker.db import Database
from flighttracker.engine import Engine
from flighttracker.models import Route
from flighttracker.providers import ProviderError, available
from flighttracker.report import money, render_terminal, write_html

log = logging.getLogger("flighttracker")


class CliError(Exception):
    pass


def parse_leg(text: str) -> tuple[str, str]:
    """`JFK-LHR`, `JFK/LHR`, `JFK:LHR` or `JFK LHR` -> ('JFK', 'LHR')."""
    cleaned = text.replace("/", "-").replace(":", "-").replace(" ", "-").strip().upper()
    bits = [b for b in cleaned.split("-") if b]
    if len(bits) != 2 or not all(2 <= len(b) <= 3 for b in bits):
        raise CliError(f"cannot read route {text!r}; expected something like JFK-LHR")
    return bits[0], bits[1]


# ------------------------------------------------------------------ commands

def cmd_add(args, db: Database, engine: Engine) -> int:
    origin, destination = parse_leg(args.route)
    if args.provider not in available():
        raise CliError(f"unknown provider {args.provider!r}; available: {', '.join(available())}")
    if bool(args.return_from) != bool(args.ret) and args.return_from:
        raise CliError("--return-from needs --return (an open jaw still has a return date)")
    route = Route(
        origin=origin, destination=destination, depart_date=args.depart,
        return_date=args.ret, return_origin=args.return_from, return_destination=args.return_to,
        adults=args.adults, cabin=args.cabin,
        currency=args.currency, max_stops=args.max_stops, threshold=args.threshold,
        interval_minutes=args.interval, depart_flex_days=args.flex_depart,
        return_flex_days=args.flex_return, provider=args.provider, label=args.label,
    )
    if route.return_date and route.return_date < route.depart_date:
        raise CliError("return date is before the departure date")
    saved = db.add_route(route)
    pairs = len(saved.date_pairs())
    print(f"tracking #{saved.id}  {saved.name}")
    print(f"  provider {saved.provider}   every {saved.interval_minutes} min"
          + (f"   target {money(saved.threshold, saved.currency)}" if saved.threshold else ""))
    if saved.is_open_jaw:
        print(f"  open jaw: out {saved.origin}→{saved.destination}, "
              f"home {saved.return_origin}→{saved.return_destination}"
              "  (priced as two one-way legs summed - see README)")
    if pairs > 1:
        print(f"  flexible dates: {pairs} date pairs searched per check")
    if args.check_now:
        result = engine.poll(saved)
        if result.error:
            print(f"  first check failed: {result.error}")
        elif result.quote:
            print(f"  first check: {money(result.quote.price, result.quote.currency)}")
    return 0


def cmd_import(args, db: Database, engine: Engine) -> int:
    """Bulk-add routes from JSON (list of objects) or CSV (header row)."""
    path = Path(args.file)
    if not path.exists():
        raise CliError(f"no such file: {path}")
    if path.suffix.lower() == ".csv":
        records = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    else:
        records = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(records, dict):
            records = records.get("routes", [])
    if not records:
        raise CliError("file contained no routes")

    added = 0
    for raw in records:
        raw = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if v not in (None, "")}
        try:
            if "route" in raw:
                raw["origin"], raw["destination"] = parse_leg(raw.pop("route"))
            route = Route(
                origin=raw["origin"], destination=raw["destination"],
                depart_date=raw["depart_date"], return_date=raw.get("return_date"),
                adults=int(raw.get("adults", 1)), cabin=raw.get("cabin", "ECONOMY"),
                currency=raw.get("currency", "USD"),
                max_stops=int(raw["max_stops"]) if "max_stops" in raw else None,
                threshold=float(raw["threshold"]) if "threshold" in raw else None,
                interval_minutes=int(raw.get("interval_minutes", 60)),
                depart_flex_days=int(raw.get("depart_flex_days", 0)),
                return_flex_days=int(raw.get("return_flex_days", 0)),
                return_origin=raw.get("return_origin"), return_destination=raw.get("return_destination"),
                provider=raw.get("provider", args.provider), label=raw.get("label"),
            )
        except (KeyError, ValueError) as exc:
            print(f"  skipped {raw}: {exc}", file=sys.stderr)
            continue
        saved = db.add_route(route)
        added += 1
        print(f"tracking #{saved.id}  {saved.name}")
    print(f"\n{added} route(s) now tracked.")
    return 0


def cmd_combo(args, db: Database, engine: Engine) -> int:
    origin = args.origin.strip().upper()
    destinations = [d.strip().upper() for d in args.to.split(",") if d.strip()]
    if len(destinations) < 2 and not args.no_open_jaw:
        raise CliError("need at least 2 destinations for open-jaw combos "
                       "(or pass --no-open-jaw for a single round trip)")
    if args.provider not in available():
        raise CliError(f"unknown provider {args.provider!r}; available: {', '.join(available())}")

    common = dict(
        adults=args.adults, cabin=args.cabin, currency=args.currency, max_stops=args.max_stops,
        threshold=args.threshold, interval_minutes=args.interval, provider=args.provider,
    )
    round_trips = 0
    for dest in destinations:
        route = Route(origin=origin, destination=dest, depart_date=args.depart,
                      return_date=args.ret, **common)
        saved = db.add_route(route)
        round_trips += 1
        print(f"tracking #{saved.id}  {saved.name}")

    open_jaws = 0
    if not args.no_open_jaw:
        for out_dest in destinations:
            for in_origin in destinations:
                if in_origin == out_dest:
                    continue
                route = Route(origin=origin, destination=out_dest, return_origin=in_origin,
                              return_destination=origin, depart_date=args.depart,
                              return_date=args.ret, **common)
                saved = db.add_route(route)
                open_jaws += 1
                print(f"tracking #{saved.id}  {saved.name}")

    print(f"\n{round_trips + open_jaws} itinerary combination(s) now tracked "
          f"({round_trips} round trip, {open_jaws} open-jaw) across {len(destinations)} "
          f"destination(s). Run `flighttracker report` once there's history to see which wins.")
    return 0


def cmd_list(args, db: Database, engine: Engine) -> int:
    routes = db.list_routes()
    if not routes:
        print("No routes tracked yet. Try: flighttracker add JFK-LHR --depart 2026-12-10 "
              "--return 2026-12-20 --threshold 650")
        return 0
    header = f"{'ID':>3}  {'ROUTE':<34} {'PROV':<8} {'EVERY':>6} {'TARGET':>10} {'LATEST':>11} {'N':>5}  STATE"
    print(header)
    print("-" * len(header))
    for route in routes:
        latest = db.latest_quote(route.id)
        n = db.quote_count(route.id)
        state = "on" if route.enabled else "paused"
        target = f"{route.threshold:,.0f}" if route.threshold is not None else "-"
        price = f"{latest.price:,.0f}" if latest else "-"
        if latest and route.threshold is not None and latest.price <= route.threshold:
            state += " *HIT*"
        print(f"{route.id:>3}  {route.name[:34]:<34} {route.provider:<8} "
              f"{route.interval_minutes:>5}m {target:>10} {price:>11} {n:>5}  {state}")
    return 0


def cmd_check(args, db: Database, engine: Engine) -> int:
    routes = _select_routes(args, db, enabled_only=not args.force)
    if not routes:
        print("nothing to check")
        return 0
    failures = 0
    for route in routes:
        result = engine.poll(route, notify=not args.no_notify)
        if result.error:
            failures += 1
            print(f"#{route.id} {route.name}: ERROR {result.error}", file=sys.stderr)
        elif result.quote:
            flags = "  " + " ".join(f"[{a.kind}]" for a in result.alerts) if result.alerts else ""
            print(f"#{route.id} {route.name}: "
                  f"{money(result.quote.price, result.quote.currency)}{flags}")
    return 1 if failures == len(routes) else 0


def cmd_watch(args, db: Database, engine: Engine) -> int:
    if not db.list_routes(enabled_only=True):
        raise CliError("no enabled routes to watch - add one first")
    engine.watch(tick_seconds=args.tick, notify=not args.no_notify)
    return 0


def cmd_report(args, db: Database, engine: Engine) -> int:
    routes = _select_routes(args, db, enabled_only=False)
    if not routes:
        raise CliError("no routes to report on")
    tz = _validate_tz(args.tz or engine.settings.tz)
    since = int(datetime.now(timezone.utc).timestamp() - args.window * 86400)

    if args.html:
        path = write_html(db, routes, args.html, tz=tz, window_days=args.window)
        print(f"wrote {path}  ({len(routes)} route(s), last {args.window} days)")
        if not args.quiet:
            print(f"open it with:  python3 -m http.server --directory {path.parent} 8000")
        return 0

    for route in routes:
        quotes = db.quotes(route.id, since_epoch=since)
        summary = summarise(route, quotes, tz=tz, window_days=args.window)
        print(render_terminal(summary, quotes))
        print()
    return 0


def cmd_alerts(args, db: Database, engine: Engine) -> int:
    alerts = db.list_alerts(route_id=args.route, limit=args.limit)
    if not alerts:
        print("no alerts recorded")
        return 0
    for alert in alerts:
        route = db.get_route(alert.route_id)
        name = route.name if route else f"route {alert.route_id}"
        mark = "" if alert.delivered else "  (undelivered)"
        print(f"{alert.ts:%Y-%m-%d %H:%M} UTC  [{alert.kind:<13}] {name}"
              f"  {alert.currency} {alert.price:,.2f}{mark}")
        print(f"    {alert.message}")
    return 0


def cmd_simulate(args, db: Database, engine: Engine) -> int:
    routes = _select_routes(args, db, enabled_only=False)
    if not routes:
        raise CliError("no routes to simulate")
    for route in routes:
        try:
            n = engine.simulate(route, days=args.days, every_minutes=args.every)
        except ProviderError as exc:
            print(f"#{route.id} {route.name}: {exc}", file=sys.stderr)
            continue
        print(f"#{route.id} {route.name}: wrote {n} synthetic observations "
              f"over {args.days} days")
    return 0


def cmd_export(args, db: Database, engine: Engine) -> int:
    routes = _select_routes(args, db, enabled_only=False)
    since = int(datetime.now(timezone.utc).timestamp() - args.window * 86400)
    rows = []
    for route in routes:
        for q in db.quotes(route.id, since_epoch=since):
            rows.append({
                "route_id": route.id, "route": route.name, "ts": q.ts.isoformat(),
                "price": q.price, "currency": q.currency,
                "depart_date": q.depart_date.isoformat(),
                "return_date": q.return_date.isoformat() if q.return_date else "",
                "carrier": q.carrier, "stops": q.stops, "provider": q.provider,
            })
    out = sys.stdout if args.out in (None, "-") else open(args.out, "w", newline="", encoding="utf-8")
    try:
        if args.format == "json":
            json.dump(rows, out, indent=2)
            out.write("\n")
        else:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["route", "ts", "price"])
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if out is not sys.stdout:
            out.close()
            print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return 0


def cmd_set(args, db: Database, engine: Engine) -> int:
    route = db.get_route(args.route)
    if not route:
        raise CliError(f"no route with id {args.route}")
    db.update_route(
        args.route, threshold=args.threshold, interval_minutes=args.interval,
        max_stops=args.max_stops, label=args.label, provider=args.provider,
    )
    updated = db.get_route(args.route)
    print(f"#{updated.id} {updated.name}: target "
          f"{money(updated.threshold, updated.currency) if updated.threshold else 'none'}, "
          f"every {updated.interval_minutes} min, provider {updated.provider}")
    return 0


def cmd_toggle(args, db: Database, engine: Engine) -> int:
    route = db.get_route(args.route)
    if not route:
        raise CliError(f"no route with id {args.route}")
    db.set_route_enabled(args.route, args.enable)
    print(f"#{route.id} {route.name}: {'enabled' if args.enable else 'paused'}")
    return 0


def cmd_remove(args, db: Database, engine: Engine) -> int:
    route = db.get_route(args.route)
    if not route:
        raise CliError(f"no route with id {args.route}")
    n = db.quote_count(route.id)
    if not args.yes:
        answer = input(f"delete #{route.id} {route.name} and its {n} observations? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0
    db.delete_route(route.id)
    print(f"deleted #{route.id} {route.name}")
    return 0


def cmd_providers(args, db: Database, engine: Engine) -> int:
    from flighttracker.providers import get_provider
    print("provider   credentials")
    print("-" * 52)
    for name in available():
        provider = get_provider(name)
        if name == "mock":
            note = "none needed (simulated market)"
        elif name == "amadeus":
            note = ("AMADEUS_CLIENT_ID/SECRET set" if provider.client_id and provider.client_secret
                    else "set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET")
        else:
            note = "SERPAPI_KEY set" if provider.api_key else "set SERPAPI_KEY"
        print(f"{name:<10} {note}")
    print(f"\nnotification channels active: {', '.join(engine.settings.active_channels())}")
    return 0


# ------------------------------------------------------------------ helpers

def _select_routes(args, db: Database, enabled_only: bool) -> list[Route]:
    route_id = getattr(args, "route", None)
    if route_id:
        route = db.get_route(route_id)
        if not route:
            raise CliError(f"no route with id {route_id}")
        return [route]
    return db.list_routes(enabled_only=enabled_only)


def _validate_tz(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise CliError(f"unknown timezone {name!r} - try e.g. Europe/London or UTC")
    return name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flighttracker",
        description="Track round-trip flight prices across multiple routes and get told "
                    "when to book.",
    )
    p.add_argument("--version", action="version", version=f"flighttracker {__version__}")
    p.add_argument("--db", help="SQLite file (default ~/.flighttracker/tracker.db)")
    p.add_argument("-v", "--verbose", action="count", default=0)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="track a new route")
    a.add_argument("route", help="e.g. JFK-LHR")
    a.add_argument("--depart", required=True, help="YYYY-MM-DD")
    a.add_argument("--return", dest="ret", help="YYYY-MM-DD (omit for one-way)")
    a.add_argument("--return-from", metavar="AIRPORT",
                   help="open jaw: fly the inbound leg from a different airport than you "
                        "arrived at, e.g. --return-from KIX when the outbound was to NRT")
    a.add_argument("--return-to", metavar="AIRPORT",
                   help="open jaw: land the inbound leg somewhere other than your original "
                        "origin (rare - defaults to the outbound origin)")
    a.add_argument("--threshold", type=float, help="alert at or below this total price")
    a.add_argument("--interval", type=int, default=60, help="minutes between checks (default 60)")
    a.add_argument("--adults", type=int, default=1)
    a.add_argument("--cabin", default="ECONOMY",
                   choices=["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"])
    a.add_argument("--currency", default="USD")
    a.add_argument("--max-stops", type=int, dest="max_stops")
    a.add_argument("--flex-depart", type=int, default=0, metavar="DAYS",
                   help="also search +/- this many days either side of the outbound date")
    a.add_argument("--flex-return", type=int, default=0, metavar="DAYS")
    a.add_argument("--provider", default="mock", choices=available())
    a.add_argument("--label", help="friendly name for reports and alerts")
    a.add_argument("--check-now", action="store_true", help="fetch one price immediately")
    a.set_defaults(func=cmd_add)

    i = sub.add_parser("import", help="bulk-add routes from a JSON or CSV file")
    i.add_argument("file")
    i.add_argument("--provider", default="mock", choices=available(),
                   help="fallback provider for rows that do not name one")
    i.set_defaults(func=cmd_import)

    combo = sub.add_parser(
        "combo",
        help="track every round-trip AND open-jaw combination across a set of destinations",
        description="For 'from Singapore, into Tokyo or Osaka, home from either' - tracks "
                    "every symmetric round trip (SIN-NRT, SIN-KIX, ...) plus every open-jaw "
                    "pairing (out NRT home KIX, out KIX home NRT, ...) so you can see which "
                    "combination is actually cheapest, not just guess.",
    )
    combo.add_argument("origin", help="home airport, e.g. SIN")
    combo.add_argument("--to", required=True, metavar="AIRPORTS",
                       help="comma-separated destination airports, e.g. NRT,HND,KIX")
    combo.add_argument("--depart", required=True, help="YYYY-MM-DD")
    combo.add_argument("--return", dest="ret", required=True,
                       help="YYYY-MM-DD (open-jaw combos need a return date)")
    combo.add_argument("--threshold", type=float, help="applied to every combo tracked")
    combo.add_argument("--interval", type=int, default=60)
    combo.add_argument("--adults", type=int, default=1)
    combo.add_argument("--cabin", default="ECONOMY",
                       choices=["ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"])
    combo.add_argument("--currency", default="USD")
    combo.add_argument("--max-stops", type=int, dest="max_stops")
    combo.add_argument("--provider", default="mock", choices=available())
    combo.add_argument("--no-open-jaw", action="store_true",
                       help="only track the symmetric round trips, skip mixed in/out pairs")
    combo.set_defaults(func=cmd_combo)

    sub.add_parser("list", help="show tracked routes").set_defaults(func=cmd_list)

    c = sub.add_parser("check", help="poll now (routes that are due, or --force for all)")
    c.add_argument("--route", type=int, help="limit to one route id")
    c.add_argument("--force", action="store_true", help="ignore each route's interval")
    c.add_argument("--no-notify", action="store_true")
    c.set_defaults(func=cmd_check)

    w = sub.add_parser("watch", help="run continuously, polling every route on its interval")
    w.add_argument("--tick", type=int, default=30, help="seconds between scheduler passes")
    w.add_argument("--no-notify", action="store_true")
    w.set_defaults(func=cmd_watch)

    r = sub.add_parser("report", help="price history, booking window and best-time analysis")
    r.add_argument("--route", type=int)
    r.add_argument("--window", type=int, default=30, help="days of history to analyse")
    r.add_argument("--tz", help="timezone for hour-of-day analysis, e.g. Europe/London")
    r.add_argument("--html", metavar="PATH", help="write a standalone HTML report instead")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(func=cmd_report)

    al = sub.add_parser("alerts", help="recent alerts")
    al.add_argument("--route", type=int)
    al.add_argument("--limit", type=int, default=20)
    al.set_defaults(func=cmd_alerts)

    sim = sub.add_parser("simulate",
                         help="backfill synthetic history (mock provider) to try the analysis")
    sim.add_argument("--route", type=int)
    sim.add_argument("--days", type=int, default=30)
    sim.add_argument("--every", type=int, default=60, help="minutes between synthetic samples")
    sim.set_defaults(func=cmd_simulate)

    e = sub.add_parser("export", help="dump observations as CSV or JSON")
    e.add_argument("--route", type=int)
    e.add_argument("--window", type=int, default=3650)
    e.add_argument("--format", choices=["csv", "json"], default="csv")
    e.add_argument("--out", help="file path, or - for stdout")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("set", help="change a tracked route")
    s.add_argument("route", type=int)
    s.add_argument("--threshold", type=float)
    s.add_argument("--interval", type=int)
    s.add_argument("--max-stops", type=int, dest="max_stops")
    s.add_argument("--label")
    s.add_argument("--provider", choices=available())
    s.set_defaults(func=cmd_set)

    pause = sub.add_parser("pause", help="stop checking a route")
    pause.add_argument("route", type=int)
    pause.set_defaults(func=cmd_toggle, enable=False)

    resume = sub.add_parser("resume", help="start checking a route again")
    resume.add_argument("route", type=int)
    resume.set_defaults(func=cmd_toggle, enable=True)

    rm = sub.add_parser("remove", help="delete a route and its history")
    rm.add_argument("route", type=int)
    rm.add_argument("-y", "--yes", action="store_true")
    rm.set_defaults(func=cmd_remove)

    sub.add_parser("providers", help="show providers and credential status").set_defaults(
        func=cmd_providers
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING if not args.verbose else (
        logging.INFO if args.verbose == 1 else logging.DEBUG
    )
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    settings = load_settings()
    db = Database(args.db or settings.db_path)
    engine = Engine(db, settings)
    try:
        return args.func(args, db, engine)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
