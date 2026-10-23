"""
omnia_edit.py - change the attributes of many OMNIA paths at once (speed range, start/end node). WRITES to OMNIA.

Only used by the Control Room's Bulk edit panel, which adds the safeguards: off unless this PC opted in, a preview
(old value -> new value for every path) before anything is sent, a typed YES, and a log with the old values so the last
batch can be undone. Nothing here runs by itself.

What was learned by trying the update call on a test path (POST with query parameters; its address is
CONTROL_ROOM_OMNIA_EDIT_URL in the git-ignored .env, never in the code):
  * speedMin, speedMax, idLogicalNodeStart and idLogicalNodeEnd are ALL required: leaving one out gives HTTP 500
    and changes nothing - so a one-field change must resend the other three, and a path with no nodes cannot get
    a speed range until it also gets both nodes.
  * pathName is optional, but leaving it out makes OMNIA rebuild the name from the nodes. So the path's current
    name is always sent back, and a bulk change never renames anything.
  * the answer to a successful call is the text "True".
"""

import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BATCH = 50          # paths per batch
DELAY_S = 0.3           # pause between requests: OMNIA struggles with concurrent calls
SPEED_LIMIT = 250       # sanity limit for km/h
FIELDS = ("speed_min", "speed_max", "start", "end")
NAME_MAX = 100          # longest path name accepted here
RENAME_LOG_HEAD = ["batch", "time", "path_id", "old_name", "new_name", "ok", "status"]
LOG_HEAD = ["batch", "time", "path_id", "name", "ok", "status",
            "old_speed_min", "old_speed_max", "old_start", "old_end",
            "new_speed_min", "new_speed_max", "new_start", "new_end"]


