"""scraper/simulator.py — synthetic airfare quote generator.

=============================================================================
THIS MODULE PRODUCES FAKE DATA. Every quote it returns is invented by the
model described in config/simulator.yaml. Nothing here touches a real airline,
a real booking site, or any real fare. Rows are written with
source = 'simulated' so that the ETL, the index, the API and the dashboard can
all tell synthetic data from a real scrape (CLAUDE.md ground rule 5).
=============================================================================

Why a simulator at all: the demo needs a dense, months-deep panel of fares
across every route x airline x booking window. No public source gives that, and
scraping it live would take weeks and breach several sites' terms. So the index
is demonstrated on a synthetic panel whose *shape* matches real airfare
behaviour, and scraper/live_scraper.py (Phase 3) proves the real collection path
separately.

The pricing model, in order of application:

    total = base_fare + taxes

    base_fare = route base
              x booking-window curve  (rises non-linearly as departure nears)
              x day-of-week factor    (Fri/Sun peak, midweek trough)
              x carrier-type factor   (full-service premium over low-cost)
              x airline factor        (brand positioning within that class)
              x demand shock          (festival / long-weekend spike, 1.5-3.0x)
              x lognormal noise

    taxes     = fixed airport charges
              + distance-scaled surcharge
              + GST on base fare
              + small noise

Some quotes are unavailable (sold out), with probability rising sharply as
departure approaches.

Output is deterministic: the same (airline, route, travel_date, query_date)
always yields the same quote, so re-running the backfill refreshes rows instead
of churning the dataset.

CLI:
    python -m scraper.simulator backfill    # generate and write the panel
    python -m scraper.simulator sanity      # fare-vs-lead-time sanity check
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import (  # noqa: E402
    Airline,
    Route,
    load_airlines,
    load_routes,
    load_simulator_config,
)

#: Every row this module produces carries this provenance. Not configurable.
SOURCE = db.SOURCE_SIMULATED

_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def _rng_for(global_seed: int, *parts: Any) -> random.Random:
    """A stable RNG for one quote.

    Seeded from a hash of the quote's identity rather than a running sequence,
    so a quote's value does not depend on generation order or on how many other
    quotes were produced. blake2b rather than hash() because hash() on strings
    is randomised per process.
    """
    key = f"{global_seed}|" + "|".join(str(p) for p in parts)
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


# ---------------------------------------------------------------------------
# Model components
# ---------------------------------------------------------------------------


def booking_window_multiplier(advance_purchase_days: int, cfg: dict[str, Any]) -> float:
    """The booking-window curve: fares rise non-linearly as departure nears.

        w(apd) = 1 + amplitude * exp(-apd / decay_days)

    Flat and cheap far out, steep inside ~3 weeks, steepest in the final days --
    the characteristic airfare shape. Monotonically decreasing in apd.
    """
    curve = cfg["booking_curve"]
    return 1.0 + curve["amplitude"] * math.exp(
        -advance_purchase_days / curve["decay_days"]
    )


def day_of_week_multiplier(travel_date: date, cfg: dict[str, Any]) -> float:
    """Friday/Sunday peak, midweek trough, keyed on the day of travel."""
    return float(cfg["day_of_week_multiplier"][_WEEKDAY_NAMES[travel_date.weekday()]])


def airline_multiplier(airline: Airline, cfg: dict[str, Any]) -> float:
    """Full-service premium over low-cost, plus per-brand positioning."""
    by_type = float(cfg["carrier_type_multiplier"][airline.carrier_type])
    by_brand = float(cfg.get("airline_multiplier", {}).get(airline.code, 1.0))
    return by_type * by_brand


def demand_shock(
    route: Route, travel_date: date, cfg: dict[str, Any]
) -> tuple[float, str | None]:
    """Festival / long-weekend spike applying to `travel_date`.

    Returns (multiplier, shock_name). 1.0 and None on an ordinary day. Windows
    are inclusive of both endpoints and are defined in config/simulator.yaml.

    The multiplier is drawn from an RNG keyed on (route, travel_date, shock)
    ONLY -- deliberately not on the airline or the query date. A festival is a
    single demand event affecting a sector on a date: every carrier's fare for
    that departure is elevated, and it stays elevated as the date is re-quoted
    day after day. Drawing per quote instead would make the same flight jump
    randomly between 1.5x and 3.0x from one observation to the next, which is
    not how a price event behaves and would show up as noise in the index
    rather than as a shock.
    """
    for shock in cfg.get("demand_shocks", []):
        start = date.fromisoformat(str(shock["start_date"]))
        end = date.fromisoformat(str(shock["end_date"]))
        if start <= travel_date <= end:
            rng = _rng_for(
                cfg["random_seed"], "shock", route.key, travel_date, shock["name"]
            )
            return (
                rng.uniform(
                    float(shock["multiplier_min"]), float(shock["multiplier_max"])
                ),
                str(shock["name"]),
            )
    return 1.0, None


def sold_out_probability(advance_purchase_days: int, cfg: dict[str, Any]) -> float:
    """Chance a quote comes back with no fare. Rises sharply near departure."""
    av = cfg["availability"]
    return av["base_probability"] + av["near_term_probability"] * math.exp(
        -advance_purchase_days / av["decay_days"]
    )


def compute_taxes(base_fare: float, route: Route, cfg: dict[str, Any], rng: random.Random) -> float:
    """Semi-fixed airport charges + distance surcharge + GST on the base fare."""
    t = cfg["taxes"]
    taxes = (
        t["fixed_component_inr"]
        + t["per_1000km_surcharge_inr"] * (route.distance_km / 1000.0)
        + t["gst_rate_on_base"] * base_fare
        + rng.gauss(0.0, t["noise_sd_inr"])
    )
    return max(0.0, taxes)


# ---------------------------------------------------------------------------
# Quote generation
# ---------------------------------------------------------------------------


def generate_quote(
    airline: Airline,
    route: Route,
    travel_date: date,
    query_date: date,
    cfg: dict[str, Any] | None = None,
    source: str = SOURCE,
) -> db.FareQuote:
    """One synthetic fare quote. Deterministic for a given identity tuple.

    `source` is overridable only so Phase 3's live scraper can reuse this exact
    model for its `fallback_simulated` path -- it must never be set to 'live'.
    """
    if source == db.SOURCE_LIVE:
        raise ValueError("simulator output must never be tagged as 'live' data")

    cfg = cfg if cfg is not None else load_simulator_config()
    advance_purchase_days = (travel_date - query_date).days
    if advance_purchase_days < 0:
        raise ValueError(
            f"travel_date {travel_date} precedes query_date {query_date}"
        )

    rng = _rng_for(
        cfg["random_seed"], airline.code, route.key, travel_date, query_date
    )

    # Availability first: a sold-out quote carries no fare at all.
    if rng.random() < sold_out_probability(advance_purchase_days, cfg):
        return db.FareQuote(
            source=source,
            airline=airline.code,
            origin=route.origin,
            dest=route.dest,
            travel_date=travel_date,
            query_date=query_date,
            base_fare=None,
            taxes=None,
            total_fare=None,
            currency=cfg["backfill"]["currency"],
            booking_class=cfg["backfill"]["booking_class"],
            is_available=False,
            scraped_at=_scraped_at_for(query_date),
        )

    shock_mult, _shock_name = demand_shock(route, travel_date, cfg)

    base_fare = (
        route.base_fare_inr
        * booking_window_multiplier(advance_purchase_days, cfg)
        * day_of_week_multiplier(travel_date, cfg)
        * airline_multiplier(airline, cfg)
        * shock_mult
        * rng.lognormvariate(0.0, cfg["noise"]["lognormal_sigma"])
    )
    base_fare = round(base_fare, 2)
    taxes = round(compute_taxes(base_fare, route, cfg, rng), 2)

    return db.FareQuote(
        source=source,
        airline=airline.code,
        origin=route.origin,
        dest=route.dest,
        travel_date=travel_date,
        query_date=query_date,
        base_fare=base_fare,
        taxes=taxes,
        total_fare=round(base_fare + taxes, 2),
        currency=cfg["backfill"]["currency"],
        booking_class=cfg["backfill"]["booking_class"],
        is_available=True,
        scraped_at=_scraped_at_for(query_date),
    )


def _scraped_at_for(query_date: date) -> str:
    """A plausible observation timestamp: 06:30 UTC on the query date.

    Backfilled rows are historical, so stamping them with "now" would be a lie
    about when the fare was seen.
    """
    return datetime(
        query_date.year, query_date.month, query_date.day, 6, 30,
        tzinfo=timezone.utc,
    ).isoformat()


def generate_panel(
    query_dates: list[date],
    max_advance_purchase_days: int,
    routes: tuple[Route, ...] | None = None,
    airlines: tuple[Airline, ...] | None = None,
    cfg: dict[str, Any] | None = None,
) -> Iterator[db.FareQuote]:
    """Every (query_date x advance-purchase window x route x airline) quote.

    Yields rather than materialising -- the full panel is ~50k rows and
    db.insert_quotes chunks it.
    """
    cfg = cfg if cfg is not None else load_simulator_config()
    routes = routes if routes is not None else load_routes()
    airlines = airlines if airlines is not None else load_airlines()

    for query_date in query_dates:
        for apd in range(1, max_advance_purchase_days + 1):
            travel_date = query_date + timedelta(days=apd)
            for route in routes:
                for airline in airlines:
                    yield generate_quote(
                        airline, route, travel_date, query_date, cfg
                    )


def backfill_query_dates(cfg: dict[str, Any], end_date: date | None = None) -> list[date]:
    """The last `query_date_days` days, ending today (inclusive)."""
    end_date = end_date or date.today()
    n = int(cfg["backfill"]["query_date_days"])
    return [end_date - timedelta(days=i) for i in range(n - 1, -1, -1)]


# ---------------------------------------------------------------------------
# CLI: backfill
# ---------------------------------------------------------------------------


def run_backfill(db_path: str | Path = db.DEFAULT_DB_PATH, end_date: date | None = None) -> int:
    """Generate the full panel and write it to the database. Returns row count."""
    cfg = load_simulator_config()
    routes, airlines = load_routes(), load_airlines()
    query_dates = backfill_query_dates(cfg, end_date)
    max_apd = int(cfg["backfill"]["max_advance_purchase_days"])

    expected = len(query_dates) * max_apd * len(routes) * len(airlines)
    print("=" * 72)
    print("GENERATING SIMULATED FARE DATA -- this is not real airline data")
    print("=" * 72)
    print(f"  query_dates          {query_dates[0]} .. {query_dates[-1]} "
          f"({len(query_dates)} days)")
    print(f"  advance-purchase     1 .. {max_apd} days")
    print(f"  routes x airlines    {len(routes)} x {len(airlines)}")
    print(f"  expected rows        {expected:,}")
    print(f"  seed                 {cfg['random_seed']} (deterministic)")

    db.init_db(db_path)
    with db.connect(db_path) as conn:
        written = db.insert_quotes(
            conn, generate_panel(query_dates, max_apd, routes, airlines, cfg)
        )

    with db.connect(db_path) as conn:
        total = db.count_quotes(conn)
        by_source = db.counts_by_source(conn)
        unavailable = conn.execute(
            "SELECT COUNT(*) FROM fare_quotes WHERE is_available = 0"
        ).fetchone()[0]
        lo, hi = db.date_range(conn)

    print(f"\n  submitted            {written:,}")
    print(f"  rows in fare_quotes  {total:,}")
    print(f"  by source            {by_source}")
    print(f"  sold out / no fare   {unavailable:,} ({unavailable / total:.1%})")
    print(f"  query_date range     {lo} .. {hi}")
    return total


# ---------------------------------------------------------------------------
# CLI: sanity check
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _is_shock_date(travel_date: date, cfg: dict[str, Any]) -> bool:
    return any(
        date.fromisoformat(str(s["start_date"]))
        <= travel_date
        <= date.fromisoformat(str(s["end_date"]))
        for s in cfg.get("demand_shocks", [])
    )


def _sanity_travel_dates(cfg: dict[str, Any], n: int = 30) -> list[date]:
    """Consecutive travel dates for the curve check, excluding shock windows.

    IMPORTANT: the curve must be traced by holding travel_date fixed and varying
    query_date -- i.e. watching one flight's fare as its departure approaches.
    Averaging at a fixed advance_purchase_days instead sweeps travel_date along
    with lead time, which mixes the booking curve together with whichever
    festival windows that lead time happens to land on, and produces a
    non-monotone curve that says nothing about the pricing model.

    Dates start far enough ahead that every lead time from 1 to max is
    reachable, and span enough days for the day-of-week effect to average out.
    """
    max_apd = int(cfg["backfill"]["max_advance_purchase_days"])
    start = date.today() + timedelta(days=max_apd + 1)
    dates, d = [], start
    while len(dates) < n:
        if not _is_shock_date(d, cfg):
            dates.append(d)
        d += timedelta(days=1)
    return dates


def run_sanity_check(html_out: Path | None = None) -> bool:
    """Fare-vs-lead-time curves for a couple of routes.

    Prints the curve numerically, asserts it actually behaves like an airfare
    curve (rising as departure nears, and steeper near the end than far out),
    and writes an interactive chart. Returns True if all checks pass.
    """
    cfg = load_simulator_config()
    airlines = load_airlines()
    sample_routes = [r for r in load_routes() if r.key in ("DEL-BOM", "BLR-MAA")]
    apds = list(range(1, int(cfg["backfill"]["max_advance_purchase_days"]) + 1))
    travel_dates = _sanity_travel_dates(cfg)

    print(f"Tracing {len(travel_dates)} flights ({travel_dates[0]} .. "
          f"{travel_dates[-1]}) from {apds[-1]} days out to departure.")
    print("Shock-window travel dates excluded, so this is the booking-window "
          "curve in isolation.")

    all_ok = True
    series: dict[str, list[float]] = {}

    for route in sample_routes:
        curve: list[float] = []
        sample_sizes: list[int] = []
        for apd in apds:
            fares = [
                q.total_fare
                for travel_date in travel_dates
                for airline in airlines
                if (q := generate_quote(
                    airline, route, travel_date, travel_date - timedelta(days=apd), cfg
                )).is_available and q.total_fare is not None
            ]
            curve.append(_mean(fares))
            sample_sizes.append(len(fares))
        series[route.key] = curve

        print(f"\n{route.key}  ({route.name}) -- mean total fare by lead time")
        print(f"{'apd':>5}  {'mean fare':>12}   curve")
        lo_f, hi_f = min(curve), max(curve)
        for apd in (1, 3, 5, 7, 10, 14, 21, 30, 45):
            fare = curve[apd - 1]
            bar = "#" * int(40 * (fare - lo_f) / (hi_f - lo_f)) if hi_f > lo_f else ""
            print(f"{apd:>5}  Rs.{fare:>9,.0f}   {bar}")

        # --- the actual assertions -------------------------------------------
        far, near = curve[44], curve[0]             # 45 days out vs 1 day out
        ratio = near / far
        late_slope = (curve[0] - curve[6]) / 6      # Rs/day over the last week
        early_slope = (curve[29] - curve[44]) / 15  # Rs/day between 45 and 30 days

        # Monotonicity is asserted strictly on a 5-day grid, where the true
        # curve step is comfortably larger than the sampling error.
        coarse = [curve[a - 1] for a in range(1, 46, 5)]
        strict_monotone = all(x > y for x, y in zip(coarse, coarse[1:]))

        # On the 1-day grid the true step far from departure (~0.5%/day) is
        # smaller than the noise in a mean of n quotes, so a fixed tolerance
        # would be testing below the noise floor. Bound it instead at 3 standard
        # errors of the difference of two sample means, derived from the
        # configured lognormal sigma and the actual sample sizes.
        sigma = float(cfg["noise"]["lognormal_sigma"])
        n_min = min(sample_sizes)
        tol = 3.0 * sigma * math.sqrt(2.0 / n_min)
        worst = min(
            curve[i] / curve[i + 1] for i in range(len(curve) - 1)
        )
        fine_monotone = worst >= 1.0 - tol

        checks = [
            ("last-minute dearer than far-out", near > far),
            ("spike is 1.8x-3.5x", 1.8 <= ratio <= 3.5),
            ("steeper near departure than far out", late_slope > 4 * early_slope),
            ("strictly monotone on 5-day grid", strict_monotone),
            (f"no 1-day inversion beyond sampling noise "
             f"(worst {1 - worst:+.2%}, tol {tol:.2%}, n>={n_min})",
             fine_monotone),
        ]
        print(f"    45d mean Rs.{far:,.0f} -> 1d mean Rs.{near:,.0f}  ({ratio:.2f}x)")
        print(f"    slope last 7d  Rs.{late_slope:,.0f}/day")
        print(f"    slope 45d->30d Rs.{early_slope:,.0f}/day")
        for label, passed in checks:
            print(f"    [{'PASS' if passed else 'FAIL'}] {label}")
            all_ok = all_ok and passed

    all_ok = _check_demand_shocks(cfg, airlines, sample_routes[0]) and all_ok

    if html_out is not None:
        _write_curve_chart(apds, series, html_out)
        print(f"\n  chart written to {html_out}")

    print(f"\nSANITY CHECK: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


def _check_demand_shocks(
    cfg: dict[str, Any], airlines: tuple[Airline, ...], route: Route
) -> bool:
    """Confirm each configured shock actually spikes fares by the stated amount.

    Compares shock-window travel dates against nearby ordinary ones at the same
    lead time, so only the shock differs.
    """
    print(f"\nDemand shocks on {route.key} (fixed 30-day lead time)")
    apd = 30
    ok = True

    for shock in cfg.get("demand_shocks", []):
        start = date.fromisoformat(str(shock["start_date"]))
        end = date.fromisoformat(str(shock["end_date"]))

        shock_dates = [
            start + timedelta(days=i) for i in range((end - start).days + 1)
        ]
        # Baseline: the week either side, excluding any other shock window.
        control_dates = [
            d for i in range(1, 8)
            for d in (start - timedelta(days=i), end + timedelta(days=i))
            if not _is_shock_date(d, cfg)
        ]

        def mean_fare(dates: list[date]) -> float:
            return _mean([
                q.total_fare
                for td in dates
                for a in airlines
                if (q := generate_quote(
                    a, route, td, td - timedelta(days=apd), cfg
                )).is_available and q.total_fare is not None
            ])

        shocked, control = mean_fare(shock_dates), mean_fare(control_dates)
        ratio = shocked / control
        lo, hi = float(shock["multiplier_min"]), float(shock["multiplier_max"])
        # Compare against the configured range, loosened for the fixed tax
        # component (which does not scale) and day-of-week mix.
        passed = (lo * 0.75) <= ratio <= (hi * 1.15)
        ok = ok and passed
        print(f"    [{'PASS' if passed else 'FAIL'}] {shock['name']:<28} "
              f"Rs.{control:>8,.0f} -> Rs.{shocked:>8,.0f}  ({ratio:.2f}x, "
              f"config {lo:.1f}-{hi:.1f}x)")

    return ok


def _write_curve_chart(apds: list[int], series: dict[str, list[float]], out: Path) -> None:
    """Interactive fare-vs-lead-time chart, for eyeballing the curve shape."""
    import plotly.graph_objects as go

    fig = go.Figure()
    for key, curve in series.items():
        fig.add_trace(go.Scatter(x=apds, y=curve, mode="lines+markers", name=key))
    fig.update_layout(
        title="SIMULATED fare vs. advance-purchase days (mean over all carriers "
              "and travel dates)",
        xaxis_title="Advance purchase days (days before departure)",
        yaxis_title="Mean total fare (INR)",
        xaxis=dict(autorange="reversed"),  # departure at the right-hand edge
        template="plotly_white",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs="cdn")


# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="APIx fare simulator (generates SIMULATED data only)"
    )
    parser.add_argument(
        "command", choices=("backfill", "sanity"), nargs="?", default="backfill"
    )
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    args = parser.parse_args(argv)

    if args.command == "backfill":
        run_backfill(args.db)
        return 0

    repo_root = Path(__file__).resolve().parents[1]
    ok = run_sanity_check(html_out=repo_root / "docs" / "simulator_curve_check.html")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
