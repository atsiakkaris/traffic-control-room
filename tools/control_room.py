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
SENSOR_NOTES_FILE = ROOT / "results" / "sensor_notes.json"
MAX_NOTE = 2000
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


# ── sensor notes ─────────────────────────────────────────────────────────────

def load_sensor_notes(path=SENSOR_NOTES_FILE):
    """{"<group slug>:<sensor id>": {"note": text, "at": ISO time}} - well-formed entries only."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, e in (data.items() if isinstance(data, dict) else []):
        if isinstance(e, dict) and isinstance(e.get("note"), str) and e["note"].strip():
            out[str(key)] = {"note": e["note"][:MAX_NOTE], "at": str(e.get("at", ""))}
    return out


def set_sensor_note(group, sensor_id, note, groups, path=SENSOR_NOTES_FILE):
    """Save (or, when the text is empty, remove) the note of one sensor. Returns the entry or None."""
    sensor_id = str(sensor_id)
    if group not in groups or not re.fullmatch(r"[\w.\-]{1,40}", sensor_id):
        raise ValueError("unknown sensor group or id")
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ValueError("a note can be at most %d characters" % MAX_NOTE)
    notes = load_sensor_notes(path)
    key = "%s:%s" % (group, sensor_id)
    if note.strip():
        notes[key] = {"note": note, "at": datetime.now().isoformat(timespec="seconds")}
    else:
        notes.pop(key, None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(notes, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return notes.get(key)


def notes_for_group(group, notes):
    return {k.split(":", 1)[1]: v for k, v in notes.items() if k.startswith(group + ":")}


# ── site visits: what was found on site ──────────────────────────────────────

SITE_VISITS_FILE = ROOT / "results" / "site_visits.json"
VISIT_KEY_RE = re.compile(r"^[\w\-]+\|(api|ref)\|[^\x00-\x1f]{1,200}$")


def load_visits(path=SITE_VISITS_FILE):
    """{item key: finding} - only well-formed entries. Keys look like 'bluetooth|api|9' (see site_visits.collect_items)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict) and VISIT_KEY_RE.match(str(k))} if isinstance(data, dict) else {}


