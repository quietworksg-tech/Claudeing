# flighttracker

Watches round-trip flight prices for as many routes as you like, keeps every
observation, and tells you **when to book** — including the exact minute the fare
was cheapest over the last month.

Standard library only. Python 3.11+. One SQLite file holds everything.

```
$ flighttracker add JFK-LHR --depart 2026-12-10 --return 2026-12-20 --threshold 650
$ flighttracker add SFO-NRT --depart 2027-01-15 --return 2027-01-29 --threshold 780 --flex-depart 2
$ flighttracker watch
```

---

## What it does

- **Tracks many routes at once.** Each route has its own polling interval; a
  worker pool sweeps everything that is due, so twelve routes are no harder than one.
- **Alerts on three rules** — your price threshold, a new all-time low, and a
  drop against the 7-day median — with a per-rule cooldown so you get told once,
  not forty times.
- **Answers "when should I book?"** from your own recorded history: the exact
  timestamp of the cheapest fare seen, the cheapest hour of day and weekday, and
  the booking-window curve by days-before-departure.
- **Delivers** to the console, a webhook (Slack/Discord-shaped), email, a JSONL
  file, or desktop notifications.
- **Reports** as a terminal summary or a self-contained HTML page with an
  interactive price timeline, a weekday × hour heatmap and a booking-window
  chart. Light and dark, no external assets, no JavaScript libraries.

## Install

```bash
git clone <this repo> && cd Claudeing
pip install -e .            # provides the `flighttracker` command
# or, with no install at all:
python3 -m flighttracker --help
```

## Try it in 30 seconds

The `mock` provider is a deterministic fare simulator — no API key, no network —
so you can see the whole analysis immediately:

```bash
flighttracker add JFK-LHR --depart 2026-12-10 --return 2026-12-20 --threshold 950
flighttracker simulate --days 30 --every 30      # backfill a month of history
flighttracker report --tz America/New_York
flighttracker report --html report.html && open report.html
```

```
──────────────────────────────────────────────────────────────────────────
  JFK-LHR 2026-12-10/2026-12-20   [mock] Economy  x1
──────────────────────────────────────────────────────────────────────────
  Now          USD 991.31   TYPICAL - middle of the observed range
  Range        USD 822.74  →  USD 1,136.09   (median USD 1,018.10)
  Cheapest at  Sat 12 Sep 2026 15:54 America/New_York   USD 822.74
               2026-12-10 → 2026-12-20  AF  nonstop
  Target       USD 950.00   [USD 41.31 above]
  Movement     24h -0.9%   7d +11.0%   deal score 69/100
  Samples      1442 observations over 29d
  Timeline     ▇▇▆▄▅▄▁▃▄▃▆▅█▇█▇█▆▅▅▄▃▂▂▄▃▆▂▇▅█▆▅▇▇▅▃▄▅▄▅▃▂▃▅▁▅▅▆▅▅▂▆▆▆▅▄▄▁

  BEST TIME TO CHECK  (local time, America/New_York)
    cheapest hour     20:00  median USD 993.60  n=60
    cheapest weekday  Wed    median USD 981.47  n=192
```

## Real prices

`mock` is a simulation. For live fares, pick a provider and set its credentials:

