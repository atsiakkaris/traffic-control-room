"""The top strip: who is down now, what is new, what recovered, and feed-wide outages."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import attention
import live_ui


def hist(*flags, days_ago_first=10):
    """History oldest first: 1 = good, 0 = bad, one run per 6 hours ending now."""
    now = datetime.now(timezone.utc)
    n = len(flags)
    return [{"run_at": (now - timedelta(hours=6 * (n - 1 - i))).isoformat(), "status": "working" if f else "failing"}
            for i, f in enumerate(flags)]


def S(group, sid, *flags):
    return {"group_name": group, "sensor_id": sid, "history": hist(*flags)}


def build(sensors, projects=None, bt_now=None, bt_prev=None):
    live = {(s["group_name"], s["sensor_id"]) for s in sensors}
    names = {k: f"{k[0]} {k[1]}" for k in live}
    return attention.build_attention(sensors, live, projects or {}, names, {}, bt_now, bt_prev)


def test_classify_states():
    assert attention.classify(hist(1, 1, 1))["state"] == "up"
    assert attention.classify(hist(1, 1, 0))["new"] is True
    assert attention.classify(hist(1, 0, 0))["new"] is False       # down for a while, not new
    assert attention.classify(hist(0, 0, 1))["recovered"] is True
    assert attention.classify(hist(0, 0, 0))["state"] == "never"
    assert attention.classify(hist(1, 0, 0))["state"] == "down"
    assert attention.classify(hist(0))["new"] is False             # first ever run, down: not "new"


def test_a_nightly_flapper_is_not_new_and_not_recovered():
    nightly = [1, 1, 1, 0] * 5            # fails at the same run every day, fine otherwise
    assert attention.classify(hist(*nightly))["state"] == "down"
    assert attention.classify(hist(*nightly))["new"] is False and attention.classify(hist(*nightly))["flapping"]
    assert attention.classify(hist(*nightly, 1))["recovered"] is False
    assert attention.classify(hist(*([1] * 12), 0))["new"] is True          # a fresh fault still is
    assert attention.classify(hist(*([1] * 10), 0, 1, 1, 1, 0))["new"] is True   # one earlier blip is not a pattern


def test_sensors_and_paths_are_counted_separately():
    r = build([S("VMS", "1", 1, 1), S("VMS", "2", 1, 0), S("Bluetooth Paths", "7", 1, 1), S("Bluetooth Paths", "8", 0, 0)])
    assert [u["id"] for u in r["sensors"]["down"]] == ["2"] and [u["id"] for u in r["sensors"]["new"]] == ["2"]
    assert [u["id"] for u in r["paths"]["down"]] == ["8"] and [u["id"] for u in r["paths"]["never"]] == ["8"]
    assert r["sensors"]["total"] == 2 and r["paths"]["total"] == 2


def test_awaiting_power_and_retired_sensors_are_left_out():
    sensors = [S("VMS", "1", 1, 0), S("VMS", "2", 0, 0), S("VMS", "3", 0, 0)]
    live = {("VMS", "1"), ("VMS", "2")}                              # 3 is retired (not active any more)
    projects = {"VMS": {"2": {"project": "X", "commissioning": "not_electrified"}}}
    r = attention.build_attention(sensors, live, projects, {})
    assert [u["id"] for u in r["sensors"]["down"]] == ["1"]
    assert r["sensors"]["total"] == 1


def test_a_feed_wide_outage_is_one_line_and_not_counted():
    sensors = [S("Traffic Detection", str(i), 0, 0, 0) for i in range(20)] + [S("VMS", "1", 1, 0)]
    r = build(sensors)
    assert [f["group"] for f in r["feeds"]] == ["Traffic Detection"]
    assert r["feeds"][0]["down"] == 20 and r["feeds"][0]["total"] == 20
    assert all(u["group"] != "Traffic Detection" for u in r["sensors"]["down"])
    assert [u["id"] for u in r["sensors"]["down"]] == ["1"]          # the VMS fault still shows


def test_a_few_failures_in_a_big_group_are_not_a_feed_outage():
    r = build([S("Traffic Detection", str(i), 1, 1, 1) for i in range(18)]
              + [S("Traffic Detection", "x", 1, 0), S("Traffic Detection", "y", 0, 0)])
    assert r["feeds"] == [] and len(r["sensors"]["down"]) == 2


def test_bluetooth_sensors_come_from_triangulation_with_new_and_recovered():
    def v(verdict, paths=()):
        return {"verdict": verdict, "reason": "r", "paths": [{"id": p, "name": p, "state": "bad"} for p in paths],
                "good": 0, "bad": len(paths), "mixed": 0, "name": "n"}
    now = {"sensors": {"A": v("faulty_high", ["1"]), "B": v("working"), "C": v("faulty_likely", ["2"]), "D": v("no_evidence")}}
    prev = {"sensors": {"A": v("working"), "B": v("faulty_high"), "C": v("faulty_likely")}}
    r = build([S("Bluetooth Paths", "1", 0, 0), S("Bluetooth Paths", "2", 1, 0)], bt_now=now, bt_prev=prev)
    down = {u["id"]: u for u in r["sensors"]["down"] if u["group"] == "Bluetooth"}
    assert set(down) == {"A", "C"}
    assert down["A"]["state"] == "never" and down["C"]["state"] == "down"   # C's path worked once
    assert [u["id"] for u in r["sensors"]["new"]] == ["A"]
    assert [u["id"] for u in r["sensors"]["recovered"]] == ["B"]
    assert r["sensors"]["total"] == 3                                      # no_evidence is not counted


def test_live_context_carries_history_for_the_live_page():
    sensors = [S("Bluetooth Paths", "1", 1, 1, 1), S("Bluetooth Paths", "2", 0, 0, 0), S("VMS", "5", 1, 0)]
    r = build(sensors)
    ctx = attention.live_context(r, "16/11/26 06:02")
    assert ctx["built"] == "16/11/26 06:02"
    assert ctx["paths"]["1"]["st"] == "up" and ctx["paths"]["2"]["nv"] is True
    assert ctx["paths"]["1"]["r"] == "111" and ctx["paths"]["1"]["fl"] is False
    assert [v["id"] for v in ctx["vms"]] == ["5"] and ctx["vms"][0]["new"] is True


def test_live_context_lists_every_bluetooth_sensor_and_places_vms():
    r = build([S("Bluetooth Paths", "1", 1, 1, 1), S("VMS", "5", 1, 1, 1)])
    bt_now = {"sensors": {"A": {"verdict": "working", "reason": "ok", "name": "Alpha", "paths": [{"id": 1, "name": "x"}], "lat": 35.1, "lon": 33.2},
                          "B": {"verdict": "no_evidence", "reason": "", "name": "Beta", "paths": [], "lat": 35.2, "lon": 33.3}}}
    ctx = attention.live_context(r, "x", bt_now, {"VMS": {"5": {"lat": 35.0, "lon": 33.4}}})
    assert ctx["bt"]["A"]["p"] == ["1"] and ctx["bt"]["B"]["v"] == "no_evidence" and ctx["bt"]["B"]["lat"] == 35.2
    assert (ctx["vms"][0]["lat"], ctx["vms"][0]["lon"]) == (35.0, 33.4)


def test_live_page_data_cannot_break_out_of_the_script_tag():
    r = build([S("Bluetooth Paths", "1", 1, 0), S("VMS", "5", 1, 0)])
    ctx = attention.live_context(r, "16/11/26 06:02")
    ctx["paths"]["1"]["n"] = "</script><img src=x onerror=alert(1)>"
    page = live_ui.render(ctx)
    assert "</script><img" not in page
    assert page.count("</script>") == page.count("<script")                # no script tag opened or closed by the data
    assert live_ui.LIVE_FEED_URL in page and "innerHTML" not in page       # data only ever set as text
