"""Bulk edit of OMNIA paths: planning, applying with a fake OMNIA, undo, and the Control Room endpoints."""
import copy
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import omnia_edit as oe  # noqa: E402
import control_room as cr  # noqa: E402

NODES = [{"id": i, "name": "Node %d" % i, "lat": 35.0, "lon": 33.0} for i in (1, 2, 3, 4)]


def snap():
    return {"fetched_at": "t", "nodes": copy.deepcopy(NODES), "paths": [
        {"id": 10, "name": "Alpha", "start": 1, "end": 2, "speed_min": 30, "speed_max": 65},
        {"id": 11, "name": "Beta", "start": 2, "end": 1, "speed_min": 30, "speed_max": 65},
        {"id": 12, "name": "NoNodes", "start": None, "end": None, "speed_min": None, "speed_max": None},
    ]}


# ── planning ─────────────────────────────────────────────────────────────────

def test_speed_change_keeps_nodes_and_name_and_skips_unchanged_paths():
    plan = oe.plan_edits(snap(), ["10", "11"], speed_max="70")
    assert [i["id"] for i in plan["items"]] == ["10", "11"] and plan["refused"] == []
    a = plan["items"][0]["after"]
    assert a == {"speed_min": 30, "speed_max": 70, "start": 1, "end": 2}
    same = oe.plan_edits(snap(), ["10"], speed_max="65")
    assert same["items"] == [] and same["refused"][0]["reason"] == "nothing to change"


def test_swap_exchanges_the_nodes():
    plan = oe.plan_edits(snap(), ["10"], swap=True)
    assert plan["items"][0]["after"]["start"] == 2 and plan["items"][0]["after"]["end"] == 1


def test_a_path_without_nodes_needs_both_nodes_and_a_speed_range_before_it_can_change():
    plan = oe.plan_edits(snap(), ["12"], speed_min="10", speed_max="50")
    assert plan["items"] == [] and "start, end" in plan["refused"][0]["reason"]
    ok = oe.plan_edits(snap(), ["12"], speed_min="10", speed_max="50", start="3", end="4")
    assert ok["items"][0]["after"] == {"speed_min": 10, "speed_max": 50, "start": 3, "end": 4}


def test_suggested_nodes_fill_only_missing_ones_and_still_need_a_speed_range():
    sug = {"12": {"start": 3, "end": 4}, "10": {"start": 4, "end": 3}}
    no_speed = oe.plan_edits(snap(), ["12"], suggested=sug)
    assert no_speed["items"] == [] and "speed min, speed max" in no_speed["refused"][0]["reason"]
    ok = oe.plan_edits(snap(), ["12", "10"], speed_min="10", speed_max="50", suggested=sug)
    by_id = {i["id"]: i for i in ok["items"]}
    assert by_id["12"]["after"] == {"speed_min": 10, "speed_max": 50, "start": 3, "end": 4}
    assert by_id["10"]["after"]["start"] == 1 and by_id["10"]["after"]["end"] == 2          # nodes already set stay
    none = oe.plan_edits(snap(), ["12"], suggested={})
    assert none["items"] == [] and "no clear nearest node" in none["refused"][0]["reason"]


def test_bad_values_are_refused_not_sent():
    s = snap()
    assert "0 < min < max" in oe.plan_edits(s, ["10"], speed_min="80")["refused"][0]["reason"]
    assert "unknown node" in oe.plan_edits(s, ["10"], start="99")["refused"][0]["reason"]
    assert "same" in oe.plan_edits(s, ["10"], end="1")["refused"][0]["reason"]
    assert "not found" in oe.plan_edits(s, ["999"], speed_max="70")["refused"][0]["reason"]
    with pytest.raises(ValueError):
        oe.plan_edits(s, ["10"])                                 # nothing to change
    with pytest.raises(ValueError):
        oe.plan_edits(s, [], speed_max="70")                     # nothing selected
    with pytest.raises(ValueError):
        oe.plan_edits(s, [str(i) for i in range(oe.MAX_BATCH + 1)], speed_max="70")
    with pytest.raises(ValueError):
        oe.plan_edits(s, ["10"], speed_max="fast")