| Provider  | Credentials | Notes |
|-----------|-------------|-------|
| `amadeus` | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` | Free test tier at [developers.amadeus.com]; set `AMADEUS_HOST=production` for live fares |
| `serpapi` | `SERPAPI_KEY` | Google Flights results via [serpapi.com]; metered per search |
| `mock`    | none | Deterministic simulator for demos and tests |

```bash
export AMADEUS_CLIENT_ID=... AMADEUS_CLIENT_SECRET=...
flighttracker providers                      # confirms what is configured
flighttracker set 1 --provider amadeus
```

Each flexible date pair costs **one API call per check**. `--flex-depart 2
--flex-return 2` means 25 pairs per poll — on a metered plan that adds up fast.
Start at zero flex and a 60-minute interval.

## Running it continuously

```bash
flighttracker watch                     # foreground, polls each route on its interval
```

For unattended operation use one of the files in `deploy/`:

- `deploy/flighttracker.service` — a systemd unit that restarts on failure.
- `deploy/crontab.example` — drives sweeps with `flighttracker check` instead.
  Each run only polls routes that are actually due, so a tight cron is safe.

## Commands

| Command | What it does |
|---------|--------------|
| `add ROUTE --depart … [--return …]` | start tracking a route |
| `import FILE` | bulk-add routes from JSON or CSV (see `examples/`) |
| `list` | every tracked route with its latest price |
| `check [--route N] [--force]` | poll now (one sweep) |
| `watch` | poll continuously until interrupted |
| `report [--route N] [--html PATH] [--tz ZONE] [--window DAYS]` | the analysis |
| `alerts` | what fired, and when |
| `simulate [--days N]` | backfill synthetic history (mock provider only) |
| `export [--format csv\|json]` | dump raw observations |
| `set N --threshold … --interval …` | change a route |
| `pause N` / `resume N` / `remove N` | lifecycle |
| `providers` | which providers are configured |

Run `flighttracker <command> --help` for the full flag list.

## Alerts

Three rules, evaluated on every poll:

| Rule | Fires when |
|------|-----------|
| `threshold` | price ≤ the `--threshold` you set for the route |
| `all_time_low` | price beats every previous observation (after 6+ samples) |
| `drop` | price is ≥ `drop_pct` (default 8%) below the 7-day median |

A per-rule cooldown (default 3 hours) stops repeat alerts while a fare sits
below your target.

### Delivery channels

Set as environment variables, or as keys in `~/.flighttracker/config.json`:

```bash
export FLIGHTTRACKER_WEBHOOK_URL=https://hooks.slack.com/services/...
export FLIGHTTRACKER_ALERT_LOG=$HOME/.flighttracker/alerts.jsonl
export FLIGHTTRACKER_SMTP_HOST=smtp.gmail.com
export FLIGHTTRACKER_SMTP_USER=you@example.com
export FLIGHTTRACKER_SMTP_PASSWORD=app-password
export FLIGHTTRACKER_EMAIL_TO=you@example.com
export FLIGHTTRACKER_DESKTOP_NOTIFY=1
```

Every channel is best-effort — a dead webhook never stops the tracker.

### All settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `FLIGHTTRACKER_DB` | `~/.flighttracker/tracker.db` | SQLite file |
| `FLIGHTTRACKER_TZ` | `UTC` | timezone for hour-of-day analysis |
| `FLIGHTTRACKER_COOLDOWN_MINUTES` | `180` | minimum gap between same-rule alerts |
| `FLIGHTTRACKER_DROP_PCT` | `8.0` | drop-rule sensitivity |
| `FLIGHTTRACKER_MAX_WORKERS` | `4` | routes polled in parallel |
| `FLIGHTTRACKER_JITTER_PCT` | `10.0` | randomises sweep timing |
| `FLIGHTTRACKER_CONFIG` | `~/.flighttracker/config.json` | JSON file holding any of the above |

## Reading the "when to book" analysis

**Cheapest at** is the exact timestamp of the lowest fare in your history.
Its precision is your polling interval — poll every 30 minutes and you can
resolve the cheapest half-hour, not the cheapest minute.

**Cheapest hour / weekday** are medians per bucket, in the timezone you pass to
`--tz`. Buckets with fewer than 3 samples are flagged as thin and are not used
to pick a winner where better-sampled buckets exist. A week of hourly polling
gives every weekday bucket ~24 samples; a day of polling gives you nothing
meaningful, and the report says so.

**Booking window** is the median fare by days-before-departure. It is the most
transferable signal here, because it reflects how the airline's fare buckets
open and close rather than when you happened to look.

**Deal score** is a percentile: 80 means the current price is cheaper than 80%
of everything recorded for that route. It is a statement about your own
history, not about the market.

## Honest limits

- This records what the provider **showed you**, at the moments you asked.
  It is not a forecast, and it cannot see fares you never polled for.
- Thirty days of history describes thirty days. Fare behaviour changes with
  season, events and capacity; do not expect last month's cheapest hour to hold
  next quarter.
- A quote is an observation, not an offer. Availability moves between the poll
  and your checkout. Always verify on the airline's own site before booking.
- The `mock` provider is a plausible simulation, not real market data. Never
  make a booking decision from it.
- Polling real providers aggressively will exhaust a free tier and may breach
  their terms. The default 60-minute interval is deliberate.

## Development

```bash
python3 -m unittest discover -s tests -v      # 62 tests, no dependencies
```

Layout:

```
flighttracker/
  models.py        Route, Offer, Quote, Alert
  db.py            SQLite schema and queries
  providers/       mock, amadeus, serpapi + the registry
  engine.py        polling, alert rules, the scheduler
  analysis.py      statistics: percentiles, buckets, best moment
  charts.py        inline-SVG chart builders
  report.py        terminal and HTML rendering
  notify.py        console / webhook / email / file / desktop
  cli.py           argparse front end
```

To add a provider, subclass `Provider`, implement `search(route) -> list[Offer]`,
and register it in `flighttracker/providers/__init__.py`.

## License

MIT.
