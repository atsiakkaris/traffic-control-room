"""Control Room app: review file, confirmed delete, and the guarantee that the
standalone/published BT map does not pick up any of the app's controls."""
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tools"))

import control_room as cr  # noqa: E402


class FakeResp:
    def __init__(self, code):
        self.status_code = code
        self.is_success = 200 <= code < 300


def _flag(*ids):
    return {i: {"status": "flag", "note": ""} for i in ids}


# ── review file ──────────────────────────────────────────────────────────────

def test_review_roundtrip_drops_malformed_entries(tmp_path):
    f = tmp_path / "r.json"
    cr.save_review({"1": {"status": "flag", "note": "x"}, "2": {"status": "bogus"}, "3": "no"}, f)
    assert cr.load_review(f) == {"1": {"status": "flag", "note": "x"}}


def test_load_review_missing_or_corrupt_file_is_empty(tmp_path):
    assert cr.load_review(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cr.load_review(bad) == {}


# ── delete planning ──────────────────────────────────────────────────────────

def test_plan_delete_only_flagged_and_known_digits():
    review = {"1": {"status": "flag"}, "2": {"status": "ok"}, "3": {"status": "flag"}, "x": {"status": "flag"}}
    ok, refused = cr.plan_delete(["1", "2", "3", "x", "9"], review, known_ids={"1", "2", "x", "9"})
    assert ok == ["1"]                       # 3 not in DB, 2 only OK'd, x not numeric, 9 not flagged
    assert sorted(refused) == ["2", "3", "9", "x"]


def test_delete_paths_stops_at_first_failure():
    sent = []

    def post(url, timeout):
        sent.append(url)
        return FakeResp(500 if url.endswith("=2") else 200)

    results, error = cr.delete_paths(["1", "2", "3"], "http://fake.test/Delete", post=post, sleep=lambda s: None)
    assert [r["id"] for r in results] == ["1", "2"]
    assert results[1]["ok"] is False and "2" in error
    assert len(sent) == 2                    # 3 was never attempted


def test_delete_paths_network_error_is_a_failure_not_a_crash():
    def post(url, timeout):
        raise OSError("unreachable")

    results, error = cr.delete_paths(["7"], "http://fake.test/Delete", post=post, sleep=lambda s: None)
    assert results[0]["ok"] is False and error


# ── HTTP: the confirmation flow ──────────────────────────────────────────────

def _start(tmp_path, post, known=("1", "2", "3"), allow_delete=True):
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "log.csv",
                         page_builder=lambda: ("<html></html>", {}),
                         paths_provider=lambda: {i: {"name": f"p{i}"} for i in known}, post=post,
                         allow_delete=allow_delete, delete_url="http://fake.test/Delete")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_dry_run_sends_nothing_and_live_needs_yes_and_matching_token(tmp_path):
    sent = []
    srv, base = _start(tmp_path, lambda url, timeout: (sent.append(url), FakeResp(200))[1])
    try:
        assert _req(base + "/api/review", "PUT", _flag("1", "2"))[0] == 200

        code, dry = _req(base + "/api/delete", "POST", {"ids": ["1", "2", "3"], "dry_run": True})
        assert code == 200 and dry["would_delete"] == ["1", "2"] and dry["refused"] == ["3"]
        assert sent == []                                     # dry run: nothing sent

        live = {"ids": ["1", "2"], "dry_run": False, "token": dry["token"]}
        assert _req(base + "/api/delete", "POST", {**live, "confirm": "yes"})[0] == 400   # not YES
        assert _req(base + "/api/delete", "POST", {**live, "confirm": "YES", "token": "nope"})[0] == 400
        assert sent == []

        # token is bound to the exact list: a different list with a valid token is refused
        code, dry2 = _req(base + "/api/delete", "POST", {"ids": ["1"], "dry_run": True})
        assert _req(base + "/api/delete", "POST",
                    {"ids": ["1", "2"], "dry_run": False, "confirm": "YES", "token": dry2["token"]})[0] == 400
        assert sent == []

        code, out = _req(base + "/api/delete", "POST", {**live, "confirm": "YES"})
        assert code == 200 and [r["id"] for r in out["results"]] == ["1", "2"]
        assert len(sent) == 2
        assert "p1" in (tmp_path / "log.csv").read_text()

        # token is single-use
        assert _req(base + "/api/delete", "POST", {**live, "confirm": "YES"})[0] == 400
        assert len(sent) == 2
    finally:
        srv.shutdown()


def test_unflagged_path_is_never_deleted(tmp_path):
    sent = []
    srv, base = _start(tmp_path, lambda url, timeout: (sent.append(url), FakeResp(200))[1])
    try:
        _req(base + "/api/review", "PUT", {"1": {"status": "ok", "note": ""}})
        code, dry = _req(base + "/api/delete", "POST", {"ids": ["1"], "dry_run": True})
        assert dry["would_delete"] == [] and dry["refused"] == ["1"]
        code, _ = _req(base + "/api/delete", "POST",
                       {"ids": ["1"], "dry_run": False, "confirm": "YES", "token": dry["token"]})
        assert code == 400 and sent == []
    finally:
        srv.shutdown()