def _int(value):
    """A positive whole number, or None for a blank value; ValueError for anything else."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        if isinstance(value, bool) or isinstance(value, float) and value != int(value):
            raise ValueError
        number = int(str(value).strip())
    except ValueError:
        raise ValueError("Speeds and node ids must be whole positive numbers (got %r)." % (value,)) from None
    if number <= 0:
        raise ValueError("Speeds and node ids must be whole positive numbers (got %r)." % (value,))
    return number


def current_values(path):
    return {"speed_min": path.get("speed_min"), "speed_max": path.get("speed_max"),
            "start": path.get("start") or None, "end": path.get("end") or None}


def plan_edits(snapshot, ids, speed_min=None, speed_max=None, swap=False, start=None, end=None):
    """What a bulk change would do. Returns {items, refused}.

    items:   [{id, name, before, after, start_name, end_name}] - only paths that really change
    refused: [{id, reason}]  - paths that cannot be changed, with the reason
    Blank speed/node values mean "leave as is". `swap` exchanges start and end node.
    """
    speed_min, speed_max, start, end = _int(speed_min), _int(speed_max), _int(start), _int(end)
    if speed_min is None and speed_max is None and not swap and start is None and end is None:
        raise ValueError("Nothing to change: set a speed, a node, or swap.")
    ids = [str(i).strip() for i in dict.fromkeys(ids) if str(i).strip()]
    if not ids:
        raise ValueError("No paths selected.")
    if len(ids) > MAX_BATCH:
        raise ValueError("At most %d paths per batch (you selected %d)." % (MAX_BATCH, len(ids)))
    paths = {str(p["id"]): p for p in snapshot["paths"]}
    nodes = {n["id"]: n["name"] for n in snapshot["nodes"]}
    items, refused = [], []
    for pid in ids:
        p = paths.get(pid)
        if p is None:
            refused.append({"id": pid, "reason": "not found in OMNIA"})
            continue
        before = current_values(p)
        after = dict(before)
        if swap:
            after["start"], after["end"] = before["end"], before["start"]
        for key, val in (("speed_min", speed_min), ("speed_max", speed_max), ("start", start), ("end", end)):
            if val is not None:
                after[key] = val
        missing = [k for k in FIELDS if after[k] is None]
        if missing:
            refused.append({"id": pid, "reason": "OMNIA needs all of speed min, speed max, start and end node; "
                                                 "still empty: " + ", ".join(missing)})
        elif not 0 < after["speed_min"] < after["speed_max"] <= SPEED_LIMIT:
            refused.append({"id": pid, "reason": "speed range %s-%s is not 0 < min < max <= %d"
                                                 % (after["speed_min"], after["speed_max"], SPEED_LIMIT)})
        elif after["start"] not in nodes or after["end"] not in nodes:
            refused.append({"id": pid, "reason": "unknown node id %s" % (after["start"] if after["start"] not in nodes else after["end"])})
        elif after["start"] == after["end"]:
            refused.append({"id": pid, "reason": "start and end node would be the same"})
        elif after == before:
            refused.append({"id": pid, "reason": "nothing to change"})
        else:
            items.append({"id": pid, "name": p.get("name") or "", "before": before, "after": after,
                          "start_name": nodes.get(after["start"], ""), "end_name": nodes.get(after["end"], "")})
    return {"items": items, "refused": refused}


def plan_deletes(snapshot, ids):
    """Which paths a bulk delete would remove. Returns {items, refused}; items carry what is needed to recognise them."""
    ids = [str(i).strip() for i in dict.fromkeys(ids) if str(i).strip()]
    if not ids:
        raise ValueError("No paths selected.")
    if len(ids) > MAX_BATCH:
        raise ValueError("At most %d paths per batch (you selected %d)." % (MAX_BATCH, len(ids)))
    paths = {str(p["id"]): p for p in snapshot["paths"]}
    nodes = {n["id"]: n["name"] for n in snapshot["nodes"]}
    items, refused = [], []
    for pid in ids:
        if not pid.isdigit():
            refused.append({"id": pid, "reason": "not a path id"})
        elif pid not in paths:
            refused.append({"id": pid, "reason": "not found in OMNIA (already deleted?)"})
        else:
            p = paths[pid]
            items.append({"id": pid, "name": p.get("name") or "", "before": current_values(p),
                          "start_name": nodes.get(p.get("start"), ""), "end_name": nodes.get(p.get("end"), "")})
    return {"items": items, "refused": refused}


def plan_renames(snapshot, ids, find="", replace="", prefix="", suffix="", new_name="", standard=None):
    """What a bulk rename would do. Returns {items, refused}; items = [{id, name, new_name, before}].

    Any combination of: a whole new name (one path only), find -> replace inside the current names, a prefix and a
    suffix. `standard` ({path id: name}) replaces the other options with the name built from the path's nodes.
    """
    ids = [str(i).strip() for i in dict.fromkeys(ids) if str(i).strip()]
    if not ids:
        raise ValueError("No paths selected.")
    if len(ids) > MAX_BATCH:
        raise ValueError("At most %d paths per batch (you selected %d)." % (MAX_BATCH, len(ids)))
    new_name = (new_name or "").strip()
    if standard is None and not (find or prefix or suffix or new_name):
        raise ValueError("Nothing to change: give a new name, a find/replace, a prefix or a suffix.")
    if new_name and len(ids) > 1 and standard is None:
        raise ValueError("A whole new name can only be given to one path; use find/replace, prefix or suffix for several.")
    paths = {str(p["id"]): p for p in snapshot["paths"]}
    items, refused = [], []
    for pid in ids:
        p = paths.get(pid)
        if p is None:
            refused.append({"id": pid, "reason": "not found in OMNIA"})
            continue
        old = p.get("name") or ""
        if standard is not None:
            new = standard.get(pid)
            if not new:
                refused.append({"id": pid, "reason": "its nodes do not lead to two known sensors, so no standard name can be built"})
                continue
        elif new_name:
            new = new_name
        else:
            new = old.replace(find, replace) if find else old
            new = (prefix or "") + new + (suffix or "")
        new = new.strip()
        if not new:
            refused.append({"id": pid, "reason": "the new name would be empty"})
        elif len(new) > NAME_MAX:
            refused.append({"id": pid, "reason": "the new name is longer than %d characters" % NAME_MAX})
        elif new == old:
            refused.append({"id": pid, "reason": "nothing to change"})
        else:
            items.append({"id": pid, "name": old, "new_name": new, "before": current_values(p)})
    return {"items": items, "refused": refused}


def apply_renames(items, rename_url, log_path, post=None, sleep=None, now=None):
    """Send the renames one at a time, stop at the first failure, log old and new names. Returns (batch, results, error).

    Only the id and the new name are sent. OMNIA's answer text is not relied on: the caller re-reads OMNIA afterwards
    and checks the names really changed.
    """
    import time
    if post is None:
        import httpx
        post = httpx.post
    sleep = sleep or time.sleep
    batch = uuid.uuid4().hex[:8]
    results, error = [], None
    for i, it in enumerate(items, 1):
        try:
            resp = post(rename_url, params={"pathId": it["id"], "pathName": it["new_name"]}, timeout=60)
            ok, status = resp.status_code == 200, resp.status_code
        except Exception as exc:
            ok, status = False, type(exc).__name__
        results.append({"id": it["id"], "ok": ok, "status": status})
        _log_rename(log_path, batch, it, ok, status, now)
        if not ok:
            error = "OMNIA refused path %s (%s); the remaining paths were not renamed." % (it["id"], status)
            break
        if i < len(items):
            sleep(DELAY_S)
    return batch, results, error


def _log_rename(path, batch, it, ok, status, now=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(RENAME_LOG_HEAD)
        w.writerow([batch, stamp, it["id"], it["name"], it["new_name"], ok, status])


def verify_renames(items, snapshot):
    """Which renames really took effect, judged from a fresh read of OMNIA: {path id: True/False}."""
    names = {str(p["id"]): (p.get("name") or "") for p in snapshot["paths"]}
    return {it["id"]: names.get(it["id"]) == it["new_name"] for it in items}


def undo_renames(log_path, snapshot):
    """Items that put the last rename batch back. Returns {items, refused, batch}."""
    try:
        with Path(log_path).open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        rows = []
    rows = [r for r in rows if r["ok"] == "True"]
    if not rows:
        raise ValueError("There is no rename batch to undo.")
    last = rows[-1]["batch"]
    paths = {str(p["id"]): p for p in snapshot["paths"]}
    items, refused = [], []
    for r in reversed([r for r in rows if r["batch"] == last]):
        p = paths.get(r["path_id"])
        if p is None:
            refused.append({"id": r["path_id"], "reason": "not found in OMNIA"})
        elif (p.get("name") or "") != r["new_name"]:
            refused.append({"id": r["path_id"], "reason": "renamed again since that batch; not touched"})
        else:
            items.append({"id": r["path_id"], "name": r["new_name"], "new_name": r["old_name"], "before": current_values(p)})
    return {"items": items, "refused": refused, "batch": last}


def stale_ids(items, snapshot):
    """Paths whose values in OMNIA are no longer what the preview showed (edited by hand in between)."""
    paths = {str(p["id"]): p for p in snapshot["paths"]}
    return [it["id"] for it in items
            if it["id"] not in paths or current_values(paths[it["id"]]) != it["before"]
            or (paths[it["id"]].get("name") or "") != it["name"]]


def request_params(item):
    """The query parameters for one path: every value explicit, the current name sent back unchanged."""
    a = item["after"]
    return {"pathId": item["id"], "pathName": item["name"], "speedMin": a["speed_min"], "speedMax": a["speed_max"],
            "idLogicalNodeStart": a["start"], "idLogicalNodeEnd": a["end"]}


def apply_items(items, edit_url, log_path, post=None, sleep=None, now=None):
    """Send the changes one at a time, stop at the first failure, log every attempt with the old values.

    Returns (batch id, results, error); results = [{id, ok, status}].
    """
    import time
    if post is None:
        import httpx
        post = httpx.post
    sleep = sleep or time.sleep
    batch = uuid.uuid4().hex[:8]
    results, error = [], None
    for i, it in enumerate(items, 1):
        try:
            resp = post(edit_url, params=request_params(it), timeout=60)
            ok = resp.status_code == 200 and str(resp.text).strip().lower() == "true"
            status = resp.status_code
        except Exception as exc:
            ok, status = False, type(exc).__name__
        results.append({"id": it["id"], "ok": ok, "status": status})
        _log(log_path, batch, it, ok, status, now)
        if not ok:
            error = "OMNIA refused path %s (%s); the remaining paths were not changed." % (it["id"], status)
            break
        if i < len(items):
            sleep(DELAY_S)
    return batch, results, error


def _log(path, batch, it, ok, status, now=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    b, a = it["before"], it["after"]
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(LOG_HEAD)
        w.writerow([batch, stamp, it["id"], it["name"], ok, status,
                    b["speed_min"], b["speed_max"], b["start"], b["end"],
                    a["speed_min"], a["speed_max"], a["start"], a["end"]])


def undo_items(log_path, snapshot):
    """Items that put the last batch back (only the paths that really changed). Returns {items, refused, batch}."""
    try:
        with Path(log_path).open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        rows = []
    rows = [r for r in rows if r["ok"] == "True"]
    if not rows:
        raise ValueError("There is no batch to undo.")
    last = rows[-1]["batch"]
    paths = {str(p["id"]): p for p in snapshot["paths"]}
    items, refused = [], []
    for r in reversed([r for r in rows if r["batch"] == last]):
        def num(key):
            return int(r[key]) if r[key] not in ("", "None") else None
        old = {"speed_min": num("old_speed_min"), "speed_max": num("old_speed_max"), "start": num("old_start"), "end": num("old_end")}
        new = {"speed_min": num("new_speed_min"), "speed_max": num("new_speed_max"), "start": num("new_start"), "end": num("new_end")}
        p = paths.get(r["path_id"])
        if p is None:
            refused.append({"id": r["path_id"], "reason": "not found in OMNIA"})
        elif None in old.values():
            refused.append({"id": r["path_id"], "reason": "it had no complete values before, and OMNIA cannot store partial ones"})
        elif current_values(p) != new:
            refused.append({"id": r["path_id"], "reason": "changed in OMNIA since that batch; not touched"})
        else:
            nodes = {n["id"]: n["name"] for n in snapshot["nodes"]}
            items.append({"id": r["path_id"], "name": p.get("name") or "", "before": new, "after": old,
                          "start_name": nodes.get(old["start"], ""), "end_name": nodes.get(old["end"], "")})
    return {"items": items, "refused": refused, "batch": last}
