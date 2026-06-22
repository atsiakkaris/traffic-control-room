"""
checks.py – Domain-specific XML assertion logic.

Each public function takes the raw response text and returns:
    {"passed": bool, "detail": str}
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone


NS = "q1"  # the namespace prefix used in SWARCO DATEX II feeds


def _parse_xml(text):
    try:
        return ET.fromstring(text.strip()), None
    except ET.ParseError as e:
        return None, str(e)


# ─── Generic ─────────────────────────────────────────────────────────────────

def valid_xml(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Invalid XML: {err}"}
    return {"passed": True, "detail": "Response is valid XML"}


# ─── VMS ─────────────────────────────────────────────────────────────────────

def vms_controller_status(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    controllers = root.findall(".//{*}vmsControllerStatus")
    working, not_working, no_status = [], [], []

    sensors_map = {}
    measurements_map = {}
    for ctrl in controllers:
        cid_el = ctrl.find("{*}vmsControllerReference")
        cid = cid_el.get("id", "unknown") if cid_el is not None else "unknown"

        ws_el = ctrl.find(".//{*}workingStatus")
        if ws_el is None:
            no_status.append(cid)
            sensors_map[cid] = "no_status"
        elif ws_el.text == "working":
            working.append(cid)
            sensors_map[cid] = "working"
        else:
            not_working.append(cid)
            sensors_map[cid] = "not_working"

        # Extract text lines from the active VMS message
        lines = [el.text.strip() for el in ctrl.findall(".//{*}textLine/{*}textLine")
                 if el.text and el.text.strip()]
        measurements_map[cid] = {"message": " | ".join(lines) if lines else None}

    detail_lines = [
        f"Working: {len(working)}",
        f"Not working: {len(not_working)}" + (f" — IDs: {', '.join(not_working)}" if not_working else ""),
        f"No status: {len(no_status)}" + (f" — {', '.join(no_status)}" if no_status else ""),
    ]

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

    paths = root.findall(".//{*}predefinedLocation")
    if not paths:
        paths = [el for el in root.iter() if el.tag.split("}")[-1].lower() == "predefinedlocation"]
    if not paths:
        paths = [el for el in root.iter() if "predefinedlocation" in el.tag.split("}")[-1].lower()]

    if not paths:
        child_tags = list({el.tag.split("}")[-1] for el in root.iter()})[:20]
        return {
            "passed": False,
            "detail": f"No predefinedLocation elements found. Tags in response: {', '.join(child_tags)}"
        }

    # Count unique IDs — each path appears twice in the XML (definition + reference)
    unique_ids = {el.get("id") for el in paths if el.get("id")}
    count = len(unique_ids) if unique_ids else len(paths) // 2
    return {
        "passed": count > 0,
        "detail": f"Total predefined paths: {count}"
    }


def bt_paths_speed_and_traveltime(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    paths = root.findall(".//{*}predefinedLocationReference")
    total = len(paths)

    if total == 0:
        return {"passed": False, "detail": "No predefinedLocationReference elements found"}

    speed_ok, ttime_ok, failing = 0, 0, []

    for path in paths:
        pid = path.get("id", "unknown")
        ext = path.find(".//{*}_predefinedLocationExtension")

        speed_el = ext.find("obs_speed") if ext is not None else None
        ttime_el = ext.find("obs_t_time") if ext is not None else None

        speed = _safe_float(speed_el)
        ttime = _safe_float(ttime_el)

        if speed and speed > 0:
            speed_ok += 1
        if ttime and ttime > 0:
            ttime_ok += 1

        missing = []
        if not (speed and speed > 0):
            missing.append("no speed")
        if not (ttime and ttime > 0):
            missing.append("no travel time")
        if missing:
            failing.append(pid)

    sensors_map = {}
    measurements_map = {}
    for path in paths:
        pid = path.get("id", "unknown")
        ext = path.find(".//{*}_predefinedLocationExtension")
        speed_el = ext.find("obs_speed") if ext is not None else None
        ttime_el = ext.find("obs_t_time") if ext is not None else None
        spd = _safe_float(speed_el)
        ttime = _safe_float(ttime_el)
        sensors_map[pid] = "failing" if pid in failing else "ok"
        measurements_map[pid] = {
            "speed_kmh": round(spd, 1) if spd is not None else None,
            "travel_time_s": round(ttime, 0) if ttime is not None else None,
        }

    detail = (
        f"Speed OK: {speed_ok}/{total} | Travel time OK: {ttime_ok}/{total}"
        + (f" | Failing paths: {', '.join(failing)}" if failing else "")
    )
    return {"passed": True, "detail": detail, "sensors": sensors_map, "measurements": measurements_map}


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
        flow_val = None

        for basic in sensor.findall(".//{*}basicData"):
            xsi_type = basic.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")

            if "TrafficSpeed" in xsi_type and speed_val is None:
                speed_el = basic.find(".//{*}speed")
                speed_val = _safe_float(speed_el)

            if "TrafficFlow" in xsi_type and flow_val is None:
                flow_el = basic.find(".//{*}vehicleFlowRate")
                flow_val = _safe_float(flow_el)

        # Accumulate total flow across all sensors (ignore negative/malfunction values)
        if flow_val is not None and flow_val >= 0:
            total_flow += flow_val
            flow_count += 1

        # Categorise by speed
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
            "speed_kmh": round(speed_val, 1) if speed_val is not None else None,
            "flow_veh_hr": round(flow_val, 0) if flow_val is not None else None,
        }

    total = len(sensors)
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

    sites = root.findall(".//{*}measurementSite")
    count = len(sites)
    passed = count > 0
    detail = f"{count} Bluetooth device(s) reported by API" if passed else "No measurementSite elements found"
    return {"passed": passed, "detail": detail, "count": count}


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
        pub_dt = datetime.fromisoformat(pub_el.text.strip().replace("Z", "+00:00"))
        age_min = int((datetime.now(timezone.utc) - pub_dt).total_seconds() / 60)
        passed = age_min <= freshness_minutes
        label = f"Feed is {age_min} min old (limit: {freshness_minutes} min)"
        return {"passed": passed, "detail": label}
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


def _extract_namespaces(text):
    """Return a dict of prefix→uri from the root element."""
    import re
    return dict(re.findall(r'xmlns:?(\w*)=["\']([^"\']+)["\']', text[:2000]))


# Registry: maps check name (from YAML) → function
REGISTRY = {
    "valid_xml": valid_xml,
    "feed_freshness": feed_freshness,
    "vms_controller_status": vms_controller_status,
    "predefined_paths_count": predefined_paths_count,
    "bt_paths_speed_and_traveltime": bt_paths_speed_and_traveltime,
    "bt_site_count": bt_site_count,
    "sensor_speed_status": sensor_speed_status,
}
