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

- [x] Pick ONE real, accessible target (a single airline search page or metasearch site).
      Check its `robots.txt` first — if scraping is disallowed, document that and skip to the
      fallback-only version of this phase.
      - **Target: Skyscanner India**, `/transport/flights/{o}/{d}/{yymmdd}/`. Chosen because
        robots.txt permits that path.
      - robots.txt checked live on 2026-09-10 with `urllib.robotparser` across 10 candidates.
        Results recorded in `config/scraper.yaml`: Skyscanner route pages ALLOWED (but
        `/prices-calendar/` DISALLOWED, so unused); **Kayak `/flights/` DISALLOWED**;
        **Cleartrip `/flights/search*` DISALLOWED**; Goibibo and Ixigo allowed.
        Kayak and Cleartrip are therefore not scraped.
      - The scraper re-checks robots.txt at runtime before every fetch, not just at build
        time — permission can be withdrawn. It fails CLOSED: an unreachable robots.txt is
        treated as "no", not as permission.
- [x] Build `/scraper/live_scraper.py` using Playwright (headless), matching the Phase 1 schema
      exactly (`source = 'live'`)
      - Emits `db.FareQuote` with `source='live'`, so scraped rows are schema-identical to
        simulated ones and nothing downstream needs to care which it got.
      - Installed the Chromium binary for this phase (deferred from Phase 0 as planned).
      - Runs a real, unmodified Chromium: no stealth plugins, no `navigator.webdriver`
        patching, no fingerprint randomisation, no IP rotation (CLAUDE.md rule 6).
      - `parse_fares()` is deliberately conservative — it only emits a quote when it can tie
        a price to a carrier in our basket, and rejects amounts outside Rs.1,000–Rs.100,000
        so a phone number or mileage figure is never mistaken for a fare. A page it cannot
        parse yields nothing and falls back, rather than inventing a number and calling it
        live.
- [x] Add rate-limiting (min delay between requests) and retry/backoff on failure
      - 6s minimum gap between requests, enforced process-wide; honours a robots.txt
        `crawl-delay` if the target ever declares one (takes the larger of the two).
      - 3 attempts, exponential backoff (2s, 4s), 90s overall deadline per call.
      - Judgment call: a **block is never retried**. Only transient errors (timeout, network)
        retry. Retrying a site that has already refused is precisely the hammering rule 6
        forbids — and it cut the observed failure path from 93s/3 attempts to 6s/1 attempt.
- [x] Add graceful fallback: if blocked/CAPTCHA'd/timeout, log it and return a simulator-backed
      result tagged `source = 'fallback_simulated'` so the caller never just gets an error
      - Never raises. Every failure path returns a `ScrapeResult` with `is_real=False` and a
        plain-English `reason`. Test-enforced across RuntimeError/ValueError/OSError/MemoryError.
      - Fallback rows are tagged `fallback_simulated`, distinct from ordinary `simulated`, so
        "the scraper was blocked and we substituted" stays visible in the data permanently
        rather than blending into the Phase 2 backfill.
      - `ScrapeResult.headline` is a ready-to-display string that says outright
        "these are NOT real quotes" on the fallback path — Phase 8's dashboard shows it verbatim.
- [x] Test: run it live for one route, confirm either a real result or a clean fallback — no
      unhandled exceptions
      - **Ran live. Result: clean fallback, no unhandled exceptions.** DEL-BOM and BLR-MAA,
        `--store` verified: 4 rows landed tagged `fallback_simulated`, `db.py info` shows
        50,400 simulated + 4 fallback_simulated, both marked "<- NOT REAL DATA".
      - **Finding — every accessible target blocks automated access** (probed 2026-09-10):
        - Skyscanner: HTTP **200** but a **312-character body containing only a challenge
          UUID** — a refusal dressed as a success, with no fare content at all.
        - Ixigo: HTTP **403** "Oops...too many requests!"
        - Goibibo: `ERR_HTTP2_PROTOCOL_ERROR`.
      - This drove a real fix: the 312-char interstitial matched none of the CAPTCHA-phrase
        indicators, so the scraper read it as "parsed nothing" and retried 3x. Added a
        `min_content_chars` check — a page too short to be a search result is now classified
        as a block and not retried. Evidence is captured for `docs/anti-bot-strategy.md`.
      - No attempt was made to solve the challenge, and none will be (CLAUDE.md rule 6).
        Being blocked is the honest outcome and the fallback is what the phase is for.
      - `tests/test_live_scraper.py`: 22 tests, fully offline (network stubbed) so they are
        deterministic and do not hammer the target. Full suite **74 passing**.
