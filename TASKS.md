# TASKS.md — APIx Prototype Build Plan (SIH26056)

Execute phases in order. Check off each task (`[x]`) only after running and verifying it.
Add a short note under any task where you made a judgment call. If a task is blocked,
mark it `[!]` and write why, then move to the next non-dependent task rather than stalling.

---

## Phase 0 — Scaffold

- [x] Initialize git repo, add `.gitignore` (Python, node if used, `/data/*.db`, `.venv`)
      - Note: repo initialized with `main` as the default branch.
      - Note: renamed the two stray files `# TASKS.md — APIx Prototype Build P.txt` and
        `# CLAUDE.md — Agent Instructions fo.txt` to `TASKS.md` / `CLAUDE.md`. Content is
        unchanged. `CLAUDE.md` only auto-loads under its exact name, and both files refer to
        `TASKS.md` by name throughout.
- [x] Create directory structure: `/scraper`, `/etl`, `/index`, `/api`, `/dashboard`, `/data`,
      `/tests`, `/docs`, `/config`
      - Note: added `__init__.py` to `scraper`, `etl`, `index`, `api`, `dashboard`, `tests` so
        cross-package imports work from the repo root without sys.path hacks. `.gitkeep` in
        `/data` and `/docs` so the (otherwise empty / gitignored) dirs survive a fresh clone.
- [x] Set up Python env (venv or poetry), `requirements.txt` with: fastapi, uvicorn, sqlite3
      (stdlib), pandas, numpy, playwright, streamlit (or note if using React instead), pytest
      - Used `venv` at `.venv` (Python 3.13.1), not poetry — zero extra tooling to install.
      - Note: `sqlite3` is stdlib, so it is a comment in `requirements.txt`, NOT a dependency.
        The `sqlite3` name on PyPI is an unrelated third-party package. Verified stdlib
        sqlite3 works (sqlite 3.45.3).
      - Note: going with **Streamlit, not React** (CLAUDE.md ground rule 7) — one language,
        one run command. Also added `plotly` (charts), `pyyaml` (config), `httpx` (needed by
        `fastapi.testclient`).
      - Note: Playwright *browser binaries* (`python -m playwright install chromium`, ~400MB)
        are deferred to Phase 3, where the scraper actually needs them. Phase 0 installs the
        Python package only.
      - Note: pip resolved pandas 3.0.5 / numpy 2.5.3 — both majors. Watch for API changes in
        the Phase 4 ETL and Phase 5 index math.
- [x] `config/routes.yaml` — hardcode top city-pairs by DGCA domestic traffic share (e.g.
      DEL-BOM, DEL-BLR, DEL-MAA, BOM-BLR, DEL-CCU, BOM-MAA, DEL-HYD, BLR-MAA) with a
      `traffic_weight` field per route (approximate is fine — cite DGCA as source in a comment)
      - All 8 city-pairs in, `traffic_weight` normalized to sum to exactly 1.000000 (verified).
      - DGCA cited in the file header, with an explicit note that the weights are hand-set
        approximations of DGCA city-pair volumes, not lifted from one specific release.
- [x] `config/airlines.yaml` — IndiGo, Air India, Akasa, SpiceJet with rough market-share
      weights (used later for index weighting)
      - Weights 0.64 / 0.27 / 0.05 / 0.04, normalized to sum to exactly 1.000000 (verified).
      - "Air India" = the merged group (AI + Vistara + Air India Express); stated in the file.
      - Added a `carrier_type` field (budget / full_service) — Phase 2's simulator needs it for
        the budget vs. full-service fare offset. Flagged in-file as a modelling input, not a
        DGCA figure.
- [x] Commit: "Phase 0: scaffold + config"

---

## Phase 1 — Shared data schema

- [x] Define the canonical fare-quote schema (used by both simulator and real scraper output),
      e.g.:
      `source | airline | origin | dest | travel_date | query_date | advance_purchase_days |
      base_fare | taxes | total_fare | currency | booking_class | is_available | scraped_at`
      - All 14 fields implemented exactly as listed, plus an `id` primary key.
      - `source` is CHECK-constrained to `simulated` / `live` / `fallback_simulated` — the
        simulated-vs-real distinction (CLAUDE.md rule 5) is enforced by the DB, not convention.
      - Mirrored in Python as the `FareQuote` dataclass in `db.py`, which both the simulator
        and the scraper will emit, so nothing downstream branches on provenance.
      - `advance_purchase_days` and `total_fare` are derived on construction if not supplied,
        so the booking-window definition lives in exactly one place.
