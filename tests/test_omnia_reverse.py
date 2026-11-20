"""Reverse of an OMNIA path: opposite-direction arcs at both ends + OMNIA's own route calculation as the check. Never writes."""
import json
import math
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import omnia_reverse as orv  # noqa: E402
import control_room as cr  # noqa: E402

LAT = 35.0
M_LON = 111320 * math.cos(math.radians(LAT))
URLS = ("http://o/arcs", "http://o/calc", "http://o/near")


def lon(x):                                   # metres east of the start of the road
    return 33.0 + x / M_LON


def html(code):
    return '<img src="/x/arrows/arrow%d_16.png">' % code


def arc(i, x, length, code, order=0, street="Main St"):
    return {"arcId": i, "arcLength": length, "arcOrder": order, "arcStreetName": street, "arcDirection": html(code), "arcLat": LAT, "arcLon": lon(x)}


# a straight road, forward going east (code 0) from x=0 to x=200; the other carriageway has its own ids (code 4, west)
F1, F2 = arc(10, 50, 100, 0, 0), arc(11, 150, 100, 0, 1)
R1, R2 = arc(20, 150, 100, 4), arc(21, 50, 100, 4)          # 20 leaves the forward END, 21 arrives at the forward START
DECOY = arc(22, 70, 40, 4)                                  # same direction, but nowhere near either end
ENDS = ((LAT, lon(0)), (LAT, lon(200)))


class Resp:
    def __init__(self, rows, status=200):
        self.status_code, self._rows = status, rows

    def json(self):
        return {"data": self._rows}


def fake(nearby=(F1, F2, R1, R2, DECOY), routes=None, forward=(F1, F2), log=None):
    routes = routes if routes is not None else {(20, 21): [R1, R2]}

    def post(url, data=None, params=None, timeout=None):
        if log is not None:
            log.append((url, dict(params or data)))
        if url.endswith("/arcs"):
            return Resp(list(forward))
        return Resp(routes.get(tuple(int(p) for p in data["listTimingPoints"]), []))

    def get(url, params=None, timeout=None):
        if log is not None:
            log.append((url, dict(params)))
        return Resp(list(nearby))
    return post, get


def test_arrow_codes_and_directions():
    assert orv.arrow('<img src="/r/arrow7_16.png">') == 7 and orv.arrow("") is None
    assert [orv.opposite(c) for c in (0, 3, 4, 7)] == [4, 7, 0, 3] and orv.opposite(None) is None


def test_urls_come_only_from_their_own_settings():
    assert orv.derive_urls({"paths": "http://omnia.test/a/b"}) == (None, None, None)          # never guessed from another address
    assert orv.derive_create_urls({"paths": "http://omnia.test/a/b", "newid": "I"}) == ("I", None)
    assert orv.derive_urls({"paths": "http://x/a/b", "arcs": "A", "calc": "C", "nearest": "N"}) == ("A", "C", "N")
    assert orv.derive_urls({}) == (None, None, None)


def test_the_reverse_starts_and_ends_on_the_opposite_arcs_at_the_two_ends_and_only_reads():
    log = []
    post, get = fake(log=log)
    plan = orv.plan_reverse("1", "A->B", URLS, post, get, ENDS)
    assert plan["ok"] and plan["problems"] == [] and plan["timing_points"] == [20, 21]
    assert plan["forward"] == {"arcs": 2, "length": 200} and plan["reverse"] == {"arcs": 2, "length": 200}
    assert {u for u, _ in log} == set(URLS)                                                     # arcs, nearest and calculate: nothing else
    assert (URLS[0], {"iPathId": "1"}) in log


def test_without_the_drawn_route_the_ends_are_estimated_from_the_first_and_last_arc():
    post, get = fake()
    assert orv.plan_reverse("1", "", URLS, post, get)["timing_points"] == [20, 21]


def test_an_arc_that_arrives_at_the_end_is_not_taken_as_the_start_of_the_reverse():
    arriving = arc(30, 250, 100, 4)                          # centre 250 running west: it ARRIVES at x=200 instead of leaving it
    post, get = fake(nearby=(F1, F2, arriving, R2))
    plan = orv.plan_reverse("1", "", URLS, post, get, ENDS)
    assert not plan["ok"] and "opposite way" in plan["problems"][0]