def test_cross_origin_writes_are_refused(tmp_path):
    srv, base = _start(tmp_path, lambda url, timeout: FakeResp(200))
    try:
        evil = {"Origin": "http://evil.example"}
        assert _req(base + "/api/review", "PUT", _flag("1"), evil)[0] == 403
        assert _req(base + "/api/delete", "POST", {"ids": ["1"]}, evil)[0] == 403
        assert _req(base + "/api/review")[1] == {}
    finally:
        srv.shutdown()


def test_server_binds_to_loopback_only(tmp_path):
    srv, _ = _start(tmp_path, None)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.shutdown()


# ── the map itself ───────────────────────────────────────────────────────────

def test_app_controls_only_appear_in_server_mode():
    import bt_paths_map as m
    paths = {"1": {"name": "100->200", "coords": [[35.0, 33.0], [35.01, 33.01]]}}
    kwargs = dict(sensors={}, ref_sensors=[])
    local = m.build_html(paths, **kwargs)
    published = m.build_html(paths, include_live_data=False, **kwargs)
    app = m.build_html(paths, server_mode=True, **kwargs)
    for marker in ("reviewDelBtn", "SERVER_MODE", "/api/delete", "_pushFlags"):
        assert marker not in local and marker not in published
        assert marker in app


# ── sensor-location pages ────────────────────────────────────────────────────

def test_sensor_views_come_from_update_projects_groups():
    views = cr.sensor_views()
    assert set(views) == {"traffic-detection", "bluetooth", "vms"}
    assert views["vms"] == ("VMS", "VMS", 500)


def test_add_nav_adds_jump_pills_and_keeps_page():
    out = cr.add_nav("<html><body>x</body></html>", "vms", cr.sensor_views())
    assert 'class="crnav"' in out and out.endswith("</body></html>")


def test_sensor_route_serves_page_and_404s_unknown_group(tmp_path):
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv",
                         page_builder=lambda: ("", {}), paths_provider=dict,
                         sensor_builder=lambda slug: f"<body>{slug}</body>")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/sensors/vms") as r:
            assert r.status == 200 and b"vms" in r.read()
        assert _req(base + "/sensors/nope")[0] == 404
    finally:
        srv.shutdown()


def test_real_sensor_page_builds_without_writing_to_the_db(monkeypatch):
    import pytest
    import qa
    if not (ROOT / "QA Locations.xlsx").exists():
        pytest.skip("QA Locations.xlsx is kept out of git")

    def boom(*a, **k):
        raise AssertionError("sensor pages must be read-only")
    monkeypatch.setattr(qa, "upsert_sensor_projects", boom, raising=False)
    page = cr.build_sensor_page("vms")
    assert "crnav" in page and "VMS" in page


# ── phase 3: caching and tab jumps ───────────────────────────────────────────

def test_pages_are_cached_until_the_db_changes_and_refresh_forces_a_rebuild(tmp_path):
    db = tmp_path / "h.db"
    db.write_text("a")
    calls = []
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv",
                         page_builder=lambda: (calls.append(1) or "<body>map</body>", {}),
                         paths_provider=dict, sensor_builder=lambda s: f"<body>{s}</body>", db_file=db)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda u: urllib.request.urlopen(base + u).read()
    try:
        get("/map"); get("/map"); get("/map?path=5")
        assert len(calls) == 1                       # query strings are ignored for routing and caching
        get("/map?refresh=1")
        assert len(calls) == 2
        import os
        os.utime(db, (1, 1))                         # DB changed (new test run)
        get("/map")
        assert len(calls) == 3
    finally:
        srv.shutdown()


def test_sensor_pages_talk_to_the_app_shell_not_a_second_tab():
    out = cr.add_nav("<body></body>", "bluetooth", cr.sensor_views())
    for needle in ("window.parent.crShow", "Back to path", "Paths here"):
        assert needle in out
    assert "window.open" not in out and "BroadcastChannel" not in out


def test_shell_has_one_frame_per_view_and_a_refresh_button(tmp_path):
    shell = cr.build_shell(cr.sensor_views(), "?path=7")
    assert shell.count("<iframe") == 1 + len(cr.sensor_views())
    assert 'src="/map?path=7"' in shell and 'id="rf"' in shell and "crShow" in shell