- [x] Create SQLite schema + migration script at `/data/schema.sql`, apply it to create
      `/data/apix.db`
      - Applied via `python db.py init`; idempotent (CREATE ... IF NOT EXISTS), so it is safe
        to run on every startup. A `schema_version` table records what has been applied.
      - Judgment call: the schema deliberately does NOT enforce
        `total_fare = base_fare + taxes`. Phase 4 is specified to *validate and flag* that
        mismatch, which it cannot do if the DB rejects the row on insert. Real scraped fares
        break the identity often (surcharges, rounding) and that is a genuine quality signal.
      - UNIQUE on (source, airline, origin, dest, travel_date, query_date, booking_class) with
        upsert-on-conflict, so re-running the Phase 2 simulator refreshes rather than
        duplicating. `booking_class` is in the key so multiple cabins can coexist; Phase 4
        still dedupes on its specified 5-tuple.
      - Indexes on route, query_date, source, airline, advance_purchase_days.
- [x] Write a small `db.py` helper (insert quote, bulk insert, query by route/date range) used
      by everything downstream
      - Judgment call: `db.py` sits at the repo ROOT, not inside a phase directory — every
        package imports it and TASKS.md refers to it bare.
      - API: `connect()` (context manager, commit/rollback/close), `init_db()`,
        `insert_quote()`, `insert_quotes()` (chunked executemany), `query_fares()`,
        `query_fares_df()` (pandas), `counts_by_source()`, `date_range()`, `count_quotes()`.
      - `query_fares(date_field=...)` switches between travel_date and query_date: the index
        series runs along query_date, the booking-window curves along travel_date.
      - `query_fares(table=...)` is whitelisted to `fare_quotes` / `fares_clean` so Phase 4 can
        reuse these helpers against the cleaned table without a second query function.
      - `python db.py info` prints row counts by source, marking synthetic sources
        "<- NOT REAL DATA".
- [x] Test: insert a dummy row, read it back, confirm schema holds
      - `tests/test_db.py`: **27 tests, all passing**. Covers the round-trip, every canonical
        column present, derived fields, code normalisation, dict-or-dataclass input, rejection
        of bad source / reversed dates / malformed dates / negative fares, sold-out rows with
        NULL fares, bulk insert, upsert idempotency, all query filters, and transaction
        rollback.
      - Also verified by hand against the real `data/apix.db` (insert -> read back -> row
        deleted again, leaving the DB empty for Phase 2 to fill).
- [x] Commit: "Phase 1: shared schema + db layer"

---

## Phase 2 — Fare simulator (primary data source for the demo)

- [x] Build `/scraper/simulator.py`: generates plausible fare quotes per
      (airline, route, travel_date, advance_purchase_days) with:
  - base fare that rises non-linearly as advance_purchase_days decreases (booking-window curve)
  - taxes/fees as a semi-fixed component + route-dependent surcharge
  - per-airline fare offset (budget vs. full-service pricing gap)
  - random "sold out" / no-fare days at low frequency
  - configurable demand-shock dates (festivals, long weekends) that spike fares 150–300%
      - All five behaviours implemented. Curve is `1 + amplitude * exp(-apd / decay)`;
        taxes are fixed airport charges + distance-scaled surcharge + 5% GST on base;
        carrier offset is carrier_type x per-brand factor; sold-out probability rises
        exponentially as departure nears; shocks are date windows with a multiplier range.
      - All model parameters live in the new `config/simulator.yaml` (CLAUDE.md: config in
        `/config`, one file to point at when asked "why these numbers"). Added
        `distance_km` / `base_fare_inr` to `config/routes.yaml` as route properties, flagged
        in-file as modelling inputs, not DGCA figures.
      - Judgment call: added `config_loader.py` at repo root (alongside `db.py`) as the single
        reader for `/config`. Phase 5 needs the same route/airline weights, and it validates
        that both weight sets sum to 1.0 at load time rather than silently rescaling the index.
      - Output is deterministic: each quote is seeded from a blake2b hash of
        (seed, airline, route, travel_date, query_date), so re-running the backfill refreshes
        rows instead of churning the dataset, and the demo is reproducible.
      - Bug found and fixed during verification: the shock multiplier was originally drawn
        per quote, so the same flight jumped randomly between 1.8x and 3.0x across query
        dates (observed Rs.7,330–Rs.12,977 on one Dussehra departure). A festival is one
        price event for a sector on a date, so the multiplier is now keyed on
        (route, travel_date, shock) only — stable across carriers and observations. Locked in
        by `test_shock_is_coherent_across_airlines_and_query_dates`.
      - `generate_quote()` refuses to emit `source='live'` (test-enforced); Phase 3 reuses the
        same model for its `fallback_simulated` path.
