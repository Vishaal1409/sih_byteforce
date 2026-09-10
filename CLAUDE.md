# CLAUDE.md — Agent Instructions for APIx Prototype (SIH26056)

This file is read automatically at the start of every Claude Code session in this repo.
Follow it for all work here unless the user explicitly overrides it in a message.

## What this project is

A prototype Real-time Airfare Price Index (APIx) for India, for SIH26056 (MoSPI problem
statement). It must be demo-able tomorrow. Read `TASKS.md` for the actual work breakdown —
this file only covers *how* to work, not *what* to build.

## Ground rules

1. **Work through `TASKS.md` in order.** Phases are sequenced so each one is demoable before
   the next starts. Do not skip ahead to a later phase leaving an earlier one broken.
2. **After finishing each task, mark it done in `TASKS.md`** (change `[ ]` to `[x]`) and add a
   one-line note under it if you made a judgment call worth flagging (e.g. "used MAD instead
   of IQR because sample size was small"). This file is the source of truth for progress —
   keep it current, don't just report status in chat.
3. **Run and verify before claiming something works.** Never mark a task done without actually
   executing the code and confirming output. If something fails, fix it or flag it — don't
   silently mark it done anyway.
4. **Commit at the end of each phase** with a clear message (`git commit -m "Phase 2: fare
   simulator + scraper interface parity"`). If git isn't initialized, initialize it first.
5. **Be honest about simulated vs. real data everywhere** — in code comments, in the README,
   and in the dashboard UI itself (a visible label, not a footnote). This project's credibility
   depends on this distinction being obvious, not glossed over. Never let simulated output be
   mistaken for a real scrape result.
6. **Do not attempt to bypass CAPTCHAs, rotate IPs to evade blocking, or scrape sites whose
   robots.txt disallows it.** If a real target blocks you, log it, fall back to the simulator,
   and note the block in `docs/anti-bot-strategy.md`. This is a hackathon prototype, not a
   production scraper — staying within ToS is non-negotiable, not a nice-to-have.
7. **Prefer working-but-simple over elaborate-but-fragile.** This ships in hours, not weeks.
   SQLite over Postgres, Streamlit over a custom React build (unless React is clearly faster
   for you to execute well), synthetic reference data clearly labeled over a brittle live
   pull from a government portal that might change format.
8. **Stop and ask the user only when genuinely blocked** — e.g. a required external dependency
   is unreachable, or a design decision materially changes what gets demoed. Otherwise make a
   reasonable call, document it, and keep moving.
9. **Every phase must leave the repo in a runnable state.** Someone should be able to check out
   the repo at the end of any phase and run what exists so far with one command.

## Repo conventions

- Structure: `/scraper`, `/etl`, `/index`, `/api`, `/dashboard`, `/data`, `/tests`, `/docs`
- Python for scraper/ETL/index/API. FastAPI for the API layer.
- SQLite at `/data/apix.db` — zero setup, good enough for a prototype.
- All fare data (simulated or real) conforms to one schema (see `TASKS.md` Phase 2) so ETL,
  index math, and the dashboard never need to know which source produced a row.
- Config (city-pair basket, weights, thresholds) lives in `/config`, not hardcoded scattered
  through the codebase — the professor will ask "why these routes/weights" and you want to be
  able to point at one file.

## Definition of done for the whole prototype

- `README.md` at repo root: setup instructions (one command to run the dashboard), and a
  "What's simulated vs real" section near the top, before anything else.
- Dashboard runs locally and displays: APIx trend, route heatmap, lead-time elasticity,
  backtest overlay, and a live-scrape trigger with visible fallback behavior.
- API serves documented endpoints at `/docs`.
- `docs/methodology.md` and `docs/anti-bot-strategy.md` exist and are readable by a
  non-technical evaluator.
- Every task in `TASKS.md` is checked off or explicitly marked blocked with a reason.