"""Test suite. Stdlib unittest only - `python3 -m unittest discover -s tests`."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flighttracker import analysis, charts, cli, report
from flighttracker.config import Settings
from flighttracker.db import Database
from flighttracker.engine import Engine
from flighttracker.models import Quote, Route, utcnow
from flighttracker.notify import Notifier, format_alert
from flighttracker.providers.mock import MockProvider

TOMORROW = date.today() + timedelta(days=120)
RETURN = TOMORROW + timedelta(days=10)


class SilentNotifier(Notifier):
    def __init__(self):
        super().__init__(Settings(webhook_url="", smtp_host="", alert_log="", desktop_notify=False))
        self.sent: list = []

    def send(self, route, alert, deep_link=None):
        self.sent.append((route, alert))
        return ["test"]


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "t.db")
        self.notifier = SilentNotifier()
        self.engine = Engine(self.db, Settings(cooldown_minutes=0), self.notifier)

    def tearDown(self):
        self._dir.cleanup()

    def make_route(self, **kw) -> Route:
        defaults = dict(origin="JFK", destination="LHR", depart_date=TOMORROW,
                        return_date=RETURN, provider="mock")
        return self.db.add_route(Route(**(defaults | kw)))

    def seed(self, route: Route, prices: list[float], start: datetime | None = None,
             step_hours: int = 1) -> None:
        when = start or (utcnow() - timedelta(hours=len(prices)))
        for i, price in enumerate(prices):
            self.db.add_quote(Quote(
                route_id=route.id, ts=when + timedelta(hours=i * step_hours), price=price,
                currency="USD", depart_date=route.depart_date, return_date=route.return_date,
                carrier="BA", provider="mock",
            ))


# ------------------------------------------------------------------- models

class TestModels(unittest.TestCase):
    def test_normalises_codes_and_dates(self):
        r = Route(" jfk ", "lhr", "2026-12-10", "2026-12-20")
        self.assertEqual((r.origin, r.destination), ("JFK", "LHR"))
        self.assertEqual(r.depart_date, date(2026, 12, 10))
        self.assertTrue(r.is_round_trip)

    def test_flex_window_holds_trip_length_and_drops_impossible_pairs(self):
        r = Route("JFK", "LHR", "2026-12-10", "2026-12-20",
                  depart_flex_days=2, return_flex_days=2)
        pairs = r.date_pairs()
        self.assertEqual(len(pairs), 25)
        self.assertTrue(all(back >= out for out, back in pairs))

    def test_one_way_has_no_return_leg(self):
        r = Route("JFK", "LHR", "2026-12-10")
        self.assertFalse(r.is_round_trip)
        self.assertEqual(r.date_pairs(), [(date(2026, 12, 10), None)])

    def test_label_overrides_generated_name(self):
        self.assertEqual(Route("JFK", "LHR", "2026-12-10", label="Xmas").name, "Xmas")


# ----------------------------------------------------------------- database

class TestDatabase(TempDbCase):
    def test_adding_the_same_route_twice_updates_instead_of_duplicating(self):
        first = self.make_route(threshold=800)
        second = self.make_route(threshold=650, interval_minutes=15)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.threshold, 650)
        self.assertEqual(second.interval_minutes, 15)
        self.assertEqual(len(self.db.list_routes()), 1)

    def test_one_way_and_round_trip_are_separate_routes(self):
        a = self.make_route()
        b = self.make_route(return_date=None)
        self.assertNotEqual(a.id, b.id)

    def test_min_quote_and_latest_quote(self):
        route = self.make_route()
        self.seed(route, [900, 700, 800])
        self.assertEqual(self.db.min_quote(route.id).price, 700)
        self.assertEqual(self.db.latest_quote(route.id).price, 800)
        self.assertEqual(self.db.quote_count(route.id), 3)

    def test_deleting_a_route_removes_its_quotes(self):
        route = self.make_route()
        self.seed(route, [900, 800])
        self.db.delete_route(route.id)
        self.assertEqual(self.db.quotes(route.id), [])

    def test_timestamps_round_trip_as_utc_aware(self):
        route = self.make_route()
        self.seed(route, [500])
        self.assertIsNotNone(self.db.latest_quote(route.id).ts.tzinfo)


# ----------------------------------------------------------------- provider

class TestMockProvider(unittest.TestCase):
    def setUp(self):
        self.provider = MockProvider()
        self.route = Route("JFK", "LHR", TOMORROW, RETURN)

    def test_same_minute_gives_the_same_price(self):
        when = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        a = self.provider.price_at(self.route, when, self.route.depart_date, self.route.return_date)
        b = self.provider.price_at(self.route, when, self.route.depart_date, self.route.return_date)
        self.assertEqual(a, b)

    def test_different_routes_get_different_base_fares(self):
        other = Route("SFO", "NRT", TOMORROW, RETURN)
        when = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
        self.assertNotEqual(
            self.provider.price_at(self.route, when, TOMORROW, RETURN),
            self.provider.price_at(other, when, TOMORROW, RETURN),
        )

    def test_last_minute_is_dearer_than_the_prime_window(self):
        route = Route("JFK", "LHR", date(2026, 6, 1), date(2026, 6, 11))
        near = self.provider.price_at(route, datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
                                      route.depart_date, route.return_date)
        far = self.provider.price_at(route, datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
                                     route.depart_date, route.return_date)
        self.assertGreater(near, far)

    def test_cheapest_respects_the_max_stops_filter(self):
        route = Route("JFK", "LHR", TOMORROW, RETURN, max_stops=0)
        self.assertEqual(self.provider.cheapest(route).stops, 0)

    def test_round_trip_costs_more_than_one_way(self):
        one_way = Route("JFK", "LHR", TOMORROW)
        when = utcnow()
        self.assertGreater(
            self.provider.price_at(self.route, when, TOMORROW, RETURN),
            self.provider.price_at(one_way, when, TOMORROW, None),
        )


# ------------------------------------------------------------------- engine

class TestAlertRules(TempDbCase):
    def test_threshold_alert_fires_at_or_below_target(self):
        route = self.make_route(threshold=1000)
        alerts = self.engine._evaluate(route, self._quote(route, 950), None, 0, [])
        self.assertEqual([a.kind for a in alerts], ["threshold"])

    def test_no_threshold_alert_above_target(self):
        route = self.make_route(threshold=1000)
        self.assertEqual(self.engine._evaluate(route, self._quote(route, 1001), None, 0, []), [])

    def test_all_time_low_needs_history_first(self):
        route = self.make_route()
        prior = self._quote(route, 900)
        thin = self.engine._evaluate(route, self._quote(route, 800), prior, 2, [])
        self.assertEqual(thin, [])
        real = self.engine._evaluate(route, self._quote(route, 800), prior, 40, [])
        self.assertEqual([a.kind for a in real], ["all_time_low"])

    def test_drop_alert_uses_the_seven_day_median(self):
        route = self.make_route()
        recent = [self._quote(route, 1000) for _ in range(10)]
        alerts = self.engine._evaluate(route, self._quote(route, 880), None, 10, recent)
        self.assertIn("drop", [a.kind for a in alerts])
        quiet = self.engine._evaluate(route, self._quote(route, 990), None, 10, recent)
        self.assertNotIn("drop", [a.kind for a in quiet])

    def test_cooldown_suppresses_a_repeat_alert(self):
        engine = Engine(self.db, Settings(cooldown_minutes=180), self.notifier)
        route = self.make_route(threshold=1000)
        first = engine._evaluate(route, self._quote(route, 900), None, 0, [])
        self.assertEqual(len(first), 1)
        self.db.add_alert(first[0])
        self.assertEqual(engine._evaluate(route, self._quote(route, 890), None, 0, []), [])

    def test_poll_stores_a_quote_and_notifies(self):
        route = self.make_route(threshold=100_000)
        result = self.engine.poll(route)
        self.assertIsNotNone(result.quote)
        self.assertEqual(self.db.quote_count(route.id), 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertTrue(self.db.list_alerts(route.id)[0].delivered)

    def test_a_departed_route_is_disabled_rather_than_polled(self):
        route = self.db.add_route(Route("JFK", "LHR", date.today() - timedelta(days=2),
                                        provider="mock"))
        result = self.engine.poll(route)
        self.assertIn("passed", result.error)
        self.assertFalse(self.db.get_route(route.id).enabled)

    def test_due_routes_respects_each_interval(self):
        route = self.make_route(interval_minutes=60)
        self.assertEqual([r.id for r in self.engine.due_routes()], [route.id])
        self.engine.poll(route)
        self.assertEqual(self.engine.due_routes(), [])
        self.assertEqual([r.id for r in self.engine.due_routes(force=True)], [route.id])

    def test_simulate_only_works_with_the_mock_provider(self):
        route = self.make_route(provider="amadeus")
        with self.assertRaises(Exception):
            self.engine.simulate(route, days=1)

    def test_simulate_backfills_history(self):
        route = self.make_route()
        written = self.engine.simulate(route, days=2, every_minutes=120)
        self.assertGreater(written, 20)
        self.assertEqual(self.db.quote_count(route.id), written)

    def _quote(self, route: Route, price: float) -> Quote:
        # id stays None: these quotes are not persisted, and alerts.quote_id is nullable
        return Quote(route_id=route.id, ts=utcnow(), price=price, currency="USD",
                     depart_date=route.depart_date, return_date=route.return_date,
                     carrier="BA", provider="mock")


# ----------------------------------------------------------------- analysis

class TestAnalysis(TempDbCase):
    def test_empty_history_reports_no_data(self):
        route = self.make_route()
        s = analysis.summarise(route, [])
        self.assertEqual(s.n, 0)
        self.assertEqual(s.verdict, "no data")
        self.assertTrue(s.notes)

    def test_core_statistics(self):
        route = self.make_route(threshold=700)
        self.seed(route, [1000, 800, 900, 750])
        s = analysis.summarise(route, self.db.quotes(route.id))
        self.assertEqual((s.low, s.high, s.current), (750, 1000, 750))
        self.assertEqual(s.median, 850)
        self.assertEqual(s.best_quote.price, 750)
        self.assertFalse(s.threshold_met)

    def test_best_moment_keeps_the_exact_timestamp(self):
        route = self.make_route()
        start = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        self.seed(route, [900, 640, 880], start=start)
        s = analysis.summarise(route, self.db.quotes(route.id), tz="UTC")
        self.assertEqual(s.best_quote.price, 640)
        self.assertEqual(s.best_moment_local, start + timedelta(hours=1))

    def test_threshold_met_drives_the_verdict(self):
        route = self.make_route(threshold=800)
        self.seed(route, [1000, 950, 900, 700])
        s = analysis.summarise(route, self.db.quotes(route.id))
        self.assertTrue(s.threshold_met)
        self.assertTrue(s.verdict.startswith("BOOK NOW"))

    def test_percentile_and_deal_score_move_together(self):
        route = self.make_route()
        self.seed(route, [500, 600, 700, 800, 900])
        s = analysis.summarise(route, self.db.quotes(route.id))
        self.assertEqual(s.percentile, 1.0)
        self.assertEqual(s.deal_score, 0)
        self.assertTrue(s.verdict.startswith("WAIT"))

    def test_buckets_are_tagged_thin_when_undersampled(self):
        route = self.make_route()
        self.seed(route, [900, 800])
        s = analysis.summarise(route, self.db.quotes(route.id))
        self.assertTrue(all(b.thin for b in s.by_hour))

    def test_timezone_shifts_the_hour_buckets(self):
        route = self.make_route()
        start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        self.seed(route, [900], start=start)
        utc = analysis.summarise(route, self.db.quotes(route.id), tz="UTC")
        ny = analysis.summarise(route, self.db.quotes(route.id), tz="America/New_York")
        self.assertEqual(utc.by_hour[0].key, "12")
        self.assertEqual(ny.by_hour[0].key, "08")

    def test_heatmap_groups_by_weekday_and_hour(self):
        route = self.make_route()
        self.seed(route, [900, 910], start=datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc),
                  step_hours=0)
        cells = analysis.heatmap(self.db.quotes(route.id), "UTC")
        self.assertEqual(cells[(0, 9)], (2, 905))

    def test_change_windows_need_enough_history(self):
        route = self.make_route()
        self.seed(route, [1000, 900], step_hours=1)
        s = analysis.summarise(route, self.db.quotes(route.id))
        self.assertIsNone(s.change_7d)

    def test_days_out_buckets_are_ordered_far_to_near(self):
        keys = [analysis._days_out_bucket(d) for d in (200, 100, 50, 10, 2)]
        self.assertEqual(keys, ["180d+", "90-180d", "45-60d", "7-14d", "0-7d"])

    def test_sparkline_width_is_capped(self):
        self.assertEqual(len(analysis.sparkline(list(range(500)), width=20)), 20)
        self.assertEqual(analysis.sparkline([]), "")


# ------------------------------------------------------------------- charts

class TestCharts(unittest.TestCase):
    def test_diverging_steps_are_signed_and_clamped(self):
        self.assertEqual(charts.diverging_step(1000, 1000, 150), 0)
        self.assertEqual(charts.diverging_step(500, 1000, 150), -charts.ARM_STEPS)
        self.assertEqual(charts.diverging_step(5000, 1000, 150), charts.ARM_STEPS)

    def test_step_classes_are_distinct(self):
        names = {charts.step_class(i) for i in range(-charts.ARM_STEPS, charts.ARM_STEPS + 1)}
        self.assertEqual(len(names), charts.ARM_STEPS * 2 + 1)

    def test_diverging_css_defines_both_modes(self):
        css = charts.diverging_css()
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn('[data-theme="dark"]', css)

    def test_line_chart_is_well_formed_svg(self):
        points = [(datetime(2026, 5, 1, h, tzinfo=timezone.utc), 900 + h) for h in range(24)]
        svg = charts.price_line_chart(points, currency="USD", threshold=905,
                                      best=(points[0][0], 900), chart_id="c0")
        root = ET.fromstring(svg[:svg.index("</svg>") + 6])
        self.assertEqual(root.tag, "svg")
        self.assertIn("threshold", svg)

    def test_line_chart_needs_two_points(self):
        self.assertIn("Not enough", charts.price_line_chart([], currency="USD", threshold=None,
                                                            best=None, chart_id="c1"))

    def test_line_chart_downsamples_but_keeps_the_low(self):
        points = [(datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(minutes=i), 900.0)
                  for i in range(1000)]
        points[500] = (points[500][0], 100.0)
        svg = charts.price_line_chart(points, currency="USD", threshold=None,
                                      best=None, chart_id="c2", max_points=50)
        self.assertIn("100.00", svg)

    def test_heatmap_and_curve_render(self):
        cells = {(d, h): (4, 900 + d * 10 + h) for d in range(7) for h in range(0, 24, 2)}
        svg = charts.weekday_hour_heatmap(cells, centre=930, currency="USD")
        ET.fromstring(svg[:svg.index("</svg>") + 6])
        buckets = [analysis.Bucket("90-180d", 10, 800, 900, 1000),
                   analysis.Bucket("60-90d", 2, 700, 850, 900)]
        curve = charts.days_out_curve(buckets, currency="USD")
        ET.fromstring(curve[:curve.index("</svg>") + 6])
        self.assertIn("n=2", curve)          # thin bucket is flagged

    def test_empty_inputs_degrade_gracefully(self):
        self.assertIn("No observations", charts.weekday_hour_heatmap({}, centre=0, currency="USD"))
        self.assertIn("No observations", charts.days_out_curve([], currency="USD"))

    def test_escaping_blocks_markup_injection(self):
        self.assertNotIn("<script>", charts.esc("<script>alert(1)</script>"))


# ------------------------------------------------------------------- report

class TestReport(TempDbCase):
    def test_html_is_self_contained_and_covers_every_route(self):
        a = self.make_route(threshold=900)
        b = self.make_route(origin="SFO", destination="NRT", label="Tokyo")
        self.seed(a, [1000, 900, 950, 870])
        self.seed(b, [1500, 1400])
        html = report.render_html(self.db, [a, b], tz="UTC", window_days=30)
        self.assertIn("<!doctype html>", html)
        self.assertNotIn("http://", html.split("<footer>")[0])   # no external assets
        self.assertIn("Tokyo", html)
        self.assertIn(f'id="route-{a.id}"', html)
        self.assertIn("All routes", html)                         # overview for 2+ routes
        self.assertIn("Data table", html)                         # accessible table view

    def test_html_survives_a_route_with_no_data(self):
        route = self.make_route()
        html = report.render_html(self.db, [route], tz="UTC")
        self.assertIn("No observations recorded yet", html)

    def test_write_html_creates_missing_directories(self):
        route = self.make_route()
        self.seed(route, [900, 800])
        target = Path(self._dir.name) / "nested" / "out.html"
        self.assertTrue(report.write_html(self.db, [route], target).exists())

    def test_terminal_report_shows_the_exact_cheapest_moment(self):
        route = self.make_route(threshold=700)
        start = datetime(2026, 5, 1, 3, 30, tzinfo=timezone.utc)
        self.seed(route, [900, 650, 880], start=start)
        summary = analysis.summarise(route, self.db.quotes(route.id), tz="UTC")
        text = report.render_terminal(summary, self.db.quotes(route.id))
        self.assertIn("04:30", text)
        self.assertIn("BOOKING WINDOW", text)

    def test_alert_formatting_includes_route_and_price(self):
        route = self.make_route(threshold=600)
        alerts = self.engine._evaluate(
            route,
            Quote(route_id=route.id, ts=utcnow(), price=500, currency="USD",
                  depart_date=route.depart_date, return_date=route.return_date),
            None, 0, [],
        )
        text = format_alert(route, alerts[0])
        self.assertIn("JFK-LHR", text)
        self.assertIn("500", text)


# ---------------------------------------------------------------------- CLI

class TestCli(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self._dir.name) / "cli.db")

    def tearDown(self):
        self._dir.cleanup()

    def run_cli(self, *args) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = cli.main(["--db", self.dbfile, *args])
        return code, buf.getvalue()

    def test_parse_leg_accepts_common_separators(self):
        for text in ("JFK-LHR", "jfk/lhr", "JFK:LHR", "jfk lhr"):
            self.assertEqual(cli.parse_leg(text), ("JFK", "LHR"))

    def test_parse_leg_rejects_nonsense(self):
        for text in ("JFK", "J-F-K-L", "TOOLONG-LHR"):
            with self.assertRaises(cli.CliError):
                cli.parse_leg(text)

    def test_add_list_and_report_round_trip(self):
        code, out = self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat(),
                                 "--return", RETURN.isoformat(), "--threshold", "650")
        self.assertEqual(code, 0)
        self.assertIn("tracking #1", out)

        code, out = self.run_cli("list")
        self.assertIn("JFK-LHR", out)

        self.assertEqual(self.run_cli("check", "--force", "--no-notify")[0], 0)
        code, out = self.run_cli("report")
        self.assertEqual(code, 0)
        self.assertIn("BEST TIME TO CHECK", out)

    def test_return_before_departure_is_rejected(self):
        code, _ = self.run_cli("add", "JFK-LHR", "--depart", RETURN.isoformat(),
                               "--return", TOMORROW.isoformat())
        self.assertEqual(code, 2)

    def test_unknown_timezone_is_rejected(self):
        self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat())
        self.assertEqual(self.run_cli("report", "--tz", "Mars/Olympus")[0], 2)

    def test_import_adds_several_routes_at_once(self):
        payload = [
            {"route": "JFK-LHR", "depart_date": TOMORROW.isoformat(),
             "return_date": RETURN.isoformat(), "threshold": 650},
            {"origin": "SFO", "destination": "NRT", "depart_date": TOMORROW.isoformat(),
             "label": "Tokyo"},
        ]
        path = Path(self._dir.name) / "routes.json"
        path.write_text(json.dumps(payload))
        code, out = self.run_cli("import", str(path))
        self.assertEqual(code, 0)
        self.assertIn("2 route(s) now tracked", out)

    def test_pause_and_resume(self):
        self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat())
        self.assertIn("paused", self.run_cli("pause", "1")[1])
        self.assertIn("enabled", self.run_cli("resume", "1")[1])

    def test_set_updates_the_target(self):
        self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat())
        _, out = self.run_cli("set", "1", "--threshold", "555", "--interval", "15")
        self.assertIn("555", out)
        self.assertIn("15 min", out)

    def test_remove_needs_confirmation_flag(self):
        self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat())
        self.assertIn("deleted", self.run_cli("remove", "1", "-y")[1])
        self.assertIn("No routes tracked", self.run_cli("list")[1])

    def test_missing_route_id_is_an_error(self):
        self.assertEqual(self.run_cli("set", "99", "--threshold", "1")[0], 2)

    def test_html_report_is_written_to_disk(self):
        self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat())
        self.run_cli("check", "--force", "--no-notify")
        target = Path(self._dir.name) / "r.html"
        code, out = self.run_cli("report", "--html", str(target), "--quiet")
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())

    def test_export_csv_to_stdout(self):
        self.run_cli("add", "JFK-LHR", "--depart", TOMORROW.isoformat())
        self.run_cli("check", "--force", "--no-notify")
        code, out = self.run_cli("export", "--format", "csv")
        self.assertEqual(code, 0)
        self.assertIn("route_id,route,ts,price", out)

    def test_providers_lists_credential_state(self):
        code, out = self.run_cli("providers")
        self.assertEqual(code, 0)
        self.assertIn("mock", out)
        self.assertIn("amadeus", out)


if __name__ == "__main__":
    unittest.main()
