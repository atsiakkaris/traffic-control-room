"""
omnia_nodes_map.py - a stand-alone map of OMNIA's logical nodes next to the API's Bluetooth sensors.

Not part of the Control Room: it writes one HTML file (reports/omnia_nodes_map.html, git-ignored, because it holds
OMNIA data) that opens in any browser. Nodes come from the saved OMNIA snapshot (results/omnia_snapshot.json,
made by the Control Room's Fetch button or by `--fetch` here); sensors come from results/history.db.

    python runner/omnia_nodes_map.py            map from the saved snapshot
    python runner/omnia_nodes_map.py --fetch    read OMNIA first (internal network needed; addresses in .env)
"""

import argparse
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bt_wiring as bw  # noqa: E402
import omnia_fetch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_FILE = ROOT / "results" / "omnia_snapshot.json"
OUT_FILE = ROOT / "reports" / "omnia_nodes_map.html"

PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OMNIA nodes and API sensors</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"/>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.0.22/leaflet-maplibre-gl.js"></script>
<style>
:root{--bg:#f4f6f8;--card:#fff;--text:#1d2630;--dim:#5b6874;--line:#dde2e7}
@media (prefers-color-scheme: dark){:root{--bg:#111a22;--card:#1a2530;--text:#e3e9ee;--dim:#9fb0bd;--line:#2c3a47}}
html,body{height:100%;margin:0}body{display:flex;flex-direction:column;background:var(--bg);color:var(--text);font:13px Arial,sans-serif}
header{padding:10px 16px;background:#1f4e79;color:#fff;display:flex;flex-wrap:wrap;gap:6px 18px;align-items:baseline}
header h1{margin:0;font-size:17px}header .sub{font-size:12px;opacity:.85}
.bar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:8px 16px;background:var(--card);border-bottom:1px solid var(--line)}
.bar label{cursor:pointer;white-space:nowrap}
.bar input[type=text]{padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);min-width:230px}
.key i{display:inline-block;width:11px;height:11px;margin:0 4px -1px 0}
.key .dia{transform:rotate(45deg)}.key .dot{border-radius:50%}
#map{flex:1 1 auto;width:100%;min-height:300px}
.nd{width:14px;height:14px;transform:rotate(45deg);border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.5)}
.leaflet-popup-content{font:13px Arial,sans-serif}
</style></head><body>
<header><h1>OMNIA logical nodes and API Bluetooth sensors</h1><span class="sub" id="sub"></span></header>
<div class="bar">
  <label><input type="checkbox" id="showNodes" checked> Nodes</label>
  <label><input type="checkbox" id="showSensors" checked> API sensors</label>
  <label><input type="checkbox" id="onlyProblems"> Only nodes with a problem</label>
  <label><input type="checkbox" id="showLines" checked> Lines node to sensor when they differ</label>
  <input type="text" id="q" placeholder="Find a node or sensor by name or id...">
  <span class="key" id="key"></span>
</div>
<div id="map"></div>
<script>
(function() {
  var D = __DATA__;
  var COL = {ok: '#27ae60', name_only: '#e67e22', location_only: '#8e44ad', case_or_space: '#c0392b', no_sensor: '#c0392b'};
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  document.getElementById('sub').textContent = D.nodes.length + ' nodes, ' + D.sensors.length + ' API sensors' + (D.fetched_at ? ' - OMNIA data from ' + D.fetched_at : '');
  var key = document.getElementById('key');
  key.innerHTML = '<span><i class="dot" style="background:#1f78b4"></i>API sensor</span> &nbsp; ' + Object.keys(D.status_text).map(function(k) {
    return '<span><i class="dia" style="background:' + COL[k] + '"></i>' + esc(D.status_text[k]) + ' (' + D.counts[k] + ')</span>';
  }).join(' &nbsp; ');
  var map = L.map('map', {preferCanvas: true});
  map.setView([35.1, 33.3], 9);      // a view must exist before the vector basemap is added
  map.setMaxZoom(19);
  var base = L.maplibreGL({style: 'https://tiles.openfreemap.org/styles/liberty', attribution:
    '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> &copy; OpenMapTiles Data from OpenStreetMap'}).addTo(map);
  if (base.getContainer && base.getContainer()) base.getContainer().style.zIndex = 0;   // keep it under the markers
  var sensorLayer = L.layerGroup().addTo(map), nodeLayer = L.layerGroup().addTo(map), lineLayer = L.layerGroup().addTo(map);
  var items = [], bounds = [];
  var sensorById = {};
  D.sensors.forEach(function(s) {
    sensorById[s.id] = s;
    var m = L.circleMarker([s.lat, s.lon], {radius: 6, color: '#fff', weight: 2, fillColor: '#1f78b4', fillOpacity: 0.95});
    m.bindPopup('<b>API sensor: ' + esc(s.name) + '</b><br>ID: ' + esc(s.id) + '<br>Site code: ' + esc(s.site_code)
      + '<br>' + s.lat.toFixed(5) + ', ' + s.lon.toFixed(5)
      + (s.nodes.length ? '<br>OMNIA node: ' + s.nodes.map(esc).join(', ') : '<br><span style="color:#c0392b">no OMNIA node stands for this sensor</span>'));
    s.marker = m; items.push({label: s.name + ' (sensor ' + s.id + ')', hay: (s.name + ' ' + s.id + ' ' + s.site_code).toLowerCase(), marker: m, ll: [s.lat, s.lon]});
    bounds.push([s.lat, s.lon]);
  });
  D.nodes.forEach(function(n) {
    var icon = L.divIcon({className: '', html: '<div class="nd" style="background:' + COL[n.status] + '"></div>', iconSize: [14, 14], iconAnchor: [7, 7]});
    var m = L.marker([n.lat, n.lon], {icon: icon});
    var h = '<b>OMNIA node ' + esc(n.id) + ': ' + esc(n.name) + '</b><br>' + esc(D.status_text[n.status]) + '<br>' + n.lat.toFixed(5) + ', ' + n.lon.toFixed(5);
    if (n.sensor_name) h += '<br>Sensor: ' + esc(n.sensor_name) + ' (id ' + esc(n.sensor) + ')' + (n.status !== 'ok' ? ' - ' + n.distance_m + ' m away' : '');
    if (n.similar.length) h += '<br>Looks like: ' + n.similar.map(esc).join(', ');
    n.notes.forEach(function(t) { h += '<br><i>' + esc(t) + '</i>'; });
    h += '<br>Paths using it: ' + (n.paths.length ? n.paths.map(esc).join(', ') : 'none');
    m.bindPopup(h);
    n.marker = m; items.push({label: n.name + ' (node ' + n.id + ')', hay: (n.name + ' ' + n.id).toLowerCase(), marker: m, ll: [n.lat, n.lon]});
    bounds.push([n.lat, n.lon]);
    if (n.sensor && n.status !== 'ok' && sensorById[n.sensor]) {
      var s = sensorById[n.sensor];
      n.line = L.polyline([[n.lat, n.lon], [s.lat, s.lon]], {color: COL[n.status], weight: 3, dashArray: '6 6'})
        .bindTooltip(esc(n.name) + ': ' + n.distance_m + ' m between node and sensor', {sticky: true});
    }
  });
  function draw() {
    var sn = document.getElementById('showNodes').checked, ss = document.getElementById('showSensors').checked;
    var op = document.getElementById('onlyProblems').checked, sl = document.getElementById('showLines').checked;
    sensorLayer.clearLayers(); nodeLayer.clearLayers(); lineLayer.clearLayers();
    if (ss) D.sensors.forEach(function(s) { sensorLayer.addLayer(s.marker); });
    D.nodes.forEach(function(n) {
      if (!sn || (op && n.status === 'ok')) return;
      nodeLayer.addLayer(n.marker);
      if (sl && n.line) lineLayer.addLayer(n.line);
    });
  }
  ['showNodes', 'showSensors', 'onlyProblems', 'showLines'].forEach(function(id) { document.getElementById(id).onchange = draw; });
  document.getElementById('q').onkeydown = function(e) {
    if (e.key !== 'Enter') return;
    var q = this.value.toLowerCase().trim();
    var hit = items.filter(function(i) { return q && i.hay.indexOf(q) >= 0; })[0];
    if (hit) { map.setView(hit.ll, 17, {animate: false}); hit.marker.openPopup(); }
  };
  draw();
  var fitted = false;
  function fit() {
    map.invalidateSize();
    if (bounds.length) map.fitBounds(bounds, {padding: [30, 30], animate: false}); else map.setView([35.1, 33.3], 10);
  }
  // fit once the map has a real size (the header wraps and fonts load after the script runs)
  new ResizeObserver(function() {
    map.invalidateSize();
    if (!fitted && document.getElementById('map').clientWidth > 0) { fitted = true; fit(); }
  }).observe(document.getElementById('map'));
})();
</script></body></html>"""


def build_html(snapshot, sensors):
    """The page as a string, from an OMNIA snapshot and the API sensors {id: {name, site_code, lat, lon}}."""
    matches = bw.match_nodes(snapshot["nodes"], sensors)
    used_by = {}
    for p in snapshot["paths"]:
        for nid in (p.get("start"), p.get("end")):
            if nid:
                used_by.setdefault(nid, []).append(p["name"] or str(p["id"]))
    nodes = []
    for m in matches.values():
        nodes.append({**m, "paths": sorted(set(used_by.get(m["id"], [])))[:12]})
    by_sensor = {}
    for m in matches.values():
        if m["sensor"] is not None:
            by_sensor.setdefault(m["sensor"], []).append("%s %s" % (m["id"], m["name"]))
    sens = [{"id": sid, "name": s.get("name") or "", "site_code": s.get("site_code") or "", "lat": s["lat"], "lon": s["lon"],
             "nodes": by_sensor.get(sid, [])} for sid, s in sensors.items() if s.get("lat") is not None]
    counts = {k: 0 for k in bw.NODE_STATUS}
    for n in nodes:
        counts[n["status"]] += 1
    data = {"nodes": nodes, "sensors": sens, "status_text": bw.NODE_STATUS, "counts": counts,
            "fetched_at": snapshot.get("fetched_at", "")}
    return PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fetch", action="store_true", help="read the nodes and paths from OMNIA first")
    ap.add_argument("--no-open", action="store_true", help="only write the file")
    args = ap.parse_args(argv)
    if args.fetch:
        sys.path.insert(0, str(ROOT / "tools"))
        import control_room
        try:
            snap = omnia_fetch.fetch_snapshot(control_room.omnia_urls_setting())
        except omnia_fetch.OmniaError as exc:
            print(exc)
            return 1
        omnia_fetch.save_snapshot(snap, SNAPSHOT_FILE)
    snap = omnia_fetch.load_snapshot(SNAPSHOT_FILE)
    if snap is None:
        print("No OMNIA snapshot yet: run with --fetch (internal network needed), or press Fetch on the Control Room's Wiring tab.")
        return 1
    import db
    page = build_html(snap, db.fetch_sensor_coords().get("Bluetooth", {}))
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(page, encoding="utf-8")
    print(f"Wrote {OUT_FILE} ({datetime.now():%H:%M:%S})")
    if not args.no_open:
        webbrowser.open(OUT_FILE.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
