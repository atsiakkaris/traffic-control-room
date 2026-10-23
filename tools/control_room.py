"""
control_room.py - local app: review Bluetooth paths on the map, flag them, delete them.

Starts a small web server on this machine only (127.0.0.1) and opens the BT paths
map in your browser. Compared with the standalone map it adds:
  * flags and notes saved to results/path_review.json (survive a browser change)
  * a "Review & delete..." button: dry run first, then type YES to delete the
    flagged paths from the SWARCO API (every result logged to results/delete_log.csv)

It also serves the sensor-location checks (/sensors/vms, /sensors/traffic-detection,
/sensors/bluetooth): each API sensor against the QA Locations.xlsx reference rows, with
the distance between them. These pages are read-only; unlike qa.py / update_projects.py
they never write to the database.

Deleting is OFF unless this PC opts in: put CONTROL_ROOM_ALLOW_DELETE=1 and
CONTROL_ROOM_DELETE_URL=<the SWARCO delete address> in the (git-ignored) .env file next to
this repo (the repo is public, so the address is never kept in the code), or set them as
environment variables. Without it the app is
read-only: you can view, compare and flag, and the server refuses every delete request.
Where delete is on, nothing is deleted unless a dry run for the same list was shown in this
session and YES was typed. The copy published to GitHub Pages is unaffected.

Usage:
    python tools/control_room.py            # or double-click control_room.bat
    python tools/control_room.py --browser  # use your browser instead of the app window
    python tools/control_room.py --no-browser --port 8765

The app window needs one extra package (pip install pywebview); without it the page opens
in your browser as before.
"""
import argparse
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(Path(__file__).parent))


REVIEW_FILE = ROOT / "results" / "path_review.json"
DELETE_LOG = ROOT / "results" / "delete_log.csv"
DEFAULT_PORT = 8765
DELAY_S = 0.3
VALID_STATUS = ("flag", "ok", "")


# ── who may delete ───────────────────────────────────────────────────────────

def _setting(name, env=None, env_file=ROOT / ".env"):
    """A setting from the environment, else from the git-ignored .env file; None if unset."""
    env = os.environ if env is None else env
    if str(env.get(name, "")).strip():
        return str(env[name]).strip()
    try:
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            key, _, val = line.partition("=")
            if key.strip() == name and val.strip():
                return val.strip().strip('"')
    except OSError:
        pass
    return None


def allow_delete_setting(env=None, env_file=ROOT / ".env"):
    """True only if this PC opted in via CONTROL_ROOM_ALLOW_DELETE=1 (environment or .env)."""
    val = _setting("CONTROL_ROOM_ALLOW_DELETE", env, env_file)
    return (val or "").lower() in ("1", "true", "yes")


def delete_url_setting(env=None, env_file=ROOT / ".env"):
    """The SWARCO delete address (CONTROL_ROOM_DELETE_URL); kept out of the public code."""
    return _setting("CONTROL_ROOM_DELETE_URL", env, env_file)


def allow_edit_setting(env=None, env_file=ROOT / ".env"):
    """True only if this PC opted in to changing OMNIA paths via CONTROL_ROOM_ALLOW_EDIT=1 (environment or .env)."""
    return (_setting("CONTROL_ROOM_ALLOW_EDIT", env, env_file) or "").lower() in ("1", "true", "yes")


def omnia_urls_setting(env=None, env_file=ROOT / ".env"):
    """The OMNIA addresses from .env, each a full address: {nodes, paths, edit} (None for one that is not set).

    CONTROL_ROOM_OMNIA_NODES_URL / _PATHS_URL read the lists, CONTROL_ROOM_OMNIA_EDIT_URL changes speeds and nodes,
    CONTROL_ROOM_OMNIA_RENAME_URL renames; deleting uses CONTROL_ROOM_DELETE_URL. None of them is in the code: the repo is public.
    """
    return {k: _setting("CONTROL_ROOM_OMNIA_%s_URL" % k.upper(), env, env_file) for k in ("nodes", "paths", "edit", "rename")}


# ── review file ──────────────────────────────────────────────────────────────

def load_review(path=REVIEW_FILE):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return clean_review(data) if isinstance(data, dict) else {}


def clean_review(data):
    """Keep only well-formed {id: {status, note}} entries."""
    out = {}
    for pid, entry in data.items():
        if not isinstance(entry, dict):
            continue
        status = entry.get("status", "")
        note = entry.get("note", "")
        if status not in VALID_STATUS or not isinstance(note, str):
            continue
        out[str(pid)] = {"status": status, "note": note}
    return out


def save_review(data, path=REVIEW_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean_review(data), indent=1), encoding="utf-8")
    tmp.replace(path)


def flagged_ids(review):
    return {pid for pid, e in review.items() if e.get("status") == "flag"}


# ── delete ───────────────────────────────────────────────────────────────────

def plan_delete(ids, review, known_ids):
    """Split requested ids into (deletable, refused). Deletable = flagged AND on file."""
    flagged = flagged_ids(review)
    ok, refused = [], []
    for pid in ids:
        pid = str(pid)
        (ok if pid in flagged and pid in known_ids and pid.isdigit() else refused).append(pid)
    return ok, refused


def delete_paths(ids, url, post=None, sleep=time.sleep, delay=DELAY_S):
    """POST the delete address for each id in order; stop at the first failure.

    Returns (results, error). results = [{"id", "ok", "status"}, ...].
    """
    if post is None:
        import httpx
        post = httpx.post
    results, error = [], None
    for i, pid in enumerate(ids, 1):
        try:
            resp = post(f"{url}?pathIdToDelete={pid}", timeout=15)
            ok, status = resp.is_success, resp.status_code
        except Exception as exc:  # network down, API unreachable...
            ok, status = False, f"{type(exc).__name__}"
        results.append({"id": pid, "ok": ok, "status": status})
        if not ok:
            error = f"request failed for path {pid} ({status})"
            break
        if i < len(ids):
            sleep(delay)
    return results, error


def log_deletes(results, names, path=DELETE_LOG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "path_id", "name", "ok", "status"])
        now = datetime.now().isoformat(timespec="seconds")
        for r in results:
            w.writerow([now, r["id"], names.get(r["id"], ""), r["ok"], r["status"]])


# ── page ─────────────────────────────────────────────────────────────────────

def build_page(allow_delete=True):
    import bt_paths_map as m
    paths = m.fetch_bt_path_coords()
    sensors = m.fetch_sensor_coords().get("Bluetooth", {})
    ref_sensors = m.load_reference_sensors()
    kwargs = m.analyse_paths(paths)
    return m.build_html(paths, sensors, ref_sensors, server_mode=True, allow_delete=allow_delete, **kwargs), paths


# ── triangulation panel (Bluetooth sensors tab) ──────────────────────────────

