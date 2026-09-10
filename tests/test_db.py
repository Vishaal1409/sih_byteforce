"""Phase 1 tests: the shared schema and the db.py layer hold up.

These are the guardrails for every later phase — the simulator, scraper, ETL and
index code all write through `db.insert_quote(s)` and read through
`db.query_fares`, so a break here breaks everything downstream.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """A freshly migrated DB per test, in pytest's tmp dir (never data/apix.db)."""
    db_path = db.init_db(tmp_path / "test.db")
    with db.connect(db_path) as c:
        yield c


def make_quote(**overrides):
    """A valid baseline quote; override just the field under test."""
    base = dict(
        source=db.SOURCE_SIMULATED,
        airline="6E",
        origin="DEL",
        dest="BOM",
        travel_date="2026-10-01",
        query_date="2026-09-01",
        base_fare=4200.0,
        taxes=800.0,
        booking_class="ECONOMY",
        is_available=True,
        scraped_at="2026-09-01T06:30:00+00:00",
    )
    base.update(overrides)
    return db.FareQuote(**base)


# --- the task's explicit ask: insert a dummy row, read it back --------------


def test_insert_one_row_and_read_it_back(conn):
    db.insert_quote(conn, make_quote())

    rows = db.query_fares(conn, origin="DEL", dest="BOM")
    assert len(rows) == 1
    row = rows[0]

    assert row["source"] == "simulated"
    assert row["airline"] == "6E"
    assert row["origin"] == "DEL"
    assert row["dest"] == "BOM"
    assert row["travel_date"] == "2026-10-01"
    assert row["query_date"] == "2026-09-01"
    assert row["base_fare"] == 4200.0
    assert row["taxes"] == 800.0
    assert row["currency"] == "INR"
    assert row["booking_class"] == "ECONOMY"
    assert row["is_available"] == 1
    assert row["scraped_at"] == "2026-09-01T06:30:00+00:00"


def test_schema_has_every_canonical_column(conn):
    """The schema matches the canonical field list in TASKS.md Phase 1."""
    expected = {
        "source", "airline", "origin", "dest", "travel_date", "query_date",
        "advance_purchase_days", "base_fare", "taxes", "total_fare",
        "currency", "booking_class", "is_available", "scraped_at",
    }
    assert expected <= set(db.table_columns(conn, "fare_quotes"))


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    db.init_db(path)
    db.init_db(path)  # must not raise
    with db.connect(path) as c:
        versions = c.execute("SELECT version FROM schema_version").fetchall()
    assert [v["version"] for v in versions] == [db.SCHEMA_VERSION]


# --- derived fields ---------------------------------------------------------


def test_advance_purchase_days_is_derived(conn):
    q = make_quote(travel_date="2026-10-01", query_date="2026-09-01")
    assert q.advance_purchase_days == 30


def test_total_fare_is_derived_from_components(conn):
    assert make_quote(base_fare=4200.0, taxes=800.0).total_fare == 5000.0


def test_explicit_total_fare_is_not_overwritten(conn):
    """A base+taxes/total mismatch survives to the DB — Phase 4's ETL is
    specified to flag it, which it cannot do if we silently 'fix' it here."""
    q = make_quote(base_fare=4200.0, taxes=800.0, total_fare=9999.0)
    assert q.total_fare == 9999.0
    db.insert_quote(conn, q)
    assert db.query_fares(conn)[0]["total_fare"] == 9999.0


def test_codes_are_normalised(conn):
    q = make_quote(airline=" 6e ", origin="del", dest=" bom")
    assert (q.airline, q.origin, q.dest) == ("6E", "DEL", "BOM")


def test_accepts_date_objects(conn):
    q = make_quote(travel_date=date(2026, 10, 1), query_date=date(2026, 9, 1))
    assert q.travel_date == "2026-10-01"
    assert q.advance_purchase_days == 30


def test_dicts_are_accepted_as_well_as_dataclasses(conn):
    db.insert_quote(conn, {
        "source": "live",
        "airline": "AI",
        "origin": "DEL",
        "dest": "BLR",
        "travel_date": "2026-10-05",
        "query_date": "2026-09-01",
        "base_fare": 5000.0,
        "taxes": 900.0,
    })
    assert db.query_fares(conn, origin="DEL", dest="BLR")[0]["total_fare"] == 5900.0


# --- validation -------------------------------------------------------------


def test_rejects_unknown_source():
    with pytest.raises(ValueError, match="source must be one of"):
        make_quote(source="made_up")


def test_rejects_travel_date_before_query_date():
    with pytest.raises(ValueError, match="before query_date"):
        make_quote(travel_date="2026-09-01", query_date="2026-10-01")


def test_rejects_malformed_date():
    with pytest.raises(ValueError):
        make_quote(travel_date="01-10-2026")


def test_db_rejects_negative_fare(conn):
    """The CHECK constraint is real, not decorative."""
    bad = make_quote()
    bad.total_fare = -1.0
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_quote(conn, bad)


