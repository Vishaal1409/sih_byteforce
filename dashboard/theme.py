"""dashboard/theme.py — the APIx visual design system.

Presentation only. Nothing in here fetches data, calls the API, or knows what
an index is; `dashboard/app.py` keeps all of that. Splitting it out means the
styling can be reworked without going anywhere near the data path.

Palette is a deep-navy aviation/fintech scheme with electric teal and blue
accents — deliberately not the purple-gradient look every SaaS template ships
with. Everything is expressed as CSS custom properties so a single value change
propagates through the whole page and the Plotly charts alike.

No webfonts and no CDN: the demo has to survive a bad conference network, so
typography rides on the system UI stack and every asset is local.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Palette — the single source of colour truth, shared with the Plotly theme
# ---------------------------------------------------------------------------

PALETTE = {
    # Surfaces, darkest to lightest
    "bg_deep": "#050a14",
    "bg": "#070e1c",
    "surface": "#0d1728",
    "surface_2": "#132136",
    "border": "#1e3050",
    "border_bright": "#2b4468",

    # Text
    "text": "#e8f0fb",
    "text_muted": "#93a7c4",
    "text_dim": "#63799a",

    # Accents — teal is the primary brand accent, blue the secondary
    "accent": "#22d3ee",
    "accent_soft": "#67e8f9",
    "accent_deep": "#0891b2",
    "blue": "#3b82f6",
    "blue_soft": "#60a5fa",

    # Semantic
    "success": "#34d399",
    "warn": "#fbbf24",
    "danger": "#f87171",
}

#: Categorical sequence for charts. Ordered so the first few are maximally
#: distinguishable, and every one is legible on the dark surface.
CHART_SEQUENCE = [
    "#22d3ee", "#3b82f6", "#34d399", "#fbbf24",
    "#f472b6", "#a78bfa", "#fb923c", "#2dd4bf",
]

#: Sequential scale for the sector heatmap: cool (cheap) to hot (expensive),
#: routed through the brand teal rather than a stock rainbow.
HEAT_SCALE = [
    [0.00, "#0b2f3f"],
    [0.20, "#0e5c6b"],
    [0.40, "#1c8f8a"],
    [0.55, "#4fb477"],
    [0.70, "#c9c05a"],
    [0.85, "#e08b4a"],
    [1.00, "#e5484d"],
]

FONT_STACK = (
    '"Inter","Segoe UI Variable Display","Segoe UI",system-ui,'
    '-apple-system,"Helvetica Neue",Arial,sans-serif'
)
MONO_STACK = '"JetBrains Mono","Cascadia Code",Consolas,"SF Mono",Menlo,monospace'


def css_variables() -> str:
    """The palette as CSS custom properties."""
    lines = [f"    --apx-{k.replace('_', '-')}: {v};" for k, v in PALETTE.items()]
    lines.append(f"    --apx-font: {FONT_STACK};")
    lines.append(f"    --apx-mono: {MONO_STACK};")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The stylesheet
# ---------------------------------------------------------------------------


def _stylesheet() -> str:
    return f"""
:root {{
{css_variables()}

    --apx-radius: 14px;
    --apx-radius-sm: 9px;
    --apx-gap: 18px;

    /* One spacing scale, used everywhere. */
    --apx-s1: 4px;  --apx-s2: 8px;  --apx-s3: 12px; --apx-s4: 16px;
    --apx-s5: 24px; --apx-s6: 32px; --apx-s7: 48px; --apx-s8: 64px;

    --apx-shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px -8px rgba(0,0,0,.55);
    --apx-shadow-lift: 0 2px 6px rgba(0,0,0,.45), 0 18px 42px -12px rgba(0,0,0,.7);
    --apx-glow: 0 0 0 1px rgba(34,211,238,.18), 0 0 28px -6px rgba(34,211,238,.28);

    --apx-ease: cubic-bezier(.22,.61,.36,1);
    --apx-dur: 260ms;
}}

/* ---------- page ground ------------------------------------------------ */