TRIANGULATION_JS = """<style>
#triBtn{position:fixed;left:10px;bottom:10px;z-index:99998;background:#7b241c;color:#fff;border:0;border-radius:999px;
  padding:8px 14px;font:13px Arial,sans-serif;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.45)}
#triPanel{position:fixed;left:10px;bottom:52px;width:400px;max-width:calc(100vw - 20px);max-height:64vh;overflow:auto;z-index:99998;
  display:none;background:#fff;color:#222;border-radius:10px;box-shadow:0 4px 22px rgba(0,0,0,.4);font:13px Arial,sans-serif}
#triPanel h4{margin:0;padding:12px 14px;border-bottom:1px solid #ddd;font-size:14px}
#triPanel .tb{padding:10px 14px}
#triPanel .sm{color:#555;font-size:12px;line-height:1.4}
#triPanel label{display:block;margin:4px 0}
#triPanel .row{padding:7px 8px;border-radius:6px;cursor:pointer;border-left:5px solid #ccc;margin:4px 0;background:#f6f7f9}
#triPanel .row:hover{background:#e8eef5}
#triPanel .row small{display:block;color:#555}
#triPanel button.ex{margin:8px 14px 12px;padding:6px 12px;border:0;border-radius:6px;background:#2c3e50;color:#fff;cursor:pointer}
.triPop{max-width:300px;font:13px Arial,sans-serif}.triPop ul{margin:6px 0 0;padding-left:18px}
.triPop a,.triInfo a{color:#2e86c1;cursor:pointer}
.triInfo{margin-top:6px;padding-top:6px;border-top:1px dashed #ccc;max-width:320px}
.triInfo .v{display:block;padding:5px 7px;border-radius:5px;color:#fff;margin:2px 0 4px}
.triInfo ul{margin:4px 0 0;padding-left:18px;max-height:140px;overflow:auto}
</style>
<button id="triBtn"></button>
<div id="triPanel">
  <h4>Which sensors look faulty? (triangulation)</h4>
  <div class="tb sm" id="triSum"></div>
  <div class="tb" id="triOpts"></div>
  <div class="tb" id="triList"></div>
  <button class="ex" id="triEx">Export CSV</button>
  <div class="tb sm" id="triSaved" style="padding-top:0;word-break:break-all"></div>
</div>
<script>
(function() {
  var D = __DATA__;
  var COLORS = {faulty_high: '#c0392b', faulty_likely: '#e67e22', path_problem: '#8e44ad'};
  var shell = (window.parent !== window && window.parent.crShow) ? window.parent : null;
  var layer = L.layerGroup().addTo(map), markers = {};
  var on = {faulty_high: true, faulty_likely: true, path_problem: false};   // path problems start hidden
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function popup(s) {
    var h = '<div class="triPop"><b>' + esc(s.name) + '</b> (site ' + esc(s.site_code) + ')<br><i>' + esc(D.labels[s.verdict]) + '</i><br>' + esc(s.reason) + '<ul>';
    s.paths.forEach(function(p) {
      h += '<li>' + esc(p.name) + ' - ' + esc(p.state) + (shell ? ' <a data-showpath="' + esc(p.id) + '">show path</a>' : '') + '</li>';
    });
    return h + '</ul></div>';
  }
  D.sensors.forEach(function(s) {
    if (s.lat == null) return;
    markers[s.id] = L.circleMarker([s.lat, s.lon], {radius: 16, color: COLORS[s.verdict], weight: 4, fill: false}).bindPopup(popup(s));
  });
  var STATE = {good: 'works', odd: 'works (unrealistic speed)', bad: 'fails', mixed: 'intermittent'};
  var VCOL = {faulty_high: '#c0392b', faulty_likely: '#e67e22', path_problem: '#8e44ad', explained: '#7f8c8d', working: '#27ae60'};
  function info(id) {
    var s = D.info[String(id)];
    if (!s) return '';
    var h = '<div class="triInfo"><span class="v" style="background:' + (VCOL[s.verdict] || '#7f8c8d') + '">' + esc(D.labels[s.verdict]) + '</span>';
    if (s.reason) h += esc(s.reason) + '.';
    if (s.paths.length) {
      h += '<br><b>Paths (' + s.paths.length + '):</b><ul>';
      s.paths.forEach(function(p) {
        h += '<li>' + esc(p.name) + ' - ' + esc(STATE[p.state] || p.state) + (shell ? ' <a data-showpath="' + esc(p.id) + '">show path</a>' : '') + '</li>';
      });
      h += '</ul>';
    }
    return h + '</div>';
  }
  // every sensor popup of the QA map gets its verdict and the paths that start or end at it
  map.on('popupopen', function(e) {
    var c = e.popup.getContent();
    if (typeof c !== 'string' || c.indexOf('triInfo') >= 0 || c.indexOf('triPop') >= 0) return;
    var parts = c.split(/<hr[^>]*>/), changed = false;
    parts = parts.map(function(part) {
      var m = /ID:\\s*(\\d+)/.exec(part), h = m ? info(m[1]) : '';
      if (h) changed = true;
      return part + h;
    });
    if (changed) e.popup.setContent(parts.join('<hr style="margin:6px 0">'));
  });
  document.addEventListener('click', function(e) {
    var a = e.target.closest && e.target.closest('[data-showpath]');
    if (a && shell) { e.preventDefault(); shell.crShow('paths', {path: a.getAttribute('data-showpath')}); }
  });
  var btn = document.getElementById('triBtn'), panel = document.getElementById('triPanel');
  var nSusp = D.sensors.filter(function(s) { return s.verdict !== 'path_problem'; }).length;
  btn.textContent = 'Triangulation: ' + nSusp + ' suspect' + (nSusp === 1 ? '' : 's');
  btn.onclick = function() { panel.style.display = panel.style.display === 'block' ? 'none' : 'block'; };
  document.getElementById('triSum').innerHTML = D.summary;
  var opts = document.getElementById('triOpts');
  ['faulty_high', 'faulty_likely', 'path_problem'].forEach(function(v) {
    var n = D.sensors.filter(function(s) { return s.verdict === v; }).length;
    var lab = document.createElement('label');
    lab.innerHTML = '<input type="checkbox"' + (on[v] ? ' checked' : '') + '> <span style="color:' + COLORS[v] + '">&#9679;</span> ' + esc(D.labels[v]) + ' (' + n + ')';
    lab.firstChild.onchange = function() { on[v] = this.checked; refresh(); };
    opts.appendChild(lab);
  });
  function refresh() {
    layer.clearLayers();
    var list = document.getElementById('triList');
    list.innerHTML = '';
    D.sensors.forEach(function(s) {
      if (!on[s.verdict]) return;
      if (markers[s.id]) layer.addLayer(markers[s.id]);
      var r = document.createElement('div');
      r.className = 'row'; r.style.borderLeftColor = COLORS[s.verdict];
      r.innerHTML = '<b>' + esc(s.name) + '</b> <small>' + esc(D.labels[s.verdict]) + ' - ' + esc(s.reason) + '</small>';
      r.onclick = function() {
        if (s.lat == null) return;
        map.flyTo([s.lat, s.lon], 16, {duration: 1.0});
        if (markers[s.id]) setTimeout(function() { markers[s.id].openPopup(); }, 1100);
      };
      list.appendChild(r);
    });
    if (!list.children.length) list.innerHTML = '<div class="sm">Nothing to show for the ticked options.</div>';
  }
  refresh();
  document.getElementById('triEx').onclick = function() {
    var note = document.getElementById('triSaved');
    note.textContent = 'Choose where to save in the dialog that opened (it may be behind this window)...';
    fetch('/api/save-csv', {method: 'POST', headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({name: 'bt_sensor_suspects', rows: D.csv})})
      .then(function(r) { return r.json(); })
      .then(function(r) {
        note.textContent = r.cancelled ? 'Cancelled - nothing saved.'
          : r.path ? 'Saved to ' + r.path + (r.fallback ? ' (no Save dialog available, so the Downloads folder was used)' : '')
          : 'Could not save: ' + (r.error || 'unknown error');
      })
      .catch(function(e) { note.textContent = 'Could not save: ' + e; });
  };
})();
</script>"""


def triangulation_html(result):
    """The panel + map rings for the Bluetooth sensors page, from bt_sensor_suspects.analyse()."""
    import bt_sensor_suspects as bs
    sensors = []
    order = {v: i for i, v in enumerate(bs.SUSPECT_VERDICTS)}
    for sid, v in sorted(result["sensors"].items(), key=lambda kv: (order.get(kv[1]["verdict"], 9), kv[1]["name"])):
        if v["verdict"] in bs.SUSPECT_VERDICTS:
            sensors.append({"id": sid, "name": v["name"], "site_code": v["site_code"], "lat": v["lat"],
                            "lon": v["lon"], "verdict": v["verdict"], "reason": v["reason"],
                            "paths": [{"id": p["id"], "name": p["name"], "state": p["state"]} for p in v["paths"]]})
    m = result["meta"]
    if not m["enough_history"]:
        summary = "Not enough healthy monitor runs yet (%d) to judge sensors." % m["runs_used"]
    else:
        summary = ("Based on the last %d runs in which the feed worked (%d outage runs ignored). "
                   "%d path(s) could not be tied to two sensors and are left out. "
                   "A path that works proves both of its sensors are alive; a sensor whose every path fails while "
                   "the sensors at the other ends work elsewhere is the suspect." %
                   (m["runs_used"], m["runs_ignored_as_outage"], m["unusable_paths"]))
    info = {str(sid): {"verdict": v["verdict"], "reason": v["reason"],
                       "paths": [{"id": p["id"], "name": p["name"], "state": p["state"]} for p in v["paths"]]}
            for sid, v in result["sensors"].items() if v["paths"] or v["reason"]}
    data = {"sensors": sensors, "info": info, "summary": summary, "labels": bs.VERDICTS, "csv": bs.csv_rows(result)}
    return TRIANGULATION_JS.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


def sensor_details(page):
    """{sensor id: details} for the popups on the Paths map, read from the built Bluetooth sensors page.

    The page already carries the API sensors (health, project, spreadsheet match) and the triangulation
    verdicts, so nothing is computed twice.
    """
    out = {}
    m = re.search(r"const apiMarkers = (\[.*?\]);\n", page, re.S)
    for s in (json.loads(m.group(1)) if m else []):
        out[str(s["id"])] = {k: s.get(k) for k in ("name", "site_code", "active", "health", "project", "accountability",
                                                  "ref_name", "ref_source", "ref_dist")}
    m = re.search(r"var D = (\{.*?\});\n", page, re.S)
    if m:
        data = json.loads(m.group(1))
        for sid, v in data.get("info", {}).items():
            d = out.setdefault(sid, {})
            d.update(verdict=v["verdict"], label=data["labels"].get(v["verdict"], ""), reason=v["reason"], paths=v["paths"])
    return out