- [x] Commit: "Phase 3: real scraper with fallback"
- Note: this module exists to *prove capability*, not to power the main demo. Keep it isolated
  so nothing else depends on it succeeding.

---

## Phase 4 — ETL / cleaning

- [x] `/etl/clean.py`: outlier stripping (IQR or MAD, threshold in config), split/validate
      base_fare + taxes vs total_fare, drop/flag nulls and unavailable rows, dedupe on
      (source, airline, route, travel_date, query_date)
      - Pipeline: dedupe -> drop unavailable -> drop nulls -> bounds check -> flag component
        mismatch -> strip outliers. All thresholds in `config/etl.yaml`.
      - Chose **MAD over IQR**: within-group samples are small (~35) and right-skewed, where
        MAD's breakdown point beats the quartile spread and needs no symmetry assumption.
      - Component mismatch is **flagged, not dropped** (`fare_mismatch` column) — the Phase 1
        schema deliberately declined to enforce `base+taxes=total` precisely so this step
        could surface it as a quality signal. Configurable to drop instead.
      - **Major bug found and fixed during verification.** The first implementation ran MAD
        within (route, airline, lead-time) groups on raw fares. It dropped 1,007 rows — and
        **all 1,007 were festival/long-weekend rows; not one was an ordinary day.** A
        market-wide 2x spike looks anomalous against its own group's median, so the cleaner
        was stripping exactly the signal the index exists to measure, which would have made
        Phase 5's demand-shock test meaningless. No threshold tuning fixes that; it is the
        wrong comparison.
        Fix: each fare is first divided by the median fare for the **same departure at the
        same lead time across all carriers** (`peer_ratio`). A shock lifts every carrier
        together so the ratio stays ~1.0 and survives; a mis-parse moves one carrier away
        from its peers and is caught. Verified: 0 shock rows dropped, and injected x10, /8
        and x6 errors all still caught — including one placed *on* a shock date, proving it
        separates "expensive because festival" from "wrong".
      - Two further robustness fixes surfaced by the tests:
        1. `modified_z_scores` fell back to all-zeros when MAD was exactly 0, going blind
           when one wild value sat in an otherwise constant group. Now uses the standard
           Iglewicz-Hoaglin meanAD fallback.
        2. Added `min_peer_deviation` (0.25): a fare within 25% of its peer median is never
           dropped regardless of z-score. In tightly-clustered groups MAD tends to zero and
           the modified z explodes, so ordinary +/-3% variation was scoring 30+. A row must
           now be both statistically extreme AND materially unlike its peers.
- [x] Output a cleaned table/view (`fares_clean`) in the same DB, or a materialized
      pandas-friendly table
      - Materialised **table** (not a view): the index math scans it repeatedly and the
        outlier step is not reasonably expressible in SQL. Rebuilt on each run.
      - **48,692 rows** from 50,404 in (96.6% retained). `db.query_fares(table='fares_clean')`
        works against it — the Phase 1 whitelist paying off, no second query function needed.
- [x] Log a data-quality summary (rows in, rows dropped, reasons) — this is useful evaluator
      material, keep it
      - Printed by `python -m etl.clean` and persisted to a `data_quality_log` table as JSON,
        one row per run, so the API and dashboard can show it. `latest_quality_report()`
        reads it back.
      - A test asserts `rows_in == rows_out + total_dropped`, so no row can vanish unaccounted.
      - Current run: 1,712 dropped (all sold-out / no-fare), 0 outliers, 0 component
        mismatches on simulated data.
