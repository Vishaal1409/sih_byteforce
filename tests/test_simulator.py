"""Phase 2 tests: the simulator produces plausible, deterministic fare quotes.

These lock in the behaviours the demo depends on — the booking-window curve is
the right shape, shocks fire coherently, provenance is always 'simulated', and
re-running the backfill does not churn the dataset.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import (  # noqa: E402
    airline_by_code,
    load_airlines,
    load_routes,
    load_simulator_config,
    route_by_key,
)
from scraper import simulator  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return load_simulator_config()


@pytest.fixture(scope="module")
def route():
    return route_by_key("DEL-BOM")


@pytest.fixture(scope="module")
def indigo():
    return airline_by_code("6E")


# --- provenance -------------------------------------------------------------


def test_every_quote_is_tagged_simulated(cfg, route, indigo):
    q = simulator.generate_quote(indigo, route, date(2026, 10, 1), date(2026, 9, 1), cfg)
    assert q.source == db.SOURCE_SIMULATED


def test_simulator_refuses_to_masquerade_as_live(cfg, route, indigo):
    """The one thing this module must never do (CLAUDE.md ground rule 5)."""
    with pytest.raises(ValueError, match="never be tagged as 'live'"):
        simulator.generate_quote(
            indigo, route, date(2026, 10, 1), date(2026, 9, 1), cfg,
            source=db.SOURCE_LIVE,
        )


def test_fallback_source_is_allowed(cfg, route, indigo):
    """Phase 3's scraper reuses this model for its fallback path."""
    q = simulator.generate_quote(
        indigo, route, date(2026, 10, 1), date(2026, 9, 1), cfg,
        source=db.SOURCE_FALLBACK,
    )
    assert q.source == db.SOURCE_FALLBACK


# --- determinism ------------------------------------------------------------


def test_same_inputs_give_identical_quote(cfg, route, indigo):
    args = (indigo, route, date(2026, 10, 1), date(2026, 9, 1), cfg)
    a, b = simulator.generate_quote(*args), simulator.generate_quote(*args)
    assert (a.base_fare, a.taxes, a.total_fare, a.is_available) == (
        b.base_fare, b.taxes, b.total_fare, b.is_available
    )


def test_different_airlines_give_different_quotes(cfg, route):
    td, qd = date(2026, 10, 1), date(2026, 9, 1)
    fares = {
        a.code: simulator.generate_quote(a, route, td, qd, cfg).base_fare
        for a in load_airlines()
    }
    assert len(set(fares.values())) == len(fares)


# --- booking-window curve ---------------------------------------------------


def test_booking_curve_is_monotone_in_lead_time(cfg):
    """The multiplier must fall as advance_purchase_days rises, with no kinks."""
    values = [simulator.booking_window_multiplier(a, cfg) for a in range(0, 91)]
    assert all(x > y for x, y in zip(values, values[1:]))


def test_booking_curve_flattens_far_out(cfg):
    """Steep near departure, flat far out — the characteristic airfare shape."""
    near = simulator.booking_window_multiplier(1, cfg) - simulator.booking_window_multiplier(7, cfg)
    far = simulator.booking_window_multiplier(38, cfg) - simulator.booking_window_multiplier(44, cfg)
    assert near > 10 * far


def test_last_minute_is_substantially_dearer(cfg):
    ratio = (
        simulator.booking_window_multiplier(1, cfg)
        / simulator.booking_window_multiplier(45, cfg)
    )
    assert 2.0 < ratio < 3.0


def test_realised_fares_rise_as_departure_nears(cfg, route):
    """End-to-end, not just the curve function: averaged over carriers and
    shock-free travel dates, fares must rise as the flight approaches."""
    travel_dates = simulator._sanity_travel_dates(cfg, n=20)
    airlines = load_airlines()

    def mean_at(apd: int) -> float:
        fares = [
            q.total_fare
            for td in travel_dates
            for a in airlines
            if (q := simulator.generate_quote(
                a, route, td, td - timedelta(days=apd), cfg
            )).is_available and q.total_fare is not None
        ]
        return sum(fares) / len(fares)

    curve = [mean_at(apd) for apd in (45, 30, 21, 14, 7, 3, 1)]
    assert all(x < y for x, y in zip(curve, curve[1:])), curve


# --- carrier positioning ----------------------------------------------------


def test_full_service_prices_above_budget(cfg, route):
    """Air India (full-service) must sit above the low-cost carriers."""
    td, qd = date(2026, 10, 1), date(2026, 9, 1)
    ai = simulator.airline_multiplier(airline_by_code("AI"), cfg)
    for code in ("6E", "QP", "SG"):
        assert ai > simulator.airline_multiplier(airline_by_code(code), cfg)


# --- taxes ------------------------------------------------------------------


def test_taxes_scale_with_distance_and_base_fare(cfg):
    """Longer sector and dearer ticket both raise taxes."""
    short, long_ = route_by_key("BLR-MAA"), route_by_key("DEL-MAA")
    rng = simulator._rng_for(1, "tax-test")
    t_short = simulator.compute_taxes(4000.0, short, cfg, simulator._rng_for(1, "a"))
    t_long = simulator.compute_taxes(4000.0, long_, cfg, simulator._rng_for(1, "a"))
    assert t_long > t_short

    t_cheap = simulator.compute_taxes(3000.0, short, cfg, simulator._rng_for(1, "a"))
    t_dear = simulator.compute_taxes(9000.0, short, cfg, simulator._rng_for(1, "a"))
    assert t_dear > t_cheap


def test_taxes_are_never_negative(cfg):
    r = route_by_key("BLR-MAA")
    for i in range(200):
        assert simulator.compute_taxes(0.0, r, cfg, simulator._rng_for(i, "t")) >= 0.0