.stApp {{
    background:
        radial-gradient(1100px 620px at 12% -8%,  rgba(34,211,238,.11), transparent 60%),
        radial-gradient(900px  560px at 88%  0%,  rgba(59,130,246,.13), transparent 62%),
        radial-gradient(760px  520px at 50% 108%, rgba(8,145,178,.10),  transparent 60%),
        var(--apx-bg);
    background-attachment: fixed;
    color: var(--apx-text);
    font-family: var(--apx-font);
    font-feature-settings: "cv02","cv03","cv04","tnum";
}}

/* Streamlit's own chrome: keep it, make it disappear into the page. */
[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stToolbar"] {{ right: 12px; }}
[data-testid="stDecoration"] {{
    background: linear-gradient(90deg, var(--apx-accent), var(--apx-blue));
    height: 2px;
}}

.block-container,
[data-testid="stMainBlockContainer"] {{
    padding-top: var(--apx-s5) !important;
    padding-bottom: var(--apx-s8) !important;
    max-width: 1500px;
}}

/* ---------- typography -------------------------------------------------- */

h1, h2, h3, h4 {{
    font-family: var(--apx-font);
    color: var(--apx-text);
    letter-spacing: -.022em;
    font-weight: 680;
}}
h1 {{ font-size: clamp(1.9rem, 3.4vw, 2.9rem); line-height: 1.08; }}
h2 {{ font-size: clamp(1.25rem, 2vw, 1.6rem); }}
h3 {{ font-size: clamp(1.05rem, 1.5vw, 1.28rem); }}

p, li, span, label, div {{ color: var(--apx-text); }}

code, kbd, pre {{
    font-family: var(--apx-mono) !important;
    background: rgba(34,211,238,.09) !important;
    color: var(--apx-accent-soft) !important;
    border: 1px solid rgba(34,211,238,.16);
    border-radius: 6px;
    padding: .10em .40em;
    font-size: .86em;
}}
pre code {{ border: none; background: transparent !important; }}

[data-testid="stCaptionContainer"], .stCaption, small {{
    color: var(--apx-text-muted) !important;
    font-size: .845rem !important;
    line-height: 1.55;
}}

a, a:visited {{ color: var(--apx-accent-soft); text-decoration-color: rgba(34,211,238,.4); }}
a:hover {{ color: var(--apx-accent); }}

hr, [data-testid="stDivider"] {{
    border: none;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--apx-border), transparent);
    margin: var(--apx-s6) 0 var(--apx-s5);
}}

/* ---------- section headings ------------------------------------------- */

.apx-section {{
    display: flex; align-items: baseline; gap: var(--apx-s3);
    margin: 0 0 var(--apx-s2);
}}
.apx-section-num {{
    font-family: var(--apx-mono);
    font-size: .78rem; font-weight: 700; letter-spacing: .10em;
    color: var(--apx-accent);
    background: rgba(34,211,238,.10);
    border: 1px solid rgba(34,211,238,.26);
    border-radius: 6px; padding: 3px 9px;
    white-space: nowrap;
}}
.apx-section-title {{
    font-size: clamp(1.12rem, 1.7vw, 1.42rem);
    font-weight: 660; letter-spacing: -.02em; color: var(--apx-text);
}}
.apx-section-note {{
    color: var(--apx-text-muted); font-size: .875rem; line-height: 1.6;
    margin: 0 0 var(--apx-s4); max-width: 88ch;
}}

/* ---------- cards & glass ---------------------------------------------- */

.apx-card {{
    background: linear-gradient(180deg, rgba(19,33,54,.86), rgba(13,23,40,.86));
    border: 1px solid var(--apx-border);
    border-radius: var(--apx-radius);
    box-shadow: var(--apx-shadow);
    backdrop-filter: blur(9px) saturate(120%);
    -webkit-backdrop-filter: blur(9px) saturate(120%);
    transition: transform var(--apx-dur) var(--apx-ease),
                box-shadow var(--apx-dur) var(--apx-ease),
                border-color var(--apx-dur) var(--apx-ease);
}}
.apx-card:hover {{
    transform: translateY(-2px);
    border-color: var(--apx-border-bright);
    box-shadow: var(--apx-shadow-lift), var(--apx-glow);
}}

/* ---------- KPI cards --------------------------------------------------- */

