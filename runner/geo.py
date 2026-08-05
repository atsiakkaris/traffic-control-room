"""
geo.py — Extract geographic coordinates from DATEX II inventory feeds.
"""
import re
import html
import xml.etree.ElementTree as ET


def _parse(response_text):
    try:
        return ET.fromstring(response_text.strip())
    except ET.ParseError:
        return None


def extract_measurement_site_coords(response_text):
    """Return {site_id: {lat, lon, name}} from a MeasurementSiteTablePublication."""
    root = _parse(response_text)
    if root is None:
        return {}
    result = {}
    for site in root.findall(".//{*}measurementSite"):
        sid = site.get("id")
        if not sid:
            continue
        lat_el = site.find(".//{*}latitude")
        lon_el = site.find(".//{*}longitude")
        name_el = site.find(".//{*}measurementSiteName//{*}value")
        code_el = site.find(".//{*}measurementSiteIdentification")
        if lat_el is not None and lon_el is not None:
            try:
                result[sid] = {
                    "lat": float(lat_el.text),
                    "lon": float(lon_el.text),
                    "name": name_el.text if name_el is not None and name_el.text else sid,
                    "site_code": code_el.text if code_el is not None else None,
                }
            except (ValueError, TypeError):
                pass
    return result


def extract_vms_coords(response_text):
    """Return {controller_id: {lat, lon, name}} from a VmsTablePublication."""
    root = _parse(response_text)
    if root is None:
        return {}
    result = {}
    for ctrl in root.findall(".//{*}vmsController"):
        cid = ctrl.get("id")
        if not cid:
            continue
        lat_el = ctrl.find(".//{*}latitude")
        lon_el = ctrl.find(".//{*}longitude")
        desc_el = ctrl.find(".//{*}description//{*}value")
        ext_id_el = ctrl.find(".//{*}externalIdentifier")
        name = (desc_el.text if desc_el is not None and desc_el.text else
                ext_id_el.text if ext_id_el is not None and ext_id_el.text else cid)
        if lat_el is not None and lon_el is not None:
            try:
                result[cid] = {
                    "lat": float(lat_el.text),
                    "lon": float(lon_el.text),
                    "name": name,
                }
            except (ValueError, TypeError):
                pass
    return result


def extract_bt_path_coords(response_text):
    """Return {path_id: {name, coords: [[lat,lon],...]}} from PredefinedLocationsPublication."""
    root = _parse(response_text)
    if root is None:
        return {}
    result = {}
    for loc in root.findall(".//{*}predefinedLocationReference"):
        pid = loc.get("id")
        if not pid:
            continue
        name_el = loc.find(".//{*}predefinedLocationName//{*}value")
        name = name_el.text if name_el is not None and name_el.text else pid
        pos_el = loc.find(".//{*}posList")
        if pos_el is None or not pos_el.text:
            continue
        m = re.search(r'<gml:coordinates[^>]*>(.*?)</gml:coordinates>', html.unescape(pos_el.text), re.DOTALL)
        if not m:
            continue
        coords = []
        for pair in m.group(1).strip().split():
            parts = pair.split(',')
            if len(parts) == 2:
                try:
                    coords.append([float(parts[0]), float(parts[1])])
                except ValueError:
                    pass
        if coords:
            result[pid] = {"name": name, "coords": coords}
    return result
