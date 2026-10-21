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
import json
import os
import secrets
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
  if (pathId && shell) pill('&larr; Back to path ' + pathId, 'Select this path on the Paths tab',
                            function() { shell.crShow('paths', {path: pathId}); });
  var here = pill('Paths here', 'Centre the Paths tab on the spot shown here', function() {
    if (last && shell) shell.crShow('paths', {lat: last[0], lon: last[1]});
  });
  here.style.display = 'none';

  window.addEventListener('load', function() {
    if (typeof flyTo === 'function') {
      var orig = flyTo;
      window.flyTo = function(a, b) { last = [a, b]; if (shell) here.style.display = ''; return orig.apply(this, arguments); };
      if (lat && lon) setTimeout(function() { flyTo(+lat, +lon, pathId ? 'Near path ' + pathId : 'Location'); }, 300);
    }
  });
})();
</script>"""


def add_nav(page, current, views):
    """Float the jump pills (Back to path / Paths here) over a generated sensor page."""
    nav = ('<style>.crnav{position:fixed;bottom:10px;left:50%;transform:translateX(-50%);z-index:99999;'
           'display:flex;gap:4px;background:#1b2631;padding:5px;border-radius:999px;'
           'box-shadow:0 2px 10px rgba(0,0,0,.45);font:13px Arial,sans-serif}'
           '.crnav:empty{display:none}'
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

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

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
        if self.path == "/api/quit" and self._same_origin():
            self._send(200, {"stopping": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            if self.server.on_quit:
                self.server.on_quit()
            return
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
                sensor_builder=None, views=None, db_file=None, allow_delete=None, delete_url=None):
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
