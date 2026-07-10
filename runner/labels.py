"""
labels.py — how a sensor is named for a human.

Single source of truth for the dashboard, the weekly digest, and anything else
that shows a sensor to a person. Kept here rather than duplicated per report so
the same sensor never appears under two different names.

The API inventory does not name every sensor: 22 of 104 Traffic Detection loops
have a NULL name but all of them carry a site_code. Falling back to the raw
sensor_id tells the reader nothing about which road went dark, so the site code
is used whenever it exists.
"""

import re


def sensor_display_name(group_name, sensor_id, name=None, site_code=None):
    """Human-readable label for one sensor.

    Traffic Detection  -> "1040 (100)"     or "1010 (Gr. Dhigeni Ave. (TCC))"
    VMS                -> "A1 Highway Limassol-Nicosia (Alambra) (8)"
    Bluetooth Paths    -> "1004->1008"     (or the bare id when the feed gives no name)
    """
    sid = str(sensor_id)
    # Feed names arrive with embedded newlines ("Gr. Dhigeni Ave.\n (TCC)").
    name = re.sub(r"\s+", " ", name).strip() or None if name else None
    site_code = (str(site_code).strip() or None) if site_code is not None else None

    if group_name == "Traffic Detection":
        return f"{site_code} ({name or sid})" if site_code else (name or sid)
    if group_name == "VMS":
        return f"{name} ({sid})" if name and name != sid else sid
    # Bluetooth Paths, Bluetooth sites, and anything added later.
    return name or sid


def with_id(label, sensor_id):
    """Append the raw id in grey, unless the label already carries it.

    The name says which road; the id is what gets quoted to the contractor.
    Matched on digit boundaries: sensor 23 must not be considered "already
    present" merely because site code 1023 contains those two characters.
    """
    sid = str(sensor_id)
    if re.search(rf"(?<!\d){re.escape(sid)}(?!\d)", label):
        return label
    return f'{label} <span style="color:#9ca3af">({sid})</span>'
