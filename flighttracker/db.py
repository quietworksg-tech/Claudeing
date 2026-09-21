"""SQLite persistence. One file holds routes, every price observation and alerts."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from flighttracker.models import Alert, Quote, Route

DEFAULT_DB = Path(os.environ.get("FLIGHTTRACKER_DB", Path.home() / ".flighttracker" / "tracker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    origin             TEXT NOT NULL,
    destination        TEXT NOT NULL,
    depart_date        TEXT NOT NULL,
    return_date        TEXT,
    return_origin      TEXT,
    return_destination TEXT,
    adults             INTEGER NOT NULL DEFAULT 1,
    cabin              TEXT NOT NULL DEFAULT 'ECONOMY',
    currency           TEXT NOT NULL DEFAULT 'USD',
    max_stops          INTEGER,
    threshold          REAL,
    interval_minutes   INTEGER NOT NULL DEFAULT 60,
    depart_flex_days   INTEGER NOT NULL DEFAULT 0,
    return_flex_days   INTEGER NOT NULL DEFAULT 0,
    provider           TEXT NOT NULL DEFAULT 'mock',
    enabled            INTEGER NOT NULL DEFAULT 1,
    label              TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (origin, destination, depart_date, return_date, return_origin,
            return_destination, adults, cabin, provider)
);

CREATE TABLE IF NOT EXISTS quotes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id         INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    ts               TEXT NOT NULL,
    ts_epoch         INTEGER NOT NULL,
    price            REAL NOT NULL,
    currency         TEXT NOT NULL,
    depart_date      TEXT NOT NULL,
    return_date      TEXT,
    carrier          TEXT,
    stops            INTEGER,
    duration_minutes INTEGER,
    out_depart_time  TEXT,
    ret_depart_time  TEXT,
    deep_link        TEXT,
    provider         TEXT,
    raw_json         TEXT
);
CREATE INDEX IF NOT EXISTS idx_quotes_route_ts ON quotes(route_id, ts_epoch);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id  INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    quote_id  INTEGER REFERENCES quotes(id) ON DELETE SET NULL,
    ts        TEXT NOT NULL,
    ts_epoch  INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    price     REAL NOT NULL,
    currency  TEXT NOT NULL,
    message   TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_route_ts ON alerts(route_id, ts_epoch);

CREATE TABLE IF NOT EXISTS poll_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    ts_epoch INTEGER NOT NULL,
    ok       INTEGER NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_poll_route_ts ON poll_log(route_id, ts_epoch);
"""

_local = threading.local()