def test_a_one_way_street_or_a_wrong_route_is_refused_with_the_reason():
    post, get = fake(nearby=(F1, F2))                         # no opposite arcs at all
    one_way = orv.plan_reverse("1", "", URLS, post, get, ENDS)
    assert not one_way["ok"] and "one-way" in one_way["problems"][0] and one_way["timing_points"] == []
    post, get = fake(routes={(20, 21): [R1]})                  # OMNIA's route is much shorter than the forward path
    short = orv.plan_reverse("1", "", URLS, post, get, ENDS)
    assert not short["ok"] and any("reverse route is" in p for p in short["problems"])
    post, get = fake(routes={})                                # OMNIA finds no route between the two arcs
    none = orv.plan_reverse("1", "", URLS, post, get, ENDS)
    assert not none["ok"] and "no route" in none["problems"][0]


def test_a_path_without_arcs_or_a_refusing_omnia_raises_a_readable_error():
    post, get = fake(forward=())
    with pytest.raises(ValueError, match="no arcs"):
        orv.plan_reverse("9", "", URLS, post, get, ENDS)
    with pytest.raises(ValueError, match="HTTP 500"):
        orv.plan_reverse("9", "", URLS, lambda url, data=None, params=None, timeout=None: Resp([], 500), get, ENDS)


def test_an_answer_without_a_route_list_means_no_route_not_a_crash():
    class Bare:
        status_code = 200

        def json(self):
            return {"success": False}
    _post, get = fake()
    plan = orv.plan_reverse("1", "", URLS, lambda url, data=None, params=None, timeout=None: Resp([F1, F2]) if url.endswith("/arcs") else Bare(), get, ENDS)
    assert not plan["ok"] and "no route" in plan["problems"][0]


def test_the_control_room_endpoint_previews_only_and_reports_per_path(tmp_path):
    log = []
    post, get = fake(log=log)
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv", page_builder=lambda: ("", {}), paths_provider=dict, views={},
                         db_file=tmp_path / "n.db", snapshot_file=tmp_path / "s.json",
                         omnia_urls={"paths": "http://o/x/GetAllPaths", "arcs": URLS[0], "calc": URLS[1], "nearest": URLS[2]},
                         omnia_post=post, omnia_get=get, wiring_inputs=lambda: ({}, {}), choose_path=None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(body):
        req = urllib.request.Request(base + "/api/reverse/plan", data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        code, out = call({"ids": ["683"]})
        assert code == 200 and out["kind"] == "reverse" and out["items"][0]["ok"] and out["items"][0]["timing_points"] == [20, 21] and out["refused"] == []
        assert {u for u, _ in log} <= set(URLS)                                              # nothing else was called
        assert call({"ids": []})[0] == 400
        assert call({"ids": [str(i) for i in range(cr.REVERSE_MAX + 1)]})[0] == 400
        # the page previews one path per call and adds them up, so it can show "path 2 of 3"; the last token holds them all
        _c, a = call({"ids": ["683"]})
        _c, b = call({"ids": ["684"], "append_to": a["token"]})
        _c, c = call({"ids": ["683"], "append_to": b["token"]})                    # the same path again is not counted twice
        assert a["token"] not in srv.edit_tokens and [it["id"] for it in srv.edit_tokens[c["token"]][1]] == ["684", "683"]
        _c, fresh = call({"ids": ["685"], "append_to": "stale-or-made-up"})          # an unknown token starts a new batch
        assert [it["id"] for it in srv.edit_tokens[fresh["token"]][1]] == ["685"]
    finally:
        srv.shutdown()


def test_the_page_previews_path_by_path_with_a_progress_line():
    src = Path(cr.__file__).read_text(encoding="utf-8")
    assert "append_to: acc.token" in src and "Checking path ' + (p.done + 1) + ' of ' + p.total" in src and "setInterval" in src
    assert "if (preview !== acc) return;" in src                                   # Cancel stops the loop


# ── creating (writes, so everything below runs against a pretend OMNIA) ─────

class Text:
    def __init__(self, text, status=200):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)