def test_stale_detection_and_request_parameters():
    plan = oe.plan_edits(snap(), ["10"], speed_max="70")
    assert oe.stale_ids(plan["items"], snap()) == []
    changed = snap()
    changed["paths"][0]["speed_min"] = 20                         # someone edited it in OMNIA meanwhile
    assert oe.stale_ids(plan["items"], changed) == ["10"]
    renamed = snap()
    renamed["paths"][0]["name"] = "Other"
    assert oe.stale_ids(plan["items"], renamed) == ["10"]
    assert oe.request_params(plan["items"][0]) == {"pathId": "10", "pathName": "Alpha", "speedMin": 30, "speedMax": 70,
                                                    "idLogicalNodeStart": 1, "idLogicalNodeEnd": 2}


# ── applying ─────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, text="True", code=200):
        self.text, self.status_code = text, code


def test_apply_sends_one_request_per_path_logs_old_and_new_and_stops_at_the_first_failure(tmp_path):
    plan = oe.plan_edits(snap(), ["10", "11"], speed_max="70")
    sent = []

    def post(url, params, timeout):
        sent.append((url, dict(params)))
        return Resp("True") if params["pathId"] == "10" else Resp("<html>error</html>", 500)
    batch, results, error = oe.apply_items(plan["items"], "http://omnia.test/e", tmp_path / "log.csv", post=post, sleep=lambda s: None)
    assert [r["ok"] for r in results] == [True, False] and "path 11" in error and "omnia.test" not in error
    assert sent[0][0] == "http://omnia.test/e" and sent[0][1]["pathName"] == "Alpha"
    log = (tmp_path / "log.csv").read_text(encoding="utf-8").splitlines()
    assert len(log) == 3 and log[1].split(",")[2:6] == ["10", "Alpha", "True", "200"] and ",30,65,1,2,30,70,1,2" in log[1]


def test_undo_restores_the_old_values_only_for_untouched_paths(tmp_path):
    s = snap()
    plan = oe.plan_edits(s, ["10", "11"], speed_max="70")
    oe.apply_items(plan["items"], "http://x.test", tmp_path / "log.csv", post=lambda url, params, timeout: Resp("True"), sleep=lambda x: None)
    now = snap()
    for p in now["paths"][:2]:
        p["speed_max"] = 70                                       # OMNIA now has the new values
    now["paths"][1]["speed_min"] = 25                             # ... but path 11 was edited by hand afterwards
    undo = oe.undo_items(tmp_path / "log.csv", now)
    assert [i["id"] for i in undo["items"]] == ["10"] and undo["items"][0]["after"]["speed_max"] == 65
    assert undo["refused"][0]["id"] == "11" and "changed in OMNIA" in undo["refused"][0]["reason"]
    with pytest.raises(ValueError):
        oe.undo_items(tmp_path / "nothing.csv", now)


# ── the Control Room endpoints, against a pretend OMNIA ─────────────────────

class FakeOmnia:
    def __init__(self):
        self.state = snap()
        self.writes = []
        self.ignore_renames = False

    def fetch(self, base):
        out = copy.deepcopy(self.state)
        out["fetched_at"] = "T%d" % len(self.writes)
        return out

    def post(self, url, params, timeout):
        self.writes.append(dict(params))
        p = [x for x in self.state["paths"] if str(x["id"]) == params["pathId"]][0]
        if url.endswith("/r"):                       # the rename address: only the name changes
            if not self.ignore_renames:
                p["name"] = params["pathName"]
            return Resp("")
        p.update(speed_min=params["speedMin"], speed_max=params["speedMax"], start=params["idLogicalNodeStart"], end=params["idLogicalNodeEnd"])
        return Resp("True")