def save_visits(store, path=SITE_VISITS_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def mark_visit(store, key, done, group="", sensor_id="", name="", now=None):
    """Mark one item visited by hand (or take it back). Findings that came from an imported sheet are never lost:
    taking the mark back only clears 'done'; an entry that holds nothing but the mark is removed."""
    if not VISIT_KEY_RE.match(str(key)):
        raise ValueError("unknown item")
    e = store.get(key)
    if done:
        if e is None:
            e = store[key] = {"source": "manual", "group": str(group)[:80], "id": str(sensor_id)[:40], "name": str(name)[:200],
                              "at": (now or datetime.now()).isoformat(timespec="seconds")}
        e["done"] = True
    elif e is not None:
        if e.get("source") == "manual" and not any(e.get(f) for f in ("found", "right", "label", "remarks")) and e.get("lat") is None:
            del store[key]
        else:
            e["done"] = False
    return store.get(key)


NOTES_JS = """<style>
.crnote{margin-top:8px;padding-top:8px;border-top:1px dashed #999;min-width:230px}
.crnote textarea{width:100%;box-sizing:border-box;min-height:62px;resize:vertical;font:13px Arial,sans-serif;padding:5px;border:1px solid #bbb;border-radius:5px}
.crnote small{color:#777}
</style>
<script>
(function() {
  var G = '__GROUP__', notes = {}, ring = L.layerGroup().addTo(map);
  var pts = typeof apiMarkers !== 'undefined' ? apiMarkers : (typeof SENSORS !== 'undefined' ? SENSORS : []);
  var BADGE = '<div style="background:#f39c12;color:#fff;border:1px solid #fff;border-radius:3px;width:16px;height:16px;font:bold 11px Arial;text-align:center;line-height:15px;box-shadow:0 0 3px rgba(0,0,0,.5)">N</div>';
  var badges = {}, idx = null, syncTimer = null;
  // The badge belongs to its sensor's dot: it is on the map exactly while that dot is.
  function markerFor(s) {
    if (typeof sensorMarkersById !== 'undefined' && sensorMarkersById[s.id]) return sensorMarkersById[s.id];
    if (typeof CAT_LAYERS !== 'undefined') {
      if (!idx) {
        idx = {};
        ['g', 'o', 'r', 'n', 'x', 'ob', 'oo'].forEach(function(c) {
          if (CAT_LAYERS[c]) CAT_LAYERS[c].eachLayer(function(m) { if (m.getLatLng) idx[m.getLatLng().lat.toFixed(5) + ',' + m.getLatLng().lng.toFixed(5)] = m; });
        });
      }
      return idx[s.lat.toFixed(5) + ',' + s.lon.toFixed(5)] || null;
    }
    return null;
  }
  function sync() {
    pts.forEach(function(s) {
      var b = badges[s.id];
      if (!b) return;
      var m = markerFor(s), vis = !m || map.hasLayer(m);
      if (vis && !ring.hasLayer(b)) ring.addLayer(b);
      if (!vis && ring.hasLayer(b)) ring.removeLayer(b);
    });
  }
  map.on('layeradd layerremove', function() { clearTimeout(syncTimer); syncTimer = setTimeout(sync, 0); });
  function rings() {
    ring.clearLayers(); badges = {};
    var k = 0;
    pts.forEach(function(s) {
      if (notes[String(s.id)] && s.lat != null) {
        k++;
        badges[s.id] = L.marker([s.lat, s.lon], {icon: L.divIcon({className: '', html: BADGE, iconSize: [16, 16], iconAnchor: [-5, 21]}), interactive: false, keyboard: false});
      }
    });
    sync();
    if (window.mfAddExtra && k) window.mfAddExtra({id: 'notes', sw: '<span style="display:inline-block;width:14px;height:14px;border-radius:3px;background:#f39c12;color:#fff;font:bold 10px Arial;text-align:center;line-height:14px;flex-shrink:0">N</span>', text: 'Orange N badge: you wrote a note on this sensor (' + k + ')'});
  }
  fetch('/api/sensor-notes?group=' + encodeURIComponent(G)).then(function(r) { return r.json(); })
    .then(function(j) { notes = j.notes || {}; rings(); }).catch(function() {});
  function attach(popup) {
    var el = popup.getElement(), c = el && el.querySelector('.leaflet-popup-content');
    if (!c || c.querySelector('.crnote')) return;
    var m = /ID:\\s*([\\w.\\-]+)(?=<|\\s|$)/.exec(c.innerHTML);   // innerHTML: as text the ID runs into the next line ("7Health")
    if (!m) return;
    var id = m[1], box = document.createElement('div'), ta = document.createElement('textarea'), st = document.createElement('small'), timer = null;
    box.className = 'crnote';
    box.innerHTML = '<b>Note</b> ';
    ta.value = (notes[id] || {}).note || ''; ta.maxLength = 2000; ta.placeholder = 'Write a note about this sensor...';
    st.textContent = notes[id] ? 'Saved ' + notes[id].at.replace('T', ' ') : '';
    box.appendChild(st); box.appendChild(ta);
    ['keydown', 'keyup', 'keypress', 'mousedown', 'dblclick'].forEach(function(ev) { ta.addEventListener(ev, function(e) { e.stopPropagation(); }); });
    function save() {
      clearTimeout(timer); timer = null; st.textContent = 'Saving...';
      fetch('/api/sensor-notes', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({group: G, id: id, note: ta.value})})
        .then(function(r) { return r.json(); })
        .then(function(j) {
          if (j.error) { st.textContent = 'Not saved: ' + j.error; return; }
          if (j.entry) { notes[id] = j.entry; st.textContent = 'Saved ' + j.entry.at.replace('T', ' '); } else { delete notes[id]; st.textContent = ''; }
          rings();
        }).catch(function() { st.textContent = 'Not saved (no contact with the Control Room)'; });
    }
    ta.addEventListener('input', function() { st.textContent = 'Typing...'; clearTimeout(timer); timer = setTimeout(save, 700); });
    ta.addEventListener('blur', function() { if (timer) save(); });
    c.appendChild(box);
  }
  map.on('popupopen', function(e) {
    attach(e.popup);
    var c = e.popup.getElement() && e.popup.getElement().querySelector('.leaflet-popup-content');
    if (c) { var o = new MutationObserver(function() { attach(e.popup); }); o.observe(c, {childList: true}); e.popup._crObs = o; }
  });
  map.on('popupclose', function(e) { if (e.popup._crObs) { e.popup._crObs.disconnect(); e.popup._crObs = null; } });
})();
</script>"""


def with_notes(page, group):
    """Add the sensor-note box (popups) and the yellow rings to a generated map page."""
    js = NOTES_JS.replace("__GROUP__", group)
    return page.replace("</body>", js + "</body>", 1) if "</body>" in page else page + js


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
    html = m.build_html(paths, sensors, ref_sensors, server_mode=True, allow_delete=allow_delete, **kwargs)
    return with_notes(html, "bluetooth"), paths


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
  if (window.mfAddExtra) ['faulty_high', 'faulty_likely', 'path_problem'].forEach(function(v) {
    var k = D.sensors.filter(function(s) { return s.verdict === v; }).length;
    var why = {faulty_high: 'no path through it works and 2 or more failing paths lead to sensors that are alive',
               faulty_likely: 'no path through it works, but the proof is thinner',
               path_problem: 'it works, but some of its paths fail: look at the paths (hidden until ticked in the Triangulation panel)'}[v];
    window.mfAddExtra({id: 'tri_' + v, sw: '<span style="display:inline-block;width:14px;height:14px;border-radius:50%;border:3px solid ' + COLORS[v] + ';box-sizing:border-box;flex-shrink:0"></span>',
                       text: 'Big ' + {faulty_high: 'red', faulty_likely: 'orange', path_problem: 'purple'}[v] + ' ring: ' + D.labels[v] + ' (' + k + '), ' + why});
  });
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
.selbar{position:sticky;bottom:0;margin:10px -20px -40px;padding:9px 20px;background:var(--card);border-top:1px solid var(--line);font-weight:bold;z-index:5}
.selbar.over{background:var(--high);color:#fff}
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
  function localTime(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return iso;
    var mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
    var age = mins < 1 ? 'just now' : mins < 60 ? mins + ' min ago' : mins < 2880 ? Math.round(mins / 60) + ' h ago' : Math.round(mins / 1440) + ' days ago';
    return d.toLocaleString('en-GB', {day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'}) + ' (' + age + ')';
  }
  var status = document.getElementById('status'), body = document.getElementById('body');
  var fetchBtn = document.getElementById('fetch'), exBtn = document.getElementById('ex');
  var withFindings = D.paths.filter(function(p) { return p.findings.length; }).length;
  document.getElementById('sub').textContent = D.has_snapshot
    ? 'OMNIA data fetched ' + localTime(D.fetched_at) + ' - ' + D.meta.paths + ' paths, ' + withFindings + ' with findings'
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

  var code = '', q = '', selected = {}, sortKey = 'severity', sortDesc = false;
  var SORTS = [['severity', 'Severity (worst first)'], ['id', 'Path ID'], ['name', 'Path name'], ['start', 'Start node'], ['end', 'End node'],
               ['speed_min', 'Speed min'], ['speed_max', 'Speed max'], ['count', 'Number of findings']];
  var SEV = {high: 0, medium: 1, low: 2};
  function sortVal(p, k) {
    if (k === 'severity') return SEV[p.severity] == null ? 3 : SEV[p.severity];
    if (k === 'id') return +p.id;
    if (k === 'name') return String(p.name || '').toLowerCase();
    if (k === 'start') return p.start_node == null ? null : +p.start_node;
    if (k === 'end') return p.end_node == null ? null : +p.end_node;
    if (k === 'count') return p.findings.length;
    return p[k] == null ? null : +p[k];
  }
  function cmp(a, b) {
    var x = sortVal(a, sortKey), y = sortVal(b, sortKey);
    if (x === null || y === null) { if (x === y) return 0; return x === null ? 1 : -1; }   // empty values always last
    var r = typeof x === 'string' ? x.localeCompare(y, undefined, {numeric: true}) : x - y;
    if (sortDesc) r = -r;
    return r || (+a.id - +b.id);
  }
  body.innerHTML = '<div class="edit" id="edit"></div><div class="bar"><input id="q" placeholder="Search path, name or node...">'
    + '<label>Sort by <select id="sk">' + SORTS.map(function(s) { return '<option value="' + s[0] + '">' + s[1] + '</option>'; }).join('') + '</select></label>'
    + '<button class="sec" id="sd" title="Reverse the order"></button></div><div class="chips" id="chips"></div>'
    + '<table id="pt"><thead><tr><th><input type="checkbox" id="all" title="Select all shown"></th><th>Path ID</th><th>Path name</th><th>Start node</th><th>End node</th><th>Speed</th><th>Findings</th><th></th></tr></thead><tbody id="rows"></tbody></table>'
    + '<h2>Nodes that need attention (' + D.nodes.length + ')</h2><table><thead><tr><th>Node</th><th>Status</th><th>Detail</th></tr></thead><tbody id="nodes"></tbody></table>'
    + '<h2>API sensors no node stands for (' + D.sensors_without_node.length + ')</h2><div class="note" id="swn"></div>'
    + '<div class="selbar" id="selbar"></div>';

  function countSelected() { return Object.keys(selected).length; }
  var lastList = [], lastIdx = null;
  function updateCounter() {
    var n = countSelected(), bar = document.getElementById('selbar');
    if (!bar) return;
    bar.className = 'selbar' + (n > D.max_batch ? ' over' : '');
    bar.textContent = n + ' of ' + D.max_batch + ' paths selected' + (n > D.max_batch ? ' - ' + (n - D.max_batch) + ' too many, untick some before previewing' : '')
      + ' (deleting: max ' + D.max_delete + (n > D.max_delete ? ', too many to delete' : '') + ')';
  }
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
    var list = shown().sort(cmp);
    lastList = list;
    document.getElementById('sd').textContent = sortDesc ? 'Descending (Z-A, high-low)' : 'Ascending (A-Z, low-high)';
    list.forEach(function(p) {
      var tr = document.createElement('tr'); tr.setAttribute('data-row', p.id);
      var speed = p.speed_min == null ? '-' : p.speed_min + '-' + p.speed_max;
      tr.innerHTML = '<td><input type="checkbox" data-sel="' + esc(p.id) + '"' + (selected[p.id] ? ' checked' : '') + '></td><td><b>' + esc(p.id) + '</b></td><td>' + esc(p.name) + '</td><td>' + esc(p.start_node || '-') + '<br>' + esc(p.start_name || '') + '</td><td>'
        + esc(p.end_node || '-') + '<br>' + esc(p.end_name || '') + '</td><td>' + esc(speed) + '</td><td class="fd">'
        + p.findings.map(function(f) { return '<span class="f ' + esc(f.severity) + '">' + esc(f.text) + '</span>'; }).join('')
        + '</td><td>' + (shell ? '<a class="go" data-p="' + esc(p.id) + '">show on map</a>' : '') + '</td>';
      rows.appendChild(tr);
    });
    if (!rows.children.length) rows.innerHTML = '<tr><td colspan="8" class="note">Nothing matches.</td></tr>';
    document.getElementById('all').checked = list.length > 0 && list.every(function(p) { return selected[p.id]; });
    drawEdit();
    updateCounter();
  }
  document.getElementById('q').oninput = function() { q = this.value.toLowerCase(); draw(); };
  document.getElementById('sk').onchange = function() { sortKey = this.value; sortDesc = false; draw(); };
  document.getElementById('sd').onclick = function() { sortDesc = !sortDesc; draw(); };
  document.getElementById('all').onchange = function() {
    var on = this.checked;
    shown().forEach(function(p) { if (on) selected[p.id] = true; else delete selected[p.id]; });
    draw();
  };
  document.addEventListener('click', function(e) {
    var a = e.target.closest && e.target.closest('[data-p]');
    if (a && shell) shell.crShow('paths', {path: a.getAttribute('data-p')});
  });
  // Like a file list: plain click = only this row, shift+click = the range from the last click (replaces the selection),
  // ctrl+click = add or remove this row and keep the rest. The checkbox still toggles its own row on a plain click.
  document.addEventListener('click', function(e) {
    var t = e.target, onBox = t.getAttribute && t.getAttribute('data-sel');
    var row = t.closest && t.closest('tr[data-row]');
    if (!row) {                                            // a click on the empty page clears the selection
      if (t.closest && !t.closest('#pt, #edit, #selbar, .bar, .chips, a, button, input, select, label') && countSelected()) { selected = {}; preview = null; lastIdx = null; draw(); }
      return;
    }
    if (t.closest('td.fd') || (!onBox && t.closest('a, button, input, select, label'))) return;      // the findings text stays free for copying
    var c = row.getAttribute('data-row'), idx = -1;
    lastList.forEach(function(p, i) { if (String(p.id) === c) idx = i; });
    if (idx < 0) return;
    var multi = e.ctrlKey || e.metaKey;
    if (e.shiftKey) {
      selected = {};
      var from = Math.min(lastIdx === null ? idx : lastIdx, idx), to = Math.max(lastIdx === null ? idx : lastIdx, idx);
      for (var i = from; i <= to; i++) selected[lastList[i].id] = true;
    } else if (multi) {
      if (selected[c]) delete selected[c]; else selected[c] = true;
    } else if (onBox) {
      lastIdx = idx; return;                               // the checkbox's own change handler does the rest
    } else {
      if (selected[c]) delete selected[c];                 // a second click on a selected row deselects it
      else { selected = {}; selected[c] = true; }
    }
    window.getSelection && window.getSelection().removeAllRanges();
    if (!e.shiftKey) lastIdx = idx;
    draw();
  });
  document.addEventListener('change', function(e) {
    var c = e.target.getAttribute && e.target.getAttribute('data-sel');
    if (c) { if (e.target.checked) selected[c] = true; else delete selected[c]; drawEdit(); updateCounter(); }
  });

  // ---- bulk edit ------------------------------------------------------------
  var preview = null;          // the last preview answer {items, refused, token}
  function nodeLabel(v) { return v && D.node_names[v] ? D.node_names[v] : ''; }
  function drawEdit() {
    var el = document.getElementById('edit');
    var keep = {};
    ['smin', 'smax', 'nstart', 'nend', 'swap', 'sugg', 'yes', 'rfind', 'rrep', 'rpre', 'rsuf', 'rnew', 'rstd'].forEach(function(id) { var x = document.getElementById(id); if (x) keep[id] = x.type === 'checkbox' ? x.checked : x.value; });
    var h = '<b>Bulk edit in OMNIA</b> <span class="note">' + countSelected() + ' path(s) selected (up to ' + D.max_batch + ' per batch)</span>';
    h += '<div class="row"><label>Speed min <input type="number" id="smin" min="1" step="1"></label><label>Speed max <input type="number" id="smax" min="1" step="1"></label>'
      + '<label><input type="checkbox" id="swap"> swap start and end node</label>'
      + '<label title="For a missing or unknown node, and for a node on a different sensor than the one the path is drawn to (the drawing is trusted). Only when one node is clearly the nearest to the drawn end (within 150 m, no close rival). Needs a speed range if the path has none."><input type="checkbox" id="sugg"> fill missing or wrong nodes with the nearest one</label>'
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
      if (!bad && lo === '' && hi === '' && !document.getElementById('swap').checked && !document.getElementById('sugg').checked && !document.getElementById('nstart').value.trim() && !document.getElementById('nend').value.trim()) bad = 'Set a speed, a node, or tick swap or the nearest-node option first.';
      if (bad) { document.getElementById('pvs').textContent = bad; return; }
      runPreview({ids: Object.keys(selected), speed_min: document.getElementById('smin').value, speed_max: document.getElementById('smax').value,
                  swap: document.getElementById('swap').checked, use_suggested: document.getElementById('sugg').checked, start: document.getElementById('nstart').value, end: document.getElementById('nend').value});
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
    var h = '<div class="note">Read from OMNIA at ' + esc(localTime(preview.fetched_at)) + '. Nothing has been changed yet.</div>';
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
      h += '<div class="row"><b>' + preview.items.length + (isDel ? ' path(s) will be PERMANENTLY DELETED from OMNIA (this cannot be undone).' : preview.kind === 'rename' ? ' path(s) will be renamed in OMNIA.' : ' path(s) will be changed in OMNIA.') + '</b>'
        + (isDel ? ' Type ' + word + ': <input id="yes" size="8" autocomplete="off"> ' : ' ') + '<button class="danger" id="apply"' + (isDel ? ' disabled' : '') + '>' + (isDel ? 'Delete from OMNIA' : 'Apply to OMNIA') + '</button></div>';
    } else if (preview.items.length) {
      h += '<div class="note">' + (isDel ? 'Deleting' : 'Editing') + ' is switched off on this PC, so this is a preview only.</div>';
    }
    h += '<div id="res"></div>';
    box.innerHTML = h;
    var yes = document.getElementById('yes'), ap = document.getElementById('apply');
    if (ap) {
      if (yes) yes.oninput = function() { ap.disabled = yes.value.trim().toLowerCase() !== word.toLowerCase(); };
      ap.onclick = function() {
        if (isDel && !confirm('Permanently delete ' + preview.items.length + ' path(s) from OMNIA?\n\nThis cannot be undone.')) return;
        if (!isDel && !ap.getAttribute('data-armed')) {          // first click: the button itself asks, right where you clicked
          var label = ap.textContent, ask = document.createElement('b'), no = document.createElement('button');
          ask.textContent = 'Are you sure? '; no.className = 'sec'; no.textContent = 'Cancel';
          ap.parentNode.insertBefore(ask, ap); ap.parentNode.insertBefore(no, ap.nextSibling);
          ap.setAttribute('data-armed', '1'); ap.textContent = 'Yes, apply to OMNIA';
          no.onclick = function() { ask.remove(); no.remove(); ap.removeAttribute('data-armed'); ap.textContent = label; };
          return;
        }
        ap.disabled = true; if (yes) yes.disabled = true;
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
    import omnia_edit
    data = {"has_snapshot": bool(result), "can_fetch": bool(can_fetch), "can_edit": bool(can_edit), "can_delete": bool(can_delete), "labels": bw.FINDINGS, "max_batch": omnia_edit.MAX_BATCH, "max_delete": omnia_edit.MAX_DELETE,
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
<div id="bar"><b>Control Room</b><a data-t="paths" class="on">Paths</a><a data-t="wiring" title="Are the paths wired to the sensors they are drawn between?">Wiring</a><a data-t="visits" title="Plan on-site checks of sensors whose position or existence is in doubt">Site visits</a><span class="sep">Sensor locations:</span>__TABS__<span class="sp"></span>__BADGE__<a id="rl" title="Restart the Control Room with the latest code and the local database. Nothing is fetched from the live feed or OMNIA.">Reload app</a><a id="rf" title="Read the live feed and OMNIA again (like run.bat), then reload every tab. Takes a few minutes.">Refresh</a><a id="qt" title="Stop the Control Room (the console closes by itself)">Quit</a></div>
<div id="stage"><iframe id="f-paths" class="on" src="/map__Q__"></iframe><iframe id="f-wiring"></iframe><iframe id="f-visits"></iframe>__FRAMES__</div>
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
  if (t === 'wiring' || t === 'visits') {
    if (!f.getAttribute('src')) f.src = '/' + t;
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
  var btn = this;
  if (btn.getAttribute('data-busy')) return;
  btn.setAttribute('data-busy', '1');
  function reloadAll() {
    Array.prototype.forEach.call(document.querySelectorAll('#stage iframe'), function(f) {
      if (f.getAttribute('src')) f.contentWindow.location.reload();
    });
  }
  function finish(text) { btn.textContent = 'Refresh'; btn.removeAttribute('data-busy'); if (text) alert(text); }
  function poll() {
    fetch('/api/refresh-data').then(function(r) { return r.json(); }).then(function(j) {
      if (j.state === 'running') { btn.textContent = 'Refreshing ' + (j.seconds || 0) + 's...'; btn.title = j.step || ''; setTimeout(poll, 2000); }
      else if (j.state === 'done') { reloadAll(); finish('Data refreshed. ' + (j.message || '')); }
      else { finish('Could not refresh: ' + (j.message || 'unknown error')); }
    }).catch(function() { finish('Lost contact with the Control Room.'); });
  }
  btn.textContent = 'Refreshing...';
  fetch('/api/refresh-data', {method: 'POST'}).then(function() { poll(); }).catch(function() { finish('Could not start the refresh.'); });
};
document.getElementById('rl').onclick = function() {
  var btn = this;
  if (btn.getAttribute('data-busy')) return;
  btn.setAttribute('data-busy', '1'); btn.textContent = 'Reloading...';
  function back(tries) {
    fetch('/api/review').then(function(r) { if (r.ok) location.reload(); else throw 0; })
      .catch(function() { if (tries > 0) setTimeout(function() { back(tries - 1); }, 700); else { btn.textContent = 'Reload app'; btn.removeAttribute('data-busy'); alert('The Control Room did not come back. Start it again with control_room.bat.'); } });
  }
  fetch('/api/restart', {method: 'POST'}).then(function() { setTimeout(function() { back(50); }, 1500); })
    .catch(function() { btn.textContent = 'Reload app'; btn.removeAttribute('data-busy'); });
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
    suspects = None
    if group == "Bluetooth":
        import bt_sensor_suspects
        suspects = bt_sensor_suspects.analyse()
    with tempfile.TemporaryDirectory() as tmp:
        out = qa.generate_html(group, api_sensors, ref_sensors, matches, Path(tmp) / "qa.html",
                               not_electrified=not_electrified, max_dist=max_dist,
                               verdicts={sid: v["verdict"] for sid, v in suspects["sensors"].items()} if suspects else None,
                               verdict_labels=bt_sensor_suspects.VERDICTS if suspects else None)
        page = Path(out).read_text(encoding="utf-8")
    if suspects:
        tri = triangulation_html(suspects)
        page = page.replace("</body>", tri + "</body>", 1) if "</body>" in page else page + tri
    page = with_notes(page, slug)
    page = page.replace("</head>", TALL_MAP_CSS + "</head>", 1)
    return add_nav(page, slug, views)


# ── site visits ──────────────────────────────────────────────────────────────

def visit_inputs(views=None):
    """What the Site visits tab plans from: per sensor group the API sensors, spreadsheet rows and their matches
    (the same matching as the sensor tabs), plus the Bluetooth verdicts. Read-only, like the sensor pages."""
    import qa
    views = views or sensor_views()
    workbook = ROOT / "QA Locations.xlsx"
    if not workbook.exists():
        return []
    acct = qa.load_project_accountability()
    out = []
    for slug, (group, sheet, max_dist) in views.items():
        api = qa.load_api_sensors(group)
        ref, _ne = qa.load_reference([f"{workbook}::{sheet}"])
        matches = qa.match_sensors(ref, api, max_dist=max_dist)
        qa.annotate_accountability(api, matches, acct, max_dist=max_dist)
        qa.annotate_ref_accountability(ref, acct)
        verdicts = {}
        if group == "Bluetooth":
            import bt_sensor_suspects
            verdicts = {sid: v["verdict"] for sid, v in bt_sensor_suspects.analyse()["sensors"].items()}
        out.append({"group": group, "slug": slug, "api": api, "matches": matches, "verdicts": verdicts})
    return out


def visit_params(query, views):
    """kinds, groups, per_day from the page's query string; anything unknown is dropped."""
    import site_visits as sv
    q = parse_qs(query or "", keep_blank_values=True)   # "kinds=" means none ticked, not "all"
    pick = lambda name, allowed: [x for x in ((q.get(name) or [""])[0]).split(",") if x in allowed] if name in q else list(allowed)
    try:
        per_day = min(50, max(1, int((q.get("per_day") or [sv.PER_DAY])[0])))
    except ValueError:
        per_day = sv.PER_DAY
    return pick("kinds", sv.KINDS), pick("groups", views), per_day, (q.get("done") or ["0"])[0] == "1"


def visit_plan(inputs, notes, visits, kinds, groups, per_day, show_done=False):
    """(items shown, days, number of visited items) for the chosen filters. Notes and findings are read fresh each
    time (they change while you work); visited items leave the plan unless show_done."""
    import site_visits as sv
    data = [dict(g, notes=notes_for_group(g["slug"], notes)) for g in inputs]
    items = sv.collect_items(data)
    for it in items:
        e = visits.get(it["key"])
        it["visit"] = None if not e else {"done": bool(e.get("done")), "at": (e.get("at") or "")[:10], "summary": sv.visit_summary(e),
                                          "action": sv.finding_action(e)[1] if (e.get("found") or e.get("right")) else ""}
    n_done = sum(1 for it in items if it["visit"] and it["visit"]["done"])
    shown = [it for it in items if show_done or not (it["visit"] and it["visit"]["done"])]
    return shown, sv.plan(shown, per_day, kinds, groups), n_done


VISITS_PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Site visits</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"/>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.0.22/leaflet-maplibre-gl.js"></script>
<style>
body{margin:0;font:13px Arial,sans-serif;color:#222;background:#f4f6f8}
header{background:#1f4e79;color:#fff;padding:12px 18px}header h1{margin:0;font-size:18px}
.sub{opacity:.85;margin-top:4px}
.bar{padding:10px 18px;background:#fff;border-bottom:1px solid #ddd;display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center}
.bar label{white-space:nowrap}.bar .grp{color:#555;font-weight:bold}
.bar button{padding:5px 11px;border:0;border-radius:5px;background:#2c3e50;color:#fff;cursor:pointer}
.bar button.go{background:#2e86c1}
.bar input[type=number]{width:46px}
#saved{color:#555;word-break:break-all}
#map{height:46vh;min-height:300px}
.day{margin:14px 18px;background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden}
.day h3{margin:0;padding:10px 14px;font-size:14px;color:#fff;cursor:pointer;user-select:none}
.day h3::before{content:'\25BE';display:inline-block;width:18px}
.day.closed h3::before{content:'\25B8'}
.day.closed table{display:none}
.day h3 a{color:#fff;font-weight:normal;margin-left:10px;font-size:12px}
table{border-collapse:collapse;width:100%}
td,th{border-top:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f6f7f9;font-size:12px;color:#555}
td.code{font-weight:bold;white-space:nowrap}
.k{display:inline-block;background:#eef2f7;border-radius:4px;padding:1px 6px;margin:0 3px 3px 0;font-size:11px}
.note{color:#9a5b00}
a{color:#2e86c1;cursor:pointer}
.empty{margin:30px 18px;color:#555;font-size:15px}
.k.done{background:#d5f0dd;color:#1e6b34}
.visit{margin-top:4px;color:#1e6b34}
.mark{margin-left:6px;padding:1px 7px;border:1px solid #9bb;border-radius:4px;background:#fff;color:#234;cursor:pointer;font-size:11px}
details.find{margin:14px 18px;background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.12)}
details.find summary{padding:10px 14px;font-weight:bold;cursor:pointer}
details.find table{margin:0 0 6px}
#imp{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;align-items:flex-start;justify-content:center;padding:30px 12px;overflow:auto}
#imp .box{background:#fff;border-radius:10px;max-width:980px;width:100%;padding:16px 18px;box-shadow:0 6px 30px rgba(0,0,0,.4)}
#imp h2{margin:0 0 8px;font-size:16px}#imp .warn{color:#9a5b00;margin:6px 0;font-size:12px}
#imp button{padding:6px 13px;border:0;border-radius:5px;background:#2c3e50;color:#fff;cursor:pointer;margin-right:6px}
#imp button.go{background:#2e86c1}
.num{width:22px;height:22px;border-radius:50%;color:#fff;font:bold 10px/20px Arial;text-align:center;border:1px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.5)}
@media print{.day.closed table{display:table}.bar,#map,.noprint{display:none}.day{box-shadow:none;break-inside:auto;margin:8px 0}body{background:#fff}}
</style></head><body>
<header><h1>Site visits</h1><div class="sub" id="sub"></div></header>
<div class="bar" id="bar"></div>
<div id="map"></div>
<div id="findings"></div>
<div id="days"></div>
<div id="imp"><div class="box"><h2>Import findings</h2><div id="impbody"></div></div></div>
<script>
(function() {
  var D = __DATA__;
  var COL = ['#c0392b', '#e67e22', '#27ae60', '#8e44ad', '#16a085', '#2c3e50', '#d35400', '#7f8c8d'];
  var shell = (window.parent !== window && window.parent.crShow) ? window.parent : null;
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function nav(p) { return 'https://www.google.com/maps/dir/?api=1&destination=' + p.lat.toFixed(6) + ',' + p.lon.toFixed(6); }
  var nStops = 0, nItems = 0;
  D.days.forEach(function(d) { nStops += d.stops.length; d.stops.forEach(function(s) { nItems += s.items.length; }); });
  document.getElementById('sub').textContent = (D.days.length
    ? nItems + ' things to check at ' + nStops + ' stops over ' + D.days.length + ' day' + (D.days.length === 1 ? '' : 's') +
      '. Stops are in driving order from the area centre (straight-line distance; the roads may add more).'
    : 'Nothing to visit for the ticked options.') +
    (D.n_done ? ' ' + D.n_done + ' already visited ' + (D.sel.done ? '(shown).' : '(left out).') : '');

  // ── filters ──
  var bar = document.getElementById('bar');
  function box(name, value, label, n, checked) {
    var l = document.createElement('label');
    l.innerHTML = '<input type="checkbox" data-f="' + name + '" value="' + esc(value) + '"' + (checked ? ' checked' : '') + '> ' + esc(label) + ' (' + n + ')';
    return l;
  }
  var g1 = document.createElement('span'); g1.className = 'grp'; g1.textContent = 'Check:'; bar.appendChild(g1);
  Object.keys(D.kinds).forEach(function(k) { bar.appendChild(box('kinds', k, D.kinds[k], D.kind_counts[k] || 0, D.sel.kinds.indexOf(k) >= 0)); });
  var g2 = document.createElement('span'); g2.className = 'grp'; g2.textContent = 'Groups:'; bar.appendChild(g2);
  Object.keys(D.groups).forEach(function(s) { bar.appendChild(box('groups', s, D.groups[s], D.group_counts[s] || 0, D.sel.groups.indexOf(s) >= 0)); });
  var pd = document.createElement('label'); pd.innerHTML = 'Stops per day <input type="number" id="perday" min="1" max="50" value="' + D.sel.per_day + '">'; bar.appendChild(pd);
  var sd = document.createElement('label'); sd.innerHTML = '<input type="checkbox" id="showdone"' + (D.sel.done ? ' checked' : '') + '> Show visited (' + D.n_done + ')'; bar.appendChild(sd);
  function query() {
    var q = {kinds: [], groups: []};
    Array.prototype.forEach.call(bar.querySelectorAll('input[data-f]'), function(i) { if (i.checked) q[i.getAttribute('data-f')].push(i.value); });
    q.per_day = +document.getElementById('perday').value || D.sel.per_day;
    q.done = document.getElementById('showdone').checked ? 1 : 0;
    return q;
  }
  function reload() {   // same options, fresh data
    var q = query();
    location.search = '?kinds=' + q.kinds.join(',') + '&groups=' + q.groups.join(',') + '&per_day=' + q.per_day + '&done=' + q.done;
  }
  function post(url, body) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})}).then(function(r) { return r.json(); });
  }
  function button(text, cls, fn, title) { var b = document.createElement('button'); b.textContent = text; b.className = cls || ''; b.onclick = fn; if (title) b.title = title; bar.appendChild(b); return b; }
  button('Plan again', 'go', reload, 'Rebuild the days with the ticked options');
  var saved = document.createElement('span'); saved.id = 'saved';
  function save(fmt) {
    var q = query(); q.format = fmt;
    saved.textContent = 'Choose where to save in the dialog that opened (it may be behind this window)...';
    fetch('/api/visits/save', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(q)})
      .then(function(r) { return r.json(); })
      .then(function(r) {
        saved.textContent = r.cancelled ? 'Cancelled - nothing saved.'
          : r.path ? 'Saved to ' + r.path + (r.fallback ? ' (no Save dialog available, so the Downloads folder was used)' : '')
          : 'Could not save: ' + (r.error || 'unknown error');
      }).catch(function(e) { saved.textContent = 'Could not save: ' + e; });
  }
  button('Save field sheet (Excel)', '', function() { save('xlsx'); }, 'One sheet per day, stops in driving order, yellow columns to fill in on site, drop-down lists and navigate links');
  button('Save for Google My Maps (KML)', '', function() { save('kml'); }, 'Import into Google My Maps to see the days on your phone; every stop has navigate links');
  button('Print', '', function() { window.print(); }, 'Print the day lists (the map and these buttons are left out; folded days print open)');
  function foldAll(closed) { Array.prototype.forEach.call(document.querySelectorAll('.day'), function(d) { d.classList.toggle('closed', closed); }); }
  if (D.days.length > 1) {
    button('Fold all days', '', function() { foldAll(true); }, 'Show only the day headings');
    button('Unfold all days', '', function() { foldAll(false); }, 'Show every stop');
  }
  button('Import findings...', 'go', function() { openImport(); }, 'Read a filled-in field sheet back: you see a preview first, nothing changes until you apply it');
  if (D.findings.length) button('Save corrections list (CSV)', '', function() { save('corrections'); }, 'What the visits found, grouped by what you have to do');
  bar.appendChild(saved);

  // ── map ──
  var map = L.map('map');
  map.whenReady(function() {
    map.setMaxZoom(19);
    var t = L.maplibreGL({style: 'https://tiles.openfreemap.org/styles/liberty', attribution:
      '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> &copy; <a href="https://www.openmaptiles.org/" target="_blank">OpenMapTiles</a> ' +
      'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>'}).addTo(map);
    t.getContainer().style.zIndex = 0;
  });
  var bounds = [], markers = {};
  function itemHtml(it) {
    var h = '<b>' + esc(it.group) + (it.id ? ' ' + esc(it.id) : '') + (it.site_code ? ' (site ' + esc(it.site_code) + ')' : '') + ': ' + esc(it.name || it.ref_name) + '</b><br>';
    it.kinds.forEach(function(k) { h += '<span class="k">' + esc(D.kinds[k]) + '</span>'; });
    if (it.project) h += '<span class="k">' + esc(it.project) + '</span>';
    h += '<br>' + it.checks.map(esc).join('<br>');
    if (it.note) h += '<br><span class="note">Your note: ' + esc(it.note) + '</span>';
    if (it.visit) {
      h += '<div class="visit">' + (it.visit.done ? '&#10003; Visited ' + esc(it.visit.at) : 'Visit recorded, not finished') +
           (it.visit.summary ? ': ' + esc(it.visit.summary) : '') + (it.visit.action ? '<br><b>' + esc(it.visit.action) + '</b>' : '') + '</div>';
    }
    h += '<br><button class="mark" data-mark="' + esc(it.key) + '" data-done="' + (it.visit && it.visit.done ? 0 : 1) + '" data-g="' + esc(it.group) + '" data-id="' + esc(it.id) +
         '" data-name="' + esc(it.name || it.ref_name) + '">' + (it.visit && it.visit.done ? 'Not visited after all' : 'Mark as visited') + '</button><br>';
    h += it.points.map(function(p) { return esc(p.label) + ' position: <a href="' + nav(p) + '" target="_blank">navigate</a>'; }).join(' &middot; ');
    return h;
  }
  D.days.forEach(function(d, di) {
    var col = COL[di % COL.length], line = [];
    d.stops.forEach(function(s, si) {
      var icon = L.divIcon({className: '', html: '<div class="num" style="background:' + col + '">' + (si + 1) + '</div>', iconSize: [22, 22], iconAnchor: [11, 11]});
      markers[s.code] = L.marker([s.lat, s.lon], {icon: icon}).addTo(map)
        .bindPopup('<b>' + esc(s.code) + '</b> (day ' + d.day + ', ' + esc(d.area) + ')<br>' + s.items.map(itemHtml).join('<hr style="margin:6px 0">'), {maxWidth: 340})
        .bindTooltip(esc(s.code) + ': ' + esc(s.items[0].name || s.items[0].ref_name), {direction: 'top', offset: [0, -10]});
      line.push([s.lat, s.lon]); bounds.push([s.lat, s.lon]);
      s.items.forEach(function(it) {
        it.points.slice(1).forEach(function(p) {
          L.polyline([[s.lat, s.lon], [p.lat, p.lon]], {color: '#1f4e79', weight: 2, dashArray: '4,4', interactive: false}).addTo(map);
          L.circleMarker([p.lat, p.lon], {radius: 5, color: '#1f4e79', fillColor: '#fff', fillOpacity: 1, weight: 2})
            .bindTooltip(esc(s.code) + ': ' + esc(p.label.toLowerCase()) + ' position').addTo(map);
          bounds.push([p.lat, p.lon]);
        });
      });
    });
    if (line.length > 1) L.polyline(line, {color: col, weight: 3, opacity: .7}).addTo(map);
  });
  if (bounds.length) map.fitBounds(bounds, {padding: [30, 30]}); else map.setView([35.0, 33.2], 9);

  // ── day lists ──
  var host = document.getElementById('days');
  if (!D.days.length) host.innerHTML = '<div class="empty">' + (D.message || 'Nothing to visit for the ticked options.') + '</div>';
  D.days.forEach(function(d, di) {
    var sec = document.createElement('div'); sec.className = 'day';
    var links = d.links.map(function(u, i) { return '<a class="noprint" href="' + esc(u) + '" target="_blank">Open in Google Maps' + (d.links.length > 1 ? ' (part ' + (i + 1) + ' of ' + d.links.length + ')' : '') + '</a>'; }).join('');
    var h = '<h3 style="background:' + COL[di % COL.length] + '">Day ' + d.day + ' - ' + esc(d.area) + ': ' + d.stops.length + ' stop' + (d.stops.length === 1 ? '' : 's') +
            ', about ' + d.km + ' km in straight lines' + links + '</h3><table><tr><th>Stop</th><th>What to check</th><th class="noprint"></th></tr>';
    d.stops.forEach(function(s) {
      h += '<tr><td class="code">' + esc(s.code) + '</td><td>' + s.items.map(itemHtml).join('<hr style="margin:6px 0;border:0;border-top:1px dashed #ccc">') + '</td>' +
           '<td class="noprint" style="white-space:nowrap"><a data-show="' + esc(s.code) + '">show on map</a>' +
           (shell ? '<br><a data-tab="' + esc(s.items[0].slug) + '" data-lat="' + s.lat + '" data-lon="' + s.lon + '">' + esc(s.items[0].group) + ' tab</a>' : '') + '</td></tr>';
    });
    sec.innerHTML = h + '</table>';
    var head = sec.querySelector('h3');
    head.title = 'Click to fold or unfold this day';
    head.onclick = function(e) { if (!(e.target.closest && e.target.closest('a'))) sec.classList.toggle('closed'); };
    host.appendChild(sec);
  });
  document.addEventListener('click', function(e) {
    var a = e.target.closest && e.target.closest('[data-show],[data-tab],[data-mark]');
    if (!a) return;
    e.preventDefault();
    if (a.hasAttribute('data-mark')) {
      a.disabled = true;
      post('/api/visits/mark', {key: a.getAttribute('data-mark'), done: a.getAttribute('data-done') === '1', group: a.getAttribute('data-g'),
                                id: a.getAttribute('data-id'), name: a.getAttribute('data-name')})
        .then(function(r) { if (r.error) { a.disabled = false; alert(r.error); } else reload(); })
        .catch(function() { a.disabled = false; alert('No contact with the Control Room.'); });
    } else if (a.hasAttribute('data-show')) {
      var m = markers[a.getAttribute('data-show')];
      document.getElementById('map').scrollIntoView({behavior: 'smooth'});
      map.flyTo(m.getLatLng(), 16, {duration: 1.0});
      setTimeout(function() { m.openPopup(); }, 1100);
    } else if (shell) {
      shell.crShow(a.getAttribute('data-tab'), {lat: +a.getAttribute('data-lat'), lon: +a.getAttribute('data-lon')});
    }
  });

  // ── what the visits found ──
  if (D.findings.length) {
    var det = document.createElement('details'); det.className = 'find'; det.open = true;
    var rows = '', last = '';
    D.findings.forEach(function(f) {
      if (f.action_label !== last) { rows += '<tr><th colspan="4">' + esc(f.action_label) + '</th></tr>'; last = f.action_label; }
      rows += '<tr><td>' + esc(f.group) + (f.id ? ' ' + esc(f.id) : '') + '</td><td><b>' + esc(f.name) + '</b><br>' + esc(f.summary) + '</td><td>' + esc(f.action_text) +
              '</td><td>' + esc((f.at || '').slice(0, 10)) + '</td></tr>';
    });
    det.innerHTML = '<summary>What the visits found (' + D.findings.length + ') - things to correct, by what you have to do</summary>' +
                    '<table><tr><th>Sensor</th><th>Found on site</th><th>What to do</th><th>Visited</th></tr>' + rows + '</table>';
    document.getElementById('findings').appendChild(det);
  }

  // ── import ──
  var imp = document.getElementById('imp'), impBody = document.getElementById('impbody');
  function closeImp() { imp.style.display = 'none'; }
  function openImp(html) { impBody.innerHTML = html; imp.style.display = 'flex'; }
  window.openImport = function() {
    openImp('<p>Choose the filled-in field sheet in the dialog that opened (it may be behind this window)...</p>');
    post('/api/visits/import', {action: 'pick'}).then(function(r) {
      if (r.cancelled) { closeImp(); return; }
      if (r.error) { openImp('<p class="warn">' + esc(r.error) + '</p><button id="impx">Close</button>'); document.getElementById('impx').onclick = closeImp; return; }
      var s = r.summary, h = '<p><b>' + esc(r.file) + '</b>: ' + s.rows + ' rows, ' + s.answered + ' with an answer (' + s.new + ' new, ' + s.updated + ' already recorded), ' +
                s.skipped_empty + ' left empty. ' + s.done + ' will count as visited and leave the plan.</p>';
      r.warnings.slice(0, 20).forEach(function(w) { h += '<div class="warn">' + esc(w) + '</div>'; });
      if (r.warnings.length > 20) h += '<div class="warn">... and ' + (r.warnings.length - 20) + ' more</div>';
      if (r.items.length) {
        h += '<table><tr><th>Sensor</th><th>Found on site</th><th>What it means</th></tr>';
        r.items.forEach(function(f) { h += '<tr><td>' + esc(f.group) + (f.id ? ' ' + esc(f.id) : '') + '<br>' + esc(f.name) + '</td><td>' + esc(f.summary) + '</td><td><b>' + esc(f.action_label) + '</b><br>' + esc(f.action_text) + '</td></tr>'; });
        h += '</table>';
        if (r.more) h += '<div class="warn">... and ' + r.more + ' more rows</div>';
        h += '<p><label><input type="checkbox" id="impnotes" checked> Also add each finding to the sensor\'s note (the orange N on the maps)</label></p>' +
             '<button class="go" id="impgo">Apply ' + s.answered + ' findings</button>';
      } else h += '<p>Nothing to import: no row has an answer yet.</p>';
      openImp(h + '<button id="impx">Cancel</button>');
      document.getElementById('impx').onclick = closeImp;
      var go = document.getElementById('impgo');
      if (go) go.onclick = function() {
        go.disabled = true;
        post('/api/visits/import', {action: 'apply', token: r.token, to_notes: document.getElementById('impnotes').checked}).then(function(a) {
          if (a.error) { openImp('<p class="warn">' + esc(a.error) + '</p><button id="impx">Close</button>'); document.getElementById('impx').onclick = closeImp; return; }
          openImp('<p>Saved ' + a.saved + ' findings (' + a.done + ' visited).' + (a.notes_added ? ' ' + a.notes_added + ' notes added.' : '') +
                  (a.notes_skipped ? ' ' + a.notes_skipped + ' notes not changed (too long, or the finding was already in the note).' : '') + '</p><button class="go" id="impok">Show the new plan</button>');
          document.getElementById('impok').onclick = reload;
        }).catch(function() { go.disabled = false; alert('No contact with the Control Room.'); });
      };
    }).catch(function() { openImp('<p class="warn">No contact with the Control Room.</p><button id="impx">Close</button>'); document.getElementById('impx').onclick = closeImp; });
  };
})();
</script></body></html>"""


def build_visits_page(items, days, kinds, groups, per_day, views, message="", visits=None, n_done=0, show_done=False):
    import site_visits as sv
    kind_counts, group_counts = sv.counts(items)
    data = {"days": days, "kinds": sv.KINDS, "kind_counts": kind_counts, "groups": {s: v[0] for s, v in views.items()},
            "group_counts": group_counts, "message": message, "n_done": n_done, "findings": sv.findings_list(visits or {}),
            "sel": {"kinds": kinds, "groups": groups, "per_day": per_day, "done": show_done}}
    return VISITS_PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


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
        if path == "/api/sensor-notes":
            group = (parse_qs(parts.query).get("group") or [""])[0]
            return self._send(200, {"notes": notes_for_group(group, load_sensor_notes(self.server.notes_file))})
        if path == "/api/refresh-data":
            job = dict(self.server.refresh)
            if job.get("started"):
                job["seconds"] = int(time.time() - job["started"])
            return self._send(200, job)
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
        if path == "/visits":
            try:
                kinds, groups, per_day, show_done = visit_params(parts.query, self.server.views)
                inputs = self.server.visits(force)
                visits = load_visits(self.server.visits_file)
                items, days, n_done = visit_plan(inputs, load_sensor_notes(self.server.notes_file), visits, kinds, groups, per_day, show_done)
                msg = "" if inputs else "QA Locations.xlsx was not found in the repo folder, so there is nothing to compare."
                page = build_visits_page(items, days, kinds, groups, per_day, self.server.views, msg, visits, n_done, show_done)
            except Exception as exc:
                return self._send(500, f"Could not build the page: {exc}".encode("utf-8"), "text/plain")
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
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
        if self.path == "/api/sensor-notes" and self._same_origin():
            try:
                body = self._body()
                groups = set(self.server.views) | {"bluetooth"}
                entry = set_sensor_note(body.get("group"), body.get("id"), body.get("note"), groups, self.server.notes_file)
            except (ValueError, AttributeError) as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {"entry": entry})
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

    def _suggested_nodes(self, snap):
        """{path id: {start, end}} nearest-node suggestions that are clear enough to apply, from the live snapshot."""
        import bt_wiring
        feed, sensors = self.server.wiring_inputs()
        return bt_wiring.usable_suggestions(bt_wiring.analyse(snap, feed, sensors)["paths"])

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
                    suggested = self._suggested_nodes(snap) if body.get("use_suggested") else None
                    plan = omnia_edit.plan_edits(snap, body.get("ids") or [], body.get("speed_min"), body.get("speed_max"),
                                                 bool(body.get("swap")), body.get("start"), body.get("end"), suggested)
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

    def _save_visits(self):
        """Field sheet (CSV) or Google My Maps file (KML) for the ticked options. Built here, never sent by the page."""
        import site_visits as sv
        try:
            body = self._body()
            fmt = body.get("format")
            if fmt not in ("xlsx", "kml", "corrections"):
                raise ValueError("format must be xlsx, kml or corrections")
            visits = load_visits(self.server.visits_file)
            if fmt == "corrections":
                days = sv.findings_list(visits)
                if not days:
                    return self._send(400, {"error": "no findings yet: import a filled-in field sheet first"})
            else:
                q = "kinds=%s&groups=%s&per_day=%s&done=%s" % (",".join(map(str, body.get("kinds") or [])),
                                                              ",".join(map(str, body.get("groups") or [])), body.get("per_day"),
                                                              1 if body.get("done") else 0)
                kinds, groups, per_day, show_done = visit_params(q, self.server.views)
                _items, days, _n = visit_plan(self.server.visits(), load_sensor_notes(self.server.notes_file), visits, kinds, groups, per_day, show_done)
                if not days:
                    return self._send(400, {"error": "nothing to visit for the ticked options"})
            folder = Path(self.server.downloads_dir or DOWNLOADS)
            ext = "csv" if fmt == "corrections" else fmt
            name = f"{'site_visit_corrections' if fmt == 'corrections' else 'site_visits'}_{datetime.now():%Y%m%d_%H%M%S}.{ext}"
            dest, fallback = None, False
            if self.server.choose_path:
                try:
                    dest = self.server.choose_path(folder, name)
                except Exception:
                    fallback = True
                else:
                    if dest is None:
                        return self._send(200, {"cancelled": True})
            out = Path(dest) if dest else folder / name
            if out.suffix.lower() != "." + ext:
                out = out.with_name(out.name + "." + ext)
            out.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "corrections":
                buf = io.StringIO()
                csv.writer(buf).writerows([["" if v is None else v for v in r] for r in sv.corrections_rows(visits)])
                out.write_bytes(buf.getvalue().encode("utf-8-sig"))
            elif fmt == "xlsx":
                sv.write_field_sheet(days, out)
            else:
                out.write_text(sv.to_kml(days), encoding="utf-8")
        except (ValueError, AttributeError, TypeError) as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            return self._send(500, {"error": f"could not write the file: {exc}"})
        return self._send(200, {"path": str(out), "fallback": fallback})

    def _import_visits(self):
        """Import findings from a filled-in field sheet: 'pick' (choose the file, preview) then 'apply' (save)."""
        import site_visits as sv
        srv = self.server
        try:
            body = self._body()
            if body.get("action") == "pick":
                if srv.choose_open is None:
                    return self._send(400, {"error": "No file dialog is available on this PC."})
                try:
                    chosen = srv.choose_open(Path(srv.downloads_dir or DOWNLOADS))
                except Exception:
                    return self._send(500, {"error": "Could not show the file dialog."})
                if chosen is None:
                    return self._send(200, {"cancelled": True})
                try:
                    rows, warnings = sv.read_findings(chosen)
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                except Exception as exc:
                    return self._send(400, {"error": "Could not read that file (%s). Is it the field sheet saved from the Site visits tab?" % type(exc).__name__})
                with srv.visits_lock:
                    store = load_visits(srv.visits_file)
                entries, summary = sv.build_entries(rows, store)
                token = secrets.token_hex(8)
                srv.visit_import = {token: entries}               # only the newest preview can be applied
                items = []
                for key, e in sorted(entries.items(), key=lambda kv: (sv.ACTIONS_ORDER.index(sv.finding_action(kv[1])[0]), kv[1]["group"], kv[1]["name"])):
                    code, text = sv.finding_action(e)
                    items.append({"key": key, "group": e["group"], "id": e["id"], "name": e["name"], "summary": sv.visit_summary(e),
                                  "action_label": sv.ACTIONS[code], "action_text": text})
                return self._send(200, {"file": Path(chosen).name, "summary": summary, "warnings": warnings, "token": token,
                                        "items": items[:300], "more": max(0, len(items) - 300)})
            if body.get("action") == "apply":
                entries = srv.visit_import.pop(body.get("token"), None)
                if not entries:
                    return self._send(400, {"error": "Choose the file again: the preview can be applied once."})
                groups = set(srv.views) | {"bluetooth"}
                added = skipped = 0
                with srv.visits_lock:
                    store = load_visits(srv.visits_file)
                    store.update(entries)
                    save_visits(store, srv.visits_file)
                if body.get("to_notes"):
                    for key, e in entries.items():
                        slug, kind, sid = (key.split("|", 2) + ["", ""])[:3]
                        if kind != "api" or slug not in groups or not (e.get("found") or e.get("remarks") or e.get("label")):
                            continue
                        line = sv.note_line(e)
                        old = (load_sensor_notes(srv.notes_file).get("%s:%s" % (slug, sid)) or {}).get("note", "")
                        new = (old + "\n" + line) if old.strip() else line
                        if line in old or len(new) > MAX_NOTE:
                            skipped += 1
                            continue
                        try:
                            set_sensor_note(slug, sid, new, groups, srv.notes_file)
                            added += 1
                        except ValueError:
                            skipped += 1
                return self._send(200, {"saved": len(entries), "done": sum(1 for e in entries.values() if e["done"]),
                                        "notes_added": added, "notes_skipped": skipped})
            raise ValueError("unknown action")
        except (ValueError, AttributeError, TypeError) as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            return self._send(500, {"error": f"could not write the file: {exc}"})

    def _mark_visit(self):
        try:
            body = self._body()
            with self.server.visits_lock:
                store = load_visits(self.server.visits_file)
                mark_visit(store, body.get("key"), bool(body.get("done")), body.get("group") or "", body.get("id") or "", body.get("name") or "")
                save_visits(store, self.server.visits_file)
        except (ValueError, AttributeError, TypeError) as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            return self._send(500, {"error": f"could not write the file: {exc}"})
        return self._send(200, {"ok": True})

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
        if self.path == "/api/restart" and self._same_origin():
            self._send(200, {"restarting": True})
            def go():
                try:
                    self.server.spawn(restart_argv())
                except Exception:
                    return                     # could not start the new copy: keep this one running
                self.server.shutdown()
                if self.server.on_quit:
                    self.server.on_quit()
            threading.Thread(target=go, daemon=True).start()
            return
        if self.path == "/api/refresh-data" and self._same_origin():
            return self._send(200, dict(start_refresh(self.server)))
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
        if self.path == "/api/visits/save" and self._same_origin():
            return self._save_visits()
        if self.path == "/api/visits/import" and self._same_origin():
            return self._import_visits()
        if self.path == "/api/visits/mark" and self._same_origin():
            return self._mark_visit()
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


def read_env_file(env_file=ROOT / ".env"):
    """{NAME: value} from the .env file, read the way run.bat does (lines starting with # are skipped)."""
    out = {}
    try:
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() and not key.lstrip().startswith("#"):
                out[key.strip()] = val.strip().strip('"')
    except OSError:
        pass
    return out


MONITOR_TIMEOUT_S = 900


def run_monitor(env_file=ROOT / ".env"):
    """What run.bat does: one monitor run against the live feed (runner/run_tests.py, with the .env settings).

    Returns (ok, message). The runner exits 1 when some tests failed, which is normal data, not an error here."""
    env = dict(os.environ, **read_env_file(env_file))
    if not env.get("BASE_URL") or not env.get("SWARCO"):
        return False, "BASE_URL and SWARCO are not set in the .env file, so the live feed cannot be read."
    try:
        r = subprocess.run([sys.executable, str(ROOT / "runner" / "run_tests.py")], cwd=str(ROOT), env=env,
                           capture_output=True, timeout=MONITOR_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, "The monitor run took more than %d minutes and was stopped." % (MONITOR_TIMEOUT_S // 60)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    summary = [ln.split("SUMMARY", 1)[1].strip() for ln in out.splitlines() if "SUMMARY" in ln]
    if r.returncode in (0, 1) and summary:
        return True, summary[-1]
    last = [ln for ln in out.splitlines() if ln.strip()][-1:] or ["no output"]
    return False, "The monitor run did not finish: " + last[0].strip()[:200]


def _refresh_job(srv):
    """Background job behind the Refresh button: new monitor run, new OMNIA snapshot, then rebuild every page."""
    import omnia_fetch
    job = srv.refresh
    notes = []
    try:
        job["step"] = "Reading the live feed (a full monitor run, this can take a few minutes)..."
        ok, msg = srv.run_monitor()
        if not ok:
            job.update(state="error", message=msg)
            return
        notes.append("Live feed read (%s)." % msg)
        if _can_fetch(srv.omnia):
            job["step"] = "Reading OMNIA..."
            try:
                snap = (srv.fetch_omnia or omnia_fetch.fetch_snapshot)(srv.omnia)
                omnia_fetch.save_snapshot(snap, srv.snapshot_file)
                notes.append("OMNIA data updated.")
            except omnia_fetch.OmniaError as exc:
                notes.append("OMNIA was not updated: %s" % exc)
            except Exception as exc:
                notes.append("OMNIA was not updated (%s)." % type(exc).__name__)
        srv.page_cache.clear()
        job.update(state="done", message=" ".join(notes))
    except Exception as exc:
        job.update(state="error", message="Refresh failed: " + type(exc).__name__)


def start_refresh(srv):
    """Start the refresh job unless one is already running; returns the job state."""
    job = srv.refresh
    if job.get("state") == "running":
        return job
    job.clear()
    job.update(state="running", step="Starting...", started=time.time(), message="")
    threading.Thread(target=_refresh_job, args=(srv,), daemon=True).start()
    return job


def restart_argv(argv=None):
    """The command that starts a fresh copy of this app with the same options. --restarted makes the new copy wait
    for the old one to free the port and not open another browser tab."""
    rest = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--restarted"]
    return [sys.executable, str(Path(__file__).resolve())] + rest + ["--restarted"]


def spawn_detached(argv):
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    subprocess.Popen(argv, cwd=str(ROOT), creationflags=flags, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


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
import os, sys, tkinter as tk
from tkinter import filedialog
ext = os.path.splitext(sys.argv[2])[1] or ".csv"
kind = {".kml": "KML files (Google My Maps)", ".xlsx": "Excel files"}.get(ext.lower(), "CSV files")
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.asksaveasfilename(parent=root, title="Save " + ext[1:].upper(), initialdir=sys.argv[1], initialfile=sys.argv[2],
                                    defaultextension=ext, filetypes=[(kind, "*" + ext), ("All files", "*.*")])
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


_OPEN_DIALOG = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askopenfilename(parent=root, title="Choose the filled-in field sheet", initialdir=sys.argv[1],
                                  filetypes=[("Excel or CSV files", "*.xlsx *.xlsm *.csv"), ("All files", "*.*")])
sys.stdout.buffer.write(path.encode("utf-8"))
"""


def choose_open_path(initial_dir):
    """Ask on this PC's desktop which file to read (a normal Open dialog). Path, or None if cancelled."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    r = subprocess.run([str(exe), "-c", _OPEN_DIALOG, str(initial_dir)], capture_output=True, timeout=900,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:] or "the open dialog failed")
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

    def visits(self, force=False):
        """The Site visits inputs, rebuilt when the database or QA Locations.xlsx changes (notes are read per request)."""
        files = (self.db_file, ROOT / "QA Locations.xlsx")
        stamps = tuple(os.stat(f).st_mtime if os.path.exists(f) else 0 for f in files)
        with self.build_lock:
            hit = getattr(self, "_visits", None)
            if hit and hit[0] == stamps and not force:
                return hit[1]
            data = self.visit_inputs()
            self._visits = (stamps, data)
            return data

    def warm(self):
        """Build every page once, so the first browser load is instant."""
        for key, build in [("map", lambda: self.page_builder()[0])] + \
                          [(slug, (lambda s=slug: self.sensor_builder(s))) for slug in self.views]:
            try:
                self.cached(key, build)
            except Exception:
                pass  # surfaced when the page is requested


def make_server(port=DEFAULT_PORT, review_file=REVIEW_FILE, notes_file=None, delete_log=DELETE_LOG,
                page_builder=None, paths_provider=None, post=None,
                sensor_builder=None, views=None, db_file=None, snapshot_file=None, omnia_urls=None, allow_edit=None, edit_log=None, rename_log=None, omnia_post=None,
                fetch_omnia=None, wiring_inputs=None, allow_delete=None, delete_url=None, downloads_dir=None, choose_path=choose_save_path,
                visit_inputs=None, visits_file=None, choose_open=choose_open_path):
    srv = Server(("127.0.0.1", port), Handler)
    srv.db_file = db_file or DB_FILE
    srv.page_cache = {}
    srv.build_lock = threading.Lock()
    srv.review_file = review_file
    srv.notes_file = notes_file or SENSOR_NOTES_FILE
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
    srv.spawn = spawn_detached          # tests pass a fake
    srv.run_monitor = run_monitor       # tests pass a fake (the real one takes minutes)
    srv.refresh = {"state": "idle"}
    srv.tokens = {}
    srv.downloads_dir = downloads_dir
    srv.choose_path = choose_path   # the Save as dialog; tests pass None or a fake
    srv.on_quit = None   # set by the app-window launcher to close the window
    srv.visit_inputs = visit_inputs or (lambda: globals()["visit_inputs"](srv.views))   # tests pass a fake
    srv.visits_file = visits_file or SITE_VISITS_FILE
    srv.visits_lock = threading.Lock()
    srv.visit_import = {}
    srv.choose_open = choose_open                # the Open dialog; tests pass a fake or None
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
    ap.add_argument("--restarted", action="store_true", help="started by the Reload button: wait for the old copy to free the port, open no new browser tab")
    args = ap.parse_args()
    if sys.stdout is None or sys.stderr is None:   # pythonw: no console, so keep a log instead
        sys.stdout = sys.stderr = open(ROOT / "results" / "control_room.log", "a", encoding="utf-8", buffering=1)
    end = time.time() + (25 if args.restarted else 0)
    while True:
        try:
            srv = make_server(args.port)
            break
        except OSError:                     # the old copy still holds the port
            if time.time() >= end:
                raise
            time.sleep(0.4)
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
    if not (args.no_browser or args.restarted):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
