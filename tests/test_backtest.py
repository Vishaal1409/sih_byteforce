"""Phase 6 tests: the backtest measures agreement honestly.

Includes the guard that matters most for credibility — the reference must be a
genuinely different estimator from APIx, not a copy of it, so a high
correlation means something.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from index import apix, backtest as bt  # noqa: E402
from tests.test_apix import synthetic_panel  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return bt.load_backtest_config()


def series(values, start="2026-08-01", col="index_value"):
    dates = pd.date_range(start, periods=len(values)).strftime("%Y-%m-%d")
    return pd.DataFrame({"query_date": dates, col: list(map(float, values))})


# --- the reference is labelled as synthetic ---------------------------------


def test_reference_is_declared_synthetic(cfg):
    """Credibility depends on this never quietly flipping to 'real'."""
    assert cfg["reference"]["is_real_data"] is False
    label = cfg["reference"]["label"].lower()
    assert "not real dgca data" in label
    assert "synthetic" in label
    assert "synthetic" in cfg["reference"]["short_label"].lower()


def test_stored_metrics_record_that_the_reference_is_not_real(tmp_path, cfg):
    db_path = _panel_db(tmp_path / "bt_flag.db")
    bt.run_backtest(db_path, cfg, write_chart=False,
                    results_json=Path(db_path).parent / 'results.json')
    stored = bt.latest_backtest(db_path)
    assert stored["metrics"]["reference_is_real_data"] == "false"
    assert "not real DGCA data" in stored["metrics"]["reference_label"].lower() or \
           "NOT REAL DGCA" in stored["metrics"]["reference_label"].upper()


# --- the reference is a different estimator ---------------------------------


def test_reference_is_not_a_copy_of_apix(cfg):
    """If the reference were APIx-with-noise the backtest would prove nothing.

    It is an unweighted market mean, so it must differ measurably from the
    weighted index on data where weighting actually matters.
    """
    def fares(qd, td, apd, route, airline):
        # Make one heavily-weighted cell move differently from the rest, so a
        # weighted and an unweighted estimator cannot agree exactly.
        if airline == "6E" and qd > pd.Timestamp("2026-08-10").date():
            return 9000.0
        return 5000.0

    df = synthetic_panel(fare_fn=fares)
    index_series = apix.compute_index(df).series
    reference = bt.build_reference_series(df, cfg)
    merged, metrics = bt.compare(index_series, reference, cfg)

    assert metrics.mape_pct > 0.5, (
        "reference tracks APIx too closely to be an independent check"
    )


def test_reference_is_rebased_to_100(cfg):
    ref = bt.build_reference_series(synthetic_panel(), cfg)
    assert ref.iloc[0]["reference_value"] == pytest.approx(100.0)


def test_reference_is_deterministic(cfg):
    df = synthetic_panel()
    a = bt.build_reference_series(df, cfg)["reference_value"].to_numpy()
    b = bt.build_reference_series(df, cfg)["reference_value"].to_numpy()
    assert a == pytest.approx(b)


def test_reference_follows_a_real_price_move(cfg):
    def fares(qd, td, apd, route, airline):
        return 5000.0 * (1.5 if qd > pd.Timestamp("2026-08-10").date() else 1.0)

    ref = bt.build_reference_series(synthetic_panel(fare_fn=fares), cfg)
    assert ref.iloc[-1]["reference_value"] > 130.0


def test_reference_on_empty_input(cfg):
    assert bt.build_reference_series(pd.DataFrame(), cfg).empty


# --- metrics ----------------------------------------------------------------


def test_identical_series_give_perfect_agreement(cfg):
    a = series([100, 102, 105, 103, 108])
    b = series([100, 102, 105, 103, 108], col="reference_value")
    _, m = bt.compare(a, b, cfg)

    assert m.correlation == pytest.approx(1.0)
    assert m.mape_pct == pytest.approx(0.0, abs=1e-9)
    assert m.rmse == pytest.approx(0.0, abs=1e-9)


def test_mape_is_computed_correctly(cfg):
    a = series([100.0, 110.0])
    b = series([100.0, 100.0], col="reference_value")
    # After rebasing both to their own first value: APIx 100,110; ref 100,100.
    _, m = bt.compare(a, b, cfg)
    assert m.mape_pct == pytest.approx(5.0)   # (0% + 10%) / 2


def test_anticorrelated_series_are_detected(cfg):
    a = series([100, 105, 110, 115])
    b = series([100, 95, 90, 85], col="reference_value")
    _, m = bt.compare(a, b, cfg)
    assert m.correlation < -0.99


def test_metrics_report_the_window(cfg):
    a = series([100, 101, 102], start="2026-08-01")
    b = series([100, 101, 102], start="2026-08-01", col="reference_value")
    _, m = bt.compare(a, b, cfg)
    assert (m.n_days, m.start_date, m.end_date) == (3, "2026-08-01", "2026-08-03")


def test_only_common_dates_are_compared(cfg):
    a = series([100, 101, 102, 103], start="2026-08-01")
    b = series([100, 101, 102], start="2026-08-02", col="reference_value")
    merged, m = bt.compare(a, b, cfg)
    assert m.n_days == 3
    assert m.start_date == "2026-08-02"


def test_disjoint_series_are_rejected(cfg):
    a = series([100, 101], start="2026-08-01")
    b = series([100, 101], start="2026-12-01", col="reference_value")
    with pytest.raises(ValueError, match="share no dates"):
        bt.compare(a, b, cfg)


def test_rank_correlation_is_robust_to_a_single_spike(cfg):
    a = series([100, 101, 102, 103, 104, 105])
    b = series([100, 101, 102, 103, 104, 500], col="reference_value")
    _, m = bt.compare(a, b, cfg)
    assert m.rank_correlation == pytest.approx(1.0), "ordering is unchanged"
    assert m.correlation < m.rank_correlation


def test_metrics_serialise_to_json(cfg):
    a = series([100, 102, 105])
    b = series([100, 103, 104], col="reference_value")
    _, m = bt.compare(a, b, cfg)
    assert json.loads(json.dumps(m.to_dict()))["n_days"] == 3


# --- end to end -------------------------------------------------------------


def _panel_db(path: Path) -> Path:
    """A DB with a full 35-day cleaned panel, ready to backtest."""
    from config_loader import load_airlines, load_routes
    from etl import clean as C

    db_path = db.init_db(path)
    panel = synthetic_panel(
        n_days=35,
        routes=tuple(r.key for r in load_routes()),
        airlines=tuple(a.code for a in load_airlines()),
        fare_fn=lambda qd, td, apd, route, airline: 5000.0 * (1.0 + 0.004 * apd),
    )
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k != "bucket"}
            for r in panel.to_dict("records")
        ])
    C.run_clean(db_path)
    return db_path


def test_run_backtest_persists_series_and_metrics(tmp_path, cfg):
    db_path = _panel_db(tmp_path / "bt.db")
    merged, m = bt.run_backtest(db_path, cfg, write_chart=False,
                                results_json=Path(db_path).parent / 'results.json')

    assert m.n_days == 35
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM apix_backtest").fetchone()[0] == 35
        assert conn.execute("SELECT COUNT(*) FROM apix_backtest_metrics").fetchone()[0] > 0

    stored = bt.latest_backtest(db_path)
    assert len(stored["series"]) == 35
    assert float(stored["metrics"]["n_days"]) == 35


def test_backtest_requires_the_minimum_window(tmp_path, cfg):
    """TASKS.md asks for 30+ days; a short run must fail loudly, not quietly."""
    from config_loader import load_airlines, load_routes
    from etl import clean as C

    db_path = db.init_db(tmp_path / "short.db")
    panel = synthetic_panel(
        n_days=5,
        routes=tuple(r.key for r in load_routes()),
        airlines=tuple(a.code for a in load_airlines()),
    )
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k != "bucket"}
            for r in panel.to_dict("records")
        ])
    C.run_clean(db_path)

    with pytest.raises(ValueError, match="at least 30 days"):
        bt.run_backtest(db_path, cfg, write_chart=False,
                    results_json=Path(db_path).parent / 'results.json')


def test_latest_backtest_returns_none_before_any_run(tmp_path):
    assert bt.latest_backtest(db.init_db(tmp_path / "empty.db")) is None


def test_rerunning_replaces_rather_than_appends(tmp_path, cfg):
    db_path = _panel_db(tmp_path / "bt2.db")
    bt.run_backtest(db_path, cfg, write_chart=False,
                    results_json=Path(db_path).parent / 'results.json')
    bt.run_backtest(db_path, cfg, write_chart=False,
                    results_json=Path(db_path).parent / 'results.json')
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM apix_backtest").fetchone()[0] == 35


# --- the sanity bar the final checklist sets --------------------------------


def test_real_backtest_numbers_are_sane():
    """TASKS.md final checklist: 'not suspiciously perfect, not garbage'.

    Runs against the actual project database if it has been built.
    """
    stored = bt.latest_backtest()
    if stored is None:
        pytest.skip("no backtest has been run against the project database yet")

    r = float(stored["metrics"]["correlation"])
    mape = float(stored["metrics"]["mape_pct"])

    assert 0.70 <= r <= 0.995, f"correlation {r} is either garbage or too perfect"
    assert 0.2 <= mape <= 15.0, f"MAPE {mape}% is either garbage or too perfect"
    assert int(float(stored["metrics"]["n_days"])) >= 30
