"""db.py — the single database access layer for APIx (SIH26056).

Everything downstream (simulator, live scraper, ETL, index math, API, dashboard)
goes through this module rather than opening SQLite directly. One place that
knows the schema, one place to change if it moves.

Canonical fare-quote schema lives in `data/schema.sql`; `FareQuote` below is the
Python mirror of it.

Typical use:

    import db

    db.init_db()                                  # create/upgrade data/apix.db

    with db.connect() as conn:
        db.insert_quote(conn, quote)              # one row
        db.insert_quotes(conn, many_quotes)       # bulk, chunked
        rows = db.query_fares(conn, origin="DEL", dest="BOM")

CLI:
    python db.py init      # apply the schema
    python db.py info      # row counts by source
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

# --- Paths -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "apix.db"
SCHEMA_PATH = REPO_ROOT / "data" / "schema.sql"

SCHEMA_VERSION = 1

# --- Vocabulary ------------------------------------------------------------

#: Provenance values. Kept in sync with the CHECK constraint in schema.sql.
SOURCE_SIMULATED = "simulated"
SOURCE_LIVE = "live"
SOURCE_FALLBACK = "fallback_simulated"
SOURCES = (SOURCE_SIMULATED, SOURCE_LIVE, SOURCE_FALLBACK)

#: Sources that are NOT real observations. The dashboard and API use this to
#: decide when to show the "simulated data" warning (CLAUDE.md ground rule 5).
SYNTHETIC_SOURCES = (SOURCE_SIMULATED, SOURCE_FALLBACK)

#: Column order used for all inserts.
QUOTE_COLUMNS = (
    "source",
    "airline",
    "origin",
    "dest",
    "travel_date",
    "query_date",
    "advance_purchase_days",
    "base_fare",
    "taxes",
    "total_fare",
    "currency",
    "booking_class",
    "is_available",
    "scraped_at",
)


def utc_now_iso() -> str:
    """Timestamp for `scraped_at`, ISO 8601, second precision, UTC."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_iso_date(value: str | date | datetime) -> str:
    """Normalise a date to ISO 'YYYY-MM-DD'.

    Accepts str/date/datetime so callers do not each reinvent this. We store
    dates as TEXT: Python 3.12+ deprecated the implicit sqlite3 date adapters,
    and explicit strings keep the DB readable from the sqlite3 CLI.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    # Validate rather than trust — a malformed date here poisons the index math.
    date.fromisoformat(text)
    return text


# --- The canonical record --------------------------------------------------


@dataclass(slots=True)
class FareQuote:
    """One fare observation. The Python mirror of the `fare_quotes` table.

    Both the simulator and the live scraper emit these, so ETL and index code
    never branch on where a row came from (beyond reading `source`).
    """

    source: str
    airline: str
    origin: str
    dest: str
    travel_date: str
    query_date: str
    advance_purchase_days: int | None = None
    base_fare: float | None = None
    taxes: float | None = None
    total_fare: float | None = None
    currency: str = "INR"
    booking_class: str = "ECONOMY"
    is_available: bool | int = True
    scraped_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, got {self.source!r}")

        self.airline = self.airline.strip().upper()
        self.origin = self.origin.strip().upper()
        self.dest = self.dest.strip().upper()
        self.travel_date = _as_iso_date(self.travel_date)
        self.query_date = _as_iso_date(self.query_date)

        # Derive the booking window if the caller did not supply it. This is the
        # definition the whole index rests on, so it lives in exactly one place.
        if self.advance_purchase_days is None:
            self.advance_purchase_days = (
                date.fromisoformat(self.travel_date)
                - date.fromisoformat(self.query_date)
            ).days
        if self.advance_purchase_days < 0:
            raise ValueError(
                f"travel_date {self.travel_date} is before query_date "
                f"{self.query_date} (advance_purchase_days="
                f"{self.advance_purchase_days})"
            )

        # Fill total from its components when only the parts are known. We do
        # NOT correct a mismatch — Phase 4 flags that as a data-quality signal.
        if self.total_fare is None and None not in (self.base_fare, self.taxes):
            self.total_fare = round(self.base_fare + self.taxes, 2)

        self.is_available = int(bool(self.is_available))

    def as_row(self) -> tuple[Any, ...]:
        """Values in QUOTE_COLUMNS order, ready for a parameterised insert."""
        d = asdict(self)
        return tuple(d[c] for c in QUOTE_COLUMNS)


def _coerce(quote: FareQuote | dict[str, Any]) -> FareQuote:
    return quote if isinstance(quote, FareQuote) else FareQuote(**quote)


# --- Connections -----------------------------------------------------------


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a connection with the project's standard settings.

    Commits on clean exit, rolls back on exception, always closes. Rows come
    back as `sqlite3.Row`, so callers can use `row["total_fare"]`.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(
    db_path: str | Path = DEFAULT_DB_PATH,
    schema_path: str | Path = SCHEMA_PATH,
) -> Path:
    """Apply `data/schema.sql`. Idempotent — safe to call on every startup."""
    db_path, schema_path = Path(db_path), Path(schema_path)
    if not schema_path.exists():
        raise FileNotFoundError(f"schema not found: {schema_path}")

    sql = schema_path.read_text(encoding="utf-8")
    with connect(db_path) as conn:
        conn.executescript(sql)
        already = conn.execute(
            "SELECT 1 FROM schema_version WHERE version = ?", (SCHEMA_VERSION,)
        ).fetchone()
        if not already:
            conn.execute(
                "INSERT INTO schema_version (version, applied_at, note) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, utc_now_iso(), "initial fare_quotes schema"),
            )
    return db_path


# --- Writes ----------------------------------------------------------------

_INSERT_SQL = f"""
INSERT INTO fare_quotes ({", ".join(QUOTE_COLUMNS)})
VALUES ({", ".join("?" * len(QUOTE_COLUMNS))})
ON CONFLICT (source, airline, origin, dest, travel_date, query_date, booking_class)
DO UPDATE SET
    advance_purchase_days = excluded.advance_purchase_days,
    base_fare             = excluded.base_fare,
    taxes                 = excluded.taxes,
    total_fare            = excluded.total_fare,
    currency              = excluded.currency,
    is_available          = excluded.is_available,
    scraped_at            = excluded.scraped_at
