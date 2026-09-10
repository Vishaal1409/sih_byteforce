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

- [ ] Define the canonical fare-quote schema (used by both simulator and real scraper output),
      e.g.:
      `source | airline | origin | dest | travel_date | query_date | advance_purchase_days |
      base_fare | taxes | total_fare | currency | booking_class | is_available | scraped_at`
- [ ] Create SQLite schema + migration script at `/data/schema.sql`, apply it to create
      `/data/apix.db`
- [ ] Write a small `db.py` helper (insert quote, bulk insert, query by route/date range) used
      by everything downstream
- [ ] Test: insert a dummy row, read it back, confirm schema holds
- [ ] Commit: "Phase 1: shared schema + db layer"

---

## Phase 2 — Fare simulator (primary data source for the demo)

- [ ] Build `/scraper/simulator.py`: generates plausible fare quotes per
      (airline, route, travel_date, advance_purchase_days) with:
  - base fare that rises non-linearly as advance_purchase_days decreases (booking-window curve)
  - taxes/fees as a semi-fixed component + route-dependent surcharge
  - per-airline fare offset (budget vs. full-service pricing gap)
  - random "sold out" / no-fare days at low frequency
  - configurable demand-shock dates (festivals, long weekends) that spike fares 150–300%
- [ ] Generate a backfilled dataset: last 30+ days of query_dates × 45 advance-purchase windows
      × all routes × all airlines, written to `apix.db`
- [ ] Tag every simulated row with `source = 'simulated'`
- [ ] Sanity check: plot a couple of routes' fare-vs-advance-purchase curves, confirm they look
      like real airfare curves (steep near departure, flatter further out)
- [ ] Commit: "Phase 2: fare simulator + backfilled dataset"

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