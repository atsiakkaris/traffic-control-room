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
    """POST path-delete for each id in order; stop at the first failure.

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
<div id="bar"><b>Control Room</b><a data-t="paths" class="on">Paths</a><span class="sep">Sensor locations:</span>__TABS__<span class="sp"></span>__BADGE__<a id="rf" title="Rebuild the open tab from the latest data">Refresh</a><a id="qt" title="Stop the Control Room (the console closes by itself)">Quit</a></div>
<div id="stage"><iframe id="f-paths" class="on" src="/map__Q__"></iframe>__FRAMES__</div>
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
  if (t !== 'paths') {
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
  var base = cur === 'paths' ? '/map' : '/sensors/' + cur;
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
                return self._send(200, self.server.sensor_details().get(sid, {}))
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

    def do_POST(self):
        self._drain()
        if self.path == "/api/quit" and self._same_origin():
            self._send(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            if self.server.on_quit:
                self.server.on_quit()
            return
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


def current_paths():
    import bt_paths_map as m
    return m.fetch_bt_path_coords()


DB_FILE = ROOT / "results" / "history.db"
DOWNLOADS = Path.home() / "Downloads"
EXPORT_NAMES = ("bt_sensor_suspects",)   # the only files the page may ask the server to write


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
                sensor_builder=None, views=None, db_file=None, allow_delete=None, delete_url=None, downloads_dir=None, choose_path=choose_save_path):
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