def test_map_jump_buttons_only_in_server_mode():
    import bt_paths_map as m
    paths = {"1": {"name": "100->200", "coords": [[35.0, 33.0], [35.01, 33.01]]}}
    kw = dict(sensors={}, ref_sensors=[])
    app = m.build_html(paths, server_mode=True, **kw)
    for needle in ("jmpStart", "crShow", "crFocusPath"):
        assert needle in app
        assert needle not in m.build_html(paths, **kw)
        assert needle not in m.build_html(paths, include_live_data=False, **kw)


def test_quit_stops_the_server_but_only_for_same_origin_requests(tmp_path):
    srv, base = _start(tmp_path, None)
    try:
        assert _req(base + "/api/quit", "POST", {}, {"Origin": "http://evil.example"})[0] == 403
        code, out = _req(base + "/api/quit", "POST", {})
        assert code == 200 and out == {"stopping": True}
        import time
        time.sleep(0.5)
        try:
            urllib.request.urlopen(base + "/api/review", timeout=1)
            alive = True
        except Exception:
            alive = False
        assert not alive
    finally:
        srv.server_close()


def test_quit_also_closes_the_app_window(tmp_path):
    closed = []
    srv, base = _start(tmp_path, None)
    srv.on_quit = lambda: closed.append(1)
    try:
        assert _req(base + "/api/quit", "POST", {})[0] == 200
        import time
        time.sleep(0.3)
        assert closed == [1]
    finally:
        srv.server_close()


# ── read-only mode ───────────────────────────────────────────────────────────

def test_delete_is_off_unless_this_pc_opts_in(tmp_path):
    env_file = tmp_path / ".env"
    assert cr.allow_delete_setting(env={}, env_file=env_file) is False             # no .env at all
    env_file.write_text("BASE_URL=x\nCONTROL_ROOM_ALLOW_DELETE=1\n")
    assert cr.allow_delete_setting(env={}, env_file=env_file) is True
    env_file.write_text("CONTROL_ROOM_ALLOW_DELETE=0\n")
    assert cr.allow_delete_setting(env={}, env_file=env_file) is False
    assert cr.allow_delete_setting(env={"CONTROL_ROOM_ALLOW_DELETE": "true"}, env_file=env_file) is True


def test_read_only_server_refuses_every_delete_request(tmp_path):
    sent = []
    srv, base = _start(tmp_path, lambda url, timeout: (sent.append(url), FakeResp(200))[1], allow_delete=False)
    try:
        _req(base + "/api/review", "PUT", _flag("1"))        # flagging still works
        assert _req(base + "/api/review")[1]["1"]["status"] == "flag"
        for body in ({"ids": ["1"], "dry_run": True},
                     {"ids": ["1"], "dry_run": False, "confirm": "YES", "token": "x"}):
            code, out = _req(base + "/api/delete", "POST", body)
            assert code == 403 and "read-only" in out["error"]
        assert sent == []
    finally:
        srv.shutdown()


def test_read_only_page_has_no_delete_button_but_a_badge():
    import bt_paths_map as m
    paths = {"1": {"name": "100->200", "coords": [[35.0, 33.0], [35.01, 33.01]]}}
    kw = dict(sensors={}, ref_sensors=[], server_mode=True)
    assert "reviewDelBtn\" style" in m.build_html(paths, **kw)
    ro = m.build_html(paths, allow_delete=False, **kw)
    assert "id=\"reviewDelBtn\"" not in ro and "id=\"exportBtn\"" in ro
    assert "Read-only" in cr.build_shell(cr.sensor_views(), "", read_only=True)
    assert "Read-only" not in cr.build_shell(cr.sensor_views(), "", read_only=False)


def test_delete_address_comes_from_env_not_the_code(tmp_path):
    env_file = tmp_path / ".env"
    assert cr.delete_url_setting(env={}, env_file=env_file) is None
    env_file.write_text("CONTROL_ROOM_DELETE_URL=http://example.test/x\n")
    assert cr.delete_url_setting(env={}, env_file=env_file) == "http://example.test/x"
    assert cr.delete_url_setting(env={"CONTROL_ROOM_DELETE_URL": "http://other.test"}, env_file=env_file) == "http://other.test"
    source = (ROOT / "tools" / "control_room.py").read_text(encoding="utf-8")
    import re
    assert not re.search(r"10\.\d+\.\d+\.\d+", source)      # the repo is public: no internal addresses


def test_live_delete_refuses_when_no_address_is_configured(tmp_path):
    sent = []
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv",
                         page_builder=lambda: ("", {}), paths_provider=lambda: {"1": {"name": "p1"}},
                         post=lambda url, timeout: sent.append(url), allow_delete=True, delete_url="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        _req(base + "/api/review", "PUT", _flag("1"))
        code, dry = _req(base + "/api/delete", "POST", {"ids": ["1"], "dry_run": True})
        code, out = _req(base + "/api/delete", "POST", {"ids": ["1"], "dry_run": False, "confirm": "YES", "token": dry["token"]})
        assert code == 400 and "CONTROL_ROOM_DELETE_URL" in out["error"] and sent == []
    finally:
        srv.shutdown()


