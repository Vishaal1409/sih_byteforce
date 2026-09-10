"""dashboard/hero.py — the animated 3D route hero.

A Three.js scene showing the eight city-pairs APIx actually tracks, drawn as
arcs over a real India outline with aircraft traversing them. It is decoration,
but it is *honest* decoration: the routes and coordinates are the ones in
`config/routes.yaml`, not invented geometry.

Streamlit renders custom HTML inside a sandboxed iframe, so the scene is fully
self-contained — its own DOM, its own script, no access to the parent page.
That suits a fixed-height hero banner; the background colour is matched to the
page so the seam is invisible.

Two deliberate constraints:

* **No CDN.** `three.min.js` is vendored in `assets/` and inlined. A conference
  network is not something a demo should depend on.
* **Never blocks paint.** A skeleton renders immediately; the canvas fades in
  once the first frame is drawn. If WebGL is unavailable the skeleton is
  replaced by a static gradient and the page carries on.

`prefers-reduced-motion` is honoured: the scene renders one static frame with
the aircraft spaced along their routes, and no animation loop ever starts.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
THREE_JS = ASSETS / "three.min.js"
INDIA_OUTLINE = ASSETS / "india_outline.json"

#: Airport coordinates (lon, lat) for the six cities in the basket.
AIRPORTS = {
    "DEL": (77.100, 28.556),
    "BOM": (72.866, 19.090),
    "BLR": (77.707, 13.199),
    "MAA": (80.171, 12.994),
    "CCU": (88.447, 22.655),
    "HYD": (78.429, 17.240),
}

#: Projection centre — roughly the centroid of the Indian landmass.
CENTRE_LON, CENTRE_LAT = 82.8, 22.5


@lru_cache(maxsize=1)
def _three_js() -> str:
    if not THREE_JS.exists():
        raise FileNotFoundError(
            f"{THREE_JS} missing — the hero needs the vendored three.js build"
        )
    return THREE_JS.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _outline() -> list[list[float]]:
    return json.loads(INDIA_OUTLINE.read_text(encoding="utf-8"))


def _routes() -> list[dict]:
    """The tracked city-pairs, weighted, straight from config/routes.yaml.

    Falls back to the known basket if config cannot be read, so the hero can
    never be the thing that breaks the page.
    """
    try:
        from config_loader import load_routes

        return [
            {"o": r.origin, "d": r.dest, "w": r.traffic_weight, "name": r.name}
            for r in load_routes()
            if r.origin in AIRPORTS and r.dest in AIRPORTS
        ]
    except Exception:
        fallback = [
            ("DEL", "BOM", 0.19), ("DEL", "BLR", 0.16), ("BOM", "BLR", 0.13),
            ("DEL", "CCU", 0.12), ("DEL", "HYD", 0.11), ("DEL", "MAA", 0.10),
            ("BOM", "MAA", 0.10), ("BLR", "MAA", 0.09),
        ]
        return [{"o": o, "d": d, "w": w, "name": f"{o}-{d}"} for o, d, w in fallback]


def render(title: str, subtitle: str, eyebrow: str = "", height: int = 460) -> str:
    """Build the hero's standalone HTML document."""
    payload = json.dumps({
        "outline": _outline(),
        "airports": AIRPORTS,
        "routes": _routes(),
        "centre": [CENTRE_LON, CENTRE_LAT],
    })

    return f"""
<!doctype html>
<meta charset="utf-8">
<style>
  :root {{
    --bg: #070e1c; --surface: #0d1728; --border: #1e3050;
    --text: #e8f0fb; --muted: #93a7c4; --dim: #63799a;
    --accent: #22d3ee; --accent-soft: #67e8f9; --blue: #3b82f6;
    --font: "Inter","Segoe UI Variable Display","Segoe UI",system-ui,
            -apple-system,"Helvetica Neue",Arial,sans-serif;
    --mono: "JetBrains Mono","Cascadia Code",Consolas,"SF Mono",Menlo,monospace;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); overflow: hidden; }}

  #wrap {{
    position: relative; width: 100%; height: {height}px;
    border-radius: 16px; overflow: hidden;
    border: 1px solid var(--border);
    background:
      radial-gradient(900px 520px at 78% 8%, rgba(34,211,238,.16), transparent 62%),
      radial-gradient(760px 460px at 18% 96%, rgba(59,130,246,.16), transparent 60%),
      linear-gradient(160deg, #08111f 0%, #070e1c 55%, #050a14 100%);
    font-family: var(--font);
  }}
  /* Fine grid, for the "instrumentation" feel. */
  #wrap::after {{
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background-image:
      linear-gradient(rgba(43,68,104,.16) 1px, transparent 1px),
      linear-gradient(90deg, rgba(43,68,104,.16) 1px, transparent 1px);
    background-size: 46px 46px;
    mask-image: radial-gradient(circle at 62% 46%, #000 0%, transparent 78%);
    -webkit-mask-image: radial-gradient(circle at 62% 46%, #000 0%, transparent 78%);
  }}

  canvas {{
    position: absolute; inset: 0; width: 100%; height: 100%;
    opacity: 0; transition: opacity 700ms cubic-bezier(.22,.61,.36,1);
  }}
  canvas.ready {{ opacity: 1; }}

  /* ---- skeleton, shown until the first frame is drawn ---- */
  #skeleton {{
    position: absolute; inset: 0; display: grid; place-items: center;
    transition: opacity 420ms ease; z-index: 3;
  }}
  #skeleton.gone {{ opacity: 0; pointer-events: none; }}
  .sk-inner {{ display: flex; flex-direction: column; align-items: center; gap: 14px; }}
  .sk-ring {{
    width: 44px; height: 44px; border-radius: 50%;
    border: 2px solid rgba(34,211,238,.18);
    border-top-color: var(--accent);
    animation: spin 900ms linear infinite;
  }}
  .sk-text {{
    font-family: var(--mono); font-size: .74rem; letter-spacing: .16em;
    text-transform: uppercase; color: var(--dim);
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  /* ---- copy overlay ---- */
  #copy {{
    position: absolute; z-index: 4; left: 0; top: 0; height: 100%;
    display: flex; flex-direction: column; justify-content: center;
    padding: 0 clamp(22px, 4vw, 54px); max-width: 47%;
    pointer-events: none;
  }}
  .eyebrow {{
    display: inline-flex; align-items: center; gap: 8px; align-self: flex-start;
    font-family: var(--mono); font-size: .70rem; font-weight: 600;
    letter-spacing: .17em; text-transform: uppercase;
    color: var(--accent-soft);
    background: rgba(34,211,238,.09); border: 1px solid rgba(34,211,238,.28);
    padding: 5px 11px; border-radius: 999px; margin-bottom: 16px;
    opacity: 0; animation: rise 620ms cubic-bezier(.22,.61,.36,1) 90ms forwards;
  }}
  .eyebrow .dot {{
    width: 6px; height: 6px; border-radius: 50%; background: #34d399;
    box-shadow: 0 0 0 0 rgba(52,211,153,.6);
    animation: pulse 2.1s ease-out infinite;
  }}
  h1 {{
    margin: 0; font-size: clamp(1.85rem, 4.0vw, 3.35rem);
    line-height: 1.03; font-weight: 720; letter-spacing: -.033em;
    color: var(--text); text-wrap: balance;
    opacity: 0; animation: rise 700ms cubic-bezier(.22,.61,.36,1) 170ms forwards;
  }}
  h1 .grad {{
    background: linear-gradient(96deg, var(--accent) 0%, var(--blue) 62%, #a5b4fc 100%);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
  }}
  p.sub {{
    margin: 15px 0 0; max-width: 54ch;
    font-size: clamp(.90rem, 1.15vw, 1.04rem); line-height: 1.62;
    color: var(--muted);
    opacity: 0; animation: rise 700ms cubic-bezier(.22,.61,.36,1) 280ms forwards;
  }}
  #legend {{
    position: absolute; right: 18px; bottom: 15px; z-index: 4;
    display: flex; gap: 7px; flex-wrap: wrap; justify-content: flex-end;
    max-width: 54%; pointer-events: none;
    opacity: 0; animation: rise 700ms cubic-bezier(.22,.61,.36,1) 420ms forwards;
  }}
  .pill {{
    font-family: var(--mono); font-size: .655rem; letter-spacing: .07em;
    color: var(--muted); background: rgba(13,23,40,.72);
    border: 1px solid rgba(43,68,104,.85); border-radius: 999px;
    padding: 3px 9px; backdrop-filter: blur(6px);
  }}

  @keyframes rise {{ from {{ opacity: 0; transform: translateY(14px); }}
                     to   {{ opacity: 1; transform: none; }} }}
  @keyframes pulse {{
    0%   {{ box-shadow: 0 0 0 0   rgba(52,211,153,.55); }}
    70%  {{ box-shadow: 0 0 0 8px rgba(52,211,153,0);   }}
    100% {{ box-shadow: 0 0 0 0   rgba(52,211,153,0);   }}
  }}

  @media (max-width: 1100px) {{
    #copy {{ max-width: 70%; }}
    #legend {{ display: none; }}
  }}

  @media (prefers-reduced-motion: reduce) {{
    .eyebrow, h1, p.sub, #legend {{ animation: none !important; opacity: 1 !important; }}
    .sk-ring {{ animation: none; }}
    .dot {{ animation: none; }}
    canvas {{ transition: none; }}
  }}
</style>

<div id="wrap">
  <div id="skeleton">
    <div class="sk-inner">
      <div class="sk-ring"></div>
      <div class="sk-text">Initialising route map</div>
    </div>
  </div>
  <div id="copy">
    <span class="eyebrow"><span class="dot"></span>{eyebrow}</span>
    <h1>{title}</h1>
    <p class="sub">{subtitle}</p>
  </div>
  <div id="legend"></div>
</div>

<script>{_three_js()}</script>
<script>
(function () {{
  "use strict";
  var DATA = {payload};
  var wrap = document.getElementById("wrap");
  var skeleton = document.getElementById("skeleton");
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Route codes as small pills, so the visual is legibly tied to the basket.
  var legend = document.getElementById("legend");
  DATA.routes.forEach(function (r) {{
    var el = document.createElement("span");
    el.className = "pill";
    el.textContent = r.o + "\\u2013" + r.d;
    legend.appendChild(el);
  }});

  function giveUp(msg) {{
    // WebGL unavailable: keep the gradient and copy, drop the spinner.
    skeleton.classList.add("gone");
    if (window.console && msg) console.info("[apix-hero]", msg);
  }}

  if (typeof THREE === "undefined") {{ giveUp("three.js not loaded"); return; }}

  var renderer;
  try {{
    renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true }});
  }} catch (e) {{ giveUp("WebGL unavailable"); return; }}
  if (!renderer || !renderer.getContext()) {{ giveUp("no WebGL context"); return; }}

  var W = wrap.clientWidth, H = wrap.clientHeight;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(W, H);
  renderer.setClearColor(0x000000, 0);
  wrap.appendChild(renderer.domElement);

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(42, W / H, 0.1, 400);

  // --- projection: equirectangular, x scaled by cos(lat) so India is not
  // --- stretched. Map lies on XZ; arcs rise in +Y.
  var C = DATA.centre, K = Math.cos(C[1] * Math.PI / 180);
  function proj(lon, lat) {{
    return new THREE.Vector3((lon - C[0]) * K, 0, -(lat - C[1]));
  }}

  var group = new THREE.Group();
  // Tilt so the plate reads as ground rather than a flat sticker.
  group.rotation.x = -0.16;
  scene.add(group);

  // --- India outline ---------------------------------------------------
  var pts = DATA.outline.map(function (p) {{ return proj(p[0], p[1]); }});
  pts.push(pts[0].clone());

  var outlineGeo = new THREE.BufferGeometry().setFromPoints(pts);
  group.add(new THREE.Line(outlineGeo, new THREE.LineBasicMaterial({{
    color: 0x2ea8c9, transparent: true, opacity: 0.85
  }})));

  // Faint landmass fill, for body without drawing attention.
  try {{
    var shape = new THREE.Shape(DATA.outline.map(function (p) {{
      var v = proj(p[0], p[1]); return new THREE.Vector2(v.x, -v.z);
    }}));
    var fill = new THREE.Mesh(
      new THREE.ShapeGeometry(shape),
      new THREE.MeshBasicMaterial({{
        color: 0x0e2740, transparent: true, opacity: 0.55, side: THREE.DoubleSide
      }})
    );
    fill.rotation.x = -Math.PI / 2;
    fill.position.y = -0.045;
    group.add(fill);
  }} catch (e) {{ /* fill is optional */ }}

  // --- city nodes --------------------------------------------------------
  var nodeGeo = new THREE.SphereGeometry(0.28, 18, 18);
  var nodeMat = new THREE.MeshBasicMaterial({{ color: 0x67e8f9 }});
  var halos = [];
  Object.keys(DATA.airports).forEach(function (code) {{
    var a = DATA.airports[code], v = proj(a[0], a[1]);
    var dot = new THREE.Mesh(nodeGeo, nodeMat);
    dot.position.copy(v); dot.position.y = 0.06;
    group.add(dot);

    var halo = new THREE.Mesh(
      new THREE.RingGeometry(0.42, 0.60, 28),
      new THREE.MeshBasicMaterial({{
        color: 0x22d3ee, transparent: true, opacity: 0.5, side: THREE.DoubleSide
      }})
    );
    halo.rotation.x = -Math.PI / 2;
    halo.position.copy(v); halo.position.y = 0.05;
    halo.userData.phase = Math.random() * Math.PI * 2;
    group.add(halo); halos.push(halo);
  }});

  // --- route arcs + aircraft --------------------------------------------
  var planes = [];
  var planeGeo = new THREE.ConeGeometry(0.17, 0.62, 4);
  planeGeo.rotateX(Math.PI / 2);   // nose along +Z, so lookAt() orients it

  DATA.routes.forEach(function (r, i) {{
    var A = DATA.airports[r.o], B = DATA.airports[r.d];
    if (!A || !B) return;
    var a = proj(A[0], A[1]), b = proj(B[0], B[1]);

    // Great-circle feel: lift the control point with distance.
    var mid = a.clone().add(b).multiplyScalar(0.5);
    mid.y = a.distanceTo(b) * 0.42 + 0.9;
    var curve = new THREE.QuadraticBezierCurve3(a, mid, b);

    // Heavier routes read brighter and thicker.
    var weight = r.w || 0.1;
    var strength = Math.min(1, 0.42 + weight * 3.2);
    var arcMat = new THREE.LineBasicMaterial({{
      color: (i % 3 === 0) ? 0x22d3ee : (i % 3 === 1) ? 0x3b82f6 : 0x2dd4bf,
      transparent: true, opacity: 0.28 + strength * 0.42,
      blending: THREE.AdditiveBlending, depthWrite: false
    }});
    group.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(curve.getPoints(72)), arcMat));

    var plane = new THREE.Mesh(planeGeo, new THREE.MeshBasicMaterial({{
      color: 0xdff6ff, transparent: true, opacity: 0.96
    }}));
    group.add(plane);
    planes.push({{
      mesh: plane, curve: curve,
      t: i / DATA.routes.length,
      speed: 0.055 + weight * 0.16
    }});
  }});

  function placePlane(p) {{
    var pos = p.curve.getPointAt(p.t);
    var ahead = p.curve.getPointAt(Math.min(0.999, p.t + 0.012));
    p.mesh.position.copy(pos);
    p.mesh.lookAt(ahead);
  }}

  var CAM_Y = 30, CAM_Z = 21;

  function layout() {{
    camera.aspect = W / H;
    camera.updateProjectionMatrix();

    // Half-width of the view at the map's depth, in world units.
    var dist = Math.sqrt(CAM_Y * CAM_Y + CAM_Z * CAM_Z);
    var halfH = Math.tan((camera.fov * Math.PI / 180) / 2) * dist;
    var halfW = halfH * camera.aspect;

    // Sit the map in the right-hand half, clear of the copy column, and
    // scale it to whatever room that leaves.
    group.position.x = halfW * 0.40;
    group.position.z = -1.2;
    // Cap the scale so the southern tip and the arc apexes stay inside the
    // frame on a wide viewport instead of being clipped by the hero edge.
    var byWidth = (halfW * 0.55) / 15;
    var byHeight = (halfH * 1.05) / 15;
    var fit = Math.min(1.22, Math.max(0.70, Math.min(byWidth, byHeight)));
    group.scale.setScalar(fit);
  }}

  camera.position.set(0, CAM_Y, CAM_Z);
  camera.lookAt(0, 0, 0);
  layout();

  var t0 = performance.now();
  var revealed = false;
  function reveal() {{
    if (revealed) return;
    revealed = true;
    renderer.domElement.classList.add("ready");
    skeleton.classList.add("gone");
  }}

  function frame(now) {{
    var dt = Math.min(0.05, (now - t0) / 1000);
    t0 = now;
    var elapsed = now / 1000;

    planes.forEach(function (p) {{
      p.t += p.speed * dt;
      if (p.t > 1) p.t -= 1;
      placePlane(p);
    }});
    halos.forEach(function (h) {{
      var s = 1 + 0.22 * Math.sin(elapsed * 1.5 + h.userData.phase);
      h.scale.set(s, s, s);
      h.material.opacity = 0.30 + 0.24 * (1 + Math.sin(elapsed * 1.5 + h.userData.phase)) / 2;
    }});

    // Slow parallax drift — enough to feel alive, not enough to distract.
    group.rotation.y = 0.085 * Math.sin(elapsed * 0.13);
    camera.position.x = 1.3 * Math.sin(elapsed * 0.10);
    camera.position.y = CAM_Y + 0.9 * Math.sin(elapsed * 0.16);
    camera.lookAt(0, 0, 0);

    renderer.render(scene, camera);
    reveal();
    requestAnimationFrame(frame);
  }}

  if (reduced) {{
    // Static composition: aircraft spaced along their routes, no loop.
    planes.forEach(placePlane);
    renderer.render(scene, camera);
    reveal();
  }} else {{
    requestAnimationFrame(frame);
  }}

  // Fallback: never leave a spinner up if something silently stalls.
  setTimeout(reveal, 2500);

  var resizeTimer = null;
  window.addEventListener("resize", function () {{
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {{
      W = wrap.clientWidth; H = wrap.clientHeight;
      if (!W || !H) return;
      renderer.setSize(W, H);
      layout();
      if (reduced) renderer.render(scene, camera);
    }}, 120);
  }});
}})();
</script>
"""
