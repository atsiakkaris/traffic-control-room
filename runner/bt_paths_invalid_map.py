#!/usr/bin/env python3
"""
bt_paths_invalid_map.py — map of Bluetooth paths that have NEVER reported valid data.

Reads sensor_results.data (the raw per-run speed/travel-time JSON, populated
whenever LIVE_MODE=true) for every path in the "Bluetooth Paths" group and
finds the ones where speed_kmh has been -1 (the API's malfunctioning sentinel)
on every single run recorded — never once a real, positive speed. Retired
paths (no longer in the live API) are excluded — there's nothing left to
action on them here. Renders the rest on a Leaflet map, each carrying its
live-data history (run count, first/last seen, latest values), with a
flag/note workflow to mark which ones to actually remove later.

Usage:
    python runner/bt_paths_invalid_map.py
    python runner/bt_paths_invalid_map.py --out reports/bt_paths_invalid_map.html
"""

import argparse
import json
import os
import sys
from datetime import datetime
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
from qa import load_reference
from stability import CYPRUS_TZ

REPORT_DIR = Path(__file__).parent.parent / "reports"
WORKBOOK = Path(__file__).parent.parent / "QA Locations.xlsx"


def find_always_invalid_paths():
    """path_id -> live-data history for every path whose recorded speed_kmh
    has been -1 on every run it has ever had data for. A path with zero
    recorded runs (LIVE_MODE was off, or it's brand new) is excluded — no
    evidence either way, not evidence of malfunction.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT sensor_id, run_at, data FROM sensor_results "
        "WHERE group_name='Bluetooth Paths' AND data IS NOT NULL ORDER BY sensor_id, run_at"
    ).fetchall()
    conn.close()

    by_path = {}
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        by_path.setdefault(r["sensor_id"], []).append(
            (r["run_at"], d.get("speed_kmh"), d.get("travel_time_s"))
        )

    result = {}
    for pid, entries in by_path.items():
        speeds = [e[1] for e in entries]
        if speeds and all(s == -1 for s in speeds):
            first_at, _, _ = entries[0]
            last_at, last_spd, last_tt = entries[-1]
            result[pid] = {
                "runs": len(entries),
                "first_seen": first_at,
                "last_seen": last_at,
                "latest_speed": last_spd,
                "latest_travel_time": last_tt,
            }
    return result


def load_reference_sensors():
    """Bluetooth sensor rows from QA Locations.xlsx, with usable coordinates only."""
    if not WORKBOOK.exists():
        print(f"WARNING: {WORKBOOK.name} not found — skipping the spreadsheet sensor layer.")
        return []
    ref_sensors, _not_electrified = load_reference([f"{WORKBOOK}::Bluetooth"])
    return [s for s in ref_sensors if s.get('lat') is not None and s.get('lon') is not None]


def build_html(invalid_paths, all_paths, sensors, ref_sensors):
    now = datetime.now(CYPRUS_TZ).strftime("%Y-%m-%d %H:%M")
    ids = sorted(invalid_paths.keys())
    features = []
    for pid in ids:
        p = all_paths.get(pid, {})
        info = invalid_paths[pid]
        features.append({
            "id": pid,
            "name": p.get("name") or pid,
            "coords": p.get("coords") or [],
            "runs": info["runs"],
            "first_seen": info["first_seen"],
            "last_seen": info["last_seen"],
            "latest_speed": info["latest_speed"],
            "latest_travel_time": info["latest_travel_time"],
        })
    features_json = json.dumps(features)
    no_geometry_count = sum(1 for f in features if not f["coords"])

    sensor_list = [
        {"id": sid, "name": s.get("name") or sid, "lat": s["lat"], "lon": s["lon"],
         "site_code": s.get("site_code")}
        for sid, s in sorted(sensors.items())
    ]
    sensors_json = json.dumps(sensor_list)

    ref_list = [
        {"name": s["name"], "lat": s["lat"], "lon": s["lon"]}
        for s in ref_sensors
    ]
    ref_json = json.dumps(ref_list)

    center_lat, center_lon = 34.95, 33.15

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bluetooth Paths — Always Invalid (-1)</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden}}
body{{font-family:Arial,sans-serif;font-size:13px;background:#f4f6f9;color:#222;
      display:flex;flex-direction:column}}
header{{flex:0 0 auto;background:#7f1d1d;color:#fff;padding:16px 24px}}
header h1{{font-size:20px;font-weight:bold}}
header p{{font-size:12px;opacity:.85;margin-top:4px}}
.summary{{flex:0 0 auto;display:flex;gap:12px;padding:16px 24px;flex-wrap:wrap}}
.card{{background:#fff;border-radius:6px;padding:14px 20px;min-width:130px;
       box-shadow:0 1px 4px rgba(0,0,0,.1);text-align:center;border-top:3px solid #7f1d1d}}
.card .num{{font-size:26px;font-weight:bold;color:#7f1d1d}}
.card .lbl{{font-size:11px;color:#666;margin-top:3px}}
.card.nogeo{{border-top-color:#b45309}}
.card.nogeo .num{{color:#b45309}}
#mapFsWrap{{flex:1 1 auto;min-height:0;margin:0 24px 24px;display:flex}}
#mapFsWrap:fullscreen{{margin:0;padding:12px;background:#f4f6f9;box-sizing:border-box}}
#mapFsWrap:-webkit-full-screen{{margin:0;padding:12px;background:#f4f6f9;box-sizing:border-box}}
#map{{flex:1 1 auto;min-height:0;border-radius:6px;
      box-shadow:0 1px 6px rgba(0,0,0,.15)}}
.leaflet-popup-content b{{color:#1a1a2e}}
.sensor-toggle-btn{{background:#1f4e79;color:#fff;border:none;border-radius:4px;
      padding:8px 12px;font-size:12px;font-weight:bold;cursor:pointer;
      box-shadow:0 1px 4px rgba(0,0,0,.4);white-space:nowrap}}
.sensor-toggle-btn:hover{{background:#163a5f}}
.sensor-toggle-btn.off{{background:#9ca3af}}
.sensor-toggle-btn.off:hover{{background:#7d8590}}
.sensor-toggle-btn.ref{{background:#8e44ad}}
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
.search-empty{{padding:8px 12px;font-size:12px;color:#888}}
.inspector{{position:absolute;left:12px;bottom:12px;z-index:1000;width:300px;max-height:60%;
      display:flex;flex-direction:column;background:#fff;border-radius:8px;
      box-shadow:0 2px 10px rgba(0,0,0,.35);font-size:12px;overflow:hidden}}
.inspector-hd{{flex:0 0 auto;background:#7f1d1d;color:#fff;padding:7px 10px;
      display:flex;align-items:center;justify-content:space-between;gap:8px;font-weight:bold}}
.inspector-hd .nav{{display:flex;align-items:center;gap:6px;font-weight:normal}}
.inspector-hd .nav button{{background:rgba(255,255,255,.18);border:none;color:#fff;border-radius:4px;
      padding:3px 9px;cursor:pointer;font-size:12px}}
.inspector-hd .nav button:hover{{background:rgba(255,255,255,.32)}}
.inspector-hd .nav span{{font-size:11px;opacity:.9;min-width:52px;text-align:center}}
.inspector-body{{padding:10px 12px;overflow-y:auto}}
.inspector-body .empty{{color:#888;font-style:italic}}
.inspector-body table{{width:100%;border-collapse:collapse;margin-top:8px}}
.inspector-body td{{padding:3px 0;font-size:12px;vertical-align:top}}
.inspector-body td.k{{color:#666;white-space:nowrap;padding-right:8px}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:bold;
      margin:6px 4px 0 0}}
.badge.nogeo{{background:#fed7aa;color:#b45309}}
.sensor-toggle-btn.export{{background:#27ae60}}
.sensor-toggle-btn.export:hover{{background:#1e8449}}
.sensor-toggle-btn.import{{background:#3949ab}}
.sensor-toggle-btn.import:hover{{background:#2c3a82}}
.flag-summary{{display:flex;align-items:center;gap:4px;font-size:11px;color:#555;
      padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid #eee;flex-wrap:wrap}}
.flag-summary-link{{color:#7f1d1d;text-decoration:underline;margin-left:auto;cursor:pointer}}
.flag-list-row{{padding:7px 4px;font-size:12px;cursor:pointer;border-bottom:1px solid #f0f0f0}}
.flag-list-row:hover{{background:#f4f6f9}}
.flag-list-id{{color:#888;font-size:11px}}
.flag-row{{display:flex;gap:6px;margin-top:9px}}
.flag-row button{{flex:1;border:1px solid #ddd;background:#f8f9fa;border-radius:4px;padding:5px 4px;
      font-size:11px;cursor:pointer}}
.flag-row button:hover{{background:#eef1f4}}
.flag-row button.active-flag{{background:#c0392b;color:#fff;border-color:#c0392b}}
.flag-row button.active-ok{{background:#1d9e75;color:#fff;border-color:#1d9e75}}
.inspector-body textarea{{width:100%;margin-top:6px;font-size:11px;padding:5px;border:1px solid #ddd;
      border-radius:4px;resize:vertical;min-height:36px;font-family:inherit}}
</style>
</head>
<body>
<header>
  <h1>Bluetooth Paths — Always Invalid (speed = -1)</h1>
  <p>Generated {now}&nbsp;&nbsp;|&nbsp;&nbsp;{len(features)} path(s) have never once reported a valid speed</p>
</header>

<div class="summary">
  <div class="card"><div class="num">{len(features)}</div><div class="lbl">Always -1 (active in API)</div></div>
  {f'<div class="card nogeo"><div class="num">{no_geometry_count}</div><div class="lbl">No geometry on record</div></div>' if no_geometry_count else ''}
</div>

<div id="mapFsWrap"><div id="map"></div></div>

<script>
var FEATURES = {features_json};
var SENSORS  = {sensors_json};
var REF_SENSORS = {ref_json};

var DEFAULT_VIEW = {{center: [{center_lat}, {center_lon}], zoom: 9}};
var map = L.map('map', {{zoomControl:false}}).setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

var FullscreenControl = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
    var link = L.DomUtil.create('a', '', container);
    link.href = '#';
    link.id = 'mapFsBtn';
    link.title = 'Toggle full screen';
    link.innerHTML = '<i class="ti ti-arrows-maximize" style="font-size:15px;line-height:26px"></i>';
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.on(link, 'click', function(e) {{
      L.DomEvent.preventDefault(e);
      toggleMapFullscreen();
    }});
    return container;
  }}
}});
map.addControl(new FullscreenControl());

var RecentreControl = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
    var link = L.DomUtil.create('a', '', container);
    link.href = '#';
    link.title = 'Recentre map';
    link.innerHTML = '<i class="ti ti-crosshair" style="font-size:15px;line-height:26px"></i>';
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.on(link, 'click', function(e) {{
      L.DomEvent.preventDefault(e);
      map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
    }});
    return container;
  }}
}});
map.addControl(new RecentreControl());
L.control.zoom({{position: 'topright'}}).addTo(map);

function toggleMapFullscreen() {{
  var el = document.getElementById('mapFsWrap');
  var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  if (!fsEl) {{
    (el.requestFullscreen || el.webkitRequestFullscreen || function(){{}}).call(el);
  }} else {{
    (document.exitFullscreen || document.webkitExitFullscreen || function(){{}}).call(document);
  }}
}}
function _onFsChange() {{
  var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  var icon = document.querySelector('#mapFsBtn i');
  if (icon) icon.className = 'ti ' + (fsEl ? 'ti-arrows-minimize' : 'ti-arrows-maximize');
  setTimeout(function() {{ map.invalidateSize(); }}, 120);
}}
document.addEventListener('fullscreenchange', _onFsChange);
document.addEventListener('webkitfullscreenchange', _onFsChange);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© OpenStreetMap contributors', maxZoom: 19
}}).addTo(map);

/* -- Paths -------------------------------------------------------------- */
// A single colour is enough — every path on this map is, by definition,
// currently active in the API and has never once returned a valid speed.
var PATH_COLOR = '#c0392b';

var pathObjs = [];
var selectedId = null;

// Many always-invalid paths are reverse-direction pairs of the same broken
// junction (e.g. 1009->6100 and 6100->1009), so they run right on top of
// each other — a plain click would only ever reach whichever line Leaflet
// hit-tests first. Point-to-polyline distance (not just point-to-endpoint)
// finds every path passing near the clicked spot, and clicking again there
// cycles to the next one instead of being stuck on the same line.
var _ADJACENT_TOL_M = 30;
var _endpointCycle = {{}};

function _projectToLocal(origin, pt) {{
  var dLat = (pt[0] - origin[0]) * 110574;
  var dLon = (pt[1] - origin[1]) * 111320 * Math.cos(origin[0] * Math.PI / 180);
  return [dLon, dLat];
}}
function _distPointToSegment(pt, a, b) {{
  var P = _projectToLocal(a, pt), B = _projectToLocal(a, b), A = [0, 0];
  var abx = B[0]-A[0], aby = B[1]-A[1];
  var len2 = abx*abx + aby*aby;
  var t = len2 > 0 ? Math.max(0, Math.min(1, ((P[0]-A[0])*abx + (P[1]-A[1])*aby) / len2)) : 0;
  var cx = A[0] + abx*t, cy = A[1] + aby*t;
  return Math.hypot(P[0]-cx, P[1]-cy);
}}
function _distPointToPolyline(pt, coords) {{
  var best = Infinity;
  for (var i = 0; i < coords.length - 1; i++) {{
    var d = _distPointToSegment(pt, coords[i], coords[i+1]);
    if (d < best) best = d;
  }}
  return best;
}}
function _overlappingPathsAt(latlng) {{
  var pt = [latlng.lat, latlng.lng];
  return pathObjs.filter(function(o) {{
    return o.polyline._coords && _distPointToPolyline(pt, o.polyline._coords) <= _ADJACENT_TOL_M;
  }});
}}
// Bolds whichever path is currently selected so, when cycling through a
// stack via repeated clicks, the tooltip reflects which one you're now on.
function _overlapTooltipHtml(here, f) {{
  if (here.length <= 1) return f.name;
  var rows = here.map(function(o) {{
    return o.feature.id === selectedId ? '<b>' + o.feature.name + '</b>' : o.feature.name;
  }});
  return '<b>' + here.length + ' paths overlap here</b> — click to cycle<br>&bull; ' + rows.join('<br>&bull; ');
}}

// Reviewer's decisions on which paths to actually remove, kept client-side
// only (never sent anywhere) so a multi-session pass over ~66 candidates
// doesn't lose progress on reload: {{id: {{status: 'remove'|'keep', note}}}}
var FLAGS = {{}};
try {{ FLAGS = JSON.parse(localStorage.getItem('btInvalidPathFlags') || '{{}}'); }} catch (e) {{ FLAGS = {{}}; }}
function saveFlags() {{ localStorage.setItem('btInvalidPathFlags', JSON.stringify(FLAGS)); }}
var showingFlagList = false;

function fmtSpeed(v) {{ return v === null || v === undefined ? '—' : v + ' km/h'; }}
function fmtTTime(v) {{ return v === null || v === undefined ? '—' : v + ' s'; }}

function detailHtml(f) {{
  var rows = '';
  rows += '<tr><td class="k">Path ID</td><td>' + f.id + '</td></tr>';
  rows += '<tr><td class="k">Runs with data</td><td>' + f.runs + '</td></tr>';
  rows += '<tr><td class="k">First seen</td><td>' + f.first_seen + '</td></tr>';
  rows += '<tr><td class="k">Last seen</td><td>' + f.last_seen + '</td></tr>';
  rows += '<tr><td class="k">Latest speed</td><td>' + fmtSpeed(f.latest_speed) + '</td></tr>';
  rows += '<tr><td class="k">Latest travel time</td><td>' + fmtTTime(f.latest_travel_time) + '</td></tr>';
  var badges = '';
  if (!f.coords.length) badges += '<span class="badge nogeo">No geometry on record</span>';
  return '<b>' + f.name + '</b>' + badges + '<table>' + rows + '</table>';
}}

var insBody, insCounter;
(function() {{
  var wrap = L.DomUtil.create('div', 'inspector', map.getContainer());
  L.DomEvent.disableClickPropagation(wrap);
  L.DomEvent.disableScrollPropagation(wrap);
  var hd = L.DomUtil.create('div', 'inspector-hd', wrap);
  hd.appendChild(document.createTextNode('Path detail'));
  var nav = L.DomUtil.create('div', 'nav', hd);
  var prevBtn = L.DomUtil.create('button', '', nav);
  prevBtn.innerHTML = '&#9664;'; prevBtn.title = 'Previous path';
  insCounter = L.DomUtil.create('span', '', nav);
  var nextBtn = L.DomUtil.create('button', '', nav);
  nextBtn.innerHTML = '&#9654;'; nextBtn.title = 'Next path';
  prevBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); stepTo(-1); }};
  nextBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); stepTo(1); }};
  insBody = L.DomUtil.create('div', 'inspector-body', wrap);
}})();

function _flaggedList() {{
  return Object.keys(FLAGS)
    .filter(function(id) {{ return FLAGS[id] && FLAGS[id].status; }})
    .map(function(id) {{
      var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
      return {{id: id, name: obj ? obj.feature.name : id, status: FLAGS[id].status}};
    }})
    .sort(function(a, b) {{ return a.name < b.name ? -1 : a.name > b.name ? 1 : 0; }});
}}
function _flagSummaryHtml() {{
  var flagged = _flaggedList();
  if (!flagged.length) return '';
  var removeCount = flagged.filter(function(x) {{ return x.status === 'remove'; }}).length;
  var keepCount = flagged.filter(function(x) {{ return x.status === 'keep'; }}).length;
  return '<div class="flag-summary">' +
    (removeCount ? '&#128465;' + removeCount + ' ' : '') + (keepCount ? '&#9989;' + keepCount + ' ' : '') +
    '<span class="flag-summary-link" id="flagSummaryToggle">' + (showingFlagList ? 'Hide list' : 'View list') + '</span>' +
    '</div>';
}}
function _wireFlagSummaryToggle() {{
  var link = document.getElementById('flagSummaryToggle');
  if (link) link.onclick = function(e) {{ L.DomEvent.stopPropagation(e); showingFlagList = !showingFlagList; renderInspector(); }};
}}

function renderInspector() {{
  var flagSummary = _flagSummaryHtml();

  if (showingFlagList) {{
    var flagged = _flaggedList();
    var rows = flagged.map(function(x) {{
      var icon = x.status === 'remove' ? '&#128465;' : '&#9989;';
      var label = icon + ' ' + x.name + ' <span class="flag-list-id">(ID ' + x.id + ')</span>';
      if (x.id === selectedId) label = '<b>' + label + '</b>';
      return '<div class="flag-list-row" data-id="' + x.id + '">' + label + '</div>';
    }}).join('');
    insBody.innerHTML = flagSummary + (flagged.length ? rows : '<div class="empty">Nothing marked yet.</div>');
    Array.prototype.forEach.call(insBody.querySelectorAll('.flag-list-row'), function(row) {{
      row.onclick = function(e) {{ L.DomEvent.stopPropagation(e); selectAndZoom(row.getAttribute('data-id')); }};
    }});
    insCounter.textContent = flagged.length + ' marked';
    _wireFlagSummaryToggle();
    return;
  }}

  if (selectedId === null) {{
    insBody.innerHTML = flagSummary + '<div class="empty">Click a path, or use &#9664; &#9654; to step through all ' +
      FEATURES.length + ' path(s).</div>';
    insCounter.textContent = FEATURES.length ? '0 / ' + FEATURES.length : '';
    _wireFlagSummaryToggle();
    return;
  }}
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === selectedId; }})[0];
  if (!obj) return;
  var idx = FEATURES.findIndex(function(f) {{ return f.id === selectedId; }});
  insCounter.textContent = (idx + 1) + ' / ' + FEATURES.length;

  insBody.innerHTML = flagSummary + detailHtml(obj.feature) +
    '<div class="flag-row"><button id="flagBtn">&#128465; Mark for removal</button><button id="okBtn">&#9989; Keep</button></div>' +
    '<textarea id="insNote" placeholder="Notes for this path…"></textarea>';

  var flagState = (FLAGS[obj.feature.id] || {{}}).status || '';
  var flagBtn = document.getElementById('flagBtn'), okBtn = document.getElementById('okBtn');
  flagBtn.classList.toggle('active-flag', flagState === 'remove');
  okBtn.classList.toggle('active-ok', flagState === 'keep');
  flagBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); setFlag(obj.feature.id, 'remove'); }};
  okBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); setFlag(obj.feature.id, 'keep'); }};
  var noteEl = document.getElementById('insNote');
  noteEl.value = (FLAGS[obj.feature.id] || {{}}).note || '';
  noteEl.onchange = function() {{ setNote(obj.feature.id, noteEl.value); }};
  _wireFlagSummaryToggle();
}}

function _applyStyles() {{
  pathObjs.forEach(function(o) {{
    var isSel = (o.feature.id === selectedId);
    o.polyline.setStyle({{
      color: isSel ? '#facc15' : PATH_COLOR,
      weight: isSel ? 8 : 4,
      opacity: isSel ? 1 : 0.85
    }});
    if (isSel) o.polyline.bringToFront();
  }});
}}

function selectPath(id) {{
  selectedId = (selectedId === id) ? null : id;
  _applyStyles();
  renderInspector();
}}

// Selects a specific path without toggling it off if already selected, and
// without moving the map — used by click-to-cycle, where jumping the view to
// fit each candidate's bounds on every click would fight the zoom level you
// deliberately chose to disambiguate the stack.
function selectById(id) {{
  selectedId = id;
  _applyStyles();
  renderInspector();
}}

// Same, but also zooms to the path — used by search, the flagged-paths list,
// and flag markers, where you always want to land on that exact path.
function selectAndZoom(id) {{
  selectById(id);
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
  if (obj) map.fitBounds(obj.polyline.getBounds(), {{padding: [60, 60], maxZoom: 16}});
}}

function stepTo(delta) {{
  if (!FEATURES.length) return;
  var idx = FEATURES.findIndex(function(f) {{ return f.id === selectedId; }});
  idx = idx === -1 ? 0 : (idx + delta + FEATURES.length) % FEATURES.length;
  selectAndZoom(FEATURES[idx].id);
}}

// Solid-fill teardrop pin at each marked path's midpoint — red flag for
// "remove", green check for "keep" — so decisions already made are visible
// on the map itself, not just buried in the inspector panel.
var flagLayer = L.layerGroup();
var flagMarkers = {{}};
function _flagMarkerIcon(status) {{
  var color = status === 'remove' ? '#c0392b' : '#1d9e75';
  var symbol = status === 'remove' ? '&#128465;' : '&#10003;';
  return L.divIcon({{
    className: '',
    html: '<div style="width:22px;height:22px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);' +
          'background:' + color + ';border:2.5px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,.5);' +
          'display:flex;align-items:center;justify-content:center">' +
          '<span style="transform:rotate(45deg);font-size:12px;color:#fff;font-weight:800;' +
          'font-family:sans-serif">' + symbol + '</span></div>',
    iconSize: [22, 26], iconAnchor: [11, 26]
  }});
}}
function updateFlagMarker(id) {{
  if (flagMarkers[id]) {{ flagLayer.removeLayer(flagMarkers[id]); delete flagMarkers[id]; }}
  var status = (FLAGS[id] || {{}}).status;
  if (!status) return;
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
  if (!obj) return;
  var latlngs = obj.polyline.getLatLngs();
  if (!latlngs.length) return;
  var mid = latlngs[Math.floor(latlngs.length / 2)];
  var m = L.marker(mid, {{icon: _flagMarkerIcon(status), interactive: true, zIndexOffset: 1000}});
  m.bindTooltip((status === 'remove' ? 'Marked for removal' : 'Kept') + ': ' + obj.feature.name,
                {{direction: 'top', opacity: 0.95}});
  m.on('click', function(e) {{ L.DomEvent.stopPropagation(e); selectAndZoom(id); }});
  m.addTo(flagLayer);
  flagMarkers[id] = m;
}}
function setFlag(id, status) {{
  if (!FLAGS[id]) FLAGS[id] = {{}};
  FLAGS[id].status = (FLAGS[id].status === status) ? '' : status;
  saveFlags();
  updateFlagMarker(id);
  renderInspector();
}}
function setNote(id, note) {{
  if (!FLAGS[id]) FLAGS[id] = {{}};
  FLAGS[id].note = note;
  saveFlags();
}}
// Prune flags for paths no longer on this list — e.g. one of them started
// reporting a real speed on a later run and dropped out of FEATURES — so
// they don't linger as dead, unselectable entries.
(function _pruneOrphanedFlags() {{
  var knownIds = {{}};
  FEATURES.forEach(function(f) {{ knownIds[f.id] = true; }});
  var changed = false;
  Object.keys(FLAGS).forEach(function(id) {{
    if (!knownIds[id]) {{ delete FLAGS[id]; changed = true; }}
  }});
  if (changed) saveFlags();
}})();

FEATURES.forEach(function(f) {{
  if (!f.coords.length) return;
  var latlngs = f.coords.map(function(c) {{ return [c[0], c[1]]; }});
  var pl = L.polyline(latlngs, {{
    color: PATH_COLOR, weight: 4, opacity: 0.85
  }}).addTo(map);
  pl._coords = latlngs;
  pl.bindTooltip('', {{sticky: true, direction: 'top', opacity: 0.95, className: 'bt-overlap-tip'}});
  pl.on('mouseover', function(e) {{
    pl.setTooltipContent(_overlapTooltipHtml(_overlappingPathsAt(e.latlng), f));
  }});
  pl.on('click', function(e) {{
    L.DomEvent.stopPropagation(e);
    var here = _overlappingPathsAt(e.latlng);
    if (here.length <= 1) {{ selectPath(f.id); return; }}
    // Multiple paths stacked at this exact spot: step to the next one on each
    // click, keyed by click position so repeated clicks near the same spot
    // advance the same cycle instead of always landing on whichever line
    // Leaflet happened to hit-test.
    var key = 'line:' + e.latlng.lat.toFixed(4) + ',' + e.latlng.lng.toFixed(4);
    var idx = (_endpointCycle[key] || 0) % here.length;
    _endpointCycle[key] = idx + 1;
    var chosen = here[idx];
    selectById(chosen.feature.id);
    pl.setTooltipContent(_overlapTooltipHtml(here, f));
  }});
  pathObjs.push({{feature: f, polyline: pl}});
}});
map.on('click', function() {{ selectPath(null); }});
flagLayer.addTo(map);
Object.keys(FLAGS).forEach(updateFlagMarker);
renderInspector();

/* -- Sensor inventory (API / local DB) ----------------------------------- */
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

var refLayer = L.layerGroup();
var refMarkersByIdx = [];
REF_SENSORS.forEach(function(s, idx) {{
  var m = L.marker([s.lat, s.lon], {{icon: makeDiamondIcon('#8e44ad')}});
  m.bindPopup('<b>' + s.name + '</b><br>Spreadsheet location<br>' + s.lat.toFixed(5) + ', ' + s.lon.toFixed(5));
  m.addTo(refLayer);
  refMarkersByIdx[idx] = m;
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

/* -- Flag findings: export / import --------------------------------------- */
function exportFindings() {{
  var rows = [['Path ID', 'Name', 'Status', 'Note', 'Runs', 'First seen', 'Last seen',
               'Latest speed', 'Latest travel time']];
  Object.keys(FLAGS).forEach(function(id) {{
    var flag = FLAGS[id];
    if (!flag || (!flag.status && !flag.note)) return;
    var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
    var f = obj ? obj.feature : null;
    rows.push([
      id, f ? f.name : id, flag.status || '', (flag.note || '').replace(/\\n/g, ' '),
      f ? f.runs : '', f ? f.first_seen : '', f ? f.last_seen : '',
      f ? f.latest_speed : '', f ? f.latest_travel_time : ''
    ]);
  }});
  if (rows.length === 1) {{
    alert('No paths marked yet — use Mark for removal / Keep in the panel at bottom-left first.');
    return;
  }}
  var csv = rows.map(function(r) {{
    return r.map(function(v) {{ return '"' + String(v).replace(/"/g, '""') + '"'; }}).join(',');
  }}).join('\\n');
  var blob = new Blob([csv], {{type: 'text/csv'}});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bt_paths_invalid_findings.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}}

// Minimal RFC4180-ish CSV parser — handles quoted fields, escaped quotes
// (""), and both CRLF and LF line endings, matching what exportFindings()
// itself writes (and what Excel/Sheets produce when re-saving that file).
function _parseCsv(text) {{
  var rows = [], row = [], field = '', inQuotes = false;
  for (var i = 0; i < text.length; i++) {{
    var c = text[i];
    if (inQuotes) {{
      if (c === '"') {{
        if (text[i + 1] === '"') {{ field += '"'; i++; }} else {{ inQuotes = false; }}
      }} else {{
        field += c;
      }}
    }} else if (c === '"') {{
      inQuotes = true;
    }} else if (c === ',') {{
      row.push(field); field = '';
    }} else if (c === '\\r') {{
      // skip; \\n handles the line break
    }} else if (c === '\\n') {{
      row.push(field); rows.push(row); row = []; field = '';
    }} else {{
      field += c;
    }}
  }}
  if (field.length || row.length) {{ row.push(field); rows.push(row); }}
  return rows;
}}

// Re-imports a CSV from exportFindings() (this tool's own, or one edited in
// Excel/Sheets) — the only way findings survive a different browser, a
// different machine, or localStorage getting cleared. Merges by Path ID.
function importFindings(file) {{
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function() {{
    var rows = _parseCsv(String(reader.result));
    if (!rows.length) {{ alert("That file looks empty."); return; }}
    var header = rows[0].map(function(h) {{ return h.trim().toLowerCase(); }});
    var idIdx = header.indexOf('path id');
    var statusIdx = header.indexOf('status');
    var noteIdx = header.indexOf('note');
    if (idIdx === -1) {{
      alert("That doesn't look like a findings CSV exported from this tool — no \\"Path ID\\" column.");
      return;
    }}
    var imported = 0;
    for (var r = 1; r < rows.length; r++) {{
      var row = rows[r];
      var id = row[idIdx];
      if (!id) continue;
      var status = statusIdx !== -1 ? row[statusIdx] : '';
      var note = noteIdx !== -1 ? row[noteIdx] : '';
      if (!status && !note) continue;
      FLAGS[id] = {{status: status || '', note: note || ''}};
      updateFlagMarker(id);
      imported++;
    }}
    saveFlags();
    renderInspector();
    alert("Imported " + imported + " path finding(s).");
  }};
  reader.readAsText(file);
}}

var ExportCtl = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var btn = L.DomUtil.create('button', 'sensor-toggle-btn export');
    btn.innerHTML = 'Export findings &#8659;';
    L.DomEvent.disableClickPropagation(btn);
    btn.onclick = exportFindings;
    return btn;
  }}
}});
map.addControl(new ExportCtl());

var ImportCtl = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var wrap = L.DomUtil.create('div');
    var btn = L.DomUtil.create('button', 'sensor-toggle-btn import', wrap);
    btn.innerHTML = 'Import findings &#8657;';
    var fileInput = L.DomUtil.create('input', '', wrap);
    fileInput.type = 'file';
    fileInput.accept = '.csv';
    fileInput.style.display = 'none';
    L.DomEvent.disableClickPropagation(wrap);
    btn.onclick = function() {{ fileInput.click(); }};
    fileInput.onchange = function() {{
      importFindings(fileInput.files[0]);
      fileInput.value = '';   // allow re-importing the same file
    }};
    return wrap;
  }}
}});
map.addControl(new ImportCtl());

/* -- Search: paths + both sensor inventories ------------------------------ */
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

function showLayer(layer, ctl, label) {{
  if (map.hasLayer(layer)) return;
  layer.addTo(map);
  var btn = ctl.getContainer();
  btn.innerHTML = label + ': ON';
  btn.classList.remove('off');
}}

function goToSearchResult(item) {{
  if (item.kind === 'path') {{
    selectAndZoom(item.id);
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
</script>
</body>
</html>"""
    return page


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPORT_DIR / "bt_paths_invalid_map.html"))
    args = ap.parse_args()

    all_paths = fetch_bt_path_coords()
    invalid_paths = {pid: info for pid, info in find_always_invalid_paths().items() if pid in all_paths}
    sensors = fetch_sensor_coords().get("Bluetooth", {})
    ref_sensors = load_reference_sensors()

    page = build_html(invalid_paths, all_paths, sensors, ref_sensors)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    print(f"Bluetooth always-invalid map written to {out_path}  "
          f"({len(invalid_paths)} active path(s) always -1; retired paths excluded)")


if __name__ == "__main__":
    main()