- [x] Unit test the cleaning functions with a few crafted edge cases (extreme outlier, null
      fare, negative fare, sold-out row)
      - `tests/test_clean.py`: 24 tests covering all four named cases plus dedupe (and that
        `source` being part of the key keeps a fallback row distinct), shock-preservation,
        error-on-a-shock-date, the zero-MAD fallback, small-group exemption, config-driven
        drop-on-mismatch, empty input, and persistence/re-run behaviour.
      - Note: my first test fixtures were wrong, not the cleaner — rows sharing a natural key
        were being deduped before reaching the step under test, and the panel varied lead
        time independently of the dates, which cannot happen in real data since
        `apd = travel_date - query_date`. Fixtures now derive travel_date from
        query_date + apd.
      - Full suite: **98 passing**.
- [x] Commit: "Phase 4: ETL cleaning"

---

## Phase 5 — Index construction

- [x] `/index/apix.py`: implement a Laspeyres-style fixed-basket weighted index:
  - fixed base period (first available date) = 100
  - weights = route traffic share × airline market share × advance-purchase-bucket weight
  - aggregate to daily, weekly, monthly APIx
      - `APIx_t = 100 * sum_i w_i * (P_it / P_i0)` over **192 basket cells**
        (8 routes × 4 airlines × 6 booking windows). Weights asserted to sum to 1.0 at load.
      - Base = first available query_date (2026-08-07) = 100.00 exactly. Bucket weights added
        to `config/index.yaml`, flagged in-file as modelling assumptions (lead-time booking
        distributions are not published at this granularity).
      - Missing cells: weight is **redistributed** across observed cells, not treated as a
        zero price — the latter would drag the index down when seats sold out, the opposite
        of what happens to prices. Each day carries a `coverage` figure; <60% is flagged.
      - Cells with no base-period price are dropped (no P_i0 = no relative). Currently 0
        dropped, 100% coverage on every day.
      - Weekly/monthly = mean of the daily series; partial periods dropped. Recomputing from
        pooled cells would mix days with different availability and break comparability.
      - **Result: 100.00 at base, rising to 109.68 by 2026-09-10** as the October festival
        dates enter the 45-day booking window. 35 daily / 5 weekly / 1 monthly points.
- [x] Compute route-level sub-indices (APIx per city-pair)
      - Same formula per route; the route weight cancels, leaving airline × bucket
        renormalised. All 8 routes, 280 rows in `apix_route_index`.
- [x] Compute lead-time elasticity: % fare change per day as advance_purchase_days decreases,
      per route
      - Log-linear fit (fare rises compound, so a rupees-vs-days line would under-read short
        lead times), reported as `exp(-slope) - 1`.
      - **Correctness fix found during verification.** Pooled OLS is biased here: in a rolling
        panel a 45-day quote is by construction a quote for a departure six weeks out, and
        some of those are festival dates — so the fit credits part of the *festival* premium
        to *long lead times* and flattens the curve. Measured: **1.11%/day pooled vs
        1.57%/day** with travel-date fixed effects, R² 0.28 vs 0.51. A ~30% understatement.
        Now demeans ln(fare) and apd within each travel_date before fitting, so the slope
        comes only from comparing the *same* departure at different lead times.
      - Results: **1.53–1.59 %/day** across the 8 routes, R² ≈ 0.51. Test
        `test_elasticity_recovers_a_known_rate` builds fares that rise exactly 2%/day and
        confirms the estimator returns 2.000%.
- [x] Write `/docs/methodology.md`: the formula, the weighting scheme and why, base period
      choice, and known limitations (must be defensible in a live Q&A)
      - Written for a non-technical reader: what the index measures and why a naive average
        is wrong, the formula, all three weight tables, why the basket is stratified by
        booking window, missing-cell handling, the elasticity estimator with the pooled-vs-
        fixed-effects comparison, and an explicit simulated-vs-real section.
      - 8 known limitations stated openly, including the single-day base period propagating
        that day's noise through the whole series (the spec's choice; the knob is exposed).
