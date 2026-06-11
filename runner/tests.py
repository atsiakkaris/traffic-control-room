"""
checks.py – Domain-specific XML assertion logic.

Each public function takes the raw response text and returns:
    {"passed": bool, "detail": str}
"""

import xml.etree.ElementTree as ET


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

    detail_lines = [
        f"Working: {len(working)}",
        f"Not working: {len(not_working)}" + (f" — IDs: — {', '.join(not_working)}" if not_working else ""),
        f"No status: {len(no_status)}" + (f" — {', '.join(no_status)}" if no_status else ""),
    ]

    passed = len(not_working) == 0
    return {
        "passed": passed,
        "detail": " | ".join(detail_lines),
        "sensors": sensors_map,
    }


# ─── Bluetooth ───────────────────────────────────────────────────────────────

def predefined_paths_count(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    # Try wildcard namespace first, then fall back to searching all tags
    paths = root.findall(".//{*}predefinedLocation")

    # Fallback: scan all elements for any tag ending in 'predefinedLocation'
    if not paths:
        paths = [el for el in root.iter() if el.tag.split("}")[-1].lower() == "predefinedlocation"]

    # Second fallback: look for 'predefinedlocationreference' (used in live feed)
    if not paths:
        paths = [el for el in root.iter() if "predefinedlocation" in el.tag.split("}")[-1].lower()]

    # Log all unique tag names to help debug if still 0
    if not paths:
        child_tags = list({el.tag.split("}")[-1] for el in root.iter()})[:20]
        return {
            "passed": False,
            "detail": f"No predefinedLocation elements found. Tags in response: {', '.join(child_tags)}"
        }

    count = len(paths)
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
    for path in paths:
        pid = path.get("id", "unknown")
        sensors_map[pid] = "failing" if pid in failing else "ok"

    passed = len(failing) == 0
    detail = (
        f"Speed OK: {speed_ok}/{total} | Travel time OK: {ttime_ok}/{total}"
        + (f" | Failing paths: {', '.join(failing)}" if failing else "")
    )
    return {"passed": passed, "detail": detail, "sensors": sensors_map}


# ─── Traffic Detection ────────────────────────────────────────────────────────

def sensor_speed_status(response_text: str) -> dict:
    root, err = _parse_xml(response_text)
    if err:
        return {"passed": False, "detail": f"Could not parse XML: {err}"}

    sensors = root.findall(".//{*}siteMeasurements")
    working, malfunctioning, no_traffic, no_measurement = [], [], [], []
    sensors_map = {}
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

    total = len(sensors)
    avg_flow = round(total_flow / flow_count, 1) if flow_count > 0 else 0

    passed = len(malfunctioning) == 0
    detail_parts = [
        f"Working: {len(working)}/{total}",
        f"No traffic (speed=0): {len(no_traffic)}",
        f"Malfunctioning (speed=-1): {len(malfunctioning)}" + (f" — {', '.join(malfunctioning[:5])}" if malfunctioning else ""),
        f"No measurement: {len(no_measurement)}",
        f"Avg flow rate: {avg_flow} veh/hr ({flow_count} sensors reporting)",
    ]
    return {"passed": passed, "detail": " | ".join(detail_parts), "sensors": sensors_map}


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
    "vms_controller_status": vms_controller_status,
    "predefined_paths_count": predefined_paths_count,
    "bt_paths_speed_and_traveltime": bt_paths_speed_and_traveltime,
    "sensor_speed_status": sensor_speed_status,
}
