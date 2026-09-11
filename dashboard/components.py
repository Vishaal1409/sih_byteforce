"""dashboard/components.py — presentational building blocks.

Two kinds of thing live here:

* **Markup helpers** (`status_bar`, `section_header`, `provenance_banner`,
  `methodology_strip`) return HTML strings for `st.markdown(unsafe_allow_html=True)`.
  They are CSS-only, so they render inline in the main document and inherit the
  page theme directly.

* **`kpi_strip`** returns a whole document for `st.components.v1.html`.
  It needs its own iframe because Streamlit strips `<script>` from markdown,
  and the count-up animation needs JavaScript. The palette is repeated inside
  that document for the same reason the hero repeats it — an iframe cannot see
  the parent's custom properties.

Everything is formatting. No data is fetched or computed here; `app.py` passes
in values it already has.
"""

from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass, field
from datetime import datetime

from dashboard.theme import FONT_STACK, MONO_STACK, PALETTE


def esc(value: object) -> str:
    """Escape untrusted text before it goes anywhere near innerHTML."""
    return _html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# KPI strip (component iframe — needs JS for the count-up)
# ---------------------------------------------------------------------------


@dataclass
class Kpi:
    """One KPI tile."""

    label: str
    value: float | str
    decimals: int = 2
    prefix: str = ""
    suffix: str = ""
    delta: str = ""
    #: "up" | "down" | "flat" — colours the delta pill.
    direction: str = "flat"
    #: Skip the count-up (dates and other non-quantities).
    animate: bool = True


def kpi_strip(items: list[Kpi], height: int = 150) -> str:
    """A row of KPI tiles whose numbers count up on load."""
    payload = json.dumps([
        {
            "label": k.label, "value": k.value, "decimals": k.decimals,
            "prefix": k.prefix, "suffix": k.suffix, "delta": k.delta,
            "direction": k.direction,
            "animate": bool(k.animate) and isinstance(k.value, (int, float)),
        }
        for k in items
    ])
    p = PALETTE

    return f"""
<!doctype html>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent;
                font-family: {FONT_STACK}; overflow: hidden; }}
  /* One row, always. The iframe height is fixed by Streamlit, so a wrapping
     grid would either clip or leave dead space; below ~1000px this scrolls
     horizontally instead, which no demo viewport hits. */
  #grid {{
    display: grid; gap: 16px;
    grid-auto-flow: column;
    grid-auto-columns: minmax(198px, 1fr);
    overflow-x: auto; overflow-y: hidden;
    scrollbar-width: thin;
  }}
  .kpi {{
    position: relative; overflow: hidden;
    padding: 16px 22px 18px;
    background: linear-gradient(180deg, rgba(19,33,54,.92), rgba(13,23,40,.92));
    border: 1px solid {p['border']};
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -8px rgba(0,0,0,.55);
    opacity: 0; transform: translateY(12px);
    animation: rise 520ms cubic-bezier(.22,.61,.36,1) forwards;
    transition: transform 260ms cubic-bezier(.22,.61,.36,1),
                box-shadow 260ms cubic-bezier(.22,.61,.36,1),
                border-color 260ms cubic-bezier(.22,.61,.36,1);
  }}
  .kpi::before {{
    content: ""; position: absolute; inset: 0 0 auto 0; height: 2px;
    background: linear-gradient(90deg, {p['accent']}, {p['blue']});
    opacity: .8;
  }}
  .kpi:hover {{
    transform: translateY(-3px);
    border-color: {p['border_bright']};
    box-shadow: 0 2px 6px rgba(0,0,0,.45), 0 18px 42px -12px rgba(0,0,0,.7),
                0 0 0 1px rgba(34,211,238,.18), 0 0 28px -6px rgba(34,211,238,.28);
  }}
  .label {{
    font-size: .705rem; font-weight: 640; letter-spacing: .13em;
    text-transform: uppercase; color: {p['text_dim']}; margin-bottom: 9px;
  }}
  .value {{
    font-size: clamp(1.7rem, 2.5vw, 2.3rem); font-weight: 700;
    letter-spacing: -.032em; line-height: 1.04; color: {p['text']};
    font-variant-numeric: tabular-nums;
  }}
  .affix {{ font-size: .56em; font-weight: 600; color: {p['text_muted']}; }}
  .delta {{
    display: inline-flex; align-items: center; gap: 5px; margin-top: 11px;
    font-size: .775rem; font-weight: 620; padding: 3px 10px; border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }}
  .delta.up   {{ color: {p['success']}; background: rgba(52,211,153,.12);  border: 1px solid rgba(52,211,153,.26); }}
  .delta.down {{ color: {p['danger']};  background: rgba(248,113,113,.12); border: 1px solid rgba(248,113,113,.26); }}
  .delta.flat {{ color: {p['text_muted']}; background: rgba(147,167,196,.10); border: 1px solid rgba(147,167,196,.20); }}

  @keyframes rise {{ to {{ opacity: 1; transform: none; }} }}

  @media (prefers-reduced-motion: reduce) {{
    .kpi {{ animation: none; opacity: 1; transform: none; }}
    .kpi:hover {{ transform: none; }}
  }}
</style>

<div id="grid"></div>

<script>
(function () {{
  "use strict";
  var ITEMS = {payload};
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var grid = document.getElementById("grid");

  function fmt(n, dp) {{
    return Number(n).toLocaleString("en-IN", {{
      minimumFractionDigits: dp, maximumFractionDigits: dp
    }});
  }}

  ITEMS.forEach(function (it, i) {{
    var card = document.createElement("div");
    card.className = "kpi";
    card.style.animationDelay = (i * 70) + "ms";

    var label = document.createElement("div");
    label.className = "label";
    label.textContent = it.label;

    var val = document.createElement("div");
    val.className = "value";

    var pre = it.prefix ? '<span class="affix">' + it.prefix + '</span>' : '';
    var suf = it.suffix ? '<span class="affix">' + it.suffix + '</span>' : '';

    if (!it.animate) {{
      val.innerHTML = pre + '<span class="num">' + it.value + '</span>' + suf;
    }} else {{
      val.innerHTML = pre + '<span class="num">' + fmt(0, it.decimals) + '</span>' + suf;
    }}

    card.appendChild(label);
    card.appendChild(val);

    if (it.delta) {{
      var d = document.createElement("span");
      d.className = "delta " + (it.direction || "flat");
      var glyph = it.direction === "up" ? "\\u2191"
                : it.direction === "down" ? "\\u2193" : "\\u00b7";
      d.textContent = glyph + "  " + it.delta;
      card.appendChild(d);
    }}
    grid.appendChild(card);

    if (!it.animate) return;

    var target = Number(it.value);
    var num = val.querySelector(".num");
    if (reduced || !isFinite(target)) {{ num.textContent = fmt(target, it.decimals); return; }}

    // Ease-out cubic: fast start, gentle settle. Long enough to be noticed,
    // short enough that a judge is never waiting on it.
    var DUR = 1100, delay = 140 + i * 70, t0 = null;
    function step(ts) {{
      if (t0 === null) t0 = ts;
      var e = Math.min(1, (ts - t0) / DUR);
      var k = 1 - Math.pow(1 - e, 3);
      num.textContent = fmt(target * k, it.decimals);
      if (e < 1) requestAnimationFrame(step);
      else num.textContent = fmt(target, it.decimals);
    }}
    setTimeout(function () {{ requestAnimationFrame(step); }}, delay);
  }});

}})();
</script>
"""