def _server(tmp_path, fake, allow_edit=True, allow_delete=False, delete_post=None):
    srv = cr.make_server(allow_delete=allow_delete, delete_url="http://omnia.test/del" if allow_delete else "", post=delete_post, port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv", page_builder=lambda: ("", {}),
                         paths_provider=dict, views={}, db_file=tmp_path / "none.db", snapshot_file=tmp_path / "snap.json",
                         omnia_urls={"nodes": "http://omnia.test/n", "paths": "http://omnia.test/p", "edit": "http://omnia.test/e", "rename": "http://omnia.test/r"}, fetch_omnia=fake.fetch, omnia_post=fake.post, allow_edit=allow_edit,
                         edit_log=tmp_path / "edit.csv", rename_log=tmp_path / "rename.csv", wiring_inputs=lambda: ({}, {}), choose_path=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _req(url, body, headers=None):
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_preview_then_apply_changes_omnia_and_undo_puts_it_back(tmp_path):
    fake = FakeOmnia()
    srv, base = _server(tmp_path, fake)
    try:
        code, plan = _req(base + "/api/edit/plan", {"ids": ["10", "11"], "speed_max": "70"})
        assert code == 200 and len(plan["items"]) == 2 and plan["token"] and fake.writes == []      # a preview sends nothing
        assert _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "no"})[0] == 400
        code, out = _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})
        assert code == 200 and [r["ok"] for r in out["results"]] == [True, True] and out["error"] is None
        assert [w["speedMax"] for w in fake.writes] == [70, 70] and all(w["pathName"] in ("Alpha", "Beta") for w in fake.writes)
        assert _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})[0] == 400  # a preview works once
        code, undo = _req(base + "/api/edit/plan", {"undo": True})
        assert code == 200 and len(undo["items"]) == 2
        assert _req(base + "/api/edit/apply", {"token": undo["token"], "confirm": "YES"})[0] == 200
        assert [p["speed_max"] for p in fake.state["paths"][:2]] == [65, 65]
    finally:
        srv.shutdown()


def test_apply_is_refused_when_editing_is_off_or_omnia_changed_after_the_preview(tmp_path):
    fake = FakeOmnia()
    srv, base = _server(tmp_path, fake, allow_edit=False)
    try:
        code, plan = _req(base + "/api/edit/plan", {"ids": ["10"], "speed_max": "70"})
        assert code == 200 and plan["allowed"] is False                       # the preview itself is allowed
        code, out = _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})
        assert code == 403 and "switched off" in out["error"] and fake.writes == []
    finally:
        srv.shutdown()
    fake = FakeOmnia()
    srv, base = _server(tmp_path, fake)
    try:
        code, plan = _req(base + "/api/edit/plan", {"ids": ["10"], "speed_max": "70"})
        fake.state["paths"][0]["speed_min"] = 20                               # someone edits it in OMNIA meanwhile
        code, out = _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})
        assert code == 409 and out["stale"] == ["10"] and fake.writes == []
        assert _req(base + "/api/edit/plan", {"ids": ["10"], "speed_max": "70"}, {"Origin": "http://evil.example"})[0] == 403
    finally:
        srv.shutdown()


def test_edit_setting_is_off_unless_the_pc_opted_in(tmp_path):
    env_file = tmp_path / ".env"
    assert cr.allow_edit_setting(env={}, env_file=env_file) is False
    env_file.write_text("CONTROL_ROOM_ALLOW_EDIT=1\n")
    assert cr.allow_edit_setting(env={}, env_file=env_file) is True


# ── positive whole numbers only ──────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["-5", "0", "12.5", "abc", "1e2", True])
def test_only_whole_positive_numbers_are_accepted(bad):
    with pytest.raises(ValueError, match="whole positive"):
        oe.plan_edits(snap(), ["10"], speed_max=bad)
    with pytest.raises(ValueError, match="whole positive"):
        oe.plan_edits(snap(), ["10"], start=bad)
    assert oe.plan_edits(snap(), ["10"], speed_max=" 70 ")["items"][0]["after"]["speed_max"] == 70


# ── bulk delete ──────────────────────────────────────────────────────────────

