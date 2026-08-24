"""
checks.py – Domain-specific XML assertion logic.

Each public function takes the raw response text and returns:
    {"passed": bool, "detail": str}
"""

import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from stability import CYPRUS_TZ


def _parse_xml(text):
    try:
        return ET.fromstring(text.strip()), None
    except ET.ParseError as e:
        return None, str(e)


def _find_by_local_name(root, local_name):
    """Find all elements whose local name matches, namespace-agnostic."""
    lower = local_name.lower()
    return [el for el in root.iter() if el.tag.split("}")[-1].lower() == lower]


# ─── Generic ─────────────────────────────────────────────────────────────────

def valid_xml(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Invalid XML: {err}"}
    return {"passed": True, "detail": "Response is valid XML"}


# ─── VMS ─────────────────────────────────────────────────────────────────────

DEFAULT_VMS_STALE_HOURS = 1


def vms_controller_status(response_text: str, stale_hours: int = DEFAULT_VMS_STALE_HOURS) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    controllers = root.findall(".//{*}vmsControllerStatus")
    now = datetime.now(timezone.utc)
    working, not_working, no_status, stale = [], [], [], []
    fresh_ok, status_checked = 0, 0
    sensors_map = {}
    measurements_map = {}

    for ctrl in controllers:
        cid_el = ctrl.find("{*}vmsControllerReference")
        cid = cid_el.get("id", "unknown") if cid_el is not None else "unknown"

        ws_el = ctrl.find(".//{*}workingStatus")
        ts_el = ctrl.find(".//{*}statusUpdateTime")

        is_stale = False
        age_hours = None
        if ws_el is not None and ts_el is not None and ts_el.text:
            status_checked += 1
            try:
                ts_dt = datetime.fromisoformat(ts_el.text.strip())
                if ts_dt.tzinfo is None:
                    # statusUpdateTime has no offset in the raw feed, unlike the
                    # sibling publicationTime — it's naive Cyprus local time, not UTC.
                    ts_dt = ts_dt.replace(tzinfo=CYPRUS_TZ)
                age_hours = (now - ts_dt).total_seconds() / 3600
                if age_hours > stale_hours:
                    is_stale = True
                else:
                    fresh_ok += 1
            except (ValueError, TypeError):
                pass

        if ws_el is None:
            no_status.append(cid)
            sensors_map[cid] = "no_status"
        elif is_stale:
            stale.append(cid)
            sensors_map[cid] = "stale"
        elif ws_el.text == "working":
            working.append(cid)
            sensors_map[cid] = "working"
        else:
            not_working.append(cid)
            sensors_map[cid] = "not_working"

        lines = [el.text.strip() for el in ctrl.findall(".//{*}textLine/{*}textLine")
                 if el.text and el.text.strip()]
        measurements_map[cid] = {
            "message":   " | ".join(lines) if lines else None,
            "age_hours": round(age_hours, 1) if age_hours is not None else None,
        }

    detail_lines = [
        f"Working: {len(working)}",
        f"Not working: {len(not_working)}" + (f" — IDs: {', '.join(not_working)}" if not_working else ""),
        f"No status: {len(no_status)}" + (f" — {', '.join(no_status)}" if no_status else ""),
    ]
    if status_checked:
        detail_lines.append(f"Fresh: {fresh_ok}/{status_checked} (limit: {stale_hours}h)")
    if stale:
        detail_lines.append(f"Stale: {len(stale)} — IDs: {', '.join(stale)}")
    return {
        "passed": True,  # feed delivered data; health shown separately in dashboard
        "detail": " | ".join(detail_lines),
        "sensors": sensors_map,
        "measurements": measurements_map,
    }


# ─── Bluetooth ───────────────────────────────────────────────────────────────

def predefined_paths_count(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    paths = _find_by_local_name(root, "predefinedLocationReference")
    if not paths:
        child_tags = list({el.tag.split("}")[-1] for el in root.iter()})[:20]
        return {
            "passed": False,
            "detail": f"No predefinedLocationReference elements found. Tags in response: {', '.join(child_tags)}"
        }

    unique_ids = {el.get("id") for el in paths if el.get("id")}
    count = len(unique_ids) if unique_ids else len(paths)
    return {
        "passed": count > 0,
        "detail": f"Total predefined paths: {count}"
    }


DEFAULT_STALE_HOURS = 1


def bt_paths_speed_and_traveltime(response_text: str, stale_hours: int = DEFAULT_STALE_HOURS) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    paths = root.findall(".//{*}predefinedLocationReference")
    total = len(paths)
    if total == 0:
        return {"passed": False, "detail": "No predefinedLocationReference elements found"}

    now = datetime.now(timezone.utc)
    speed_ok, ttime_ok, fresh_ok, stale_checked, failing, stale = 0, 0, 0, 0, [], []
    sensors_map = {}
    measurements_map = {}
    ts_seen = []

    for path in paths:
        pid = path.get("id", "unknown")
        ext = path.find(".//{*}_predefinedLocationExtension")
        speed_el = ext.find("obs_speed") if ext is not None else None
        ttime_el = ext.find("obs_t_time") if ext is not None else None
        ts_el    = ext.find("measurement_timestamp") if ext is not None else None
        spd   = _safe_float(speed_el)
        ttime = _safe_float(ttime_el)

        is_stale = False
        age_hours = None
        if ts_el is not None and ts_el.text:
            stale_checked += 1
            try:
                ts_dt = datetime.fromisoformat(ts_el.text.strip().replace("Z", "+00:00"))
                age_hours = (now - ts_dt).total_seconds() / 3600
                if age_hours > stale_hours:
                    is_stale = True
                else:
                    fresh_ok += 1
                ts_seen.append(ts_dt)
            except (ValueError, TypeError):
                pass

        has_data = bool(spd and spd > 0) and bool(ttime and ttime > 0)
        if spd and spd > 0:
            speed_ok += 1
        if ttime and ttime > 0:
            ttime_ok += 1
        if not has_data:
            failing.append(pid)
        if is_stale:
            stale.append(pid)

        sensors_map[pid] = "failing" if not has_data else ("stale" if is_stale else "ok")
        measurements_map[pid] = {
            "speed_kmh":    round(spd,   1) if spd   is not None else None,
            "travel_time_s": round(ttime, 0) if ttime is not None else None,
            "age_hours":    round(age_hours, 1) if age_hours is not None else None,
        }

    detail = (
        f"Speed OK: {speed_ok}/{total} | Travel time OK: {ttime_ok}/{total}"
        + (f" | Fresh: {fresh_ok}/{stale_checked} (limit: {stale_hours}h)" if stale_checked else "")
        + (f" | Failing paths: {', '.join(failing)}" if failing else "")
        + (f" | Stale paths: {', '.join(stale)}" if stale else "")
    )
    # Mode, not max/min: the timestamp shared by the most paths represents the
    # feed's bulk state, resistant to a handful of outlier paths in either
    # direction (one stray fresh path masking a broad freeze, or one
    # permanently-broken path keeping an "ongoing" duration stuck forever).
    common_ts = Counter(ts_seen).most_common(1)[0][0] if ts_seen else None

    return {
        "passed": True,
        "detail": detail,
        "sensors": sensors_map,
        "measurements": measurements_map,
        "common_measurement_timestamp": common_ts.isoformat() if common_ts else None,
    }


# ─── Traffic Detection ────────────────────────────────────────────────────────

def sensor_speed_status(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    sensors = root.findall(".//{*}siteMeasurements")
    working, malfunctioning, no_traffic, no_measurement = [], [], [], []
    sensors_map = {}
    measurements_map = {}
    total_flow = 0
    flow_count = 0

    for sensor in sensors:
        ref = sensor.find("{*}measurementSiteReference")
        sid = ref.get("id", "unknown") if ref is not None else "unknown"

        speed_val = None
        flow_val  = None

        for basic in sensor.findall(".//{*}basicData"):
            xsi_type = basic.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
            if "TrafficSpeed" in xsi_type and speed_val is None:
                speed_val = _safe_float(basic.find(".//{*}speed"))
            if "TrafficFlow" in xsi_type and flow_val is None:
                flow_val = _safe_float(basic.find(".//{*}vehicleFlowRate"))

        if flow_val is not None and flow_val >= 0:
            total_flow += flow_val
            flow_count += 1

        if speed_val is None:
            no_measurement.append(sid)
            sensor_status = "no_measurement"
        elif speed_val == -1:
            malfunctioning.append(sid)
            sensor_status = "malfunctioning"
        elif speed_val == 0:
            no_traffic.append(sid)
            sensor_status = "no_traffic"
        else:
            working.append(sid)
            sensor_status = "working"

        sensors_map[sid] = sensor_status
        measurements_map[sid] = {
            "speed_kmh":    round(speed_val, 1) if speed_val is not None else None,
            "flow_veh_hr":  round(flow_val,  0) if flow_val  is not None else None,
        }

    total    = len(sensors)
    avg_flow = round(total_flow / flow_count, 1) if flow_count > 0 else 0
    detail_parts = [
        f"Working: {len(working)}/{total}",
        f"No traffic (speed=0): {len(no_traffic)}",
        f"Malfunctioning (speed=-1): {len(malfunctioning)}" + (f" — {', '.join(malfunctioning[:5])}" if malfunctioning else ""),
        f"No measurement: {len(no_measurement)}",
        f"Avg flow rate: {avg_flow} veh/hr ({flow_count} sensors reporting)",
    ]
    return {"passed": True, "detail": " | ".join(detail_parts), "sensors": sensors_map, "measurements": measurements_map}


def bt_site_count(response_text: str) -> dict:
    """Count measurementSite elements in a BTMeasurementSiteTablePublication."""
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    count = len(root.findall(".//{*}measurementSite"))
    if count > 0:
        return {"passed": True, "detail": f"{count} Bluetooth device(s) reported by API", "count": count}
    return {"passed": False, "detail": "No measurementSite elements found", "count": 0}


# ─── Data freshness ──────────────────────────────────────────────────────────

DEFAULT_FRESHNESS_MINUTES = 5

def feed_freshness(response_text: str, freshness_minutes: int = DEFAULT_FRESHNESS_MINUTES) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    pub_el = root.find(".//{*}publicationTime")
    if pub_el is None:
        return {"passed": False, "detail": "No publicationTime element found in feed"}

    try:
        pub_dt  = datetime.fromisoformat(pub_el.text.strip().replace("Z", "+00:00"))
        age_min = int((datetime.now(timezone.utc) - pub_dt).total_seconds() / 60)
        passed  = age_min <= freshness_minutes
        return {"passed": passed, "detail": f"Feed is {age_min} min old (limit: {freshness_minutes} min)"}
    except Exception as e:
        return {"passed": False, "detail": f"Could not parse publicationTime: {e}"}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _safe_float(el):
    if el is None:
        return None
    try:
        return float(el.text or el.get("_") or "")
    except (ValueError, TypeError):
        return None


# Registry: maps check name (from YAML) → function
REGISTRY = {
    "valid_xml":                    valid_xml,
    "feed_freshness":               feed_freshness,
    "vms_controller_status":        vms_controller_status,
    "predefined_paths_count":       predefined_paths_count,
    "bt_paths_speed_and_traveltime": bt_paths_speed_and_traveltime,
    "bt_site_count":                bt_site_count,
    "sensor_speed_status":          sensor_speed_status,
}
