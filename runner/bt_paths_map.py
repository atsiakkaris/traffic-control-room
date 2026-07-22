#!/usr/bin/env python3
"""
bt_paths_map.py — standalone map of Bluetooth travel-time paths only.

Reads bt_path_coords and Bluetooth sensor_coords from the local SQLite DB and
writes a self-contained HTML page with a Leaflet map showing just the path
polylines plus the sensor inventory that anchors them — no other groups, no
health data. Useful for checking path geometry/routing in isolation from
everything else on the main dashboard.

Usage:
    python runner/bt_paths_map.py
    python runner/bt_paths_map.py --out reports/bt_paths_map.html
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("DB_PATH", str(Path(__file__).parent.parent / "results" / "history.db"))

# Load .env from repo root if present
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding='utf-8').splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _, _v = _line.partition('=')
            os.environ.setdefault(_k.strip(), _v.strip())

from db import fetch_bt_path_coords, fetch_sensor_coords, get_connection
from qa import load_reference, _haversine_m

REPORT_DIR = Path(__file__).parent.parent / "reports"
WORKBOOK = Path(__file__).parent.parent / "QA Locations.xlsx"

# Rotating categorical palette so adjacent/overlapping paths stay visually
# distinct without health data to color by.
PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4d4", "#f032e6", "#9acd32", "#fabed4", "#008080",
    "#dcbeff", "#9a6324", "#800000", "#aaffc3", "#808000",
    "#000075", "#1f4e79", "#c0392b", "#27ae60", "#e67e22",
]


# Geometric-overlap detection for colour assignment — deliberately ignores path
# names/IDs. The whole point of this map is to spot data problems, e.g. a pair
# that's *supposed* to be a simple reverse of each other but whose traced
# coordinates actually diverge — so "do these run alongside each other" has to
# be answered from real coordinates, not from the A->B naming convention.
SAMPLE_POINTS     = 16   # points sampled evenly along each path for proximity checks
# 25m and 50m both undershot real cases (a motorway's opposite carriageways, with a
# wide median and interchange ramps, traced up to ~100m apart). There's no clean gap
# in the data between "same road" and "different nearby road" separations, so this
# leans generous: a false "these run alongside each other" costs nothing worse than
# two unrelated paths getting different colours, while undershooting reproduces the
# exact same-colour-on-parallel-carriageways bug this exists to prevent.
OVERLAP_M         = 120  # metres apart counts as "running alongside"
OVERLAP_FRACTION  = 0.5  # fraction of a path's samples that must be this close to the other


def _sample_points(coords, n=SAMPLE_POINTS):
    if len(coords) <= n:
        return coords
    step = (len(coords) - 1) / (n - 1)
    return [coords[round(i * step)] for i in range(n)]


def _bbox(coords):
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return min(lats), max(lats), min(lons), max(lons)


def _bboxes_overlap(a, b, buffer_deg):
    a_min_lat, a_max_lat, a_min_lon, a_max_lon = a
    b_min_lat, b_max_lat, b_min_lon, b_max_lon = b
    return (a_min_lat - buffer_deg <= b_max_lat and b_min_lat - buffer_deg <= a_max_lat and
            a_min_lon - buffer_deg <= b_max_lon and b_min_lon - buffer_deg <= a_max_lon)


def _overlap_fraction(samples_a, samples_b, threshold_m):
    if not samples_a:
        return 0.0
    close = sum(
        1 for pa in samples_a
        if any(_haversine_m(pa[0], pa[1], pb[0], pb[1]) <= threshold_m for pb in samples_b)
    )
    return close / len(samples_a)


def assign_contrasting_colors(features):
    """Colour each path so that any two paths running alongside each other in
    real space — by any margin, however their names relate — never share a
    colour. Bounding-box pre-filtering keeps the pairwise geometry check cheap
    across hundreds of paths.
    """
    with_coords = [f for f in features if f["coords"]]
    samples = {f["id"]: _sample_points(f["coords"]) for f in with_coords}
    bboxes  = {f["id"]: _bbox(f["coords"]) for f in with_coords}
    buffer_deg = (OVERLAP_M + 100) / 111_000  # generous; exact check follows via haversine

    conflicts = {f["id"]: set() for f in features}
    ids = [f["id"] for f in with_coords]
    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1:]:
            if not _bboxes_overlap(bboxes[id_a], bboxes[id_b], buffer_deg):
                continue
            frac_a = _overlap_fraction(samples[id_a], samples[id_b], OVERLAP_M)
            frac_b = _overlap_fraction(samples[id_b], samples[id_a], OVERLAP_M)
            if max(frac_a, frac_b) >= OVERLAP_FRACTION:
                conflicts[id_a].add(id_b)
                conflicts[id_b].add(id_a)

    color_idx = {}
    for i, f in enumerate(features):
        used = {color_idx[pid] for pid in conflicts[f["id"]] if pid in color_idx}
        chosen = next((idx for idx in range(len(PALETTE)) if idx not in used), i % len(PALETTE))
        color_idx[f["id"]] = chosen
        f["color"] = PALETTE[chosen]


# Duplicate-registration detection ------------------------------------------
# Some path names carry two active path_ids in bt_path_coords — SWARCO
# re-registering a route under a new ID without retiring the old one.
# Coordinate overlap alone can't reliably separate a true duplicate from a
# coincidental name collision: the one confirmed non-duplicate found by hand
# (7084->7086, IDs 443/444) overlaps just as much on coordinates as the real
# duplicates do. What actually told them apart is pass/fail history — a true
# duplicate is the same measurement mirrored under two IDs, so it succeeds and
# fails on the exact same test runs; independent paths that merely share a
# name don't. Threshold matches stability.ATTENTION_FAIL_RATIO's convention.
DUPLICATE_STATUS_MATCH = 0.8


def find_duplicate_groups(paths):
    """Group active paths by name; for any name with 2+ ids, decide from status
    history whether it's a true duplicate (collapse to one) or two genuinely
    different paths sharing a name (keep both, flag for manual review).

    Returns (keep_ids, collapsed, ambiguous):
      keep_ids   — path_ids to actually render
      collapsed  — [(name, kept_id, dropped_ids, match_pct)]
      ambiguous  — [(name, ids, match_pct)] — same name, status diverges, kept
    """
    by_name = {}
    for pid, p in paths.items():
        by_name.setdefault(p.get('name'), []).append(pid)
    dup_groups = {name: ids for name, ids in by_name.items() if len(ids) > 1}

    keep_ids = set(paths.keys())
    collapsed, ambiguous = [], []
    if not dup_groups:
        return keep_ids, collapsed, ambiguous

    conn = get_connection()
    for name, ids in dup_groups.items():
        histories = {}
        for pid in ids:
            rows = conn.execute(
                "SELECT run_id, status FROM sensor_results "
                "WHERE group_name='Bluetooth Paths' AND sensor_id=?", (pid,)
            ).fetchall()
            histories[pid] = {r['run_id']: r['status'] for r in rows}

        base = ids[0]
        match_pcts = []
        for other in ids[1:]:
            common = set(histories[base]) & set(histories[other])
            if common:
                matching = sum(1 for r in common if histories[base][r] == histories[other][r])
                match_pcts.append(matching / len(common))
        match_pct = min(match_pcts) if match_pcts else 0.0

        if match_pct >= DUPLICATE_STATUS_MATCH:
            kept = max(ids, key=lambda pid: len(paths[pid].get('coords') or []))
            dropped = [pid for pid in ids if pid != kept]
            keep_ids -= set(dropped)
            collapsed.append((name, kept, dropped, match_pct))
        else:
            ambiguous.append((name, ids, match_pct))
    conn.close()

    return keep_ids, collapsed, ambiguous


def load_reference_sensors():
    """Bluetooth sensor rows from QA Locations.xlsx, with usable coordinates only."""
    if not WORKBOOK.exists():
        print(f"WARNING: {WORKBOOK.name} not found — skipping the spreadsheet sensor layer.")
        return []
    ref_sensors, _not_electrified = load_reference([f"{WORKBOOK}::Bluetooth"])
    return [s for s in ref_sensors if s.get('lat') is not None and s.get('lon') is not None]


def build_html(paths, sensors, ref_sensors, duplicate_groups=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ids = sorted(paths.keys())
    features = []
    for pid in ids:
        p = paths[pid]
        features.append({
            "id": pid,
            "name": p.get("name") or pid,
            "coords": p.get("coords") or [],
        })
    assign_contrasting_colors(features)
    features_json = json.dumps(features)
    duplicate_groups_json = json.dumps(duplicate_groups or {})

    sensor_list = [
        {"id": sid, "name": s.get("name") or sid, "lat": s["lat"], "lon": s["lon"],
         "site_code": s.get("site_code")}
        for sid, s in sorted(sensors.items())
    ]
    sensors_json = json.dumps(sensor_list)

    ref_list = [
        {"name": s["name"], "lat": s["lat"], "lon": s["lon"],
         "commissioning": s.get("commissioning", "active")}
        for s in ref_sensors
    ]
    ref_json = json.dumps(ref_list)

    all_pts = [c for f in features for c in f["coords"]]
    all_pts += [[s["lat"], s["lon"]] for s in sensor_list]
    all_pts += [[s["lat"], s["lon"]] for s in ref_list]
    if all_pts:
        center_lat = sum(c[0] for c in all_pts) / len(all_pts)
        center_lon = sum(c[1] for c in all_pts) / len(all_pts)
    else:
        center_lat, center_lon = 34.95, 33.15

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bluetooth Paths — Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden}}
body{{font-family:Arial,sans-serif;font-size:13px;background:#f4f6f9;color:#222;
      display:flex;flex-direction:column}}
header{{flex:0 0 auto;background:#1f4e79;color:#fff;padding:16px 24px}}
header h1{{font-size:20px;font-weight:bold}}
header p{{font-size:12px;opacity:.8;margin-top:4px}}
.summary{{flex:0 0 auto;display:flex;gap:12px;padding:16px 24px;flex-wrap:wrap}}
.card{{background:#fff;border-radius:6px;padding:14px 20px;min-width:130px;
       box-shadow:0 1px 4px rgba(0,0,0,.1);text-align:center;border-top:3px solid #1f4e79}}
.card .num{{font-size:26px;font-weight:bold;color:#1f4e79}}
.card .lbl{{font-size:11px;color:#666;margin-top:3px}}
#map{{flex:1 1 auto;min-height:0;margin:0 24px 24px;border-radius:6px;
      box-shadow:0 1px 6px rgba(0,0,0,.15)}}
.leaflet-popup-content b{{color:#1a1a2e}}
.sensor-toggle-btn{{background:#1f4e79;color:#fff;border:none;border-radius:4px;
      padding:8px 12px;font-size:12px;font-weight:bold;cursor:pointer;
      box-shadow:0 1px 4px rgba(0,0,0,.4);white-space:nowrap}}
.sensor-toggle-btn:hover{{background:#163a5f}}
.sensor-toggle-btn.off{{background:#9ca3af}}
.sensor-toggle-btn.off:hover{{background:#7d8590}}
.sensor-toggle-btn.ref{{background:#8e44ad;margin-top:6px}}
.sensor-toggle-btn.ref:hover{{background:#6c3483}}
.sensor-toggle-btn.ref.off{{background:#9ca3af}}
.sensor-toggle-btn.ref.off:hover{{background:#7d8590}}
.search-ctl{{position:absolute;top:12px;left:50%;transform:translateX(-50%);
      z-index:1000;width:320px}}
.search-ctl input{{width:100%;padding:8px 12px;border:none;border-radius:4px;font-size:13px;
      box-shadow:0 1px 4px rgba(0,0,0,.4);box-sizing:border-box}}
.search-results{{background:#fff;border-radius:4px;margin-top:4px;max-height:280px;overflow-y:auto;
      box-shadow:0 2px 8px rgba(0,0,0,.3);display:none}}
.search-results.visible{{display:block}}
.search-result{{padding:8px 12px;cursor:pointer;font-size:12px;border-bottom:1px solid #eee}}
.search-result:last-child{{border-bottom:none}}
.search-result:hover{{background:#f0f6ff}}
.search-result .type{{color:#888;font-size:10px;text-transform:uppercase;letter-spacing:.03em}}
.search-empty{{padding:8px 12px;font-size:12px;color:#888}}
</style>
</head>
<body>
<header>
  <h1>Bluetooth Paths — Map</h1>
  <p>Generated {now}&nbsp;&nbsp;|&nbsp;&nbsp;{len(features)} active paths&nbsp;&nbsp;|&nbsp;&nbsp;
     {len(sensor_list)} API sensors&nbsp;&nbsp;|&nbsp;&nbsp;{len(ref_list)} spreadsheet sensors</p>
</header>

<div class="summary">
  <div class="card"><div class="num">{len(features)}</div><div class="lbl">Bluetooth paths</div></div>
  <div class="card"><div class="num">{len(sensor_list)}</div><div class="lbl">API sensors</div></div>
  <div class="card"><div class="num">{len(ref_list)}</div><div class="lbl">Spreadsheet sensors</div></div>
</div>

<div id="map"></div>

<script>
var FEATURES = {features_json};
var SENSORS  = {sensors_json};
var REF_SENSORS = {ref_json};
var DUPLICATE_GROUPS = {duplicate_groups_json};

var map = L.map('map', {{zoomControl:false}}).setView([{center_lat}, {center_lon}], 9);
L.control.zoom({{position: 'topright'}}).addTo(map);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}}).addTo(map);

/* -- Paths: click to highlight ------------------------------------ */
var bounds = [];
var pathObjs = [];
var selectedId = null;

function baseStyle(f, selected) {{
  if (selected) return {{color: f.color, weight: 9, opacity: 1}};
  return {{color: f.color, weight: 4, opacity: 0.8}};
}}

function applySelection() {{
  pathObjs.forEach(function(o) {{
    var isSel = (o.feature.id === selectedId);
    o.polyline.setStyle(baseStyle(o.feature, isSel));
    if (isSel) {{ o.polyline.bringToFront(); o.decorator.bringToFront(); }}
  }});
}}

function selectPath(id) {{
  selectedId = (selectedId === id) ? null : id;
  applySelection();
}}

FEATURES.forEach(function(f) {{
  if (!f.coords.length) return;
  var latlngs = f.coords.map(function(c) {{ return [c[0], c[1]]; }});
  var pl = L.polyline(latlngs, {{color: f.color, weight: 4, opacity: 0.8}}).addTo(map);
  var popupHtml = '<b>' + f.name + '</b><br>Path ID: ' + f.id + '<br>' + f.coords.length + ' points';
  var dup = DUPLICATE_GROUPS[f.name];
  if (dup) {{
    var otherIds = dup.ids.filter(function(id) {{ return id !== f.id; }});
    popupHtml += '<br><span style="color:#c0392b;font-weight:bold">⚠ Duplicate registration</span>' +
                 '<br>Also registered as: ID ' + otherIds.join(', ID ') +
                 '<br>Status match: ' + Math.round(dup.match_pct * 100) + '%';
    if (!dup.confirmed) {{
      popupHtml += '<br><span style="color:#e67e22">Not a clean duplicate — needs manual check</span>';
    }}
  }}
  pl.bindPopup(popupHtml);
  pl.on('mouseover', function() {{ if (selectedId === null) pl.setStyle({{weight: 7}}); }});
  pl.on('mouseout',  function() {{ if (selectedId === null) pl.setStyle({{weight: 4}}); }});
  pl.on('click', function(e) {{ L.DomEvent.stopPropagation(e); selectPath(f.id); }});
  var decorator = L.polylineDecorator(pl, {{
    patterns: [{{
      offset: 20, repeat: 80,
      symbol: L.Symbol.arrowHead({{
        pixelSize: 9, headAngle: 40,
        pathOptions: {{color: '#333', fillOpacity: 0.8, weight: 0, fillColor: '#333', interactive: false}}
      }})
    }}]
  }}).addTo(map);
  pathObjs.push({{feature: f, polyline: pl, decorator: decorator}});
  bounds = bounds.concat(latlngs);
}});
map.on('click', function() {{ selectPath(null); }});

/* -- Bluetooth sensor inventory (API / local DB) -------------------- */
var sensorLayer = L.layerGroup();
var sensorMarkersById = {{}};
SENSORS.forEach(function(s) {{
  var m = L.circleMarker([s.lat, s.lon], {{
    radius: 6, color: '#fff', weight: 2, fillColor: '#1f4e79', fillOpacity: 0.95
  }});
  var rows = '<b>' + s.name + '</b><br>ID: ' + s.id;
  if (s.site_code) rows += '<br>Site: ' + s.site_code;
  rows += '<br>' + s.lat.toFixed(5) + ', ' + s.lon.toFixed(5);
  m.bindPopup(rows);
  m.addTo(sensorLayer);
  sensorMarkersById[s.id] = m;
  bounds.push([s.lat, s.lon]);
}});
sensorLayer.addTo(map);

function makeDiamondIcon(color) {{
  return L.divIcon({{
    className: '',
    html: '<div style="width:12px;height:12px;background:' + color + ';border:2px solid #fff;' +
          'box-shadow:0 1px 3px rgba(0,0,0,.4);transform:rotate(45deg)"></div>',
    iconSize: [12, 12], iconAnchor: [6, 6]
  }});
}}

/* -- Bluetooth sensor inventory (spreadsheet / reference) ----------- */
var COMMISSIONING_LABEL = {{not_electrified: 'Awaiting power', decommissioned: 'Decommissioned'}};
var refLayer = L.layerGroup();
var refMarkersByIdx = [];
REF_SENSORS.forEach(function(s, idx) {{
  var m = L.marker([s.lat, s.lon], {{icon: makeDiamondIcon('#8e44ad')}});
  var rows = '<b>' + s.name + '</b><br>Spreadsheet location';
  var statusLabel = COMMISSIONING_LABEL[s.commissioning];
  if (statusLabel) rows += '<br>Status: ' + statusLabel;
  rows += '<br>' + s.lat.toFixed(5) + ', ' + s.lon.toFixed(5);
  m.bindPopup(rows);
  m.addTo(refLayer);
  refMarkersByIdx[idx] = m;
  bounds.push([s.lat, s.lon]);
}});
refLayer.addTo(map);

function makeToggle(label, layer, extraClass) {{
  return L.Control.extend({{
    options: {{position: 'topright'}},
    onAdd: function() {{
      var btn = L.DomUtil.create('button', 'sensor-toggle-btn' + (extraClass ? ' ' + extraClass : ''));
      btn.innerHTML = label + ': ON';
      L.DomEvent.disableClickPropagation(btn);
      btn.onclick = function() {{
        if (map.hasLayer(layer)) {{
          map.removeLayer(layer);
          btn.innerHTML = label + ': OFF';
          btn.classList.add('off');
        }} else {{
          layer.addTo(map);
          btn.innerHTML = label + ': ON';
          btn.classList.remove('off');
        }}
      }};
      return btn;
    }}
  }});
}}
var apiToggleCtl = new (makeToggle('API sensors', sensorLayer))();
var refToggleCtl = new (makeToggle('Spreadsheet sensors', refLayer, 'ref'))();
map.addControl(apiToggleCtl);
map.addControl(refToggleCtl);

// Turns a toggleable layer back on (and syncs its button) if search jumps to
// something currently hidden — otherwise the marker/popup a search result
// points at wouldn't actually be visible.
function showLayer(layer, ctl, label) {{
  if (map.hasLayer(layer)) return;
  layer.addTo(map);
  var btn = ctl.getContainer();
  btn.innerHTML = label + ': ON';
  btn.classList.remove('off');
}}

/* -- Search: paths + both sensor inventories ------------------------ */
var SEARCH_INDEX = [];
FEATURES.forEach(function(f) {{
  SEARCH_INDEX.push({{type: 'Path', label: f.name, sub: 'ID ' + f.id, kind: 'path', id: f.id}});
}});
SENSORS.forEach(function(s) {{
  SEARCH_INDEX.push({{type: 'API sensor', label: s.name, sub: 'ID ' + s.id, kind: 'api', id: s.id}});
}});
REF_SENSORS.forEach(function(s, idx) {{
  SEARCH_INDEX.push({{type: 'Spreadsheet sensor', label: s.name, sub: '', kind: 'ref', idx: idx}});
}});

function goToSearchResult(item) {{
  if (item.kind === 'path') {{
    var o = pathObjs.filter(function(p) {{ return p.feature.id === item.id; }})[0];
    if (!o) return;
    selectPath(item.id);
    map.fitBounds(o.polyline.getBounds(), {{padding: [40, 40], maxZoom: 16}});
  }} else if (item.kind === 'api') {{
    showLayer(sensorLayer, apiToggleCtl, 'API sensors');
    var m = sensorMarkersById[item.id];
    map.setView(m.getLatLng(), 16);
    m.openPopup();
  }} else {{
    showLayer(refLayer, refToggleCtl, 'Spreadsheet sensors');
    var m2 = refMarkersByIdx[item.idx];
    map.setView(m2.getLatLng(), 16);
    m2.openPopup();
  }}
}}

(function() {{
    // Built as a plain overlay appended directly to the map container, not a
    // corner L.Control — Leaflet's corner boxes shrink-wrap to their content,
    // so a CSS left:50% inside one centers on its own tiny width, not the
    // map's. Appending straight to the map container (position:relative,
    // full map width) makes centering work correctly.
    var wrap = L.DomUtil.create('div', 'search-ctl', map.getContainer());
    L.DomEvent.disableClickPropagation(wrap);
    L.DomEvent.disableScrollPropagation(wrap);
    var input = L.DomUtil.create('input', '', wrap);
    input.type = 'text';
    input.placeholder = 'Search paths or sensors…';
    var results = L.DomUtil.create('div', 'search-results', wrap);

    function render(matches, query) {{
      results.innerHTML = '';
      if (!query) {{ results.classList.remove('visible'); return; }}
      if (!matches.length) {{
        results.innerHTML = '<div class="search-empty">No matches</div>';
      }} else {{
        matches.slice(0, 25).forEach(function(item) {{
          var row = L.DomUtil.create('div', 'search-result', results);
          row.innerHTML = '<div class="type">' + item.type + '</div>' + item.label +
                           (item.sub ? ' <span style="color:#999">(' + item.sub + ')</span>' : '');
          row.onclick = function() {{
            goToSearchResult(item);
            results.classList.remove('visible');
            input.value = item.label;
          }};
        }});
      }}
      results.classList.add('visible');
    }}

    input.addEventListener('input', function() {{
      var q = input.value.trim().toLowerCase();
      if (!q) {{ render([], ''); return; }}
      var matches = SEARCH_INDEX.filter(function(item) {{
        return item.label.toLowerCase().indexOf(q) !== -1 || item.sub.toLowerCase().indexOf(q) !== -1;
      }});
      render(matches, q);
    }});
}})();

if (bounds.length) map.fitBounds(bounds, {{padding: [20, 20]}});
</script>
</body>
</html>"""
    return page


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPORT_DIR / "bt_paths_map.html"))
    ap.add_argument("--show-duplicates", action="store_true",
                     help="Render every duplicate path registration instead of collapsing "
                          "confirmed duplicates down to one — useful for eyeballing them "
                          "on the map before removing any from the API.")
    args = ap.parse_args()

    paths = fetch_bt_path_coords()

    keep_ids, collapsed, ambiguous = find_duplicate_groups(paths)
    if collapsed:
        verb = "Found" if args.show_duplicates else "Collapsed"
        print(f"{verb} {len(collapsed)} duplicate path registration(s)"
              f"{' (kept 1 of each)' if not args.show_duplicates else ' — showing all, per --show-duplicates'}:")
        for name, kept, dropped, pct in collapsed:
            print(f"  {name}: kept {kept}, dropped {', '.join(dropped)}  (status match {pct*100:.0f}%)")
    if ambiguous:
        print(f"{len(ambiguous)} same-name group(s) left as-is — status diverges, needs manual check:")
        for name, ids, pct in ambiguous:
            print(f"  {name}: ids {', '.join(ids)}  (status match {pct*100:.0f}%)")
    if not args.show_duplicates:
        paths = {pid: p for pid, p in paths.items() if pid in keep_ids}

    duplicate_groups = {}
    for name, kept, dropped, pct in collapsed:
        duplicate_groups[name] = {"ids": [kept] + dropped, "match_pct": pct, "confirmed": True}
    for name, ids, pct in ambiguous:
        duplicate_groups[name] = {"ids": ids, "match_pct": pct, "confirmed": False}

    sensors = fetch_sensor_coords().get("Bluetooth", {})
    ref_sensors = load_reference_sensors()
    page = build_html(paths, sensors, ref_sensors, duplicate_groups)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(f"Bluetooth paths map written to {out_path}  "
          f"({len(paths)} active paths, {len(sensors)} API sensors, {len(ref_sensors)} spreadsheet sensors)")


if __name__ == "__main__":
    main()