def test_plan_deletes_lists_what_would_go_and_refuses_unknown_ids():
    plan = oe.plan_deletes(snap(), ["10", "999", "x"])
    assert [i["id"] for i in plan["items"]] == ["10"] and plan["items"][0]["start_name"] == "Node 1"
    assert {r["id"] for r in plan["refused"]} == {"999", "x"}
    with pytest.raises(ValueError):
        oe.plan_deletes(snap(), [])
    with pytest.raises(ValueError):
        oe.plan_deletes(snap(), [str(i) for i in range(oe.MAX_BATCH + 1)])


class DelResp:
    def __init__(self, ok=True):
        self.is_success, self.status_code = ok, 200 if ok else 500


def test_bulk_delete_needs_the_opt_in_the_word_DELETE_and_a_fresh_preview(tmp_path):
    fake = FakeOmnia()
    deleted = []

    def delete_post(url, timeout):
        deleted.append(url)
        pid = url.rsplit("=", 1)[1]
        fake.state["paths"] = [p for p in fake.state["paths"] if str(p["id"]) != pid]
        return DelResp()
    srv, base = _server(tmp_path, fake, allow_delete=False, delete_post=delete_post)
    try:
        code, plan = _req(base + "/api/edit/plan", {"delete": True, "ids": ["10"]})
        assert code == 200 and plan["kind"] == "delete" and plan["allowed"] is False
        assert _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "DELETE"})[0] == 403 and deleted == []
    finally:
        srv.shutdown()
    fake = FakeOmnia()
    srv, base = _server(tmp_path, fake, allow_delete=True, delete_post=delete_post)
    try:
        code, plan = _req(base + "/api/edit/plan", {"delete": True, "ids": ["10", "11"]})
        assert code == 200 and plan["allowed"] is True and len(plan["items"]) == 2
        assert _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})[0] == 400      # YES is for edits, not deletes
        code, out = _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "DELETE"})
        assert code == 200 and out["kind"] == "delete" and [r["ok"] for r in out["results"]] == [True, True]
        assert deleted == ["http://omnia.test/del?pathIdToDelete=10", "http://omnia.test/del?pathIdToDelete=11"]
        assert [p["id"] for p in fake.state["paths"]] == [12]
        assert _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "DELETE"})[0] == 400      # a preview works once
    finally:
        srv.shutdown()


def test_both_delete_dialogs_ask_for_a_confirmation_popup_first():
    import bt_paths_map as m
    assert "Permanently delete" in cr.build_wiring_page(None, None, True, True, True)
    html = m.build_html({}, {}, [], server_mode=True, allow_delete=True, duplicate_groups=[], dropped_ids=set(), reversed_by_id={}, reversed_groups=[], direction_by_id={}, direction_pairs=[], data_quality_by_id={})
    assert "Permanently delete" in html


# ── rename ───────────────────────────────────────────────────────────────────

def test_plan_renames_find_replace_prefix_suffix_whole_name_and_refusals():
    s = snap()
    plan = oe.plan_renames(s, ["10", "11"], find="a", replace="o", prefix="P-", suffix="-X")
    assert [(i["name"], i["new_name"]) for i in plan["items"]] == [("Alpha", "P-Alpho-X"), ("Beta", "P-Beto-X")]
    assert oe.plan_renames(s, ["10"], new_name=" Fresh ")["items"][0]["new_name"] == "Fresh"
    with pytest.raises(ValueError, match="only be given to one path"):
        oe.plan_renames(s, ["10", "11"], new_name="Same")
    with pytest.raises(ValueError):
        oe.plan_renames(s, ["10"])
    assert oe.plan_renames(s, ["10"], new_name="Alpha")["refused"][0]["reason"] == "nothing to change"
    assert "empty" in oe.plan_renames(s, ["10"], find="Alpha", replace="")["refused"][0]["reason"]
    assert "longer" in oe.plan_renames(s, ["10"], new_name="x" * 101)["refused"][0]["reason"]
    assert "not found" in oe.plan_renames(s, ["999"], prefix="a")["refused"][0]["reason"]
    std = oe.plan_renames(s, ["10", "11"], standard={"10": "1001->1002"})
    assert [i["new_name"] for i in std["items"]] == ["1001->1002"] and "standard name" in std["refused"][0]["reason"]