def test_sold_out_row_with_null_fares_is_allowed(conn):
    db.insert_quote(conn, make_quote(
        base_fare=None, taxes=None, total_fare=None, is_available=False,
    ))
    row = db.query_fares(conn)[0]
    assert row["is_available"] == 0
    assert row["total_fare"] is None


# --- bulk insert and idempotency -------------------------------------------


def test_bulk_insert(conn):
    quotes = [
        make_quote(travel_date=f"2026-10-{d:02d}", airline=a)
        for d in range(1, 11)
        for a in ("6E", "AI", "QP", "SG")
    ]
    assert db.insert_quotes(conn, quotes) == 40
    assert db.count_quotes(conn) == 40


def test_reinsert_updates_rather_than_duplicates(conn):
    """Re-running the simulator must not double the dataset."""
    db.insert_quote(conn, make_quote(base_fare=4200.0, taxes=800.0))
    db.insert_quote(conn, make_quote(base_fare=6000.0, taxes=900.0))

    assert db.count_quotes(conn) == 1
    assert db.query_fares(conn)[0]["total_fare"] == 6900.0


def test_booking_class_distinguishes_rows(conn):
    db.insert_quote(conn, make_quote(booking_class="ECONOMY"))
    db.insert_quote(conn, make_quote(booking_class="BUSINESS"))
    assert db.count_quotes(conn) == 2


# --- queries ----------------------------------------------------------------


@pytest.fixture()
def populated(conn):
    db.insert_quotes(conn, [
        make_quote(origin="DEL", dest="BOM", travel_date="2026-10-01", query_date="2026-09-01"),
        make_quote(origin="DEL", dest="BOM", travel_date="2026-10-15", query_date="2026-09-10"),
        make_quote(origin="BLR", dest="MAA", travel_date="2026-10-01", query_date="2026-09-01"),
        make_quote(origin="DEL", dest="BOM", travel_date="2026-11-01", query_date="2026-09-01",
                   airline="AI", source=db.SOURCE_LIVE),
        make_quote(origin="DEL", dest="BOM", travel_date="2026-10-20", query_date="2026-09-05",
                   is_available=False, base_fare=None, taxes=None, total_fare=None),
    ])
    return conn


def test_filter_by_route(populated):
    assert len(db.query_fares(populated, origin="DEL", dest="BOM")) == 4
    assert len(db.query_fares(populated, origin="BLR", dest="MAA")) == 1


def test_filter_by_travel_date_range(populated):
    rows = db.query_fares(
        populated, start_date="2026-10-01", end_date="2026-10-15",
        date_field="travel_date",
    )
    assert {r["travel_date"] for r in rows} == {"2026-10-01", "2026-10-15"}


def test_filter_by_query_date_range(populated):
    rows = db.query_fares(
        populated, start_date="2026-09-05", end_date="2026-09-30",
        date_field="query_date",
    )
    assert {r["query_date"] for r in rows} == {"2026-09-05", "2026-09-10"}


def test_filter_by_source_and_airline(populated):
    assert len(db.query_fares(populated, source=db.SOURCE_LIVE)) == 1
    assert len(db.query_fares(populated, source=db.SYNTHETIC_SOURCES)) == 4
    assert len(db.query_fares(populated, airline="AI")) == 1


def test_available_only(populated):
    assert len(db.query_fares(populated, available_only=True)) == 4


def test_limit_and_ordering(populated):
    rows = db.query_fares(populated, limit=3)
    assert len(rows) == 3
    assert [r["query_date"] for r in rows] == sorted(r["query_date"] for r in rows)


def test_rejects_bad_date_field_and_table(populated):
    with pytest.raises(ValueError):
        db.query_fares(populated, date_field="total_fare")
    with pytest.raises(ValueError):
        db.query_fares(populated, table="sqlite_master")


# --- summary helpers the dashboard/API will use -----------------------------


def test_counts_by_source_and_date_range(populated):
    assert db.counts_by_source(populated) == {"live": 1, "simulated": 4}
    assert db.date_range(populated) == ("2026-09-01", "2026-09-10")


def test_dataframe_helper_keeps_columns_when_empty(populated):
    empty = db.query_fares_df(populated, origin="XXX", dest="YYY")
    assert len(empty) == 0
    assert "total_fare" in empty.columns

    full = db.query_fares_df(populated, origin="DEL", dest="BOM")
    assert len(full) == 4


# --- transaction behaviour --------------------------------------------------


def test_failed_transaction_rolls_back(tmp_path):
    path = db.init_db(tmp_path / "rollback.db")
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(path) as c:
            db.insert_quote(c, make_quote())
            bad = make_quote(airline="AI")
            bad.taxes = -5.0
            db.insert_quote(c, bad)

    with db.connect(path) as c:
        assert db.count_quotes(c) == 0
