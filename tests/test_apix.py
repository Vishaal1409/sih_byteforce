"""Phase 5 tests: the index is 100 at base and moves for the right reasons.

The two the task names explicitly — base period == 100, and a synthetic demand
shock moves it sensibly — plus the properties that make a Laspeyres index worth
having: immunity to composition shifts, fixed weights, correct handling of
missing cells.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import load_airlines, load_routes  # noqa: E402
from index import apix  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return apix.load_index_config()


def synthetic_panel(
    n_days: int = 20,
    routes: tuple[str, ...] = ("DEL-BOM", "BLR-MAA"),
    airlines: tuple[str, ...] = ("6E", "AI"),
    apds: tuple[int, ...] = (2, 5, 10, 18, 25, 35),
    fare_fn=None,
) -> pd.DataFrame:
    """A prepared fare frame with full basket coverage on every day.

    One apd inside each of the six configured buckets, so every day has a
    complete set of cells and coverage is 100%.
    """
    fare_fn = fare_fn or (lambda q, td, apd, route, airline: 5000.0)
    rows = []
    start = date(2026, 8, 1)
    for d in range(n_days):
        qd = start + timedelta(days=d)
        for apd in apds:
            td = qd + timedelta(days=apd)
            for r in routes:
                o, dst = r.split("-")
                for a in airlines:
                    rows.append({
                        "source": db.SOURCE_SIMULATED,
                        "airline": a, "origin": o, "dest": dst,
                        "travel_date": td.isoformat(),
                        "query_date": qd.isoformat(),
                        "advance_purchase_days": apd,
                        "base_fare": None, "taxes": None,
                        "total_fare": float(fare_fn(qd, td, apd, r, a)),
                        "currency": "INR", "booking_class": "ECONOMY",
                        "is_available": 1,
                        "scraped_at": f"{qd.isoformat()}T06:30:00+00:00",
                    })
    return apix.prepare(pd.DataFrame(rows))


# --- the basket -------------------------------------------------------------


def test_basket_weights_sum_to_one(cfg):
    w = apix.basket_weights(cfg)
    assert abs(w["weight"].sum() - 1.0) < 1e-9


def test_basket_has_one_cell_per_combination(cfg):
    w = apix.basket_weights(cfg)
    expected = len(load_routes()) * len(load_airlines()) * len(cfg["advance_purchase_buckets"])
    assert len(w) == expected == 192


def test_bucket_assignment_covers_the_configured_range(cfg):
    apd = pd.Series(range(0, 60))
    buckets = apix.assign_bucket(apd, cfg)
    assert buckets.iloc[0] is pd.NA or pd.isna(buckets.iloc[0])   # apd 0
    assert buckets.iloc[1] == "0-3d"
    assert buckets.iloc[45] == "31-45d"
    assert pd.isna(buckets.iloc[46])                              # beyond basket


def test_rows_outside_any_bucket_are_excluded(cfg):
    df = synthetic_panel(apds=(2, 90))
    assert df["advance_purchase_days"].max() <= 45


# --- the task's explicit checks ---------------------------------------------


def test_index_is_100_at_the_base_period(cfg):
    result = apix.compute_index(synthetic_panel(), cfg)
    assert result.series.iloc[0]["index_value"] == pytest.approx(100.0, abs=1e-9)


def test_index_is_100_throughout_when_prices_are_flat(cfg):
    result = apix.compute_index(synthetic_panel(), cfg)
    assert result.series["index_value"].to_numpy() == pytest.approx(100.0, abs=1e-9)


def test_demand_shock_moves_the_index_up(cfg):
    """The task's second named check: a synthetic shock must show up."""
    shock_day = date(2026, 8, 10)

    def fares(qd, td, apd, route, airline):
        return 5000.0 * (2.5 if qd == shock_day else 1.0)

    result = apix.compute_index(synthetic_panel(fare_fn=fares), cfg)
    s = result.series.set_index("query_date")["index_value"]

    assert s.loc[shock_day.isoformat()] == pytest.approx(250.0, rel=1e-9)
    assert s.loc["2026-08-09"] == pytest.approx(100.0, abs=1e-9)
    assert s.loc["2026-08-11"] == pytest.approx(100.0, abs=1e-9)


def test_index_scales_exactly_with_a_uniform_price_change(cfg):
    """A 10% rise everywhere must give exactly 110, not approximately."""
    def fares(qd, td, apd, route, airline):
        return 5000.0 * (1.10 if qd > date(2026, 8, 5) else 1.0)

    s = apix.compute_index(synthetic_panel(fare_fn=fares), cfg).series
    assert s.iloc[-1]["index_value"] == pytest.approx(110.0, rel=1e-9)