class World:
    """A pretend OMNIA that records every call: new id 700, builds a path from its end arcs, edits it."""

    def __init__(self, new_id="700", built_route=(20, 21), existing=("1",), edit_answer="True"):
        self.calls, self.new_id, self.built_route, self.edit_answer = [], new_id, list(built_route), edit_answer
        self.paths = {pid: {"id": int(pid), "name": "A->B", "start": 5, "end": 6, "speed_min": 30, "speed_max": 90} for pid in existing}

    def post(self, url, data=None, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], dict(params or data)))
        if url.endswith("/arcs"):
            return Resp([F1, F2] if (params or {}).get("iPathId") == "1" else [arc(i, 0, 100, 4) for i in self.built_route])
        if url.endswith("/calc"):
            return Resp([R1, R2] if [int(p) for p in data["listTimingPoints"]] == [20, 21] else [])
        if url.endswith("/build"):
            self.paths[str(data["pathId"])] = {"id": int(data["pathId"]), "name": data["pathName"], "start": None, "end": None, "speed_min": None, "speed_max": None}
            return Text(str(data["pathId"]))
        if url.endswith("/edit"):
            p = self.paths[params["pathId"]]
            p.update(name=params["pathName"], start=params["idLogicalNodeStart"], end=params["idLogicalNodeEnd"], speed_min=params["speedMin"], speed_max=params["speedMax"])
            return Text(self.edit_answer)
        raise AssertionError(url)

    def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], dict(params)))
        if url.endswith("/newid"):
            return Text(self.new_id)
        return Resp([F1, F2, R1, R2, DECOY])

    def snapshot(self, _urls=None):
        return {"fetched_at": "t", "nodes": [], "paths": [dict(p) for p in self.paths.values()]}


CREATE_URLS = ("http://o/newid", "http://o/build", "http://o/arcs")


def _item(world):
    plan = orv.plan_reverse("1", "A->B", URLS, world.post, world.get, ENDS)
    plan["new_name"] = orv.reverse_name("A->B", "1")
    return plan


def test_reverse_name_duplicates_and_new_id_answers():
    assert orv.reverse_name("7052->7053", "1") == "7053->7052" and orv.reverse_name(" 1001 -> 1002 ", "1") == "1002->1001"
    assert orv.reverse_name("New Path", "9") == "New Path reverse" and orv.reverse_name("", "9") == "Reverse of 9"
    assert orv.parse_new_id(Text("701")) == 701 and orv.parse_new_id(Text('{"data": 702}')) == 702 and orv.parse_new_id(Text('"703"')) == 703
    for bad in (Text("oops"), Text("0"), Text("5", 500)):
        with pytest.raises(ValueError):
            orv.parse_new_id(bad)


def test_create_refuses_only_on_a_drawn_reverse_or_one_that_cannot_be_compared():
    assert cr.existing_reverse({"pair": "690", "check": None})[0].startswith("path 690 is already drawn the other way")
    block, warn = cr.existing_reverse({"pair": None, "check": {"id": "692", "why": "nodes swapped", "gap": None}})
    assert "may already be its reverse" in block and "Refresh" in block and warn is None        # e.g. a reverse made a moment ago
    block, warn = cr.existing_reverse({"pair": None, "check": {"id": "120", "why": "names swapped", "gap": (0, 69)}})
    assert block is None and "not treated as the reverse" in warn and "same name as path 120" in warn
    assert cr.existing_reverse(None) == (None, None)


def test_create_gets_an_id_builds_from_the_end_arcs_checks_the_arcs_then_sets_swapped_nodes(tmp_path):
    w = World()
    res = orv.create_reverse(_item(w), w.paths["1"], {"1"}, CREATE_URLS, "http://o/edit", w.post, w.get, tmp_path / "c.csv", clock=lambda: 1.0)
    assert res["error"] is None and res["new_id"] == 700 and res["built"] and res["arcs_match"] and res["speeds_nodes_set"]
    writes = [c for c in w.calls if c[0] in ("build", "edit")]
    assert writes[0] == ("build", {"listPathArcs": ["20", "21"], "pathName": "B->A", "pathId": "700"})
    assert writes[1][1]["idLogicalNodeStart"] == 6 and writes[1][1]["idLogicalNodeEnd"] == 5 and writes[1][1]["pathName"] == "B->A"
    assert ("newid", {"_dc": 1000}) in w.calls
    assert w.paths["700"] == {"id": 700, "name": "B->A", "start": 6, "end": 5, "speed_min": 30, "speed_max": 90}
    log = (tmp_path / "c.csv").read_text(encoding="utf-8")
    assert "forward_id,new_id" in log and ",1,700,B->A,20 21,True,True,True," in log