def _iso_to_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Database:
    """Thin SQLite wrapper. Safe to share across threads: one connection per thread."""

    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        # id() is recycled once an instance is collected, so a stale thread-local
        # connection could otherwise be handed to a different database.
        self._conn_key = f"conn_{uuid.uuid4().hex}"
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a DB's creation. Cheap, and idempotent
        via the column-existence check, so it runs on every open."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(routes)")}
        for column in ("return_origin", "return_destination"):
            if column not in existing:
                conn.execute(f"ALTER TABLE routes ADD COLUMN {column} TEXT")

    def connect(self) -> sqlite3.Connection:
        key = self._conn_key
        conn = getattr(_local, key, None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            setattr(_local, key, conn)
        return conn

    # ---------------------------------------------------------------- routes

    def add_route(self, route: Route) -> Route:
        sql = """
        INSERT INTO routes (origin, destination, depart_date, return_date, return_origin,
                            return_destination, adults, cabin, currency, max_stops, threshold,
                            interval_minutes, depart_flex_days, return_flex_days, provider,
                            enabled, label, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (origin, destination, depart_date, return_date, return_origin,
                     return_destination, adults, cabin, provider)
        DO UPDATE SET threshold=excluded.threshold,
                      interval_minutes=excluded.interval_minutes,
                      depart_flex_days=excluded.depart_flex_days,
                      return_flex_days=excluded.return_flex_days,
                      max_stops=excluded.max_stops,
                      label=COALESCE(excluded.label, routes.label),
                      enabled=1
        """
        with self._write_lock:
            conn = self.connect()
            conn.execute(
                sql,
                (
                    route.origin, route.destination, route.depart_date.isoformat(),
                    route.return_date.isoformat() if route.return_date else None,
                    route.return_origin, route.return_destination,
                    route.adults, route.cabin, route.currency, route.max_stops,
                    route.threshold, route.interval_minutes, route.depart_flex_days,
                    route.return_flex_days, route.provider, int(route.enabled),
                    route.label, datetime.now(timezone.utc).isoformat(),
                ),
            )
        found = self.find_route(route)
        assert found is not None
        return found

    def find_route(self, route: Route) -> Route | None:
        row = self.connect().execute(
            """SELECT * FROM routes WHERE origin=? AND destination=? AND depart_date=?
               AND return_date IS ? AND return_origin IS ? AND return_destination IS ?
               AND adults=? AND cabin=? AND provider=?""",
            (
                route.origin, route.destination, route.depart_date.isoformat(),
                route.return_date.isoformat() if route.return_date else None,
                route.return_origin, route.return_destination,
                route.adults, route.cabin, route.provider,
            ),
        ).fetchone()
        return _row_to_route(row) if row else None

    def get_route(self, route_id: int) -> Route | None:
        row = self.connect().execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
        return _row_to_route(row) if row else None

    def list_routes(self, enabled_only: bool = False) -> list[Route]:
        sql = "SELECT * FROM routes"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        return [_row_to_route(r) for r in self.connect().execute(sql)]

    def set_route_enabled(self, route_id: int, enabled: bool) -> None:
        with self._write_lock:
            self.connect().execute("UPDATE routes SET enabled=? WHERE id=?", (int(enabled), route_id))

    def update_route(self, route_id: int, **fields) -> None:
        allowed = {
            "threshold", "interval_minutes", "max_stops", "label", "provider",
            "depart_flex_days", "return_flex_days", "currency", "enabled",
        }
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return
        clause = ", ".join(f"{k}=?" for k in sets)
        with self._write_lock:
            self.connect().execute(
                f"UPDATE routes SET {clause} WHERE id=?", (*sets.values(), route_id)
            )

    def delete_route(self, route_id: int) -> None:
        with self._write_lock:
            self.connect().execute("DELETE FROM routes WHERE id=?", (route_id,))

    # ---------------------------------------------------------------- quotes

    def add_quote(self, quote: Quote) -> int:
        with self._write_lock:
            cur = self.connect().execute(
                """INSERT INTO quotes (route_id, ts, ts_epoch, price, currency, depart_date,
                                       return_date, carrier, stops, duration_minutes,
                                       out_depart_time, ret_depart_time, deep_link, provider, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    quote.route_id, quote.ts.isoformat(), int(quote.ts.timestamp()),
                    quote.price, quote.currency, quote.depart_date.isoformat(),
                    quote.return_date.isoformat() if quote.return_date else None,
                    quote.carrier, quote.stops, quote.duration_minutes,
                    quote.out_depart_time, quote.ret_depart_time, quote.deep_link,
                    quote.provider, quote.raw_json,
                ),
            )
            quote.id = int(cur.lastrowid)
            return quote.id

    def quotes(self, route_id: int, since_epoch: int | None = None, limit: int | None = None) -> list[Quote]:
        sql = "SELECT * FROM quotes WHERE route_id=?"
        args: list = [route_id]
        if since_epoch is not None:
            sql += " AND ts_epoch >= ?"
            args.append(since_epoch)
        sql += " ORDER BY ts_epoch"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_row_to_quote(r) for r in self.connect().execute(sql, args)]

    def latest_quote(self, route_id: int) -> Quote | None:
        row = self.connect().execute(
            "SELECT * FROM quotes WHERE route_id=? ORDER BY ts_epoch DESC LIMIT 1", (route_id,)
        ).fetchone()
        return _row_to_quote(row) if row else None

    def min_quote(self, route_id: int, since_epoch: int | None = None) -> Quote | None:
        sql = "SELECT * FROM quotes WHERE route_id=?"
        args: list = [route_id]
        if since_epoch is not None:
            sql += " AND ts_epoch >= ?"
            args.append(since_epoch)
        sql += " ORDER BY price ASC, ts_epoch ASC LIMIT 1"
        row = self.connect().execute(sql, args).fetchone()
        return _row_to_quote(row) if row else None

    def quote_count(self, route_id: int) -> int:
        return int(self.connect().execute(
            "SELECT COUNT(*) c FROM quotes WHERE route_id=?", (route_id,)
        ).fetchone()["c"])

    # ---------------------------------------------------------------- alerts

    def add_alert(self, alert: Alert) -> int:
        with self._write_lock:
            cur = self.connect().execute(
                """INSERT INTO alerts (route_id, quote_id, ts, ts_epoch, kind, price,
                                       currency, message, delivered)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    alert.route_id, alert.quote_id, alert.ts.isoformat(),
                    int(alert.ts.timestamp()), alert.kind, alert.price,
                    alert.currency, alert.message, int(alert.delivered),
                ),
            )
            alert.id = int(cur.lastrowid)
            return alert.id

    def mark_alert_delivered(self, alert_id: int) -> None:
        with self._write_lock:
            self.connect().execute("UPDATE alerts SET delivered=1 WHERE id=?", (alert_id,))

    def last_alert_epoch(self, route_id: int, kind: str) -> int | None:
        row = self.connect().execute(
            "SELECT MAX(ts_epoch) m FROM alerts WHERE route_id=? AND kind=?", (route_id, kind)
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    def list_alerts(self, route_id: int | None = None, limit: int = 50) -> list[Alert]:
        sql = "SELECT * FROM alerts"
        args: list = []
        if route_id is not None:
            sql += " WHERE route_id=?"
            args.append(route_id)
        sql += " ORDER BY ts_epoch DESC LIMIT ?"
        args.append(limit)
        return [
            Alert(
                id=r["id"], route_id=r["route_id"], quote_id=r["quote_id"],
                ts=_iso_to_dt(r["ts"]), kind=r["kind"], price=r["price"],
                currency=r["currency"], message=r["message"], delivered=bool(r["delivered"]),
            )
            for r in self.connect().execute(sql, args)
        ]

    # ------------------------------------------------------------- poll log

    def log_poll(self, route_id: int, ok: bool, detail: str = "") -> None:
        with self._write_lock:
            self.connect().execute(
                "INSERT INTO poll_log (route_id, ts_epoch, ok, detail) VALUES (?,?,?,?)",
                (route_id, int(datetime.now(timezone.utc).timestamp()), int(ok), detail[:500]),
            )

    def last_poll_epoch(self, route_id: int) -> int | None:
        row = self.connect().execute(
            "SELECT MAX(ts_epoch) m FROM poll_log WHERE route_id=?", (route_id,)
        ).fetchone()
        return int(row["m"]) if row and row["m"] is not None else None

    def poll_stats(self, route_id: int) -> tuple[int, int]:
        row = self.connect().execute(
            "SELECT COUNT(*) n, COALESCE(SUM(ok),0) ok FROM poll_log WHERE route_id=?", (route_id,)
        ).fetchone()
        return int(row["n"]), int(row["ok"])


def _row_to_route(row: sqlite3.Row) -> Route:
    return Route(
        id=row["id"], origin=row["origin"], destination=row["destination"],
        depart_date=date.fromisoformat(row["depart_date"]),
        return_date=date.fromisoformat(row["return_date"]) if row["return_date"] else None,
        return_origin=row["return_origin"], return_destination=row["return_destination"],
        adults=row["adults"], cabin=row["cabin"], currency=row["currency"],
        max_stops=row["max_stops"], threshold=row["threshold"],
        interval_minutes=row["interval_minutes"], depart_flex_days=row["depart_flex_days"],
        return_flex_days=row["return_flex_days"], provider=row["provider"],
        enabled=bool(row["enabled"]), label=row["label"],
    )


def _row_to_quote(row: sqlite3.Row) -> Quote:
    return Quote(
        id=row["id"], route_id=row["route_id"], ts=_iso_to_dt(row["ts"]),
        price=row["price"], currency=row["currency"],
        depart_date=date.fromisoformat(row["depart_date"]),
        return_date=date.fromisoformat(row["return_date"]) if row["return_date"] else None,
        carrier=row["carrier"] or "", stops=row["stops"] or 0,
        duration_minutes=row["duration_minutes"], out_depart_time=row["out_depart_time"],
        ret_depart_time=row["ret_depart_time"], deep_link=row["deep_link"],
        provider=row["provider"] or "", raw_json=row["raw_json"],
    )