- [x] Generate a backfilled dataset: last 30+ days of query_dates × 45 advance-purchase windows
      × all routes × all airlines, written to `apix.db`
      - **50,400 rows** = 35 query_dates (2026-08-07..2026-09-10) × 45 apd × 8 routes × 4
        airlines. Verified idempotent: re-running leaves the count at 50,400.
      - 1,712 rows (3.4%) are sold out / no fare — the "low frequency" the brief asks for.
      - `scraped_at` is stamped 06:30 UTC on the row's own query_date, not "now": these are
        backfilled historical observations and dating them today would be a lie.
- [x] Tag every simulated row with `source = 'simulated'`
      - Verified: `counts_by_source` returns `{'simulated': 50400}`, nothing else.
- [x] Sanity check: plot a couple of routes' fare-vs-advance-purchase curves, confirm they look
      like real airfare curves (steep near departure, flatter further out)
      - `python -m scraper.simulator sanity` — prints the curve, asserts its shape, and writes
        `docs/simulator_curve_check.html` (plotly). **All checks PASS.**
      - DEL-BOM: Rs.5,304 at 45 days -> Rs.11,173 at 1 day (**2.11x**), slope Rs.24/day far out
        vs Rs.412/day in the last week. BLR-MAA: 2.07x, Rs.17/day vs Rs.255/day.
      - All three configured shocks verified to fire at 1.79x / 1.90x / 2.07x vs adjacent
        non-shock dates.
      - Judgment call on the check itself: the first version averaged fares at fixed
        advance_purchase_days across query_dates, which produced a NON-monotone curve. That
        was an artifact of the check, not the model — at fixed apd, travel_date slides with
        lead time, so different lead times sweep different numbers of festival windows. The
        check now holds travel_date fixed and varies query_date (watching one flight approach
        departure) and excludes shock-window dates, which isolates the booking curve.
      - Second judgment call: 1-day-resolution monotonicity is asserted against a tolerance
        derived from the configured noise and actual sample size (3 standard errors), not a
        magic constant — far from departure the true step (~0.5%/day) is smaller than the
        sampling error, so a fixed threshold would be testing below the noise floor.
      - `tests/test_simulator.py`: 25 tests. Full suite now **52 passing**.
- [x] Commit: "Phase 2: fare simulator + backfilled dataset"

---

## Phase 3 — Real scraper module (small, honest, non-blocking)

- [ ] Pick ONE real, accessible target (a single airline search page or metasearch site).
      Check its `robots.txt` first — if scraping is disallowed, document that and skip to the
      fallback-only version of this phase.
- [ ] Build `/scraper/live_scraper.py` using Playwright (headless), matching the Phase 1 schema
      exactly (`source = 'live'`)
- [ ] Add rate-limiting (min delay between requests) and retry/backoff on failure
- [ ] Add graceful fallback: if blocked/CAPTCHA'd/timeout, log it and return a simulator-backed
      result tagged `source = 'fallback_simulated'` so the caller never just gets an error
- [ ] Test: run it live for one route, confirm either a real result or a clean fallback — no
      unhandled exceptions
- [ ] Commit: "Phase 3: real scraper with fallback"
- Note: this module exists to *prove capability*, not to power the main demo. Keep it isolated
  so nothing else depends on it succeeding.

---

## Phase 4 — ETL / cleaning

- [ ] `/etl/clean.py`: outlier stripping (IQR or MAD, threshold in config), split/validate
      base_fare + taxes vs total_fare, drop/flag nulls and unavailable rows, dedupe on
      (source, airline, route, travel_date, query_date)
- [ ] Output a cleaned table/view (`fares_clean`) in the same DB, or a materialized
      pandas-friendly table