def test_create_never_reuses_an_existing_id_and_stops_when_the_arcs_differ(tmp_path):
    w = World(new_id="1")                                      # OMNIA hands out an id that is taken: nothing may be built
    res = orv.create_reverse(_item(w), w.paths["1"], {"1"}, CREATE_URLS, "http://o/edit", w.post, w.get, tmp_path / "c.csv")
    assert "already has" in res["error"] and not res["built"] and not any(c[0] in ("build", "edit") for c in w.calls)
    w = World(built_route=(20,))                               # OMNIA built something else than the preview
    res = orv.create_reverse(_item(w), w.paths["1"], {"1"}, CREATE_URLS, "http://o/edit", w.post, w.get, tmp_path / "c.csv")
    assert res["built"] and not res["arcs_match"] and "differ" in res["error"] and not any(c[0] == "edit" for c in w.calls)


def test_a_forward_path_without_speeds_or_nodes_gets_a_reverse_without_them(tmp_path):
    w = World()
    w.paths["1"].update(start=None, end=None)
    res = orv.create_reverse(_item(w), w.paths["1"], {"1"}, CREATE_URLS, "http://o/edit", w.post, w.get, tmp_path / "c.csv")
    assert res["error"] is None and res["built"] and not res["speeds_nodes_set"] and "none to copy" in res["note"]
    assert not any(c[0] == "edit" for c in w.calls)
    w = World(edit_answer="False")
    res = orv.create_reverse(_item(w), w.paths["1"], {"1"}, CREATE_URLS, "http://o/edit", w.post, w.get, tmp_path / "c.csv")
    assert res["built"] and not res["speeds_nodes_set"] and "refused its speeds" in res["error"]


def _create_server(tmp_path, w, allow_edit=True):
    from omnia_fetch import save_snapshot
    save_snapshot(w.snapshot(), tmp_path / "s.json")
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv", page_builder=lambda: ("", {}), paths_provider=dict, views={},
                         db_file=tmp_path / "n.db", snapshot_file=tmp_path / "s.json", allow_edit=allow_edit, fetch_omnia=w.snapshot,
                         omnia_urls={"paths": "http://o/x/GetAllPaths", "arcs": URLS[0], "calc": URLS[1], "nearest": URLS[2],
                                     "newid": CREATE_URLS[0], "build": CREATE_URLS[1], "edit": "http://o/edit"},
                         omnia_post=w.post, omnia_get=w.get, wiring_inputs=lambda: ({"1": {"coords": list(ENDS)}}, {}), choose_path=None,
                         create_log=tmp_path / "c.csv")
    srv.create_delay = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    return srv, call


def test_the_control_room_creates_only_after_a_preview_and_yes_and_only_once(tmp_path):
    w = World()
    srv, call = _create_server(tmp_path, w)
    try:
        code, plan = call("/api/reverse/plan", {"ids": ["1"]})
        it = plan["items"][0]
        assert code == 200 and plan["allowed"] and plan["token"] and it["ok"] and it["new_name"] == "B->A" and it["copy"]["start"] == 5
        assert not any(c[0] in ("newid", "build", "edit") for c in w.calls)                  # the preview writes nothing
        assert call("/api/reverse/apply", {"token": plan["token"], "confirm": "yes please"})[0] == 400
        assert call("/api/reverse/apply", {"token": "made-up", "confirm": "YES"})[0] == 400
        code, out = call("/api/reverse/apply", {"token": plan["token"], "confirm": "YES"})
        assert code == 200 and out["error"] is None and out["results"][0]["new_id"] == 700 and out["results"][0]["speeds_nodes_set"]
        assert call("/api/reverse/apply", {"token": plan["token"], "confirm": "YES"})[0] == 400  # a preview works once
        code, again = call("/api/reverse/plan", {"ids": ["1"]})                                   # now the reverse exists: refused
        assert not again["items"][0]["ok"] and "may already be its reverse" in again["items"][0]["problems"][0] and again["token"] is None
    finally:
        srv.shutdown()


def test_creating_is_off_without_the_edit_switch(tmp_path):
    w = World()
    srv, call = _create_server(tmp_path, w, allow_edit=False)
    try:
        code, plan = call("/api/reverse/plan", {"ids": ["1"]})
        assert code == 200 and plan["allowed"] is False
        assert call("/api/reverse/apply", {"token": plan["token"], "confirm": "YES"})[0] == 403
        assert not any(c[0] in ("newid", "build", "edit") for c in w.calls)
    finally:
        srv.shutdown()