# ── wiring page ──────────────────────────────────────────────────────────────

WIRING_PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Wiring</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--text:#1d2630;--dim:#5b6874;--line:#dde2e7;--high:#c0392b;--medium:#e67e22;--low:#7f8c8d}
@media (prefers-color-scheme: dark){:root{--bg:#111a22;--card:#1a2530;--text:#e3e9ee;--dim:#9fb0bd;--line:#2c3a47}}
body{margin:0;background:var(--bg);color:var(--text);font:13px Arial,sans-serif}
header{padding:14px 20px;background:#1f4e79;color:#fff}
header h1{margin:0 0 4px;font-size:18px}header .sub{font-size:12px;opacity:.85}
main{padding:14px 20px 40px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px}
button,select,input{font:13px Arial,sans-serif;padding:6px 11px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text)}
input[type=number]{width:74px;-moz-appearance:textfield}
input[type=number]::-webkit-outer-spin-button,input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
button{cursor:pointer;background:#2e86c1;color:#fff;border:0}button.sec{background:#2c3e50}button.danger{background:#c0392b}button:disabled{opacity:.5;cursor:not-allowed}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.chip{padding:5px 10px;border-radius:999px;background:var(--card);border:1px solid var(--line);cursor:pointer}
.chip.on{background:#2e86c1;color:#fff;border-color:#2e86c1}
.note{color:var(--dim);margin:6px 0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);margin:0 0 20px}
th{background:#1f4e79;color:#fff;text-align:left;padding:7px 9px;font-size:12px}
td{padding:7px 9px;border-top:1px solid var(--line);vertical-align:top}
.f{display:block;margin:0 0 3px;padding-left:8px;border-left:4px solid var(--low)}
.f.high{border-color:var(--high)}.f.medium{border-color:var(--medium)}
a.go{color:#2e86c1;cursor:pointer}
h2{font-size:15px;margin:22px 0 8px}
.msg{padding:10px 12px;border-radius:6px;background:var(--card);border:1px solid var(--line);margin:0 0 12px}
.err{border-color:var(--high);color:var(--high)}
.edit{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:0 0 14px}
.edit .row{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin:4px 0}
.edit label{display:inline-flex;gap:5px;align-items:center}
.pv td.ch{font-family:Consolas,monospace}
.del{color:var(--high)}.add{color:#27ae60}
</style></head><body>
<header><h1>Wiring check</h1><div class="sub" id="sub"></div></header>
<main>
<div class="bar"><button id="fetch">Fetch from OMNIA</button><button class="sec" id="ex">Export CSV</button><span class="note" id="status"></span></div>
<div id="body"></div>
</main>
<script>
(function() {
  var D = __DATA__;
  var shell = (window.parent !== window && window.parent.crShow) ? window.parent : null;
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  var status = document.getElementById('status'), body = document.getElementById('body');
  var fetchBtn = document.getElementById('fetch'), exBtn = document.getElementById('ex');
  var withFindings = D.paths.filter(function(p) { return p.findings.length; }).length;
  document.getElementById('sub').textContent = D.has_snapshot
    ? 'OMNIA data fetched ' + D.fetched_at + ' - ' + D.meta.paths + ' paths, ' + withFindings + ' with findings'
    : 'No OMNIA data yet';
  if (!D.can_fetch) { fetchBtn.disabled = true; fetchBtn.title = 'Set the OMNIA addresses in .env'; }
  function post(url, data) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data || {})})
      .then(function(r) { return r.json().then(function(j) { j.__status = r.status; return j; }); });
  }
  fetchBtn.onclick = function() {
    fetchBtn.disabled = true; status.textContent = 'Fetching from OMNIA - the path list can take up to a minute...';
    post('/api/wiring/fetch').then(function(r) {
      if (r.error) { status.textContent = r.error; fetchBtn.disabled = false; } else { location.reload(); }
    }).catch(function(e) { status.textContent = 'Could not fetch: ' + e; fetchBtn.disabled = false; });
  };
  if (!D.has_snapshot) {
    exBtn.style.display = 'none';
    body.innerHTML = '<div class="msg">The check compares what is configured in OMNIA (each path start and end node) with the API sensors and the drawn paths. '
      + (D.can_fetch ? 'Press <b>Fetch from OMNIA</b> (you must be on the internal network).'
                     : 'This PC has no OMNIA address: add the OMNIA addresses (<b>CONTROL_ROOM_OMNIA_NODES_URL</b> and <b>CONTROL_ROOM_OMNIA_PATHS_URL</b>) to the .env file, or copy a results/omnia_snapshot.json from a PC that has fetched one.') + '</div>';
    return;
  }
  exBtn.onclick = function() {
    status.textContent = 'Choose where to save in the dialog that opened (it may be behind this window)...';
    post('/api/save-csv', {name: 'bt_wiring', rows: D.csv}).then(function(r) {
      status.textContent = r.cancelled ? 'Cancelled - nothing saved.' : r.path ? 'Saved to ' + r.path + (r.fallback ? ' (no Save dialog available, so the Downloads folder was used)' : '') : 'Could not save: ' + (r.error || 'unknown error');
    }).catch(function(e) { status.textContent = 'Could not save: ' + e; });
  };

  var code = '', q = '', selected = {};
  body.innerHTML = '<div class="edit" id="edit"></div><div class="bar"><input id="q" placeholder="Search path, name or node..."></div><div class="chips" id="chips"></div>'
    + '<table><thead><tr><th><input type="checkbox" id="all" title="Select all shown"></th><th>Path ID</th><th>Path name</th><th>Start node</th><th>End node</th><th>Speed</th><th>Findings</th><th></th></tr></thead><tbody id="rows"></tbody></table>'
    + '<h2>Nodes that need attention (' + D.nodes.length + ')</h2><table><thead><tr><th>Node</th><th>Status</th><th>Detail</th></tr></thead><tbody id="nodes"></tbody></table>'
    + '<h2>API sensors no node stands for (' + D.sensors_without_node.length + ')</h2><div class="note" id="swn"></div>';

  function countSelected() { return Object.keys(selected).length; }
  function shown() {
    return D.paths.filter(function(p) {
      if (code === '__all') { /* every path */ }
      else if (code === '') { if (!p.findings.length) return false; }
      else if (!p.findings.some(function(f) { return f.code === code; })) return false;
      var hay = (p.id + ' ' + p.name + ' ' + (p.start_name || '') + ' ' + (p.end_name || '') + ' ' + p.findings.map(function(f) { return f.text; }).join(' ')).toLowerCase();
      return !q || hay.indexOf(q) >= 0;
    });
  }
  function chip(label, c, n) {
    var el = document.createElement('span');
    el.className = 'chip' + (code === c ? ' on' : ''); el.textContent = label + ' (' + n + ')';
    el.onclick = function() { code = c; draw(); };
    return el;
  }
  function draw() {
    var cs = document.getElementById('chips'); cs.innerHTML = '';
    cs.appendChild(chip('All with findings', '', withFindings));
    Object.keys(D.meta.by_code).sort(function(a, b) { return D.meta.by_code[b] - D.meta.by_code[a]; }).forEach(function(c) {
      cs.appendChild(chip(D.labels[c] ? D.labels[c][1] : c, c, D.meta.by_code[c]));
    });
    cs.appendChild(chip('All paths', '__all', D.paths.length));
    var rows = document.getElementById('rows'); rows.innerHTML = '';
    var list = shown();
    list.forEach(function(p) {
      var tr = document.createElement('tr');
      var speed = p.speed_min == null ? '-' : p.speed_min + '-' + p.speed_max;
      tr.innerHTML = '<td><input type="checkbox" data-sel="' + esc(p.id) + '"' + (selected[p.id] ? ' checked' : '') + '></td><td><b>' + esc(p.id) + '</b></td><td>' + esc(p.name) + '</td><td>' + esc(p.start_node || '-') + '<br>' + esc(p.start_name || '') + '</td><td>'
        + esc(p.end_node || '-') + '<br>' + esc(p.end_name || '') + '</td><td>' + esc(speed) + '</td><td>'
        + p.findings.map(function(f) { return '<span class="f ' + esc(f.severity) + '">' + esc(f.text) + '</span>'; }).join('')
        + '</td><td>' + (shell ? '<a class="go" data-p="' + esc(p.id) + '">show on map</a>' : '') + '</td>';
      rows.appendChild(tr);
    });
    if (!rows.children.length) rows.innerHTML = '<tr><td colspan="8" class="note">Nothing matches.</td></tr>';
    document.getElementById('all').checked = list.length > 0 && list.every(function(p) { return selected[p.id]; });
    drawEdit();
  }
  document.getElementById('q').oninput = function() { q = this.value.toLowerCase(); draw(); };
  document.getElementById('all').onchange = function() {
    var on = this.checked;
    shown().forEach(function(p) { if (on) selected[p.id] = true; else delete selected[p.id]; });
    draw();
  };
  document.addEventListener('click', function(e) {
    var a = e.target.closest && e.target.closest('[data-p]');
    if (a && shell) shell.crShow('paths', {path: a.getAttribute('data-p')});
  });
  document.addEventListener('change', function(e) {
    var c = e.target.getAttribute && e.target.getAttribute('data-sel');
    if (c) { if (e.target.checked) selected[c] = true; else delete selected[c]; drawEdit(); }
  });

  // ---- bulk edit ------------------------------------------------------------
  var preview = null;          // the last preview answer {items, refused, token}
  function nodeLabel(v) { return v && D.node_names[v] ? D.node_names[v] : ''; }
  function drawEdit() {
    var el = document.getElementById('edit');
    var keep = {};
    ['smin', 'smax', 'nstart', 'nend', 'swap', 'yes', 'rfind', 'rrep', 'rpre', 'rsuf', 'rnew', 'rstd'].forEach(function(id) { var x = document.getElementById(id); if (x) keep[id] = x.type === 'checkbox' ? x.checked : x.value; });
    var h = '<b>Bulk edit in OMNIA</b> <span class="note">' + countSelected() + ' path(s) selected (up to 50 per batch)</span>';
    h += '<div class="row"><label>Speed min <input type="number" id="smin" min="1" step="1"></label><label>Speed max <input type="number" id="smax" min="1" step="1"></label>'
      + '<label><input type="checkbox" id="swap"> swap start and end node</label>'
      + '<label>Start node id <input type="number" id="nstart" min="1" step="1"></label><span class="note" id="nsl"></span><label>End node id <input type="number" id="nend" min="1" step="1"></label><span class="note" id="nel"></span></div>'
      + '<div class="row"><b>Rename:</b><label>Find <input id="rfind" size="9"></label><label>Replace with <input id="rrep" size="9"></label><label>Prefix <input id="rpre" size="6"></label><label>Suffix <input id="rsuf" size="6"></label>'
      + '<label>New name (one path) <input id="rnew" size="14"></label><label><input type="checkbox" id="rstd"> name from its nodes (site codes)</label></div>'
      + '<div class="row"><button id="pv">Preview changes</button><button class="sec" id="pvr">Preview rename</button><button class="sec" id="pvu">Preview undo of last batch</button><button class="sec" id="pvru">Preview undo of last rename</button><button class="sec" id="clr">Clear selection</button>' + (D.can_delete ? '<button class="danger" id="del">Delete selected paths...</button>' : '') + '<span class="note" id="pvs"></span></div>'
      + '<div id="pvbox"></div>';
    el.innerHTML = h;
    Object.keys(keep).forEach(function(id) { var x = document.getElementById(id); if (x) { if (x.type === 'checkbox') x.checked = keep[id]; else x.value = keep[id]; } });
    function labels() { document.getElementById('nsl').textContent = nodeLabel(+document.getElementById('nstart').value); document.getElementById('nel').textContent = nodeLabel(+document.getElementById('nend').value); }
    document.getElementById('nstart').oninput = labels; document.getElementById('nend').oninput = labels; labels();
    document.getElementById('clr').onclick = function() { selected = {}; preview = null; draw(); };
    ['smin', 'smax', 'nstart', 'nend'].forEach(function(id) {
      document.getElementById(id).onkeydown = function(e) { if (['-', '+', 'e', 'E', '.', ','].indexOf(e.key) >= 0) e.preventDefault(); };
    });
    if (document.getElementById('del')) document.getElementById('del').onclick = function() {
      if (!countSelected()) { document.getElementById('pvs').textContent = 'Select the paths to delete first.'; return; }
      runPreview({delete: true, ids: Object.keys(selected)});
    };
    document.getElementById('pv').onclick = function() {
      var bad = '';
      [['smin', 'Speed min'], ['smax', 'Speed max'], ['nstart', 'Start node'], ['nend', 'End node']].forEach(function(f) {
        var v = document.getElementById(f[0]).value.trim();
        if (v !== '' && !/^[1-9][0-9]*$/.test(v)) bad = f[1] + ' must be a whole number above 0.';
      });
      var lo = document.getElementById('smin').value, hi = document.getElementById('smax').value;
      if (!bad && lo !== '' && hi !== '' && +lo >= +hi) bad = 'Speed min must be below speed max.';
      if (!bad && !countSelected()) bad = 'Select at least one path first.';
      if (!bad && lo === '' && hi === '' && !document.getElementById('swap').checked && !document.getElementById('nstart').value.trim() && !document.getElementById('nend').value.trim()) bad = 'Set a speed, a node, or tick swap first.';
      if (bad) { document.getElementById('pvs').textContent = bad; return; }
      runPreview({ids: Object.keys(selected), speed_min: document.getElementById('smin').value, speed_max: document.getElementById('smax').value,
                  swap: document.getElementById('swap').checked, start: document.getElementById('nstart').value, end: document.getElementById('nend').value});
    };
    document.getElementById('pvu').onclick = function() { runPreview({undo: true}); };
    document.getElementById('pvru').onclick = function() { runPreview({undo_rename: true}); };
    document.getElementById('pvr').onclick = function() {
      var r = {find: document.getElementById('rfind').value, replace: document.getElementById('rrep').value, prefix: document.getElementById('rpre').value,
               suffix: document.getElementById('rsuf').value, new_name: document.getElementById('rnew').value, standard: document.getElementById('rstd').checked};
      var s = document.getElementById('pvs');
      if (!countSelected()) { s.textContent = 'Select at least one path first.'; return; }
      if (!r.standard && !r.find && !r.prefix && !r.suffix && !r.new_name.trim()) { s.textContent = 'Give a new name, a find/replace, a prefix or a suffix first.'; return; }
      runPreview({ids: Object.keys(selected), rename: r});
    };
    if (preview) drawPreview();
  }
  function runPreview(req) {
    var s = document.getElementById('pvs'); s.textContent = 'Reading OMNIA (can take up to a minute)... nothing is sent yet.';
    post('/api/edit/plan', req).then(function(r) {
      if (r.error) { s.textContent = r.error; return; }
      preview = r; s.textContent = ''; drawPreview();
    }).catch(function(e) { s.textContent = 'Could not preview: ' + e; });
  }
  function chg(a, b) { return a === b ? esc(a == null ? '-' : a) : '<span class="del">' + esc(a == null ? '-' : a) + '</span> &rarr; <span class="add">' + esc(b == null ? '-' : b) + '</span>'; }
  function drawPreview() {
    var box = document.getElementById('pvbox'); if (!box) return;
    var h = '<div class="note">Read from OMNIA at ' + esc(preview.fetched_at) + '. Nothing has been changed yet.</div>';
    var isDel = preview.kind === 'delete';
    if (preview.kind === 'rename' && preview.items.length) {
      h += '<table class="pv"><thead><tr><th>Path ID</th><th>Path name (old to new)</th></tr></thead><tbody>';
      preview.items.forEach(function(it) { h += '<tr><td><b>' + esc(it.id) + '</b></td><td class="ch">' + chg(it.name, it.new_name) + '</td></tr>'; });
      h += '</tbody></table>';
    } else if (isDel && preview.items.length) {
      h += '<table class="pv"><thead><tr><th>Path ID (to DELETE)</th><th>Path name</th><th>Speed</th><th>Start node</th><th>End node</th></tr></thead><tbody>';
      preview.items.forEach(function(it) {
        h += '<tr><td><b>' + esc(it.id) + '</b></td><td>' + esc(it.name) + '</td><td>' + esc(it.before.speed_min == null ? '-' : it.before.speed_min + '-' + it.before.speed_max) + '</td><td>'
          + esc(it.before.start || '-') + ' ' + esc(it.start_name) + '</td><td>' + esc(it.before.end || '-') + ' ' + esc(it.end_name) + '</td></tr>';
      });
      h += '</tbody></table>';
    } else if (preview.items.length) {
      h += '<table class="pv"><thead><tr><th>Path ID</th><th>Path name</th><th>Speed min</th><th>Speed max</th><th>Start node</th><th>End node</th></tr></thead><tbody>';
      preview.items.forEach(function(it) {
        h += '<tr><td><b>' + esc(it.id) + '</b></td><td>' + esc(it.name) + '</td><td class="ch">' + chg(it.before.speed_min, it.after.speed_min) + '</td><td class="ch">' + chg(it.before.speed_max, it.after.speed_max)
          + '</td><td class="ch">' + chg(it.before.start, it.after.start) + ' ' + esc(it.start_name) + '</td><td class="ch">' + chg(it.before.end, it.after.end) + ' ' + esc(it.end_name) + '</td></tr>';
      });
      h += '</tbody></table>';
    }
    if (preview.refused.length) {
      h += '<div class="msg err"><b>' + preview.refused.length + ' path(s) will not be changed:</b><br>'
        + preview.refused.map(function(r) { return esc(r.id) + ': ' + esc(r.reason); }).join('<br>') + '</div>';
    }
    var word = isDel ? 'DELETE' : 'YES';
    if (preview.items.length && preview.token && preview.allowed) {
      h += '<div class="row"><b>' + preview.items.length + (isDel ? ' path(s) will be PERMANENTLY DELETED from OMNIA (this cannot be undone).' : preview.kind === 'rename' ? ' path(s) will be renamed in OMNIA.' : ' path(s) will be changed in OMNIA.') + '</b> Type ' + word
        + ': <input id="yes" size="8" autocomplete="off"> <button class="danger" id="apply" disabled>' + (isDel ? 'Delete from OMNIA' : 'Apply to OMNIA') + '</button></div>';
    } else if (preview.items.length) {
      h += '<div class="note">' + (isDel ? 'Deleting' : 'Editing') + ' is switched off on this PC, so this is a preview only.</div>';
    }
    h += '<div id="res"></div>';
    box.innerHTML = h;
    var yes = document.getElementById('yes'), ap = document.getElementById('apply');
    if (yes) {
      yes.oninput = function() { ap.disabled = yes.value !== word; };
      ap.onclick = function() {
        if (isDel && !confirm('Permanently delete ' + preview.items.length + ' path(s) from OMNIA?\n\nThis cannot be undone.')) return;
        ap.disabled = true; yes.disabled = true;
        document.getElementById('res').innerHTML = '<div class="note">Sending to OMNIA one path at a time...</div>';
        post('/api/edit/apply', {token: preview.token, confirm: word}).then(function(r) {
          var res = document.getElementById('res');
          if (r.__status !== 200) { res.innerHTML = '<div class="msg err">' + esc(r.error || 'failed') + '</div>'; return; }
          var ok = r.results.filter(function(x) { return x.ok; }).length;
          res.innerHTML = '<div class="msg' + (r.error ? ' err' : '') + '"><b>' + ok + ' of ' + preview.items.length + (r.kind === 'delete' ? ' path(s) deleted.</b> ' : r.kind === 'rename' ? ' path(s) renamed.</b> ' : ' path(s) changed.</b> ') + esc(r.error || '')
            + (r.kind === 'delete' ? ' Logged in results/delete_log.csv.' : r.kind === 'rename' ? ' Logged as batch ' + esc(r.batch) + ' ("Preview undo of last rename" reverts it).' : ' Logged as batch ' + esc(r.batch) + ' (results/omnia_edit_log.csv; "Preview undo of last batch" reverts it).')
            + ' <button id="rl">Reload with the new data</button></div>';
          document.getElementById('rl').onclick = function() { location.reload(); };
          preview = null;
        }).catch(function(e) { document.getElementById('res').innerHTML = '<div class="msg err">Could not apply: ' + esc(e) + '</div>'; });
      };
    }
  }

  var nodes = document.getElementById('nodes');
  D.nodes.forEach(function(n) {
    var tr = document.createElement('tr');
    tr.innerHTML = '<td><b>' + esc(n.id) + '</b> ' + esc(n.name) + '</td><td>' + esc(n.status_text) + '</td><td>' + esc(n.detail) + '</td>';
    nodes.appendChild(tr);
  });
  if (!D.nodes.length) nodes.innerHTML = '<tr><td colspan="3" class="note">Every node matches a sensor.</td></tr>';
  document.getElementById('swn').textContent = D.sensors_without_node.length
    ? D.sensors_without_node.map(function(s) { return s[1] + ' (id ' + s[0] + ')'; }).join(', ') : 'None.';
  draw();
})();
</script></body></html>
"""


def build_wiring_page(result, snapshot, can_fetch, can_edit=False, can_delete=False):
    """The Wiring tab: configured OMNIA nodes versus the API sensors and the drawn paths."""
    import bt_wiring as bw
    data = {"has_snapshot": bool(result), "can_fetch": bool(can_fetch), "can_edit": bool(can_edit), "can_delete": bool(can_delete), "labels": bw.FINDINGS,
            "node_names": {n["id"]: n["name"] for n in (snapshot or {}).get("nodes", [])},
            "fetched_at": (snapshot or {}).get("fetched_at", ""), "meta": {}, "paths": [], "nodes": [],
            "sensors_without_node": [], "csv": []}
    if result:
        order = {"high": 0, "medium": 1, "low": 2}
        paths = list(result["paths"].values())          # every path; the page filters to those with findings
        paths.sort(key=lambda p: (order.get(p["severity"], 3), int(p["id"]) if str(p["id"]).isdigit() else 0))
        nodes = []
        for m in sorted(result["nodes"].values(), key=lambda m: m["id"]):
            if m["status"] == "ok" and not m["notes"]:
                continue
            detail = []
            if m["sensor_name"] and m["status"] != "ok":
                detail.append("sensor '%s' (%s m away)" % (m["sensor_name"], m["distance_m"]))
            if m["similar"]:
                detail.append("looks like: " + ", ".join(m["similar"]))
            detail += m["notes"]
            nodes.append({"id": m["id"], "name": m["name"], "status_text": bw.NODE_STATUS[m["status"]],
                          "detail": "; ".join(detail)})
        data.update(meta=result["meta"], paths=paths, nodes=nodes,
                    sensors_without_node=result["sensors_without_node"], csv=bw.csv_rows(result))
    return WIRING_PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


# ── sensor-location pages ────────────────────────────────────────────────────

def sensor_views():
    """slug -> (DB group, workbook sheet, match radius m), from update_projects.GROUPS."""
    import update_projects
    return {g.lower().replace(" ", "-"): (g, sheet, dist) for g, sheet, dist in update_projects.GROUPS}


NAV_JS = """<script>
(function() {
  var q = new URLSearchParams(location.search);
  var lat = q.get('lat'), lon = q.get('lon'), pathId = q.get('path'), last = null;
  var shell = (window.parent !== window && window.parent.crShow) ? window.parent : null;
  var nav = document.querySelector('.crnav');
  function pill(html, title, fn) {
    var a = document.createElement('a');
    a.href = '#'; a.innerHTML = html; a.title = title;
    a.onclick = function(e) { e.preventDefault(); fn(); };
    nav.appendChild(a);
    return a;
  }
  if (pathId && shell) {
    pill('&larr; Back to path ' + pathId, 'Select this path on the Paths tab',
         function() { shell.crShow('paths', {path: pathId}); });
    nav.classList.add('on');
  }
  var here = pill('Paths here', 'Centre the Paths tab on the spot shown here', function() {
    if (last && shell) shell.crShow('paths', {lat: last[0], lon: last[1]});
  });
  here.style.display = 'none';

  window.addEventListener('load', function() {
    if (typeof flyTo === 'function') {
      var orig = flyTo;
      window.flyTo = function(a, b) { last = [a, b]; if (shell) { here.style.display = ''; nav.classList.add('on'); } return orig.apply(this, arguments); };
      if (lat && lon) setTimeout(function() { flyTo(+lat, +lon, pathId ? 'Near path ' + pathId : 'Location'); }, 300);
    }
  });
})();
</script>"""


def add_nav(page, current, views):
    """Float the jump pills (Back to path / Paths here) over a generated sensor page."""
    nav = ('<style>.crnav{position:fixed;bottom:10px;left:50%;transform:translateX(-50%);z-index:99999;'
           'display:none;gap:4px;background:#1b2631;padding:5px;border-radius:999px;'
           'box-shadow:0 2px 10px rgba(0,0,0,.45);font:13px Arial,sans-serif}'
           '.crnav.on{display:flex}'
           '.crnav a{color:#cfd8dc;text-decoration:none;padding:6px 14px;border-radius:999px}'
           '.crnav a:hover{background:#34495e}</style>'
           '<div class="crnav"></div>') + NAV_JS
    return page.replace("</body>", nav + "</body>", 1) if "</body>" in page else page + nav


SHELL_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Control Room</title>
<style>
html,body{margin:0;height:100%;background:#0f1720;font:13px Arial,sans-serif}
#bar{height:38px;display:flex;align-items:center;gap:4px;padding:0 10px;background:#1b2631;color:#cfd8dc}
#bar b{margin-right:10px;color:#fff;font-size:14px}
#bar .sep{opacity:.55;margin:0 6px 0 12px}
#bar a{color:#cfd8dc;text-decoration:none;padding:6px 14px;border-radius:6px;cursor:pointer}
#bar a.on{background:#2e86c1;color:#fff}
#bar a:hover{background:#34495e}
#bar .sp{flex:1}
#bar .ro{background:#7d6608;color:#fff;border-radius:4px;padding:3px 9px;margin-right:8px;font-size:12px}
#stage{position:absolute;top:38px;left:0;right:0;bottom:0}
#stage iframe{position:absolute;top:0;left:0;width:100%;height:100%;border:0;visibility:hidden}
#stage iframe.on{visibility:visible}
</style></head><body>
<div id="bar"><b>Control Room</b><a data-t="paths" class="on">Paths</a><a data-t="wiring" title="Are the paths wired to the sensors they are drawn between?">Wiring</a><span class="sep">Sensor locations:</span>__TABS__<span class="sp"></span>__BADGE__<a id="rf" title="Rebuild the open tab from the latest data">Refresh</a><a id="qt" title="Stop the Control Room (the console closes by itself)">Quit</a></div>
<div id="stage"><iframe id="f-paths" class="on" src="/map__Q__"></iframe><iframe id="f-wiring"></iframe>__FRAMES__</div>
<script>
var cur = 'paths';
function frame(t) { return document.getElementById('f-' + t); }
function render() {
  Array.prototype.forEach.call(document.querySelectorAll('#bar a[data-t]'), function(a) {
    a.className = a.getAttribute('data-t') === cur ? 'on' : '';
  });
  Array.prototype.forEach.call(document.querySelectorAll('#stage iframe'), function(f) {
    f.className = f.id === 'f-' + cur ? 'on' : '';
  });
}
// Called by the pages inside the frames: show a tab, optionally aimed at a path or a spot.
window.crShow = function(t, o) {
  o = o || {};
  var f = frame(t);
  if (t === 'wiring') {
    if (!f.getAttribute('src')) f.src = '/wiring';
  } else if (t !== 'paths') {
    var q = o.lat != null ? '?lat=' + o.lat + '&lon=' + o.lon + (o.path ? '&path=' + encodeURIComponent(o.path) : '') : '';
    if (q || !f.getAttribute('src')) f.src = '/sensors/' + t + q;
  } else {
    var w = f.contentWindow;
    if (o.path && w.crFocusPath) w.crFocusPath(o.path);
    else if (o.lat != null && w.crFocusLoc) w.crFocusLoc(o.lat, o.lon);
  }
  cur = t;
  render();
};
Array.prototype.forEach.call(document.querySelectorAll('#bar a[data-t]'), function(a) {
  a.onclick = function() { window.crShow(a.getAttribute('data-t')); };
});
document.getElementById('rf').onclick = function() {
  var btn = this, f = frame(cur);
  var base = cur === 'paths' ? '/map' : cur === 'wiring' ? '/wiring' : '/sensors/' + cur;
  btn.textContent = 'Refreshing...';
  fetch(base + '?refresh=1').then(function() {
    f.contentWindow.location.reload();
    btn.textContent = 'Refresh';
  });
};
document.getElementById('qt').onclick = function() {
  if (!confirm('Stop the Control Room? Flags are already saved.')) return;
  fetch('/api/quit', {method: 'POST'}).then(function() {
    window.close();   // works when the browser allows it (tab opened by the launcher); otherwise the note below stays
    document.body.innerHTML = '<p style="color:#cfd8dc;padding:30px;font:15px Arial">Control Room stopped. You can close this tab.</p>';
  });
};
</script></body></html>"""


def build_shell(views, query="", read_only=False):
    """The app page: tab bar + one iframe per view, all kept alive so switching never reloads."""
    tabs = "".join(f'<a data-t="{slug}">{group}</a>' for slug, (group, _s, _d) in views.items())
    frames = "".join(f'<iframe id="f-{slug}"></iframe>' for slug in views)
    badge = '<span class="ro" title="Deleting is switched off on this PC">Read-only</span>' if read_only else ""
    return (SHELL_HTML.replace("__TABS__", tabs).replace("__FRAMES__", frames)
            .replace("__Q__", query).replace("__BADGE__", badge))


# Taller map than the standalone QA page (same width); the header and cards above stay visible.
TALL_MAP_CSS = "<style>#map{height:max(420px,68vh)!important}</style>"


def build_sensor_page(slug, views=None):
    """Sensor-vs-spreadsheet page for one group. Read-only: no DB writes."""
    import tempfile
    import qa
    views = views or sensor_views()
    group, sheet, max_dist = views[slug]
    workbook = ROOT / "QA Locations.xlsx"
    if not workbook.exists():
        return add_nav(f"<body style='font:15px Arial;padding:30px'><h2>{group}</h2>"
                       f"<p>QA Locations.xlsx was not found in the repo folder, so there is nothing to compare "
                       f"the API sensors against.</p></body>", slug, views)
    api_sensors = qa.load_api_sensors(group)
    ref_sensors, not_electrified = qa.load_reference([f"{workbook}::{sheet}"])
    matches = qa.match_sensors(ref_sensors, api_sensors, max_dist=max_dist)
    qa.annotate_accountability(api_sensors, matches, qa.load_project_accountability(), max_dist=max_dist)
    qa.annotate_ref_accountability(ref_sensors, qa.load_project_accountability())
    with tempfile.TemporaryDirectory() as tmp:
        out = qa.generate_html(group, api_sensors, ref_sensors, matches, Path(tmp) / "qa.html",
                               not_electrified=not_electrified, max_dist=max_dist)
        page = Path(out).read_text(encoding="utf-8")
    if group == "Bluetooth":
        import bt_sensor_suspects
        tri = triangulation_html(bt_sensor_suspects.analyse())
        page = page.replace("</body>", tri + "</body>", 1) if "</body>" in page else page + tri
    page = page.replace("</head>", TALL_MAP_CSS + "</head>", 1)
    return add_nav(page, slug, views)


# ── server ───────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ControlRoom"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _same_origin(self):
        """Refuse browser requests that did not come from this app's own page."""
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        return origin is None or origin in allowed

    def _drain(self):
        """Read the request body up front, so an early 403/400 never leaves it unread (a reset connection)."""
        n = int(self.headers.get("Content-Length") or 0)
        self._raw = self.rfile.read(n) if 0 < n <= 2_000_000 else b""
        self._too_big = n > 2_000_000

    def _body(self):
        if self._too_big:
            raise ValueError("body too large")
        return json.loads(self._raw or b"{}")

    def do_GET(self):
        parts = urlsplit(self.path)
        path, force = parts.path, "refresh" in parse_qs(parts.query)
        if path == "/api/review":
            return self._send(200, load_review(self.server.review_file))
        if path in ("/", "/index.html"):
            qs = parse_qs(parts.query).get("path")
            query = "?path=" + qs[0] if qs and qs[0].isdigit() else ""
            return self._send(200, build_shell(self.server.views, query, not self.server.allow_delete).encode("utf-8"), "text/html; charset=utf-8")
        if path == "/map":
            try:
                page = self.server.cached("map", lambda: self.server.page_builder()[0], force)
            except Exception as exc:
                return self._send(500, f"Could not build the map: {exc}".encode("utf-8"), "text/plain")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/sensor":
            sid = (parse_qs(parts.query).get("id") or [""])[0]
            try:
                import bt_wiring
                out = dict(self.server.sensor_details().get(sid, {}))
                result, _snap = self.server.wiring()
                if result:
                    w = bt_wiring.wiring_for_sensor(result, sid)
                    if w["nodes"] or w["paths"]:
                        out["wiring"] = w
                return self._send(200, out)
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
        if path == "/wiring":
            try:
                result, snap = self.server.wiring()
                page = build_wiring_page(result, snap, _can_fetch(self.server.omnia), self.server.allow_edit and bool(self.server.omnia["edit"]),
                                         self.server.allow_delete and bool(self.server.delete_url))
            except Exception as exc:
                return self._send(500, f"Could not build the page: {exc}".encode("utf-8"), "text/plain")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/wiring":
            pid = (parse_qs(parts.query).get("path") or [""])[0]
            try:
                result, _snap = self.server.wiring()
                return self._send(200, (result or {"paths": {}})["paths"].get(pid, {}))
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
        if path.startswith("/sensors/"):
            slug = path[len("/sensors/"):].strip("/")
            views = self.server.views
            if slug not in views:
                return self._send(404, {"error": "unknown sensor group"})
            try:
                page = self.server.cached(slug, lambda: self.server.sensor_builder(slug), force)
            except Exception as exc:
                return self._send(500, f"Could not build the page: {exc}".encode("utf-8"), "text/plain")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        self._drain()
        if self.path != "/api/review" or not self._same_origin():
            return self._send(403, {"error": "forbidden"})
        try:
            data = self._body()
            if not isinstance(data, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        save_review(data, self.server.review_file)
        self._send(200, {"saved": len(clean_review(data))})

    def _standard_names(self, snap, ids):
        """{path id: name built from the site codes of its two nodes' sensors, e.g. 1001->1002}."""
        import bt_wiring
        _feed, sensors = self.server.wiring_inputs()
        matches = bt_wiring.match_nodes(snap["nodes"], sensors)
        out = {}
        paths = {str(p["id"]): p for p in snap["paths"]}
        for pid in ids:
            p = paths.get(str(pid))
            a, b = (matches.get(p.get("start")), matches.get(p.get("end"))) if p else (None, None)
            if a and b and a["sensor"] is not None and b["sensor"] is not None:
                out[str(pid)] = "%s->%s" % (sensors[a["sensor"]].get("site_code"), sensors[b["sensor"]].get("site_code"))
        return out

    def _edit(self, action):
        """Bulk edit of OMNIA paths: 'plan' previews (reads OMNIA live, sends nothing), 'apply' writes."""
        import omnia_edit
        import omnia_fetch
        srv = self.server
        try:
            body = self._body()
            if not _can_fetch(srv.omnia):
                return self._send(400, {"error": "The OMNIA addresses are not set on this PC (CONTROL_ROOM_OMNIA_NODES_URL and "
                                                 "CONTROL_ROOM_OMNIA_PATHS_URL in .env)."})
            fetch = srv.fetch_omnia or omnia_fetch.fetch_snapshot
            snap = fetch(srv.omnia)                 # always the live state, never an old snapshot
            omnia_fetch.save_snapshot(snap, srv.snapshot_file)
            if action == "plan":
                if body.get("delete"):
                    plan = omnia_edit.plan_deletes(snap, body.get("ids") or [])
                elif body.get("undo_rename"):
                    plan = omnia_edit.undo_renames(srv.rename_log, snap)
                elif body.get("rename"):
                    r = body["rename"]
                    standard = self._standard_names(snap, body.get("ids") or []) if r.get("standard") else None
                    plan = omnia_edit.plan_renames(snap, body.get("ids") or [], r.get("find") or "", r.get("replace") or "",
                                                   r.get("prefix") or "", r.get("suffix") or "", r.get("new_name") or "", standard)
                elif body.get("undo"):
                    plan = omnia_edit.undo_items(srv.edit_log, snap)
                else:
                    plan = omnia_edit.plan_edits(snap, body.get("ids") or [], body.get("speed_min"), body.get("speed_max"),
                                                 bool(body.get("swap")), body.get("start"), body.get("end"))
                kind = "delete" if body.get("delete") else "rename" if (body.get("rename") or body.get("undo_rename")) else "edit"
                token = None
                if plan["items"]:
                    token = secrets.token_hex(8)
                    srv.edit_tokens = {token: (kind, plan["items"])}      # only the newest preview can be applied
                allowed = (srv.allow_delete and bool(srv.delete_url)) if kind == "delete" else (srv.allow_edit and bool(srv.omnia["rename" if kind == "rename" else "edit"]))
                return self._send(200, dict(plan, token=token, kind=kind, allowed=allowed, fetched_at=snap["fetched_at"]))
            held = srv.edit_tokens.get(body.get("token"))
            kind = held[0] if held else "edit"
            if kind == "delete":
                if not (srv.allow_delete and srv.delete_url):
                    return self._send(403, {"error": "Deleting is switched off on this PC (CONTROL_ROOM_ALLOW_DELETE=1 and the delete address in .env)."})
                if body.get("confirm") != "DELETE":
                    return self._send(400, {"error": "Type DELETE to confirm."})
            else:
                needed = "CONTROL_ROOM_OMNIA_RENAME_URL" if kind == "rename" else "CONTROL_ROOM_OMNIA_EDIT_URL"
                if not (srv.allow_edit and srv.omnia["rename" if kind == "rename" else "edit"]):
                    return self._send(403, {"error": "Editing is switched off on this PC (CONTROL_ROOM_ALLOW_EDIT=1 and "
                                                     + needed + " in .env)."})
                if body.get("confirm") != "YES":
                    return self._send(400, {"error": "Type YES to confirm."})
            if not held:
                return self._send(400, {"error": "Preview the changes first (the preview can be applied once)."})
            items = srv.edit_tokens.pop(body.get("token"))[1]
            stale = omnia_edit.stale_ids(items, snap)
            if stale:
                return self._send(409, {"error": "Changed in OMNIA since the preview (paths %s). Preview again." % ", ".join(stale), "stale": stale})
            if kind == "delete":
                results, error = delete_paths([it["id"] for it in items], srv.delete_url, post=srv.post)
                log_deletes(results, {it["id"]: it["name"] for it in items}, srv.delete_log)
                srv.page_cache.clear()                  # the map page still lists the deleted paths
                batch = "deleted"
            elif kind == "rename":
                batch, results, error = omnia_edit.apply_renames(items, srv.omnia["rename"], srv.rename_log, post=srv.omnia_post)
            else:
                batch, results, error = omnia_edit.apply_items(items, srv.omnia["edit"], srv.edit_log, post=srv.omnia_post)
            after = fetch(srv.omnia)
            if kind == "rename":
                took = omnia_edit.verify_renames(items, after)      # OMNIA's answer is not trusted: read the names back
                for r in results:
                    r["ok"] = r["ok"] and took.get(r["id"], False)
                if not error and not all(r["ok"] for r in results):
                    error = "OMNIA answered but did not change every name (see the list); nothing else was sent."
            omnia_fetch.save_snapshot(after, srv.snapshot_file)
            return self._send(200, {"batch": batch, "kind": kind, "results": results, "error": error, "fetched_at": after["fetched_at"]})
        except omnia_fetch.OmniaError as exc:
            return self._send(502, {"error": str(exc)})
        except (ValueError, TypeError, AttributeError) as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": "could not complete: " + type(exc).__name__})

    def do_POST(self):
        self._drain()
        if self.path == "/api/quit" and self._same_origin():
            self._send(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            if self.server.on_quit:
                self.server.on_quit()
            return
        if self.path in ("/api/edit/plan", "/api/edit/apply") and self._same_origin():
            return self._edit(self.path.rsplit("/", 1)[1])
        if self.path == "/api/wiring/fetch" and self._same_origin():
            import omnia_fetch
            try:
                snap = (self.server.fetch_omnia or omnia_fetch.fetch_snapshot)(self.server.omnia)
                omnia_fetch.save_snapshot(snap, self.server.snapshot_file)
            except omnia_fetch.OmniaError as exc:
                return self._send(502, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": "could not fetch: " + type(exc).__name__})
            return self._send(200, {"fetched_at": snap["fetched_at"], "paths": len(snap["paths"]), "nodes": len(snap["nodes"])})
        if self.path == "/api/save-csv" and self._same_origin():
            try:
                body = self._body()
                name = body.get("name")
                folder = Path(self.server.downloads_dir or DOWNLOADS)
                dest, fallback = None, False
                if self.server.choose_path and name in EXPORT_NAMES:
                    try:
                        dest = self.server.choose_path(folder, default_csv_name(name))
                    except Exception:      # no dialog possible on this PC: save to Downloads instead
                        fallback = True
                    else:
                        if dest is None:
                            return self._send(200, {"cancelled": True})
                out = save_csv(name, body.get("rows"), folder, dest)
            except (ValueError, AttributeError, TypeError) as exc:
                return self._send(400, {"error": str(exc)})
            except OSError as exc:
                return self._send(500, {"error": f"could not write the file: {exc}"})
            return self._send(200, {"path": str(out), "fallback": fallback})
        if self.path != "/api/delete" or not self._same_origin():
            return self._send(403, {"error": "forbidden"})
        if not self.server.allow_delete:
            return self._send(403, {"error": "Deleting is switched off on this PC (read-only mode)."})
        try:
            body = self._body()
            ids = [str(i) for i in body.get("ids", [])]
        except (ValueError, AttributeError, TypeError):
            return self._send(400, {"error": "bad request"})

        review = load_review(self.server.review_file)
        paths = self.server.paths_provider()
        ok, refused = plan_delete(ids, review, set(paths))

        if body.get("dry_run", True):
            token = secrets.token_hex(8)
            self.server.tokens[token] = tuple(sorted(ok))
            return self._send(200, {"would_delete": ok, "refused": refused, "token": token})

        # live delete: needs typed YES + the token of a dry run for exactly this list
        if not self.server.delete_url:
            return self._send(400, {"error": "CONTROL_ROOM_DELETE_URL is not set in this PC's .env."})
        if body.get("confirm") != "YES":
            return self._send(400, {"error": "Type YES to confirm."})
        if refused or not ok or self.server.tokens.pop(body.get("token"), None) != tuple(sorted(ok)):
            return self._send(400, {"error": "Run a dry run for this exact list first."})
        results, error = delete_paths(ok, self.server.delete_url, post=self.server.post)
        log_deletes(results, {pid: paths[pid]["name"] for pid in ok if pid in paths}, self.server.delete_log)
        self.server.page_cache.clear()
        self._send(200, {"results": results, "error": error})


def _can_fetch(omnia):
    return bool(omnia.get("nodes") and omnia.get("paths"))


def current_sensors():
    import db
    return db.fetch_sensor_coords().get("Bluetooth", {})


def current_paths():
    import bt_paths_map as m
    return m.fetch_bt_path_coords()


DB_FILE = ROOT / "results" / "history.db"
DOWNLOADS = Path.home() / "Downloads"
SNAPSHOT_FILE = ROOT / "results" / "omnia_snapshot.json"
EDIT_LOG = ROOT / "results" / "omnia_edit_log.csv"
RENAME_LOG = ROOT / "results" / "omnia_rename_log.csv"
EXPORT_NAMES = ("bt_sensor_suspects", "bt_wiring")   # the only files the page may ask the server to write


def default_csv_name(name):
    return f"{name}_{datetime.now():%Y%m%d_%H%M%S}.csv"


_SAVE_DIALOG = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.asksaveasfilename(parent=root, title="Save CSV", initialdir=sys.argv[1], initialfile=sys.argv[2],
                                    defaultextension=".csv", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
sys.stdout.buffer.write(path.encode("utf-8"))
"""


def choose_save_path(initial_dir, initial_name):
    """Ask on this PC's desktop where to save (a normal Save as dialog). Path, or None if cancelled.

    Runs in its own python process so the dialog never fights the server threads; raises if the
    dialog cannot be shown.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")      # pythonw has no stdout to read the answer from
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run([str(exe), "-c", _SAVE_DIALOG, str(initial_dir), initial_name],
                       capture_output=True, timeout=900, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:] or "the save dialog failed")
    chosen = r.stdout.decode("utf-8").strip()
    return Path(chosen) if chosen else None


def save_csv(name, rows, folder=None, dest=None):
    """Write `rows` as a CSV into the Downloads folder and return its path.

    The window the app runs in may block browser downloads, so exports are written by the
    server instead. Only whitelisted names are accepted and the file name carries a timestamp.
    """
    if name not in EXPORT_NAMES:
        raise ValueError("unknown export")
    if (not isinstance(rows, list) or len(rows) > 50000
            or not all(isinstance(r, list) and all(isinstance(v, (str, int, float, type(None))) for v in r)
                       for r in rows)):
        raise ValueError("rows must be a list of lists of plain values")
    folder = Path(folder or DOWNLOADS)
    if dest is not None:                      # chosen by the user in the Save dialog
        out = Path(dest)
        if out.suffix.lower() != ".csv":
            out = out.with_name(out.name + ".csv")
    else:
        out = folder / default_csv_name(name)
    out.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    csv.writer(buf).writerows([["" if v is None else v for v in r] for r in rows])
    out.write_bytes(buf.getvalue().encode("utf-8-sig"))   # BOM for Excel; bytes keep the csv module line endings as they are
    return out


class Server(ThreadingHTTPServer):
    """HTTP server that remembers built pages until results/history.db changes."""

    def cached(self, key, build, force=False):
        stamp = os.stat(self.db_file).st_mtime if os.path.exists(self.db_file) else 0
        with self.build_lock:
            hit = self.page_cache.get(key)
            if hit and hit[0] == stamp and not force:
                return hit[1]
            page = build()
            self.page_cache[key] = (stamp, page)
            return page

    def wiring(self):
        """(analysis, snapshot) from the saved OMNIA snapshot, or (None, None) if there is none."""
        import bt_wiring
        import omnia_fetch
        stamps = tuple(os.stat(f).st_mtime if os.path.exists(f) else 0 for f in (self.snapshot_file, self.db_file))
        with self.build_lock:
            hit = getattr(self, "_wiring", None)
            if hit and hit[0] == stamps:
                return hit[1], hit[2]
            snap = omnia_fetch.load_snapshot(self.snapshot_file)
            result = None
            if snap:
                feed, sensors = self.wiring_inputs()
                result = bt_wiring.analyse(snap, feed, sensors)
            self._wiring = (stamps, result, snap)
            return result, snap

    def sensor_details(self):
        """Details of every Bluetooth sensor, rebuilt only when the Bluetooth page itself was rebuilt."""
        if "bluetooth" not in self.views:
            return {}
        page = self.cached("bluetooth", lambda: self.sensor_builder("bluetooth"))
        with self.build_lock:
            if getattr(self, "_details", (None, None))[0] is not page:
                self._details = (page, sensor_details(page))
            return self._details[1]

    def warm(self):
        """Build every page once, so the first browser load is instant."""
        for key, build in [("map", lambda: self.page_builder()[0])] + \
                          [(slug, (lambda s=slug: self.sensor_builder(s))) for slug in self.views]:
            try:
                self.cached(key, build)
            except Exception:
                pass  # surfaced when the page is requested


def make_server(port=DEFAULT_PORT, review_file=REVIEW_FILE, delete_log=DELETE_LOG,
                page_builder=None, paths_provider=None, post=None,
                sensor_builder=None, views=None, db_file=None, snapshot_file=None, omnia_urls=None, allow_edit=None, edit_log=None, rename_log=None, omnia_post=None,
                fetch_omnia=None, wiring_inputs=None, allow_delete=None, delete_url=None, downloads_dir=None, choose_path=choose_save_path):
    srv = Server(("127.0.0.1", port), Handler)
    srv.db_file = db_file or DB_FILE
    srv.page_cache = {}
    srv.build_lock = threading.Lock()
    srv.review_file = review_file
    srv.delete_log = delete_log
    srv.allow_delete = allow_delete_setting() if allow_delete is None else allow_delete
    srv.delete_url = delete_url_setting() if delete_url is None else delete_url
    srv.page_builder = page_builder or (lambda: build_page(srv.allow_delete))
    srv.paths_provider = paths_provider or current_paths
    srv.post = post
    srv.views = views if views is not None else sensor_views()
    srv.sensor_builder = sensor_builder or (lambda slug: build_sensor_page(slug, srv.views))
    srv.snapshot_file = snapshot_file or SNAPSHOT_FILE
    srv.allow_edit = allow_edit_setting() if allow_edit is None else allow_edit
    srv.edit_log = edit_log or EDIT_LOG
    srv.rename_log = rename_log or RENAME_LOG
    srv.edit_tokens = {}
    srv.omnia_post = omnia_post         # None = httpx.post; tests pass a fake
    srv.omnia = dict({"nodes": None, "paths": None, "edit": None, "rename": None}, **(omnia_urls_setting() if omnia_urls is None else omnia_urls))
    srv.fetch_omnia = fetch_omnia      # None = the real OMNIA fetch; tests pass a fake
    srv.wiring_inputs = wiring_inputs or (lambda: (srv.paths_provider(), current_sensors()))
    srv.tokens = {}
    srv.downloads_dir = downloads_dir
    srv.choose_path = choose_path   # the Save as dialog; tests pass None or a fake
    srv.on_quit = None   # set by the app-window launcher to close the window
    return srv


def window_available():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def run_window(srv, url):
    """Show the app in its own window; closing the window stops the server."""
    import webview
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass   # older pywebview: no such setting
    win = webview.create_window("Control Room", url, width=1500, height=950, min_size=(900, 600))
    srv.on_quit = win.destroy
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    webview.start()          # blocks until the window is closed
    srv.shutdown()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--browser", action="store_true", help="open in your browser instead of the app window")
    ap.add_argument("--no-browser", action="store_true", help="start the server only (implies --browser)")
    args = ap.parse_args()
    if sys.stdout is None or sys.stderr is None:   # pythonw: no console, so keep a log instead
        sys.stdout = sys.stderr = open(ROOT / "results" / "control_room.log", "a", encoding="utf-8", buffering=1)
    srv = make_server(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    threading.Thread(target=srv.warm, daemon=True).start()

    use_window = not (args.browser or args.no_browser) and window_available()
    if use_window:
        print("Control Room starting in its own window...")
        run_window(srv, url)
        print("Stopped.")
        return

    if not (args.browser or args.no_browser):
        print("(Install the app window with: pip install pywebview - opening in your browser for now.)")
    print(f"Control Room running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