.apx-kpis {{
    display: grid; gap: var(--apx-gap);
    grid-template-columns: repeat(auto-fit, minmax(215px, 1fr));
    margin: var(--apx-s2) 0 var(--apx-s4);
}}
.apx-kpi {{
    position: relative; overflow: hidden;
    padding: var(--apx-s4) var(--apx-s5) calc(var(--apx-s4) + 2px);
    background: linear-gradient(180deg, rgba(19,33,54,.9), rgba(13,23,40,.9));
    border: 1px solid var(--apx-border);
    border-radius: var(--apx-radius);
    box-shadow: var(--apx-shadow);
    transition: transform var(--apx-dur) var(--apx-ease),
                box-shadow var(--apx-dur) var(--apx-ease),
                border-color var(--apx-dur) var(--apx-ease);
}}
.apx-kpi::before {{
    content: ""; position: absolute; inset: 0 0 auto 0; height: 2px;
    background: linear-gradient(90deg, var(--apx-accent), var(--apx-blue));
    opacity: .75;
}}
.apx-kpi:hover {{
    transform: translateY(-3px);
    border-color: var(--apx-border-bright);
    box-shadow: var(--apx-shadow-lift), var(--apx-glow);
}}
.apx-kpi-label {{
    font-size: .715rem; font-weight: 640; letter-spacing: .13em;
    text-transform: uppercase; color: var(--apx-text-dim);
    margin-bottom: var(--apx-s2);
}}
.apx-kpi-value {{
    font-size: clamp(1.75rem, 2.6vw, 2.35rem);
    font-weight: 700; letter-spacing: -.032em; line-height: 1.05;
    color: var(--apx-text);
    font-variant-numeric: tabular-nums;
}}
.apx-kpi-unit {{ font-size: .58em; font-weight: 600; color: var(--apx-text-muted); margin-left: 3px; }}
.apx-kpi-delta {{
    display: inline-flex; align-items: center; gap: 5px;
    margin-top: var(--apx-s3);
    font-size: .78rem; font-weight: 620;
    padding: 3px 9px; border-radius: 999px;
    font-variant-numeric: tabular-nums;
}}
.apx-kpi-delta.up   {{ color: var(--apx-success); background: rgba(52,211,153,.12); border: 1px solid rgba(52,211,153,.26); }}
.apx-kpi-delta.down {{ color: var(--apx-danger);  background: rgba(248,113,113,.12); border: 1px solid rgba(248,113,113,.26); }}
.apx-kpi-delta.flat {{ color: var(--apx-text-muted); background: rgba(147,167,196,.10); border: 1px solid rgba(147,167,196,.20); }}

/* ---------- provenance banner ------------------------------------------ */