# ---------------------------------------------------------------------------
# Inline markup helpers (CSS-only, rendered in the main document)
# ---------------------------------------------------------------------------


def status_bar(
    last_updated: datetime,
    api_base: str,
    rows: int | None = None,
    extra: list[str] | None = None,
) -> str:
    """The live / last-updated strip. Reinforces the real-time positioning."""
    chips = [
        f'<span class="apx-chip live"><span class="apx-dot"></span>Live</span>',
        f'<span class="apx-chip">Updated {esc(last_updated.strftime("%H:%M:%S"))} '
        f'· {esc(last_updated.strftime("%d %b %Y"))}</span>',
    ]
    if rows is not None:
        chips.append(f'<span class="apx-chip">{rows:,} observations</span>')
    chips.append(f'<span class="apx-chip">API <code>{esc(api_base)}</code></span>')
    for e in extra or []:
        chips.append(f'<span class="apx-chip">{esc(e)}</span>')
    return f'<div class="apx-livebar">{"".join(chips)}</div>'


def section_header(number: str, title: str, note: str = "") -> str:
    """A numbered section heading with an optional explanatory line."""
    note_html = f'<p class="apx-section-note">{note}</p>' if note else ""
    return (
        f'<div class="apx-section">'
        f'<span class="apx-section-num">{esc(number)}</span>'
        f'<span class="apx-section-title">{esc(title)}</span>'
        f"</div>{note_html}"
    )


def provenance_banner(counts: dict[str, int] | None) -> str:
    """The permanent simulated-data banner (CLAUDE.md ground rule 5)."""
    counts = counts or {}
    detail = ", ".join(f"{k}: {v:,}" for k, v in sorted(counts.items())) or "—"
    return f"""
<div class="apx-banner">
  <div class="apx-banner-icon">⚠</div>
  <div>
    <div class="apx-banner-title">Simulated demo data — not real airline fares</div>
    <div class="apx-banner-body">
      Every fare behind these charts is synthetically generated. The index
      methodology, cleaning pipeline and scraper are real and would run
      unchanged on live data — see <code>docs/methodology.md</code> for the
      method and the real data-sourcing plan.
    </div>
    <div class="apx-banner-meta">Rows by source — {esc(detail)}</div>
  </div>
</div>
"""


@dataclass
class Step:
    """One stage in the methodology strip."""

    number: str
    title: str
    body: str
    detail: str = ""
    tags: list[str] = field(default_factory=list)


def methodology_strip(steps: list[Step]) -> str:
    """The "how it works" strip: collection → cleaning → index → validation.

    Judges probe methodology, so it is shown on the page rather than buried in
    a document nobody opens during a five-minute demo.
    """
    cards = []
    for s in steps:
        tags = "".join(
            f'<span class="apx-step-tag">{esc(t)}</span>' for t in s.tags
        )
        detail = f'<div class="apx-step-detail">{esc(s.detail)}</div>' if s.detail else ""
        cards.append(f"""
<div class="apx-step">
  <div class="apx-step-num">{esc(s.number)}</div>
  <div class="apx-step-title">{esc(s.title)}</div>
  <div class="apx-step-body">{esc(s.body)}</div>
  {detail}
  <div class="apx-step-tags">{tags}</div>
</div>""")
    return f'<div class="apx-steps">{"".join(cards)}</div>'