def test_index_falls_when_fares_fall(cfg):
    def fares(qd, td, apd, route, airline):
        return 5000.0 * (0.8 if qd > date(2026, 8, 5) else 1.0)

    s = apix.compute_index(synthetic_panel(fare_fn=fares), cfg).series
    assert s.iloc[-1]["index_value"] == pytest.approx(80.0, rel=1e-9)


# --- the property that justifies a fixed basket ------------------------------


def test_index_ignores_a_shift_in_observation_mix(cfg):
    """The whole point of fixed weights.

    Flood the sample with cheap short-haul rows partway through. A naive
    average of all fares would collapse; APIx must not move at all, because
    prices did not move.
    """
    def fares(qd, td, apd, route, airline):
        return 8000.0 if route == "DEL-BOM" else 3000.0

    df = synthetic_panel(fare_fn=fares)

    later = df[
        (df["query_date"] > "2026-08-10") & (df["origin"] == "BLR")
    ]
    flooded = pd.concat([df, later, later, later], ignore_index=True)

    naive = flooded.groupby("query_date")["total_fare"].mean()
    assert naive.iloc[-1] < naive.iloc[0] * 0.95, "the naive average should drop"

    s = apix.compute_index(flooded, cfg).series
    assert s["index_value"].to_numpy() == pytest.approx(100.0, abs=1e-9)


# --- missing cells ----------------------------------------------------------


def test_missing_cells_are_reflected_in_coverage(cfg):
    df = synthetic_panel()
    drop = (df["query_date"] == "2026-08-05") & (df["airline"] == "AI")
    result = apix.compute_index(df[~drop], cfg)

    s = result.series.set_index("query_date")
    assert s.loc["2026-08-05", "coverage"] < 1.0
    assert s.loc["2026-08-04", "coverage"] == pytest.approx(1.0)


def test_missing_cells_do_not_drag_the_index_down(cfg):
    """Weight is redistributed, not treated as a zero price."""
    df = synthetic_panel()
    drop = (df["query_date"] == "2026-08-05") & (df["airline"] == "AI")
    s = apix.compute_index(df[~drop], cfg).series.set_index("query_date")
    assert s.loc["2026-08-05", "index_value"] == pytest.approx(100.0, abs=1e-9)


def test_cells_absent_at_base_are_dropped_from_the_basket(cfg):
    result = apix.compute_index(synthetic_panel(routes=("DEL-BOM",)), cfg)
    assert result.dropped_cells > 0
    assert any("base-period" in n for n in result.notes)


def test_empty_input_returns_an_empty_series(cfg):
    result = apix.compute_index(pd.DataFrame(columns=["query_date"]), cfg)
    assert result.series.empty


# --- aggregation ------------------------------------------------------------


def test_weekly_and_monthly_aggregate_the_daily_series(cfg):
    daily = apix.compute_index(synthetic_panel(n_days=40), cfg).series
    weekly = apix.aggregate_index(daily, "weekly", cfg)
    monthly = apix.aggregate_index(daily, "monthly", cfg)

    assert len(weekly) >= 4
    assert len(monthly) >= 1
    assert weekly["index_value"].to_numpy() == pytest.approx(100.0, abs=1e-9)
    assert monthly["index_value"].to_numpy() == pytest.approx(100.0, abs=1e-9)


def test_partial_periods_are_dropped(cfg):
    """A 3-day stub must not be published as a week."""
    daily = apix.compute_index(synthetic_panel(n_days=3), cfg).series
    assert apix.aggregate_index(daily, "weekly", cfg).empty


def test_unsupported_frequency_is_rejected(cfg):
    daily = apix.compute_index(synthetic_panel(), cfg).series
    with pytest.raises(ValueError, match="unsupported frequency"):
        apix.aggregate_index(daily, "hourly", cfg)


# --- route sub-indices ------------------------------------------------------


def test_route_subindex_is_100_at_base(cfg):
    df = synthetic_panel()
    res = apix.route_subindex(df, "DEL", "BOM", cfg)
    assert res.series.iloc[0]["index_value"] == pytest.approx(100.0, abs=1e-9)


def test_route_subindex_tracks_only_its_own_route(cfg):
    def fares(qd, td, apd, route, airline):
        if route == "DEL-BOM" and qd > date(2026, 8, 10):
            return 10000.0
        return 5000.0

    df = synthetic_panel(fare_fn=fares)
    del_bom = apix.route_subindex(df, "DEL", "BOM", cfg).series
    blr_maa = apix.route_subindex(df, "BLR", "MAA", cfg).series

    assert del_bom.iloc[-1]["index_value"] == pytest.approx(200.0, rel=1e-9)
    assert blr_maa.iloc[-1]["index_value"] == pytest.approx(100.0, abs=1e-9)


