"""
sensor_status.py - the one-sentence status that leads every sensor popup on the maps.

Pure functions. Two jobs:

  history_fields(history)      what a sensor is doing now, taken from its run history (stability.py does the work, so the
                               popups, the dashboard and the weekly digest never disagree about who is down);
  status_sentence(sensor, ...) one plain sentence and a tone (good / warn / bad / neutral) for the popup banner.

Lifetime uptime is kept next to the current state, because they answer different questions (see "Three metrics" in
ARCHITECTURE.md): a sensor can be down today with a good record, or working today with a bad one.
"""
import stability as st

TONES = ("good", "warn", "bad", "neutral")

BT_SENTENCES = {
    "working":       ("good",    "Working: a Bluetooth path through it works."),
    "explained":     ("good",    "Probably fine: its failing paths end at a sensor that is faulty."),
    "path_problem":  ("warn",    "Works, but some of its paths fail: look at the paths, not the sensor."),
    "faulty_high":   ("bad",     "Probably faulty: no path through it works, and several failing paths lead to sensors that are alive."),
    "faulty_likely": ("bad",     "Possibly faulty: no path through it works, but the proof is thin."),
    "no_evidence":   ("neutral", "Not enough evidence: no usable path to judge it by."),
}


def history_fields(history):
    """{state, down_days, last_status, fault_since, never_worked, recent_good, recent_total, is_fault, tier, runs} from a
    sensor's history [{run_at, status}] (oldest first); {} when there is none."""
    if not history:
        return {}
    label, _colour, _tip, down_days = st.current_state(history)
    good = sum(1 for h in history if h["status"] in st.GOOD_STATUSES)
    is_fault, rg, rt = st.windowed_fail(history)
    start = st.fault_streak_start(history)
    tier = st.tier_for_counts(good, len(history)) if len(history) >= st.TIER_MIN_RUNS else None
    return {"state": label, "down_days": down_days, "last_status": history[-1]["status"],
            "fault_since": start["run_at"][:10] if start else None, "never_worked": good == 0,
            "recent_good": rg, "recent_total": rt, "is_fault": is_fault, "tier": tier.label if tier else None, "runs": len(history)}


def _days(n):
    return "%d day%s" % (n, "" if n == 1 else "s")


def _lifetime(s):
    h = s.get("health_pct")
    if h is None:
        return ""
    return " Lifetime uptime %d%%%s." % (round(h), " (%s)" % s["tier"] if s.get("tier") else "")


def status_sentence(s, verdict=None, group=""):
    """(sentence, tone) for one API sensor dict (as qa.load_api_sensors builds it, plus project/accountability/commissioning).

    `verdict` is the Bluetooth triangulation verdict when known. Never contains the text "ID:" (the note box finds the
    sensor id in the popup by that text)."""
    ended = s.get("accountability") == "out_of_support"
    contract = " Contract ended, so a failure is expected." if ended else ""

    if not s.get("active", True):
        seen = s.get("last_seen")
        return ("Retired: it is missing from the latest API data%s." % (" (last seen %s)" % str(seen)[:10] if seen else ""), "neutral")
    comm = s.get("commissioning")
    if comm == "not_electrified":
        return ("Awaiting power: not expected to work yet, so it is left out of the statistics.", "neutral")
    if comm == "decommissioned":
        return ("Decommissioned: not expected to work, so it is left out of the statistics.", "neutral")

    if group == "Bluetooth":
        if verdict in BT_SENTENCES:
            tone, text = BT_SENTENCES[verdict]
            return (text + (contract if tone in ("warn", "bad") else ""), tone)
        return ("Bluetooth sensors have no uptime of their own: they are judged by their paths (Triangulation panel, Control Room).", "neutral")

    if not s.get("state"):
        return ("No monitor history yet.", "neutral")

    last = s.get("last_status")
    since = s.get("fault_since")
    if last in st.GOOD_STATUSES:
        bad = (s.get("recent_total") or 0) - (s.get("recent_good") or 0)
        if s.get("recent_total") and bad / s["recent_total"] >= 0.2:
            return ("Working now, but it failed %d of its last %d runs." % (bad, s["recent_total"]) + _lifetime(s), "warn")
        return ("Working now." + _lifetime(s), "good")

    days = s.get("down_days") or 0
    reason = st.STATUS_LABEL.get(last, last)
    if last == "no_traffic":
        return ("Reporting zero traffic since %s (%s): normal at night, suspicious if it lasts." % (since, _days(days)) + _lifetime(s),
                "warn" if days < 3 else "bad")
    if s.get("never_worked"):
        text = "Never worked since it was first seen%s: %s." % (" (%s ago)" % _days(days) if days else "", reason)
    elif days < 1:
        text = "Failing in the latest check (since %s, under a day): %s." % (since, reason)
    else:
        text = "Down for %s (since %s): %s." % (_days(days), since, reason)
    text += _lifetime(s)
    if s.get("is_fault") and not ended and s.get("project"):
        text += " A persistent fault for %s: it failed %d of its last %d runs." % (s["project"], s["recent_total"] - s["recent_good"], s["recent_total"])
    elif not ended and not s.get("project"):
        text += " No maintenance contract is on record for it."
    return (text + contract, "warn" if days < 1 else "bad")
