"""Phase 8 tests: the dashboard degrades gracefully and never hides provenance.

Streamlit's AppTest runs the real script in-process. These cover the paths that
are awkward to demo by hand — chiefly "the API is down", which must produce an
actionable message rather than a stack trace or a blank page.

The full rendered-in-a-browser verification (all five sections, four charts,
clicking the live-scrape button) is a manual step recorded in TASKS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")

AppTest = pytest.importorskip(
    "streamlit.testing.v1", reason="streamlit testing harness unavailable"
).AppTest


def run_app(api_base: str, timeout: float = 60.0):
    """Run the dashboard against `api_base` and return the finished AppTest."""
    import os

    # API_BASE is read at module import, so set it before the script runs and
    # drop any cached copy of the module.
    os.environ["APIX_API_BASE"] = api_base
    sys.modules.pop("dashboard.app", None)

    at = AppTest.from_file(APP, default_timeout=timeout)
    return at.run()


def all_text(at) -> str:
    parts = []
    for coll in (at.markdown, at.error, at.warning, at.info, at.success,
                 at.caption, at.title, at.subheader):
        try:
            parts += [el.value for el in coll]
        except Exception:
            pass
    return "\n".join(str(p) for p in parts)


# --- the API-down path ------------------------------------------------------


@pytest.fixture(scope="module")
def down_app():
    """A port nothing is listening on."""
    return run_app("http://127.0.0.1:9")


def test_app_does_not_crash_when_the_api_is_down(down_app):
    assert not down_app.exception, f"unhandled exception: {down_app.exception}"


def test_api_down_message_is_actionable(down_app):
    text = all_text(down_app)
    assert "Cannot reach the APIx API" in text
    assert "python run.py" in text, "must tell the user how to start it"


def test_banner_is_shown_even_when_the_api_is_down(down_app):
    """Provenance must never depend on a service being reachable."""
    text = all_text(down_app)
    assert "SIMULATED DEMO DATA" in text
    assert "NOT REAL AIRLINE FARES" in text


def test_hero_renders_instead_of_the_fallback_title(down_app):
    """The <h1> now lives inside the hero iframe, which AppTest cannot see.

    So assert the negative instead: render_hero() falls back to a plain
    st.title only when the 3D hero raises. Seeing no fallback title means the
    hero component was emitted successfully.
    """
    titles = [el.value for el in down_app.title]
    assert not any("Real-time Airfare Price Index for India" in v for v in titles), (
        f"hero fell back to the plain title: {titles}"
    )
    assert not any("hero visual unavailable" in str(c.value) for c in down_app.caption)
    # And the product is still named somewhere the user can read.
    assert "APIx" in all_text(down_app)


# --- the banner text itself -------------------------------------------------


def test_banner_names_the_methodology_doc():
    import dashboard.app as app

    import io
    src = io.open(app.__file__, encoding="utf-8").read()
    assert "docs/methodology.md" in src
    assert "SIMULATED DEMO DATA" in src


def test_dashboard_reads_api_base_from_the_environment(monkeypatch):
    monkeypatch.setenv("APIX_API_BASE", "http://example.invalid:1234")
    sys.modules.pop("dashboard.app", None)
    import dashboard.app as app

    assert app.API_BASE == "http://example.invalid:1234"
    sys.modules.pop("dashboard.app", None)


def test_scrape_timeout_exceeds_the_scrapers_deadline():
    """The dashboard must not give up before the scraper does."""
    sys.modules.pop("dashboard.app", None)
    import dashboard.app as app
    from scraper.live_scraper import load_scraper_config

    deadline = load_scraper_config()["retry"]["overall_deadline_seconds"]
    assert app.SCRAPE_TIMEOUT > deadline, (
        "the dashboard would time out mid-scrape and show a false failure"
    )


# --- visual layer: theme + hero ---------------------------------------------
# These exist mainly as a tripwire. dashboard/hero.py builds a large HTML
# document with an f-string, so a single unescaped `{` in the embedded
# JavaScript is a SyntaxError that only surfaces when Streamlit imports the
# module — which during development meant a blank hero and a confusing hunt.


import re  # noqa: E402


def test_theme_stylesheet_is_well_formed():
    from dashboard import theme

    css = theme.inject_theme()
    assert css.startswith("<style>") and css.endswith("</style>")
    assert css.count("{") == css.count("}"), "unbalanced braces in stylesheet"
    # Every colour must be a real hex value.
    for name, value in theme.PALETTE.items():
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{name}={value!r}"


def test_theme_respects_reduced_motion():
    from dashboard import theme

    assert "prefers-reduced-motion" in theme.inject_theme()


def test_plotly_theme_registers_and_becomes_default():
    import plotly.io as pio

    from dashboard import theme

    name = theme.register_plotly_theme()
    assert pio.templates.default == name
    tpl = pio.templates[name]
    assert tpl.layout.colorway[0] == theme.PALETTE["accent"]


def test_hero_renders_without_fstring_errors():
    """The regression guard: render() must not raise, and must emit real JS."""
    from dashboard import hero

    out = hero.render(
        title='Airfare Price Index <span class="grad">— Live</span>',
        subtitle="subtitle",
        eyebrow="eyebrow",
    )
    assert len(out) > 100_000, "three.js does not appear to be inlined"
    assert "function layout()" in out

    # Check only OUR script block for escaping slips. Two reasons not to scan
    # the whole document: minified three.js legitimately contains `{{` and
    # `}}`, and letting pytest build an assertion diff over a 600KB string
    # hangs the run rather than reporting a failure.
    scene = out.rsplit("<script>", 1)[-1]
    assert len(scene) < 60_000, "unexpected: scene script should be small"
    assert "{{" not in scene, "unescaped f-string braces leaked into the scene JS"
    assert "}}" not in scene, "unescaped f-string braces leaked into the scene JS"


def test_hero_embeds_the_real_route_basket():
    """The arcs must be the routes we actually track, not invented geometry."""
    from config_loader import load_routes
    from dashboard import hero

    out = hero.render("t", "s")
    for route in load_routes():
        if route.origin in hero.AIRPORTS and route.dest in hero.AIRPORTS:
            assert f'"o": "{route.origin}"' in out or f'"o":"{route.origin}"' in out


def test_hero_assets_are_vendored_not_cdn():
    """A conference network must not be able to break the hero."""
    from dashboard import hero

    assert hero.THREE_JS.exists(), "three.min.js is not vendored"
    assert hero.INDIA_OUTLINE.exists(), "india outline is not vendored"
    out = hero.render("t", "s")
    assert "cdn." not in out and "https://" not in out.split("<script>")[0]


def test_india_outline_is_plausible():
    from dashboard import hero

    pts = hero._outline()
    assert len(pts) > 40
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    # India's real bounding box, give or take.
    assert 67 < min(lons) < 70 and 95 < max(lons) < 98
    assert 6 < min(lats) < 10 and 35 < max(lats) < 38