def test_jump_bar_is_hidden_until_a_button_shows(tmp_path):
    out = cr.add_nav("<body></body>", "bluetooth", cr.sensor_views())
    assert ".crnav{" in out and "display:none" in out.split(".crnav.on")[0]   # hidden by default
    assert ".crnav.on{display:flex}" in out
    assert out.count("nav.classList.add('on')") == 2                           # back pill and "Paths here"


# ── CSV export is written by the server ──────────────────────────────────────

def test_save_csv_writes_a_real_csv_with_quotes_and_non_ascii(tmp_path):
    rows = [["Sensor ID", "Name", "Reason"], ["1", 'Iroon, "Morfou"', "\u03a0\u03ac\u03c6\u03bf\u03c2"], ["2", None, 5]]
    out = cr.save_csv("bt_sensor_suspects", rows, tmp_path)
    assert out.parent == tmp_path and out.name.startswith("bt_sensor_suspects_") and out.suffix == ".csv"
    import csv as _csv
    back = list(_csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert back == [["Sensor ID", "Name", "Reason"], ["1", 'Iroon, "Morfou"', "\u03a0\u03ac\u03c6\u03bf\u03c2"], ["2", "", "5"]]


def test_save_csv_refuses_unknown_names_and_odd_rows(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        cr.save_csv("../../evil", [["a"]], tmp_path)
    with pytest.raises(ValueError):
        cr.save_csv("bt_sensor_suspects", [[{"x": 1}]], tmp_path)
    with pytest.raises(ValueError):
        cr.save_csv("bt_sensor_suspects", "not rows", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_save_csv_endpoint_saves_and_rejects_cross_origin(tmp_path):
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv",
                         page_builder=lambda: ("", {}), paths_provider=dict, downloads_dir=tmp_path / "dl", choose_path=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    body = {"name": "bt_sensor_suspects", "rows": [["a", "b"], ["1", "2"]]}
    try:
        assert _req(base + "/api/save-csv", "POST", body, {"Origin": "http://evil.example"})[0] == 403
        code, out = _req(base + "/api/save-csv", "POST", body)
        assert code == 200 and Path(out["path"]).read_text(encoding="utf-8-sig").splitlines() == ["a,b", "1,2"]
        assert _req(base + "/api/save-csv", "POST", {"name": "other", "rows": []})[0] == 400
    finally:
        srv.shutdown()


def test_triangulation_export_uses_the_server_not_a_browser_download():
    import bt_sensor_suspects as bs
    result = {"sensors": {}, "meta": {"enough_history": True, "runs_used": 20, "runs_ignored_as_outage": 0,
                                      "unusable_paths": 0}}
    html = cr.triangulation_html(result)
    assert "/api/save-csv" in html and "createObjectURL" not in html and 'id="triSaved"' in html


def _dl_server(tmp_path, chooser):
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv",
                         page_builder=lambda: ("", {}), paths_provider=dict, downloads_dir=tmp_path / "dl",
                         choose_path=chooser)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_export_saves_where_the_user_chooses_in_the_dialog(tmp_path):
    asked = []
    chosen = tmp_path / "mine" / "suspects"          # no extension: .csv is added
    srv, base = _dl_server(tmp_path, lambda folder, name: (asked.append((folder, name)), chosen)[1])
    try:
        code, out = _req(base + "/api/save-csv", "POST", {"name": "bt_sensor_suspects", "rows": [["a"], ["1"]]})
        assert code == 200 and Path(out["path"]) == tmp_path / "mine" / "suspects.csv" and not out["fallback"]
        assert Path(out["path"]).exists()
        assert asked[0][0] == tmp_path / "dl" and asked[0][1].startswith("bt_sensor_suspects_")
    finally:
        srv.shutdown()


def test_cancelling_the_dialog_saves_nothing(tmp_path):
    srv, base = _dl_server(tmp_path, lambda folder, name: None)
    try:
        code, out = _req(base + "/api/save-csv", "POST", {"name": "bt_sensor_suspects", "rows": [["a"]]})
        assert code == 200 and out == {"cancelled": True}
        assert not (tmp_path / "dl").exists() or not any((tmp_path / "dl").iterdir())
    finally:
        srv.shutdown()


def test_no_dialog_available_falls_back_to_downloads_and_says_so(tmp_path):
    def broken(folder, name):
        raise RuntimeError("no display")
    srv, base = _dl_server(tmp_path, broken)
    try:
        code, out = _req(base + "/api/save-csv", "POST", {"name": "bt_sensor_suspects", "rows": [["a"]]})
        assert code == 200 and out["fallback"] is True and Path(out["path"]).parent == tmp_path / "dl"
    finally:
        srv.shutdown()