def test_total_equals_base_plus_taxes(cfg, route, indigo):
    q = simulator.generate_quote(indigo, route, date(2026, 10, 1), date(2026, 9, 1), cfg)
    assert q.total_fare == pytest.approx(q.base_fare + q.taxes, abs=0.01)


# --- demand shocks ----------------------------------------------------------


def test_ordinary_day_has_no_shock(cfg, route):
    mult, name = simulator.demand_shock(route, date(2026, 9, 15), cfg)
    assert (mult, name) == (1.0, None)


def test_configured_shock_dates_fire_within_range(cfg, route):
    for shock in cfg["demand_shocks"]:
        start = date.fromisoformat(str(shock["start_date"]))
        mult, name = simulator.demand_shock(route, start, cfg)
        assert name == shock["name"]
        assert shock["multiplier_min"] <= mult <= shock["multiplier_max"]


def test_shock_is_coherent_across_airlines_and_query_dates(cfg, route):
    """A festival is one price event for a date, not per-quote noise.

    The multiplier must be identical however the date is observed — otherwise
    the same flight jumps randomly between 1.5x and 3.0x between observations,
    which shows up as noise in the index instead of a shock.
    """
    td = date.fromisoformat(str(cfg["demand_shocks"][0]["start_date"]))
    first, _ = simulator.demand_shock(route, td, cfg)
    for _ in range(5):
        assert simulator.demand_shock(route, td, cfg)[0] == first


def test_shock_dates_are_dearer_than_neighbours(cfg, route):
    """End-to-end: a shock travel date must actually cost more."""
    airlines = load_airlines()
    shock = cfg["demand_shocks"][2]
    shock_date = date.fromisoformat(str(shock["start_date"]))
    control = shock_date + timedelta(days=10)
    assert not simulator._is_shock_date(control, cfg)

    def mean_base(td: date) -> float:
        fares = [
            q.base_fare
            for a in airlines
            for apd in (25, 30, 35)
            if (q := simulator.generate_quote(
                a, route, td, td - timedelta(days=apd), cfg
            )).is_available and q.base_fare is not None
        ]
        return sum(fares) / len(fares)

    assert mean_base(shock_date) > 1.5 * mean_base(control)


# --- availability -----------------------------------------------------------


def test_sold_out_probability_rises_as_departure_nears(cfg):
    ps = [simulator.sold_out_probability(a, cfg) for a in range(1, 46)]
    assert all(x > y for x, y in zip(ps, ps[1:]))


def test_sold_out_rate_is_low_overall(cfg, route):
    """'Low frequency', per the brief — a few percent, not a fifth of the data."""
    airlines = load_airlines()
    quotes = [
        simulator.generate_quote(a, route, qd + timedelta(days=apd), qd, cfg)
        for a in airlines
        for apd in range(1, 46)
        for qd in (date(2026, 8, 10), date(2026, 8, 20), date(2026, 9, 1))
    ]
    rate = sum(1 for q in quotes if not q.is_available) / len(quotes)
    assert 0.005 < rate < 0.10, rate


def test_sold_out_quotes_carry_no_fare(cfg, route):
    airlines = load_airlines()
    sold_out = [
        q
        for a in airlines
        for apd in range(1, 10)
        for qd in (date(2026, 8, 10), date(2026, 8, 15), date(2026, 8, 20))
        if not (q := simulator.generate_quote(
            a, route, qd + timedelta(days=apd), qd, cfg
        )).is_available
    ]
    assert sold_out, "expected at least one sold-out quote in this sample"
    for q in sold_out:
        assert (q.base_fare, q.taxes, q.total_fare) == (None, None, None)


# --- panel generation and persistence --------------------------------------


def test_panel_has_expected_shape(cfg):
    query_dates = [date(2026, 9, 1), date(2026, 9, 2)]
    panel = list(simulator.generate_panel(query_dates, max_advance_purchase_days=5, cfg=cfg))
    assert len(panel) == 2 * 5 * len(load_routes()) * len(load_airlines())
    assert {q.advance_purchase_days for q in panel} == {1, 2, 3, 4, 5}


def test_scraped_at_reflects_the_query_date_not_now(cfg, route, indigo):
    """Backfilled rows are historical; stamping them 'now' would misdate them."""
    q = simulator.generate_quote(indigo, route, date(2026, 10, 1), date(2026, 9, 1), cfg)
    assert q.scraped_at.startswith("2026-09-01")


def test_backfill_writes_and_is_idempotent(tmp_path, cfg):
    """Re-running the backfill must refresh rows, not duplicate them."""
    db_path = tmp_path / "backfill.db"
    query_dates = [date(2026, 9, 1), date(2026, 9, 2)]
    panel = list(simulator.generate_panel(query_dates, 3, cfg=cfg))

    db.init_db(db_path)
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, panel)
    with db.connect(db_path) as conn:
        first = db.count_quotes(conn)

    with db.connect(db_path) as conn:
        db.insert_quotes(conn, simulator.generate_panel(query_dates, 3, cfg=cfg))
    with db.connect(db_path) as conn:
        assert db.count_quotes(conn) == first
        assert db.counts_by_source(conn) == {db.SOURCE_SIMULATED: first}


def test_backfill_query_dates_end_today(cfg):
    dates = simulator.backfill_query_dates(cfg, end_date=date(2026, 9, 10))
    assert dates[-1] == date(2026, 9, 10)
    assert len(dates) == cfg["backfill"]["query_date_days"]
    assert dates == sorted(dates)


def test_rejects_travel_date_before_query_date(cfg, route, indigo):
    with pytest.raises(ValueError, match="precedes query_date"):
        simulator.generate_quote(indigo, route, date(2026, 9, 1), date(2026, 10, 1), cfg)
