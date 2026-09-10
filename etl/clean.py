"""etl/clean.py — turn raw fare_quotes into a trustworthy fares_clean table.

The index is only as defensible as the rows underneath it, so every row that
gets dropped is counted and the reason recorded. `python -m etl.clean` prints
that summary and persists it to the `data_quality_log` table, where the API and
dashboard can read it.

Pipeline, in order:

    1. load        raw fare_quotes
    2. dedupe      on (source, airline, origin, dest, travel_date, query_date),
                   keeping the most recently scraped row
    3. availability drop sold-out / no-fare rows (they carry no price)
    4. nulls       drop rows with no total_fare
    5. bounds      drop implausible fares (parse errors)
    6. components  FLAG rows where base_fare + taxes != total_fare
    7. outliers    drop MAD outliers within (route, airline, lead-time) groups
    8. write       fares_clean

Thresholds all live in config/etl.yaml.

CLI:
    python -m etl.clean            # clean and write fares_clean
    python -m etl.clean --dry-run  # report only, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from config_loader import CONFIG_DIR, _read_yaml  # noqa: E402

ETL_CONFIG_PATH = CONFIG_DIR / "etl.yaml"

#: 0.6745 is the 0.75 quantile of the standard normal — it rescales MAD so the
#: modified z-score is comparable to an ordinary z-score for normal data.
_MAD_SCALE = 0.6745


def load_etl_config() -> dict[str, Any]:
    return _read_yaml(ETL_CONFIG_PATH)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class CleaningReport:
    """Row counts at every stage, with a reason for each drop.

    Kept deliberately verbose: "how much data did you throw away and why" is
    the first question a statistician asks about an index.
    """

    rows_in: int = 0
    rows_out: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def drop(self, reason: str, n: int) -> None:
        if n:
            self.dropped[reason] = self.dropped.get(reason, 0) + int(n)

    def flag(self, reason: str, n: int) -> None:
        if n:
            self.flagged[reason] = self.flagged.get(reason, 0) + int(n)

    @property
    def total_dropped(self) -> int:
        return sum(self.dropped.values())

    @property
    def retention_rate(self) -> float:
        return self.rows_out / self.rows_in if self.rows_in else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "total_dropped": self.total_dropped,
            "retention_rate": round(self.retention_rate, 6),
            "dropped": self.dropped,
            "flagged": self.flagged,
            "notes": self.notes,
        }

    def render(self) -> str:
        lines = [
            "=" * 68,
            "DATA QUALITY SUMMARY",
            "=" * 68,
            f"  rows in                {self.rows_in:>9,}",
        ]
        for reason, n in sorted(self.dropped.items(), key=lambda kv: -kv[1]):
            lines.append(f"    - dropped: {reason:<28} {n:>9,}")
        lines.append(f"  rows out               {self.rows_out:>9,}"
                     f"   ({self.retention_rate:.1%} retained)")
        if self.flagged:
            lines.append("  flagged (kept, not dropped):")
            for reason, n in sorted(self.flagged.items(), key=lambda kv: -kv[1]):
                lines.append(f"    ! {reason:<30} {n:>9,}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        lines.append("=" * 68)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cleaning steps — each takes and returns a DataFrame, and records what it did
# ---------------------------------------------------------------------------


def dedupe(df: pd.DataFrame, cfg: dict[str, Any], report: CleaningReport) -> pd.DataFrame:
    """Collapse repeat observations of the same quote, keeping the newest."""
    key = list(cfg["dedupe"]["key"])
    before = len(df)
    out = (
        df.sort_values("scraped_at")
        .drop_duplicates(subset=key, keep="last")
        .reset_index(drop=True)
    )
    report.drop("duplicate observations", before - len(out))
    return out


def drop_unavailable(df: pd.DataFrame, cfg: dict[str, Any], report: CleaningReport) -> pd.DataFrame:
    """Remove sold-out rows — they have no price, so no place in a price index."""
    if not cfg["availability"].get("exclude_unavailable", True):
        return df
    mask = df["is_available"] == 1
    n = int((~mask).sum())
    report.drop("sold out / no fare offered", n)
    if n:
        report.notes.append(
            f"{n:,} sold-out rows excluded from fares_clean but retained in "
            f"fare_quotes ({n / max(len(df), 1):.1%} of input)"
        )
    return df[mask].reset_index(drop=True)


def drop_null_fares(df: pd.DataFrame, report: CleaningReport) -> pd.DataFrame:
    mask = df["total_fare"].notna()
    report.drop("null total_fare", int((~mask).sum()))
    return df[mask].reset_index(drop=True)


def drop_out_of_bounds(df: pd.DataFrame, cfg: dict[str, Any], report: CleaningReport) -> pd.DataFrame:
    """Remove fares outside the plausible domestic-economy band (parse errors)."""
    v = cfg["fare_validation"]
    lo, hi = float(v["min_total_fare_inr"]), float(v["max_total_fare_inr"])

    too_low = df["total_fare"] < lo
    too_high = df["total_fare"] > hi
    report.drop(f"total_fare < {lo:,.0f}", int(too_low.sum()))
    report.drop(f"total_fare > {hi:,.0f}", int(too_high.sum()))
    return df[~(too_low | too_high)].reset_index(drop=True)


def validate_components(
    df: pd.DataFrame, cfg: dict[str, Any], report: CleaningReport
) -> pd.DataFrame:
    """Check base_fare + taxes == total_fare, adding a `fare_mismatch` column.

    Flagged rather than dropped by default: real fares break this identity
    routinely, and a mismatch is a quality signal worth surfacing rather than a
    reason to discard an otherwise usable observation. The Phase 1 schema
    deliberately does not enforce the identity either, for the same reason.
    """
    v = cfg["fare_validation"]
    tol = float(v["components_tolerance_inr"])

    both_present = df["base_fare"].notna() & df["taxes"].notna()
    implied = df["base_fare"].fillna(0) + df["taxes"].fillna(0)
    mismatch = both_present & ((implied - df["total_fare"]).abs() > tol)

    df = df.copy()
    df["fare_mismatch"] = mismatch.astype(int)
    report.flag("base_fare + taxes != total_fare", int(mismatch.sum()))

    if v.get("drop_on_component_mismatch", False):
        report.drop("component mismatch", int(mismatch.sum()))
        return df[~mismatch].reset_index(drop=True)
    return df


def modified_z_scores(values: pd.Series) -> pd.Series:
    """Modified z-score: 0.6745 * |x - median| / MAD.

    Falls back to the mean absolute deviation when MAD is exactly zero
    (Iglewicz & Hoaglin's standard remedy). That case is not hypothetical: if
    most values in a group are identical, the median absolute deviation is 0
    and a naive implementation divides by zero — or, if it guards by returning
    zeros, goes blind precisely when one wild value is sitting in an otherwise
    constant group. A group of 59 identical fares plus one 10x mis-parse is
    exactly the situation the outlier step exists to catch.

    Only when there is genuinely no spread at all does this return zeros, which
    is then the right answer.
    """
    median = values.median()
    abs_dev = (values - median).abs()

    mad = abs_dev.median()
    if mad > 0 and not np.isnan(mad):
        return _MAD_SCALE * abs_dev / mad

    # 1.253314 = sqrt(pi/2), rescaling meanAD to be comparable to a std dev.
    mean_ad = abs_dev.mean()
    if mean_ad > 0 and not np.isnan(mean_ad):
        return abs_dev / (1.253314 * mean_ad)

    return pd.Series(np.zeros(len(values)), index=values.index)


def peer_ratio(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.Series:
    """Each fare divided by the median fare for the same departure.

    "Same departure" means the same route, travel_date and lead time — i.e.
    what the other carriers were charging for that exact flight-date at that
    exact moment in the booking window.

    This is what makes the outlier test survive demand shocks. A festival lifts
    every carrier at once, so every ratio stays near 1.0. A mis-parsed price
    moves one carrier away from its peers, so its ratio is extreme.
    """
    peer_cols = list(cfg["outliers"]["peer_group_by"])
    baseline = df.groupby(peer_cols, sort=False)["total_fare"].transform("median")
    # A zero/NaN baseline cannot happen after the bounds check, but guard anyway
    # so one odd group cannot poison the whole run.
    return (df["total_fare"] / baseline).replace([np.inf, -np.inf], np.nan).fillna(1.0)


def drop_outliers(df: pd.DataFrame, cfg: dict[str, Any], report: CleaningReport) -> pd.DataFrame:
    """Strip MAD outliers, measured on peer-normalised fares.

    Testing raw fares does not work: measured on the Phase 2 panel it dropped
    1,007 rows, every single one a festival/long-weekend row and not one an
    ordinary day. A market-wide spike is not an anomaly, and an index that
    strips demand shocks fails at its only job.

    So the test runs on `peer_ratio` — each fare relative to what other carriers
    charged for the same departure — within (route, airline) groups. Genuine
    volatility is shared and survives; rows inconsistent with their peers do not.
    """
    o = cfg["outliers"]
    if o.get("method", "mad") != "mad":
        raise ValueError(f"unsupported outlier method: {o['method']!r}")

    group_cols = list(o["group_by"])
    threshold = float(o["mad_threshold"])
    min_size = int(o["min_group_size"])

    if df.empty:
        return df

    df = df.copy()
    use_peers = o.get("peer_normalise", True)
    df["_ratio"] = peer_ratio(df, cfg)
    df["_tested"] = df["_ratio"] if use_peers else df["total_fare"]

    grouped = df.groupby(group_cols, sort=False)["_tested"]
    df["_mz"] = grouped.transform(modified_z_scores)
    df["_group_n"] = grouped.transform("size")

    # Small groups are exempt — MAD is not meaningful on a handful of points.
    is_outlier = (df["_mz"] > threshold) & (df["_group_n"] >= min_size)

    # ...and a row must ALSO disagree materially with its peers. Where a group's
    # fares are tightly clustered, MAD tends to zero and the modified z-score
    # explodes, so ordinary variation can score 30+. Requiring a real gap as
    # well means only genuinely anomalous rows are ever discarded.
    min_dev = float(o.get("min_peer_deviation", 0.0))
    if use_peers and min_dev > 0:
        is_outlier &= (df["_ratio"] - 1.0).abs() > min_dev

    n = int(is_outlier.sum())
    report.drop(f"MAD outlier (modified z > {threshold:g} on peer ratio)", n)

    small = int((df["_group_n"] < min_size).sum())
    if small:
        report.notes.append(
            f"{small:,} rows sat in groups smaller than {min_size} and were "
            f"exempt from outlier testing"
        )

    return (
        df[~is_outlier]
        .drop(columns=["_ratio", "_tested", "_mz", "_group_n"])
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def clean_dataframe(
    df: pd.DataFrame, cfg: dict[str, Any] | None = None
) -> tuple[pd.DataFrame, CleaningReport]:
    """Run the whole pipeline over a raw fare_quotes frame."""
    cfg = cfg or load_etl_config()
    report = CleaningReport(rows_in=len(df))

    if df.empty:
        report.rows_out = 0
        return df, report

    df = dedupe(df, cfg, report)
    df = drop_unavailable(df, cfg, report)
    df = drop_null_fares(df, report)
    df = drop_out_of_bounds(df, cfg, report)
    df = validate_components(df, cfg, report)
    df = drop_outliers(df, cfg, report)

    report.rows_out = len(df)
    return df, report


CLEAN_COLUMNS = (
    "source", "airline", "origin", "dest", "travel_date", "query_date",
    "advance_purchase_days", "base_fare", "taxes", "total_fare", "currency",
    "booking_class", "is_available", "scraped_at", "fare_mismatch",
)

_CREATE_CLEAN_TABLE = """
DROP TABLE IF EXISTS fares_clean;
CREATE TABLE fares_clean (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT    NOT NULL,
    airline                 TEXT    NOT NULL,
    origin                  TEXT    NOT NULL,
    dest                    TEXT    NOT NULL,
    travel_date             TEXT    NOT NULL,
    query_date              TEXT    NOT NULL,
    advance_purchase_days   INTEGER NOT NULL,
    base_fare               REAL,
    taxes                   REAL,
    total_fare              REAL    NOT NULL,
    currency                TEXT    NOT NULL,
    booking_class           TEXT    NOT NULL,
    is_available            INTEGER NOT NULL,
    scraped_at              TEXT    NOT NULL,
    fare_mismatch           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_fc_route      ON fares_clean (origin, dest, travel_date);
CREATE INDEX idx_fc_query_date ON fares_clean (query_date);
CREATE INDEX idx_fc_apd        ON fares_clean (advance_purchase_days);
CREATE INDEX idx_fc_source     ON fares_clean (source);
"""

_CREATE_QUALITY_LOG = """
CREATE TABLE IF NOT EXISTS data_quality_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    rows_in     INTEGER NOT NULL,
    rows_out    INTEGER NOT NULL,
    summary     TEXT NOT NULL      -- JSON: per-reason drop and flag counts
);
"""


def write_clean_table(
    df: pd.DataFrame, report: CleaningReport, db_path: str | Path = db.DEFAULT_DB_PATH
) -> None:
    """Materialise fares_clean and append the run to data_quality_log.

    A real table rather than a view: the index math scans it repeatedly, and
    the outlier step is not expressible in SQL without a lot of pain.
    """
    with db.connect(db_path) as conn:
        conn.executescript(_CREATE_CLEAN_TABLE)
        conn.executescript(_CREATE_QUALITY_LOG)

        if not df.empty:
            rows = [
                tuple(None if pd.isna(v) else v for v in row)
                for row in df[list(CLEAN_COLUMNS)].itertuples(index=False, name=None)
            ]
            conn.executemany(
                f"INSERT INTO fares_clean ({', '.join(CLEAN_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(CLEAN_COLUMNS))})",
                rows,
            )

        conn.execute(
            "INSERT INTO data_quality_log (run_at, rows_in, rows_out, summary) "
            "VALUES (?, ?, ?, ?)",
            (db.utc_now_iso(), report.rows_in, report.rows_out,
             json.dumps(report.to_dict())),
        )


def run_clean(
    db_path: str | Path = db.DEFAULT_DB_PATH, dry_run: bool = False
) -> CleaningReport:
    """Load fare_quotes, clean it, and (unless dry_run) write fares_clean."""
    with db.connect(db_path) as conn:
        raw = db.query_fares_df(conn)

    cleaned, report = clean_dataframe(raw)

    if not dry_run:
        write_clean_table(cleaned, report, db_path)
    else:
        report.notes.append("DRY RUN - fares_clean was not written")

    return report


def latest_quality_report(db_path: str | Path = db.DEFAULT_DB_PATH) -> dict[str, Any] | None:
    """Most recent data-quality summary, for the API and dashboard."""
    with db.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='data_quality_log'"
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(
            "SELECT run_at, rows_in, rows_out, summary FROM data_quality_log "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {"run_at": row["run_at"], **json.loads(row["summary"])}


# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean fare_quotes into fares_clean")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    report = run_clean(args.db, dry_run=args.dry_run)
    print(report.render())

    if not args.dry_run:
        with db.connect(args.db) as conn:
            n = conn.execute("SELECT COUNT(*) FROM fares_clean").fetchone()[0]
            by_source = {
                r["source"]: r["n"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) n FROM fares_clean GROUP BY source"
                )
            }
        print(f"  fares_clean now holds {n:,} rows  {by_source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
