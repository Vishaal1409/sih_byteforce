"""Phase 4 tests: the cleaner drops what it should and keeps what it must.

The crafted edge cases the task asks for — extreme outlier, null fare, negative
fare, sold-out row — plus the one that actually bit during development: a
demand shock must NOT be mistaken for an outlier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from etl import clean as C  # noqa: E402


@pytest.fixture()
def cfg():
    return C.load_etl_config()


def row(**over):
    """One well-formed clean row; override the field under test.

    travel_date defaults to something derived from `id`, so two default rows do
    NOT share a natural key — otherwise dedupe would collapse them before the
    step under test ever ran. Tests that mean to create a duplicate pass
    travel_date explicitly.
    """
    rid = over.get("id", 1)
    base = dict(
        id=rid,
        source=db.SOURCE_SIMULATED,
        airline="6E",
        origin="DEL",
        dest="BOM",
        travel_date=f"2026-10-{(rid - 1) % 28 + 1:02d}",
        query_date="2026-09-01",
        advance_purchase_days=30,
        base_fare=4200.0,
        taxes=800.0,
        total_fare=5000.0,
        currency="INR",
        booking_class="ECONOMY",
        is_available=1,
        scraped_at="2026-09-01T06:30:00+00:00",
    )
    base.update(over)
    return base


def frame(rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def panel(n_query_dates=20, airlines=("6E", "AI", "QP", "SG"), apds=(20, 30, 40)):
    """A realistic block: several carriers quoting many departures.

    travel_date is derived as query_date + advance_purchase_days, matching how
    real observations work — lead time is not free to vary independently of the
    two dates. Big enough for the outlier machinery to engage (groups above
    min_group_size), so tests exercise the real path, not the exemption.
    """
    from datetime import date as _date, timedelta as _td

    rows, rid = [], 0
    for d in range(n_query_dates):
        query_date = _date(2026, 8, 1) + _td(days=d)
        for apd in apds:
            travel_date = query_date + _td(days=apd)
            for i, a in enumerate(airlines):
                rid += 1
                rows.append(row(
                    id=rid,
                    airline=a,
                    travel_date=travel_date.isoformat(),
                    query_date=query_date.isoformat(),
                    advance_purchase_days=apd,
                    base_fare=4000.0 + 100 * i + (rid % 7) * 15.0,
                    taxes=800.0,
                    total_fare=4800.0 + 100 * i + (rid % 7) * 15.0,
                ))
    return frame(rows)


def a_travel_date(df: pd.DataFrame) -> str:
    """A travel_date that several carriers actually quote in `df`."""
    counts = df["travel_date"].value_counts()
    return str(counts.index[0])


# --- the crafted edge cases the task calls for ------------------------------


def test_null_fare_is_dropped(cfg):
    df = frame([row(id=1), row(id=2, total_fare=None, base_fare=None, taxes=None)])
    out, rep = C.clean_dataframe(df, cfg)
    assert list(out["id"]) == [1]
    assert rep.dropped["null total_fare"] == 1


def test_sold_out_row_is_dropped_but_counted(cfg):
    df = frame([
        row(id=1),
        row(id=2, is_available=0, base_fare=None, taxes=None, total_fare=None),
    ])
    out, rep = C.clean_dataframe(df, cfg)
    assert list(out["id"]) == [1]
    assert rep.dropped["sold out / no fare offered"] == 1
    assert any("sold-out" in n for n in rep.notes)


def test_negative_fare_is_dropped(cfg):
    df = frame([row(id=1), row(id=2, total_fare=-500.0)])
    out, rep = C.clean_dataframe(df, cfg)
    assert list(out["id"]) == [1]
    assert any("total_fare <" in k for k in rep.dropped)


def test_absurdly_large_fare_is_dropped(cfg):
    df = frame([row(id=1), row(id=2, total_fare=9_999_999.0)])
    out, rep = C.clean_dataframe(df, cfg)
    assert list(out["id"]) == [1]
    assert any("total_fare >" in k for k in rep.dropped)


def test_extreme_outlier_is_dropped(cfg):
    """A single carrier ten times its peers on the same departure."""
    df = panel()
    victim = df.index[0]
    df.loc[victim, "total_fare"] = df.loc[victim, "total_fare"] * 10
    bad_id = df.loc[victim, "id"]

    out, rep = C.clean_dataframe(df, cfg)
    assert bad_id not in set(out["id"])
    assert any("MAD outlier" in k for k in rep.dropped)


def test_duplicate_is_collapsed_keeping_latest(cfg):
    df = frame([
        row(id=1, travel_date="2026-10-01", total_fare=5000.0,
            scraped_at="2026-09-01T06:00:00+00:00"),
        row(id=2, travel_date="2026-10-01", total_fare=5500.0,
            scraped_at="2026-09-01T18:00:00+00:00"),
    ])
    out, rep = C.clean_dataframe(df, cfg)
    assert len(out) == 1
    assert out.iloc[0]["total_fare"] == 5500.0
    assert rep.dropped["duplicate observations"] == 1


def test_rows_differing_only_by_source_are_not_duplicates(cfg):
    """source is part of the natural key — a fallback row is its own observation."""
    df = frame([
        row(id=1, travel_date="2026-10-01", source=db.SOURCE_SIMULATED),
        row(id=2, travel_date="2026-10-01", source=db.SOURCE_FALLBACK),
    ])
    out, _ = C.clean_dataframe(df, cfg)
    assert len(out) == 2


# --- the bug that actually bit ----------------------------------------------


def test_demand_shock_is_not_treated_as_an_outlier(cfg):
    """The whole point of the peer-ratio normalisation.

    A market-wide 2.5x spike on one travel date must survive cleaning. Testing
    raw fares dropped 1,007 rows on the real panel, every one of them a
    festival row — an index that strips demand shocks is useless.
    """
    df = panel()
    shock = df["travel_date"] == a_travel_date(df)
    df.loc[shock, "total_fare"] = df.loc[shock, "total_fare"] * 2.5
    shocked_ids = set(df.loc[shock, "id"])

    out, rep = C.clean_dataframe(df, cfg)
    survived = shocked_ids & set(out["id"])
    assert survived == shocked_ids, (
        f"{len(shocked_ids - survived)} shock rows were wrongly stripped"
    )


def test_error_on_a_shock_date_is_still_caught(cfg):
    """Shocks are preserved, but a genuine error inside one is not excused."""
    df = panel()
    shock = df["travel_date"] == a_travel_date(df)
    df.loc[shock, "total_fare"] = df.loc[shock, "total_fare"] * 2.5

    victim = df[shock].index[0]
    df.loc[victim, "total_fare"] = df.loc[victim, "total_fare"] * 6
    bad_id = df.loc[victim, "id"]

    out, _ = C.clean_dataframe(df, cfg)
    assert bad_id not in set(out["id"])


def test_peer_ratio_is_near_one_when_all_carriers_move_together(cfg):
    df = panel()
    ratios = C.peer_ratio(df, cfg)
    assert ratios.between(0.9, 1.1).all()


# --- component validation ---------------------------------------------------


def test_component_mismatch_is_flagged_not_dropped(cfg):
    """Phase 1 chose not to enforce base+taxes==total so Phase 4 could flag it."""
    df = frame([row(id=1), row(id=2, base_fare=4200.0, taxes=800.0, total_fare=9999.0)])
    out, rep = C.clean_dataframe(df, cfg)

    assert set(out["id"]) == {1, 2}, "mismatched rows should survive"
    assert rep.flagged["base_fare + taxes != total_fare"] == 1
    assert out.set_index("id").loc[2, "fare_mismatch"] == 1
    assert out.set_index("id").loc[1, "fare_mismatch"] == 0


def test_mismatch_can_be_configured_to_drop(cfg):
    cfg = {**cfg, "fare_validation": {**cfg["fare_validation"],
                                      "drop_on_component_mismatch": True}}
    df = frame([row(id=1), row(id=2, total_fare=9999.0)])
    out, _ = C.clean_dataframe(df, cfg)
    assert list(out["id"]) == [1]


def test_missing_components_are_not_flagged(cfg):
    """A live scrape often gives only a total — that is not a mismatch."""
    df = frame([row(id=1, base_fare=None, taxes=None, total_fare=5000.0)])
    out, rep = C.clean_dataframe(df, cfg)
    assert out.iloc[0]["fare_mismatch"] == 0
    assert "base_fare + taxes != total_fare" not in rep.flagged


# --- MAD mechanics ----------------------------------------------------------


def test_modified_z_score_handles_zero_spread():
    """Genuinely identical values must give zeros, not a divide-by-zero."""
    z = C.modified_z_scores(pd.Series([5000.0] * 10))
    assert (z == 0).all()


def test_outlier_in_an_otherwise_constant_group_is_still_caught():
    """MAD is 0 when most values are identical — the meanAD fallback.

    Without it the score is all zeros and a lone 10x mis-parse sitting in a
    constant group goes undetected, which is exactly the case this step exists
    to catch.
    """
    z = C.modified_z_scores(pd.Series([5000.0] * 59 + [50_000.0]))
    assert z.iloc[-1] > 8.0
    assert (z.iloc[:-1] < 1.0).all()


def test_small_groups_are_exempt_from_outlier_testing(cfg):
    """MAD on a handful of points would drop legitimate data."""
    df = panel(n_query_dates=1)
    df.loc[df.index[0], "total_fare"] = 90_000.0
    out, rep = C.clean_dataframe(df, cfg)
    assert not any("MAD outlier" in k for k in rep.dropped)
    assert any("smaller than" in n for n in rep.notes)


def test_unsupported_outlier_method_is_rejected(cfg):
    cfg = {**cfg, "outliers": {**cfg["outliers"], "method": "zscore"}}
    with pytest.raises(ValueError, match="unsupported outlier method"):
        C.clean_dataframe(panel(), cfg)


# --- report -----------------------------------------------------------------


def test_report_accounts_for_every_row(cfg):
    """rows_in must equal rows_out plus everything dropped — no silent losses."""
    df = frame([
        row(id=1),
        row(id=2, total_fare=None),
        row(id=3, is_available=0, total_fare=None),
        row(id=4, total_fare=-1.0, airline="AI"),
    ])
    _, rep = C.clean_dataframe(df, cfg)
    assert rep.rows_in == rep.rows_out + rep.total_dropped


def test_report_renders_and_serialises(cfg):
    _, rep = C.clean_dataframe(panel(), cfg)
    assert "DATA QUALITY SUMMARY" in rep.render()
    d = rep.to_dict()
    assert d["rows_in"] and "retention_rate" in d


def test_empty_input_does_not_explode(cfg):
    out, rep = C.clean_dataframe(pd.DataFrame(columns=list(C.CLEAN_COLUMNS)), cfg)
    assert out.empty and rep.rows_in == 0 and rep.rows_out == 0


# --- persistence ------------------------------------------------------------


def test_writes_fares_clean_and_quality_log(tmp_path, cfg):
    db_path = db.init_db(tmp_path / "etl.db")
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k != "id"}
            for r in panel(n_query_dates=12).to_dict("records")
        ])

    report = C.run_clean(db_path)
    assert report.rows_out > 0

    with db.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM fares_clean").fetchone()[0]
        assert n == report.rows_out
        assert conn.execute("SELECT COUNT(*) FROM fares_clean WHERE is_available=0").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM data_quality_log").fetchone()[0] == 1

    latest = C.latest_quality_report(db_path)
    assert latest["rows_out"] == report.rows_out
    assert "run_at" in latest


def test_rerunning_replaces_rather_than_appends(tmp_path, cfg):
    db_path = db.init_db(tmp_path / "etl2.db")
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k != "id"}
            for r in panel(n_query_dates=12).to_dict("records")
        ])

    first = C.run_clean(db_path)
    C.run_clean(db_path)
    with db.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fares_clean").fetchone()[0] == first.rows_out
        # but the audit trail keeps both runs
        assert conn.execute("SELECT COUNT(*) FROM data_quality_log").fetchone()[0] == 2


def test_dry_run_writes_nothing(tmp_path):
    db_path = db.init_db(tmp_path / "etl3.db")
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k != "id"}
            for r in panel(n_query_dates=12).to_dict("records")
        ])
    report = C.run_clean(db_path, dry_run=True)
    assert any("DRY RUN" in n for n in report.notes)
    with db.connect(db_path) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='fares_clean'"
        ).fetchone() is None


def test_query_fares_works_against_the_clean_table(tmp_path):
    """db.query_fares(table='fares_clean') — the Phase 1 whitelist paying off."""
    db_path = db.init_db(tmp_path / "etl4.db")
    with db.connect(db_path) as conn:
        db.insert_quotes(conn, [
            {k: v for k, v in r.items() if k != "id"}
            for r in panel(n_query_dates=12).to_dict("records")
        ])
    C.run_clean(db_path)
    with db.connect(db_path) as conn:
        rows = db.query_fares(conn, origin="DEL", dest="BOM", table="fares_clean")
    assert rows and all(r["is_available"] == 1 for r in rows)