- [ ] Log a data-quality summary (rows in, rows dropped, reasons) — this is useful evaluator
      material, keep it
- [ ] Unit test the cleaning functions with a few crafted edge cases (extreme outlier, null
      fare, negative fare, sold-out row)
- [ ] Commit: "Phase 4: ETL cleaning"

---

## Phase 5 — Index construction

- [ ] `/index/apix.py`: implement a Laspeyres-style fixed-basket weighted index:
  - fixed base period (first available date) = 100
  - weights = route traffic share × airline market share × advance-purchase-bucket weight
  - aggregate to daily, weekly, monthly APIx
- [ ] Compute route-level sub-indices (APIx per city-pair)
- [ ] Compute lead-time elasticity: % fare change per day as advance_purchase_days decreases,
      per route
- [ ] Write `/docs/methodology.md`: the formula, the weighting scheme and why, base period
      choice, and known limitations (must be defensible in a live Q&A)
- [ ] Test: confirm index = 100 at base period, confirm it moves sensibly under a synthetic
      demand shock
- [ ] Commit: "Phase 5: index construction + methodology doc"

---

## Phase 6 — Backtest / validation

- [ ] Obtain or synthesize a DGCA-style reference average-fare series. If real DGCA data isn't
      fetchable in time, generate a plausible reference series correlated with your simulator's
      demand shocks, and label it clearly as a stand-in with a note on where real DGCA data
      would plug in
- [ ] `/index/backtest.py`: compare APIx to reference over 30+ days — correlation, MAPE
- [ ] Produce an overlay chart (APIx vs reference) and save backtest results to DB/JSON for the
      API and dashboard to read
- [ ] Commit: "Phase 6: backtest"

---

## Phase 7 — API

- [ ] `/api/main.py` (FastAPI):
  - `GET /index` — current + historical APIx (daily/weekly/monthly)
  - `GET /index/route/{origin}/{dest}` — route sub-index
  - `GET /index/elasticity` — lead-time elasticity data
  - `GET /backtest/results` — correlation, MAPE, series for overlay
  - `POST /scrape/live` — triggers Phase 3 scraper for a given route (used by dashboard button)
- [ ] Confirm `/docs` (OpenAPI/Swagger) renders and all endpoints return valid JSON
- [ ] Commit: "Phase 7: API"

---

## Phase 8 — Dashboard

- [ ] Build dashboard (Streamlit, or React if you're faster there) with:
  1. APIx trend chart, daily/weekly/monthly toggle
  2. Sector/route heatmap (fare index by city-pair)
  3. Lead-time elasticity chart
  4. Backtest overlay chart (APIx vs. reference), with correlation/MAPE shown
  5. "Run live scrape" button → calls `POST /scrape/live` → shows result landing in the data,
     with a visible message if it fell back to simulated data
- [ ] Add a persistent, visible banner: "Simulated demo data — see methodology.md for the real
      scraper module and data sourcing plan" (or similar) — do not bury this
- [ ] Polish pass: consistent labels, no debug output visible, loads in one command
- [ ] Commit: "Phase 8: dashboard"

---

## Phase 9 — Docs for the pitch

- [ ] `docs/architecture.md` — data flow diagram (mermaid), and an honest explanation of the
      simulator/real-scraper split
- [ ] `docs/anti-bot-strategy.md` — one page covering: JS-rendered pages (Playwright), CAPTCHA
      (why not bypassed — ToS/legal risk — fallback strategy instead), IP rotation (flagged as
      a future scaling consideration, not implemented), robots.txt compliance. This is the
      section most likely to be probed — be precise, not hand-wavy.
- [ ] `README.md` at repo root: one-command setup/run instructions, "what's simulated vs real"
      section near the top, links to the two docs above
- [ ] Commit: "Phase 9: docs"

---

## Final checklist (confirm before presenting)

- [ ] Fresh clone of the repo + one command → dashboard running
- [ ] Every chart on the dashboard populated with data, nothing blank or erroring
- [ ] Live-scrape button demoed once successfully (or fallback path demoed cleanly)
- [ ] `/docs` API page loads and endpoints return data
- [ ] Backtest correlation/MAPE numbers are sane (not suspiciously perfect, not garbage)
- [ ] README's "simulated vs real" section is accurate and matches what's on screen
- [ ] All tasks above are `[x]` or `[!]` with a documented reason