def test_unknown_route_is_rejected(cfg):
    with pytest.raises(KeyError, match="not in the basket"):
        apix.route_subindex(synthetic_panel(), "XXX", "YYY", cfg)


# --- elasticity -------------------------------------------------------------


def test_elasticity_recovers_a_known_rate(cfg):
    """Fares built to rise exactly 2%/day closer to departure must measure 2%."""
    rate = 0.02

    def fares(qd, td, apd, route, airline):
        return 4000.0 * (1.0 + rate) ** (45 - apd)

    results = apix.lead_time_elasticity(synthetic_panel(fare_fn=fares), cfg)
    assert results
    for e in results:
        assert e.pct_change_per_day == pytest.approx(rate * 100, rel=1e-6)
        assert e.r_squared > 0.99


def test_elasticity_is_positive_and_reported_per_route(cfg):
    def fares(qd, td, apd, route, airline):
        return 4000.0 * (1.03 ** (45 - apd))

    results = apix.lead_time_elasticity(synthetic_panel(fare_fn=fares), cfg)
    assert {e.route for e in results} == {"DEL-BOM", "BLR-MAA"}
    assert all(e.pct_change_per_day > 0 for e in results)


def test_elasticity_controls_for_travel_date_effects(cfg):
    """A festival premium on late departures must not flatten the estimate.

    This is the bias the fixed-effects estimator exists to remove: it cost ~30%
    of the measured elasticity on the real panel.
    """
    rate = 0.02
    festival = date(2026, 9, 5)

    def fares(qd, td, apd, route, airline):
        base = 4000.0 * (1.0 + rate) ** (45 - apd)
        return base * (2.5 if td >= festival else 1.0)

    df = synthetic_panel(n_days=25, fare_fn=fares)

    controlled = apix.lead_time_elasticity(df, cfg)
    naive_cfg = {**cfg, "elasticity": {**cfg["elasticity"],
                                       "control_for_travel_date": False}}
    naive = apix.lead_time_elasticity(df, naive_cfg)

    for e in controlled:
        assert e.pct_change_per_day == pytest.approx(rate * 100, rel=0.05)
    assert min(e.pct_change_per_day for e in naive) < rate * 100 * 0.9, (
        "the naive estimator should be visibly biased by the festival premium"
    )


def test_elasticity_skips_thin_routes(cfg):
    df = synthetic_panel(n_days=1, apds=(5,))
    assert apix.lead_time_elasticity(df, cfg) == []


def test_elasticity_on_empty_input(cfg):
    assert apix.lead_time_elasticity(pd.DataFrame(), cfg) == []


# --- persistence ------------------------------------------------------------


def test_build_and_store_writes_every_table(tmp_path, cfg):
    from etl import clean as C

    db_path = db.init_db(tmp_path / "idx.db")
    panel = synthetic_panel(n_days=30, routes=tuple(r.key for r in load_routes()),
                            airlines=tuple(a.code for a in load_airlines()))
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k not in ("bucket",)}
            for r in panel.to_dict("records")
        ])
    C.run_clean(db_path)

    summary = apix.build_and_store(db_path, cfg)
    assert summary["daily_points"] == 30
    assert summary["routes"] == len(load_routes())

    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM apix_index").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM apix_route_index").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM apix_elasticity").fetchone()[0] > 0
        meta = dict(conn.execute("SELECT key, value FROM apix_meta").fetchall())
        assert meta["base_value"] == "100.0"
        assert meta["base_period"]

        daily = conn.execute(
            "SELECT index_value FROM apix_index WHERE period_type='daily' "
            "ORDER BY period LIMIT 1"
        ).fetchone()[0]
        assert daily == pytest.approx(100.0, abs=1e-9)


def test_rebuilding_replaces_rather_than_appends(tmp_path, cfg):
    from etl import clean as C

    db_path = db.init_db(tmp_path / "idx2.db")
    panel = synthetic_panel(n_days=20, routes=tuple(r.key for r in load_routes()),
                            airlines=tuple(a.code for a in load_airlines()))
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k not in ("bucket",)}
            for r in panel.to_dict("records")
        ])
    C.run_clean(db_path)

    apix.build_and_store(db_path, cfg)
    with db.connect(db_path) as conn:
        first = conn.execute("SELECT COUNT(*) FROM apix_index").fetchone()[0]
    apix.build_and_store(db_path, cfg)
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM apix_index").fetchone()[0] == first