.apx-banner {{
    position: relative;
    display: flex; gap: var(--apx-s4); align-items: flex-start;
    padding: var(--apx-s4) var(--apx-s5);
    margin: var(--apx-s4) 0 var(--apx-s5);
    border-radius: var(--apx-radius);
    background: linear-gradient(100deg, rgba(120,20,28,.42), rgba(78,14,22,.30));
    border: 1px solid rgba(248,113,113,.40);
    box-shadow: 0 0 0 1px rgba(248,113,113,.07), 0 14px 34px -16px rgba(180,30,40,.6);
}}
.apx-banner::before {{
    content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 3px;
    border-radius: 3px; background: var(--apx-danger);
}}
.apx-banner-icon {{ font-size: 1.28rem; line-height: 1.2; flex-shrink: 0; }}
.apx-banner-title {{
    font-weight: 720; letter-spacing: .045em; font-size: .875rem;
    color: #ffd9d9; text-transform: uppercase; margin-bottom: 5px;
}}
.apx-banner-body {{ font-size: .875rem; line-height: 1.6; color: #f3dede; }}
.apx-banner-meta {{
    margin-top: var(--apx-s2); font-family: var(--apx-mono);
    font-size: .76rem; color: rgba(255,220,220,.72);
}}

/* ---------- live badge -------------------------------------------------- */

.apx-livebar {{
    display: flex; flex-wrap: wrap; align-items: center; gap: var(--apx-s3);
    margin: var(--apx-s2) 0 var(--apx-s4);
}}
.apx-chip {{
    display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 12px; border-radius: 999px;
    font-size: .765rem; font-weight: 600; letter-spacing: .015em;
    background: rgba(19,33,54,.75);
    border: 1px solid var(--apx-border);
    color: var(--apx-text-muted);
    backdrop-filter: blur(6px);
}}
.apx-chip.live {{
    color: var(--apx-accent-soft);
    border-color: rgba(34,211,238,.34);
    background: rgba(34,211,238,.09);
}}
.apx-chip code {{ background: none !important; border: none; padding: 0; color: inherit !important; }}
.apx-dot {{
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--apx-success);
    box-shadow: 0 0 0 0 rgba(52,211,153,.6);
    animation: apx-pulse 2.1s var(--apx-ease) infinite;
}}
@keyframes apx-pulse {{
    0%   {{ box-shadow: 0 0 0 0   rgba(52,211,153,.55); }}
    70%  {{ box-shadow: 0 0 0 9px rgba(52,211,153,0);   }}
    100% {{ box-shadow: 0 0 0 0   rgba(52,211,153,0);   }}
}}

/* ---------- Streamlit widgets ------------------------------------------ */

[data-testid="stAlert"] {{
    border-radius: var(--apx-radius-sm);
    border: 1px solid var(--apx-border);
    background: rgba(19,33,54,.72);
    backdrop-filter: blur(6px);
    color: var(--apx-text);
}}
[data-testid="stAlert"] p, [data-testid="stAlert"] div {{ color: var(--apx-text) !important; }}

.stButton > button {{
    background: linear-gradient(180deg, var(--apx-accent), var(--apx-accent-deep));
    color: #04222b;
    font-weight: 680; letter-spacing: .012em;
    border: 1px solid rgba(34,211,238,.5);
    border-radius: var(--apx-radius-sm);
    padding: .58rem 1.1rem;
    box-shadow: 0 6px 18px -8px rgba(34,211,238,.65);
    transition: transform 160ms var(--apx-ease), box-shadow var(--apx-dur) var(--apx-ease),
                filter var(--apx-dur) var(--apx-ease);
}}
.stButton > button:hover {{
    transform: translateY(-1px);
    filter: brightness(1.08);
    box-shadow: 0 10px 26px -8px rgba(34,211,238,.8);
}}
.stButton > button:active {{ transform: translateY(0); }}
.stButton > button:focus-visible {{ outline: 2px solid var(--apx-accent-soft); outline-offset: 2px; }}

[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
[data-testid="stNumberInput"] input,
[data-testid="stTextInput"] input {{
    background: var(--apx-surface) !important;
    border-color: var(--apx-border) !important;
    color: var(--apx-text) !important;
    border-radius: var(--apx-radius-sm) !important;
}}
[data-testid="stSelectbox"] div[data-baseweb="select"] > div:hover,
[data-testid="stNumberInput"] input:hover {{ border-color: var(--apx-border-bright) !important; }}

div[data-baseweb="popover"] li {{ background: var(--apx-surface) !important; color: var(--apx-text) !important; }}
div[data-baseweb="popover"] li:hover {{ background: var(--apx-surface-2) !important; }}

[data-testid="stRadio"] label {{ color: var(--apx-text-muted) !important; }}
[data-testid="stRadio"] label:hover {{ color: var(--apx-text) !important; }}

[data-testid="stExpander"] {{
    border: 1px solid var(--apx-border);
    border-radius: var(--apx-radius);
    background: rgba(13,23,40,.6);
    overflow: hidden;
}}
[data-testid="stExpander"] summary:hover {{ color: var(--apx-accent-soft); }}

[data-testid="stDataFrame"] {{
    border: 1px solid var(--apx-border);
    border-radius: var(--apx-radius-sm);
    overflow: hidden;
}}

[data-testid="stMetricValue"] {{ color: var(--apx-text); font-variant-numeric: tabular-nums; }}
[data-testid="stMetricLabel"] {{ color: var(--apx-text-dim); }}

/* Charts sit on the page ground, not on a white card. */
[data-testid="stPlotlyChart"] {{
    border: 1px solid var(--apx-border);
    border-radius: var(--apx-radius);
    background: rgba(13,23,40,.55);
    padding: var(--apx-s2);
    box-shadow: var(--apx-shadow);
    transition: border-color var(--apx-dur) var(--apx-ease),
                box-shadow var(--apx-dur) var(--apx-ease);
}}
[data-testid="stPlotlyChart"]:hover {{
    border-color: var(--apx-border-bright);
    box-shadow: var(--apx-shadow-lift);
}}

[data-testid="stSpinner"] > div {{ border-top-color: var(--apx-accent) !important; }}

::-webkit-scrollbar {{ width: 11px; height: 11px; }}
::-webkit-scrollbar-track {{ background: var(--apx-bg-deep); }}
::-webkit-scrollbar-thumb {{ background: #1d3050; border-radius: 6px; border: 2px solid var(--apx-bg-deep); }}
::-webkit-scrollbar-thumb:hover {{ background: #2b4468; }}

::selection {{ background: rgba(34,211,238,.28); color: #fff; }}

/* ---------- responsive -------------------------------------------------- */

/* Projector / laptop at ~1366px: tighten padding, keep four KPIs on one row. */
@media (max-width: 1400px) {{
    .block-container, [data-testid="stMainBlockContainer"] {{
        padding-left: var(--apx-s5) !important;
        padding-right: var(--apx-s5) !important;
    }}
    .apx-kpis {{ grid-template-columns: repeat(auto-fit, minmax(185px, 1fr)); gap: 14px; }}
    .apx-kpi {{ padding: var(--apx-s3) var(--apx-s4); }}
}}
@media (max-width: 900px) {{
    .apx-kpis {{ grid-template-columns: repeat(2, 1fr); }}
    .apx-banner {{ flex-direction: column; gap: var(--apx-s2); }}
}}

/* ---------- accessibility ----------------------------------------------- */

@media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{
        animation-duration: .001ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: .001ms !important;
        scroll-behavior: auto !important;
    }}
    .apx-card:hover, .apx-kpi:hover, .stButton > button:hover {{ transform: none; }}
}}
"""


def inject_theme() -> str:
    """The `<style>` block. Caller passes this to `st.markdown(unsafe_allow_html=True)`."""
    return f"<style>{_stylesheet()}</style>"


# ---------------------------------------------------------------------------
# Plotly
# ---------------------------------------------------------------------------


def register_plotly_theme(name: str = "apix_dark") -> str:
    """Register the APIx Plotly template and make it the default.

    Charts then inherit the page palette without each figure repeating layout
    settings. Returns the template name so callers can be explicit.
    """
    import plotly.graph_objects as go
    import plotly.io as pio

    p = PALETTE
    pio.templates[name] = go.layout.Template(
        layout=dict(
            font=dict(family=FONT_STACK, size=13, color=p["text_muted"]),
            title=dict(font=dict(size=15, color=p["text"])),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            colorway=CHART_SEQUENCE,
            margin=dict(t=28, b=44, l=60, r=24),
            xaxis=dict(
                gridcolor="rgba(43,68,104,.34)",
                zerolinecolor="rgba(43,68,104,.55)",
                linecolor="rgba(43,68,104,.75)",
                tickcolor="rgba(43,68,104,.75)",
                tickfont=dict(color=p["text_dim"], size=11.5),
                title=dict(font=dict(color=p["text_muted"], size=12.5)),
                showspikes=True,
                spikemode="across",
                spikethickness=1,
                spikecolor="rgba(34,211,238,.45)",
                spikedash="dot",
            ),
            yaxis=dict(
                gridcolor="rgba(43,68,104,.34)",
                zerolinecolor="rgba(43,68,104,.55)",
                linecolor="rgba(43,68,104,.0)",
                tickfont=dict(color=p["text_dim"], size=11.5),
                title=dict(font=dict(color=p["text_muted"], size=12.5)),
            ),
            hoverlabel=dict(
                bgcolor="rgba(13,23,40,.96)",
                bordercolor=p["border_bright"],
                font=dict(family=FONT_STACK, size=12.5, color=p["text"]),
                align="left",
            ),
            legend=dict(
                bgcolor="rgba(0,0,0,0)",
                font=dict(color=p["text_muted"], size=12),
                borderwidth=0,
            ),
            colorscale=dict(sequential=HEAT_SCALE),
            coloraxis=dict(
                colorbar=dict(
                    outlinewidth=0,
                    tickfont=dict(color=p["text_dim"], size=11),
                    title=dict(font=dict(color=p["text_muted"], size=12)),
                )
            ),
        )
    )
    pio.templates.default = name
    return name
