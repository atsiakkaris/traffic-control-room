"""
attention.py - what needs attention RIGHT NOW, for the dashboard's top strip.

Pure logic (no database, no HTML) so it can be tested and shared. Two separate
sections, because a Bluetooth path is not a piece of equipment:

  sensors  Traffic Detection loops, VMS signs and Bluetooth sensors. A Bluetooth
           sensor has no status of its own: it counts as down when triangulation
           (bt_sensor_suspects) judges it faulty from how its paths behave.
  paths    Bluetooth paths, each reported on its own.

Each section gives four lists: down now, new since the last run, recovered since
the last run, and never worked.

A whole feed that is down at once (nearly every unit failing) is one outage, not
hundreds of faults. It is reported as a single line and left out of the counts.
"""
from datetime import datetime, timezone
from statistics import median

from stability import (EXCLUDED_COMMISSIONING, GOOD_STATUSES, STATUS_LABEL, fault_streak_start,
                       tier_for_counts, to_cyprus)

# A group counts as one feed outage when at least this share of its expected-to-work
# units is down at once, and it has at least this many units (so a tiny group with two
# failures is not called a feed outage).
# A unit that has dropped from good to bad this many times in its recent runs is a flapper
# (e.g. a path that fails every night at low traffic). Its latest drop or return is not
# news, so it is never listed as "new" or "recovered" (it still counts as down now).
FLAP_WINDOW_RUNS = 20
FLAP_MIN_EARLIER_DROPS = 2

FEED_OUTAGE_SHARE = 0.8
FEED_OUTAGE_MIN_UNITS = 10

SENSOR_GROUPS = ("Traffic Detection", "VMS")
PATH_GROUP = "Bluetooth Paths"
BT_SENSOR_GROUP = "Bluetooth"
GROUP_LABEL = {"Traffic Detection": "Traffic Detection", "VMS": "VMS",
               "Bluetooth Paths": "Bluetooth path", "Bluetooth": "Bluetooth sensor"}
FEED_LABEL = {"Traffic Detection": "Traffic Detection feed", "VMS": "VMS feed",
              "Bluetooth Paths": "Bluetooth paths feed"}
BT_DOWN_VERDICTS = ("faulty_high", "faulty_likely")
RUNS_SHOWN = 20


def _days_since(iso):
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (datetime.now(timezone.utc) - t).days


def _earlier_drops(good):
    """How many good->bad drops happened in the window before the latest run."""
    window = good[-(FLAP_WINDOW_RUNS + 1):-1]
    return sum(1 for a, b in zip(window, window[1:]) if a and not b)


def classify(history):
    """{state, new, recovered, flapping} from one unit's run history, oldest first.

    state: 'up' | 'down' | 'never' (down and never once good).
    new: down now, the run before was good, and it is not a flapper.
    recovered: up now, the run before bad, and it is not a flapper.
    """
    if not history:
        return {"state": "nodata", "new": False, "recovered": False, "flapping": False}
    good = [h["status"] in GOOD_STATUSES for h in history]
    flapping = _earlier_drops(good) >= FLAP_MIN_EARLIER_DROPS
    if good[-1]:
        return {"state": "up", "new": False, "flapping": flapping,
                "recovered": len(good) > 1 and not good[-2] and not flapping}
    return {"state": "down" if any(good) else "never", "recovered": False, "flapping": flapping,
            "new": len(good) > 1 and good[-2] and not flapping}


def _runs_string(history):
    return "".join("1" if h["status"] in GOOD_STATUSES else "0" for h in history[-RUNS_SHOWN:])


def _unit(group, sid, history, name, project, acct):
    start = fault_streak_start(history)
    last = history[-1]["status"] if history else None
    good = sum(1 for h in history if h["status"] in GOOD_STATUSES)
    tier = tier_for_counts(good, len(history))
    c = classify(history)
    return {
        "key": f"{group}|{sid}", "id": sid, "group": group, "group_label": GROUP_LABEL.get(group, group),
        "name": name, "state": c["state"], "new": c["new"], "recovered": c["recovered"],
        "flapping": c["flapping"], "reason": STATUS_LABEL.get(last, last or ""),
        "since": to_cyprus(start["run_at"]) if start else None,
        "days": _days_since(start["run_at"]) if start else 0,
        "project": project, "acct": acct,
        "tier": tier.label if tier else None, "tier_key": tier.key if tier else None,
        "good": good, "total": len(history), "runs": _runs_string(history),
        "last_run": to_cyprus(history[-1]["run_at"]) if history else None,
    }


