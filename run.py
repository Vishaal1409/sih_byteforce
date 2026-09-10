"""run.py — start the whole APIx demo with one command.

    python run.py

Builds the data pipeline if it has never been run, starts the FastAPI service,
waits for it to answer, then opens the Streamlit dashboard. Ctrl+C stops both.

    python run.py --rebuild     regenerate the dataset and all derived tables
    python run.py --api-only    just the API
    python run.py --no-browser  do not open a browser window
"""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

API_HOST, API_PORT = "127.0.0.1", 8000
DASHBOARD_PORT = 8501
API_BASE = f"http://{API_HOST}:{API_PORT}"

#: The venv's interpreter if we are inside one, else whatever is running us.
PYTHON = sys.executable

_children: list[subprocess.Popen] = []


def _shutdown() -> None:
    for proc in _children:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


atexit.register(_shutdown)


# ---------------------------------------------------------------------------


def pipeline_is_built() -> bool:
    import db

    if not db.DEFAULT_DB_PATH.exists():
        return False
    with db.connect(db.DEFAULT_DB_PATH) as conn:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    required = {"fare_quotes", "fares_clean", "apix_index", "apix_backtest"}
    if not required <= names:
        return False
    return conn_rowcount() > 0


def conn_rowcount() -> int:
    import db

    with db.connect(db.DEFAULT_DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM apix_index").fetchone()[0]


def build_pipeline() -> None:
    """Run Phases 2, 4, 5 and 6 in order."""
    steps = [
        ("Generating simulated fares", ["-m", "scraper.simulator", "backfill"]),
        ("Cleaning", ["-m", "etl.clean"]),
        ("Building the index", ["-m", "index.apix"]),
        ("Backtesting", ["-m", "index.backtest"]),
    ]
    for label, args in steps:
        print(f"\n>>> {label} ...")
        result = subprocess.run([PYTHON, *args], cwd=REPO_ROOT)
        if result.returncode != 0:
            print(f"\nFAILED: {label} (exit {result.returncode})", file=sys.stderr)
            sys.exit(result.returncode)


def wait_for_api(timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{API_BASE}/openapi.json", timeout=3):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def start_api() -> subprocess.Popen:
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "api.main:app",
         "--host", API_HOST, "--port", str(API_PORT), "--log-level", "warning"],
        cwd=REPO_ROOT,
    )
    _children.append(proc)
    return proc


def start_dashboard(open_browser: bool) -> subprocess.Popen:
    env = dict(os.environ, APIX_API_BASE=API_BASE)
    proc = subprocess.Popen(
        [PYTHON, "-m", "streamlit", "run", "dashboard/app.py",
         "--server.port", str(DASHBOARD_PORT),
         "--server.headless", "false" if open_browser else "true",
         "--browser.gatherUsageStats", "false"],
        cwd=REPO_ROOT,
        env=env,
    )
    _children.append(proc)
    return proc


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the APIx demo")
    parser.add_argument("--rebuild", action="store_true",
                        help="regenerate the dataset and all derived tables")
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    print("=" * 68)
    print("APIx — Real-time Airfare Price Index (India)   SIH26056")
    print("=" * 68)
    print("NOTE: the fare data in this demo is SIMULATED, not real airline")
    print("      pricing. See docs/methodology.md.")

    if args.rebuild or not pipeline_is_built():
        reason = "rebuild requested" if args.rebuild else "no data found"
        print(f"\nBuilding the pipeline ({reason}). First run takes a minute.")
        build_pipeline()
    else:
        print("\nPipeline already built. Use --rebuild to regenerate.")

    print(f"\nStarting API on {API_BASE} ...")
    api = start_api()
    if not wait_for_api():
        print("\nThe API did not come up in time.", file=sys.stderr)
        if api.poll() is not None:
            print(f"uvicorn exited with code {api.returncode}", file=sys.stderr)
        return 1
    print(f"  API ready       {API_BASE}")
    print(f"  Swagger docs    {API_BASE}/docs")

    if args.api_only:
        print("\n--api-only: dashboard not started. Ctrl+C to stop.")
        try:
            api.wait()
        except KeyboardInterrupt:
            pass
        return 0

    print(f"\nStarting dashboard on http://localhost:{DASHBOARD_PORT} ...")
    dash = start_dashboard(open_browser=not args.no_browser)

    print("\n" + "=" * 68)
    print(f"  Dashboard   http://localhost:{DASHBOARD_PORT}")
    print(f"  API docs    {API_BASE}/docs")
    print("  Ctrl+C to stop both.")
    print("=" * 68)

    try:
        while True:
            if dash.poll() is not None:
                print(f"\nDashboard exited (code {dash.returncode}).")
                return dash.returncode or 0
            if api.poll() is not None:
                print(f"\nAPI exited unexpectedly (code {api.returncode}).")
                return api.returncode or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    finally:
        _shutdown()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        _shutdown()
        raise SystemExit(0)