"""


def insert_quote(conn: sqlite3.Connection, quote: FareQuote | dict[str, Any]) -> int:
    """Insert (or refresh) a single quote. Returns rows affected."""
    return conn.execute(_INSERT_SQL, _coerce(quote).as_row()).rowcount


def insert_quotes(
    conn: sqlite3.Connection,
    quotes: Iterable[FareQuote | dict[str, Any]],
    chunk_size: int = 5_000,
) -> int:
    """Bulk insert. Re-observing the same quote updates it rather than
    duplicating, so re-running the simulator is idempotent.

    Returns the number of quotes submitted.
    """
    total = 0
    batch: list[tuple[Any, ...]] = []
    for quote in quotes:
        batch.append(_coerce(quote).as_row())
        if len(batch) >= chunk_size:
            conn.executemany(_INSERT_SQL, batch)
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(_INSERT_SQL, batch)
        total += len(batch)
    return total


# --- Reads -----------------------------------------------------------------

_DATE_FIELDS = ("travel_date", "query_date", "scraped_at")
_TABLES = ("fare_quotes", "fares_clean")


def query_fares(
    conn: sqlite3.Connection,
    origin: str | None = None,
    dest: str | None = None,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    date_field: str = "travel_date",
    source: str | Sequence[str] | None = None,
    airline: str | None = None,
    available_only: bool = False,
    limit: int | None = None,
    table: str = "fare_quotes",
) -> list[sqlite3.Row]:
    """Query quotes by route and date range. All filters optional.

    `date_field` selects which date the range applies to — 'travel_date' (when
    the flight departs) or 'query_date' (when the fare was observed). The index
    series is built along query_date; the booking-window curves along
    travel_date. Both are needed, hence the switch.

    `table` lets Phase 4+ point these same helpers at the cleaned table
    (`fares_clean`) without duplicating this function.
    """
    if date_field not in _DATE_FIELDS:
        raise ValueError(f"unsupported date_field: {date_field!r}")
    if table not in _TABLES:
        raise ValueError(f"unsupported table: {table!r}")

    where: list[str] = []
    params: list[Any] = []

    if origin:
        where.append("origin = ?")
        params.append(origin.strip().upper())
    if dest:
        where.append("dest = ?")
        params.append(dest.strip().upper())
    if start_date is not None:
        where.append(f"{date_field} >= ?")
        params.append(_as_iso_date(start_date))
    if end_date is not None:
        where.append(f"{date_field} <= ?")
        params.append(_as_iso_date(end_date))
    if source is not None:
        sources = (source,) if isinstance(source, str) else tuple(source)
        placeholders = ", ".join("?" * len(sources))
        where.append(f"source IN ({placeholders})")
        params.extend(sources)
    if airline:
        where.append("airline = ?")
        params.append(airline.strip().upper())
    if available_only:
        where.append("is_available = 1")

    # table and date_field are whitelisted above; every value is parameterised.
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY query_date, travel_date, origin, dest, airline"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    return conn.execute(sql, params).fetchall()


def query_fares_df(conn: sqlite3.Connection, **kwargs: Any):
    """`query_fares` as a pandas DataFrame, for the ETL / index / dashboard.

    pandas is imported lazily so the scraper does not pay for it. The column
    list comes from the table itself, so an empty result still has the right
    shape and downstream `df["total_fare"]` does not blow up.
    """
    import pandas as pd

    rows = query_fares(conn, **kwargs)
    columns = table_columns(conn, kwargs.get("table", "fare_quotes"))
    return pd.DataFrame([dict(r) for r in rows], columns=columns)


def table_columns(conn: sqlite3.Connection, table: str = "fare_quotes") -> list[str]:
    """Column names of `table`, in declaration order."""
    if table not in _TABLES:
        raise ValueError(f"unsupported table: {table!r}")
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def count_quotes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM fare_quotes").fetchone()[0]


def counts_by_source(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per provenance — the number the dashboard banner reports."""
    return {
        r["source"]: r["n"]
        for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM fare_quotes GROUP BY source ORDER BY source"
        )
    }