def _bt_units(bt_now, bt_prev, path_history, groups_in_outage):
    """Bluetooth sensors as units, from the triangulation verdicts of now and of the run before."""
    units = []
    if not bt_now or PATH_GROUP in groups_in_outage:
        return units
    prev = (bt_prev or {}).get("sensors", {})
    for sid, v in bt_now["sensors"].items():
        if v["verdict"] == "no_evidence":
            continue
        down = v["verdict"] in BT_DOWN_VERDICTS
        was_down = (prev.get(sid, {}).get("verdict") in BT_DOWN_VERDICTS) if prev else None
        ever_good = any(h["status"] in GOOD_STATUSES
                        for p in v["paths"] for h in path_history.get(str(p["id"]), []))
        units.append({
            "key": f"{BT_SENSOR_GROUP}|{sid}", "id": sid, "group": BT_SENSOR_GROUP,
            "group_label": GROUP_LABEL[BT_SENSOR_GROUP], "name": v.get("name") or sid,
            "state": ("never" if not ever_good else "down") if down else "up",
            "new": bool(down and was_down is False), "recovered": bool(not down and was_down is True),
            "reason": v["reason"], "since": None, "days": None, "project": None, "acct": None,
            "tier": None, "tier_key": None, "good": v["good"], "total": v["good"] + v["bad"] + v["mixed"],
            "runs": "", "last_run": None, "verdict": v["verdict"],
            "paths": [{"id": p["id"], "name": p["name"], "state": p["state"]} for p in v["paths"]],
        })
    return units


def _section(units):
    return {
        "total": len(units),
        "down": sorted((u for u in units if u["state"] in ("down", "never")),
                       key=lambda u: (-(u["days"] or 0), u["name"])),
        "new": [u for u in units if u["new"]],
        "recovered": [u for u in units if u["recovered"]],
        "never": [u for u in units if u["state"] == "never"],
    }


def build_attention(sensors, live_keys, sensor_projects, names, project_acct=None,
                    bt_now=None, bt_prev=None):
    """sensors: db.fetch_sensor_stability(); live_keys: {(group, id)} of active sensors/paths;
    names: {(group, id): display name}. Returns {feeds, sensors, paths, units}."""
    project_acct = project_acct or {}
    path_history = {s["sensor_id"]: s["history"] for s in sensors if s["group_name"] == PATH_GROUP}

    by_group = {}
    for s in sensors:
        g, sid = s["group_name"], s["sensor_id"]
        if g not in SENSOR_GROUPS + (PATH_GROUP,) or (g, sid) not in live_keys or not s["history"]:
            continue
        info = sensor_projects.get(g, {}).get(sid) or {}
        if info.get("commissioning") in EXCLUDED_COMMISSIONING:
            continue
        project = info.get("project")
        by_group.setdefault(g, []).append(
            _unit(g, sid, s["history"], names.get((g, sid), sid), project, project_acct.get(project)))

    feeds, kept = [], []
    for g, units in by_group.items():
        down = [u for u in units if u["state"] in ("down", "never")]
        if len(units) >= FEED_OUTAGE_MIN_UNITS and len(down) / len(units) >= FEED_OUTAGE_SHARE:
            days = [u["days"] for u in down if u["days"] is not None]
            feeds.append({"group": g, "label": FEED_LABEL.get(g, g), "down": len(down), "total": len(units),
                          "days": int(median(days)) if days else 0})
        else:
            kept.extend(units)
    outage_groups = {f["group"] for f in feeds}

    sensor_units = [u for u in kept if u["group"] in SENSOR_GROUPS]
    sensor_units += _bt_units(bt_now, bt_prev, path_history, outage_groups)
    path_units = [u for u in kept if u["group"] == PATH_GROUP]
    return {"feeds": sorted(feeds, key=lambda f: f["group"]),
            "sensors": _section(sensor_units), "paths": _section(path_units),
            "units": {u["key"]: u for u in sensor_units + path_units}}


def live_context(attn, built_label, bt_now=None, all_coords=None):
    """What the Live page needs from the last stored run, as plain JSON-able data.

    The live page checks each Bluetooth path against the live feed itself; this is the
    history it joins that with: whether a path ever worked, whether it goes quiet
    regularly, how its last runs went, and which paths end at which Bluetooth sensor
    (every Bluetooth sensor is listed, with its position, so the map can draw them all).
    VMS: its stored state, for when the live feed does not carry it.
    """
    all_coords = all_coords or {}
    paths, bt, vms = {}, {}, []
    for u in attn["units"].values():
        if u["group"] == PATH_GROUP:
            paths[str(u["id"])] = {"n": u["name"], "r": u["runs"], "nv": u["state"] == "never",
                                   "fl": bool(u["flapping"]), "st": u["state"], "sn": u["since"], "d": u["days"],
                                   "t": u["tier"], "g": u["good"], "tot": u["total"]}
        elif u["group"] == "VMS":
            c = all_coords.get("VMS", {}).get(u["id"]) or {}
            vms.append({"id": u["id"], "n": u["name"], "st": u["state"], "new": u["new"], "rec": u["recovered"],
                        "sn": u["since"], "d": u["days"], "why": u["reason"], "proj": u["project"],
                        "acct": u["acct"], "t": u["tier"], "g": u["good"], "tot": u["total"], "r": u["runs"],
                        "lat": c.get("lat"), "lon": c.get("lon")})
    for sid, v in ((bt_now or {}).get("sensors") or {}).items():
        bt[str(sid)] = {"n": v.get("name") or str(sid), "v": v["verdict"], "why": v["reason"],
                        "p": [str(q["id"]) for q in v["paths"]], "lat": v.get("lat"), "lon": v.get("lon")}
    return {"built": built_label, "feeds": [f for f in attn["feeds"] if f["group"] != PATH_GROUP],
            "paths": paths, "bt": bt, "vms": vms}