def test_rename_sends_only_id_and_name_logs_and_undo_restores(tmp_path):
    s = snap()
    plan = oe.plan_renames(s, ["10", "11"], suffix="-v2")
    sent = []
    batch, results, error = oe.apply_renames(plan["items"], "http://omnia.test/r", tmp_path / "ren.csv",
                                             post=lambda url, params, timeout: (sent.append(dict(params)), Resp(""))[1], sleep=lambda x: None)
    assert error is None and sent == [{"pathId": "10", "pathName": "Alpha-v2"}, {"pathId": "11", "pathName": "Beta-v2"}]
    now = snap()
    now["paths"][0]["name"], now["paths"][1]["name"] = "Alpha-v2", "Beta-v2"
    assert oe.verify_renames(plan["items"], now) == {"10": True, "11": True}
    assert oe.verify_renames(plan["items"], s) == {"10": False, "11": False}
    now["paths"][1]["name"] = "Edited by hand"
    undo = oe.undo_renames(tmp_path / "ren.csv", now)
    assert [(i["id"], i["new_name"]) for i in undo["items"]] == [("10", "Alpha")] and "renamed again" in undo["refused"][0]["reason"]


def test_rename_through_the_control_room_checks_the_names_really_changed(tmp_path):
    fake = FakeOmnia()
    srv, base = _server(tmp_path, fake)
    try:
        code, plan = _req(base + "/api/edit/plan", {"ids": ["10", "11"], "rename": {"suffix": "-v2"}})
        assert code == 200 and plan["kind"] == "rename" and plan["allowed"] is True and fake.writes == []
        assert _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "no"})[0] == 400
        code, out = _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})
        assert code == 200 and out["kind"] == "rename" and out["error"] is None and [r["ok"] for r in out["results"]] == [True, True]
        assert [p["name"] for p in fake.state["paths"][:2]] == ["Alpha-v2", "Beta-v2"]
        code, undo = _req(base + "/api/edit/plan", {"undo_rename": True})
        assert code == 200 and len(undo["items"]) == 2
        assert _req(base + "/api/edit/apply", {"token": undo["token"], "confirm": "YES"})[0] == 200
        assert [p["name"] for p in fake.state["paths"][:2]] == ["Alpha", "Beta"]
    finally:
        srv.shutdown()
    fake = FakeOmnia()
    fake.ignore_renames = True                                   # OMNIA says fine but changes nothing
    srv, base = _server(tmp_path, fake)
    try:
        code, plan = _req(base + "/api/edit/plan", {"ids": ["10"], "rename": {"new_name": "Fresh"}})
        code, out = _req(base + "/api/edit/apply", {"token": plan["token"], "confirm": "YES"})
        assert code == 200 and out["results"][0]["ok"] is False and "did not change" in out["error"]
    finally:
        srv.shutdown()


def test_standard_name_comes_from_the_site_codes_of_the_nodes_sensors(tmp_path):
    fake = FakeOmnia()
    fake.state["nodes"] = [{"id": 1, "name": "Alpha", "lat": 35.0, "lon": 33.0}, {"id": 2, "name": "Beta", "lat": 35.01, "lon": 33.0},
                           {"id": 3, "name": "Node 3", "lat": 36.0, "lon": 33.0}, {"id": 4, "name": "Node 4", "lat": 37.0, "lon": 33.0}]
    sensors = {"a": {"name": "Alpha", "site_code": "1001", "lat": 35.0, "lon": 33.0}, "b": {"name": "Beta", "site_code": "1002", "lat": 35.01, "lon": 33.0}}
    srv, base = _server(tmp_path, fake)
    srv.wiring_inputs = lambda: ({}, sensors)
    try:
        code, plan = _req(base + "/api/edit/plan", {"ids": ["10", "11"], "rename": {"standard": True}})
        assert code == 200 and [(i["id"], i["new_name"]) for i in plan["items"]] == [("10", "1001->1002"), ("11", "1002->1001")]
    finally:
        srv.shutdown()