def date_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """(earliest, latest) query_date present, or (None, None) if empty."""
    row = conn.execute(
        "SELECT MIN(query_date) AS lo, MAX(query_date) AS hi FROM fare_quotes"
    ).fetchone()
    return (row["lo"], row["hi"])


# --- CLI -------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "init"

    if cmd == "init":
        path = init_db()
        with connect(path) as conn:
            tables = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
        print(f"schema v{SCHEMA_VERSION} applied to {path}")
        print(f"tables: {', '.join(tables)}")
        return 0

    if cmd == "info":
        if not DEFAULT_DB_PATH.exists():
            print(f"no database at {DEFAULT_DB_PATH} — run: python db.py init")
            return 1
        with connect() as conn:
            total = count_quotes(conn)
            by_source = counts_by_source(conn)
            lo, hi = date_range(conn)
        print(f"{DEFAULT_DB_PATH}")
        print(f"  fare_quotes: {total:,} rows")
        for src, n in by_source.items():
            marker = "   <- NOT REAL DATA" if src in SYNTHETIC_SOURCES else ""
            print(f"    {src:<20} {n:>9,}{marker}")
        print(f"  query_date range: {lo} .. {hi}")
        return 0

    print(f"unknown command {cmd!r}; expected 'init' or 'info'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