- [x] Test: confirm index = 100 at base period, confirm it moves sensibly under a synthetic
      demand shock
      - Both named checks pass, plus exact-value tests: a uniform +10% gives exactly 110.00,
        −20% gives 80.00, and a 2.5× one-day shock gives exactly 250.00 with neighbouring
        days unmoved at 100.00.
      - `test_index_ignores_a_shift_in_observation_mix` is the one that proves the design:
        flooding the sample with cheap short-haul rows drops a naive average but leaves APIx
        at exactly 100.00 — fixed weights doing their job.
      - `tests/test_apix.py`: 27 tests. Full suite **125 passing**.
- [x] Commit: "Phase 5: index construction + methodology doc"

---

## Phase 6 — Backtest / validation

- [x] Obtain or synthesize a DGCA-style reference average-fare series. If real DGCA data isn't
      fetchable in time, generate a plausible reference series correlated with your simulator's
      demand shocks, and label it clearly as a stand-in with a note on where real DGCA data
      would plug in
      - **Real DGCA data attempted first.** dgca.gov.in and civilaviation.gov.in both return
        HTTP 200 but serve a JavaScript shell with no machine-readable fare series;
        data.gov.in's catalogue API returns HTTP 400 without a registered key.
      - The blocking issue is **granularity, not access**: DGCA publishes *monthly* PDF
        aggregates, APIx here is a *daily* series over 35 days. No real DGCA daily
        average-fare series exists to validate against, so no amount of scraping produces
        one. Recorded in `config/backtest.yaml`.
      - Stand-in built and labelled `"DGCA-style reference (SYNTHETIC STAND-IN - not real
        DGCA data)"`, with `is_real_data: false` carried through the DB, the JSON and the
        chart title. A test asserts that flag can never quietly flip.
      - Judgment call on construction: the reference is deliberately **not** APIx-with-noise
        (which would give a meaningless ~1.0 correlation). It is an **unweighted market
        mean** — how a simple published average fare is actually computed — plus
        measurement noise and 3-day smoothing. A test enforces that it stays a genuinely
        different estimator.
      - Where real data plugs in: replace `build_reference_series()`; it returns two columns
        and nothing downstream cares where they came from.
- [x] `/index/backtest.py`: compare APIx to reference over 30+ days — correlation, MAPE
      - **35 days. r = 0.9669, Spearman = 0.9336, MAPE = 2.69%**, RMSE 3.48 index points.
        High but not suspiciously perfect — the final-checklist bar. A test asserts
        0.70 <= r <= 0.995 and 0.2% <= MAPE <= 15% so a rigged-looking result fails CI.
      - Refuses to run on fewer than 30 days rather than reporting a thin window.
      - **The divergence turned out to be the strongest result in the project.** APIx moves
        +9.68% over the window; the naive average moves +14.86%. Investigated rather than
        hand-waved: the 31–45 day bucket supplies **34.1% of rows but carries 10% index
        weight** (3.41x over-represented), and it is the *only* bucket whose travel dates
        reach the October festivals (**22.8% of its rows**, vs 0% for every bucket inside
        three weeks). So the naive average reads +36.78% on that bucket alone and −0.16%
        on the 0–3 day bucket. Most of the "price rise" a simple average reports is
        composition, not price — which is exactly what the fixed basket removes. Written up
        as a worked example in `docs/methodology.md` §7a.
- [x] Produce an overlay chart (APIx vs reference) and save backtest results to DB/JSON for the
      API and dashboard to read
      - `docs/backtest_overlay.html` (plotly), with r / MAPE / n and a
        "BOTH SERIES DERIVED FROM SIMULATED FARE DATA" note in the chart subtitle.
      - Persisted to `apix_backtest` + `apix_backtest_metrics` tables and
        `data/backtest_results.json`. `latest_backtest()` reads it back for Phase 7/8.
      - Bug found and fixed: `run_backtest()` wrote to hardcoded module-level output paths,
        so running the test suite silently overwrote the real `data/backtest_results.json`
        with a degenerate flat-fare run (correlation came out `nan`). Output paths are now
        parameters; tests write to `tmp_path`. Verified the real artifacts survive a full
        test run.
      - `tests/test_backtest.py`: 20 tests. Full suite **145 passing**.
- [x] Commit: "Phase 6: backtest"

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