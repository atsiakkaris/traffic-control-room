"""The one-sentence status that leads every sensor popup."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import sensor_status as ss  # noqa: E402


def hist(*statuses):
    """History, one run per 6 h, the last run now; statuses oldest first."""
    now = datetime.now(timezone.utc)
    n = len(statuses)
    return [{"run_at": (now - timedelta(hours=6 * (n - 1 - i))).isoformat(), "status": s} for i, s in enumerate(statuses)]


def sensor(history, **kw):
    good = sum(1 for h in history if h["status"] in ("working", "ok"))
    base = {"id": "7", "active": True, "health_pct": round(good / len(history) * 100, 1) if history else None,
            "accountability": "supported", "project": "Contract A", "commissioning": "active", "total_runs": len(history)}
    base.update(ss.history_fields(history))
    base.update(kw)
    return base


def test_history_fields_empty_and_basic():
    assert ss.history_fields([]) == {}
    f = ss.history_fields(hist("working", "working", "not_working", "not_working", "not_working", "not_working"))
    assert f["state"].startswith("Down") and f["last_status"] == "not_working" and f["never_worked"] is False
    assert f["recent_total"] == 6 and f["recent_good"] == 2 and f["runs"] == 6 and f["fault_since"]


def test_working_sensor_is_good_and_shows_lifetime():
    text, tone = ss.status_sentence(sensor(hist(*["working"] * 20)))
    assert tone == "good" and text.startswith("Working now.") and "Lifetime uptime 100% (Always on)" in text


def test_working_again_but_recent_failures_warn():
    text, tone = ss.status_sentence(sensor(hist(*["not_working"] * 6 + ["working"] * 14)))
    assert tone == "warn" and "failed 6 of its last 20 runs" in text


def test_down_for_days_names_the_reason_the_age_and_a_persistent_fault():
    # 80 runs at 6 h = 20 days, the last 40 bad (10 days)
    text, tone = ss.status_sentence(sensor(hist(*["working"] * 40 + ["malfunctioning"] * 40)))
    assert tone == "bad" and ("Down for 9 days" in text or "Down for 10 days" in text)
    assert "Speed = -1 (sensor fault)" in text and "Lifetime uptime 50% (Unstable)" in text
    assert "persistent fault for Contract A" in text and "20 of its last 20" in text


def test_never_worked_and_just_failed():
    text, tone = ss.status_sentence(sensor(hist(*["not_working"] * 30)))
    assert tone == "bad" and text.startswith("Never worked since it was first seen")
    text, tone = ss.status_sentence(sensor(hist(*["working"] * 10 + ["not_working"])))
    assert tone == "warn" and text.startswith("Failing in the latest check")


def test_contract_ended_and_no_contract():
    s = sensor(hist(*["working"] * 10 + ["not_working"] * 30), accountability="out_of_support")
    text, _ = ss.status_sentence(s)
    assert text.endswith("Contract ended, so a failure is expected.") and "persistent fault" not in text
    text, _ = ss.status_sentence(sensor(hist(*["working"] * 10 + ["not_working"] * 30), project=None))
    assert "No maintenance contract is on record" in text


def test_zero_traffic_is_not_called_a_failure_straight_away():
    text, tone = ss.status_sentence(sensor(hist(*["working"] * 10 + ["no_traffic"] * 2)))
    assert tone == "warn" and "zero traffic" in text and "Down for" not in text


def test_retired_awaiting_power_decommissioned_and_no_history():
    assert ss.status_sentence({"active": False, "last_seen": "2026-10-05T10:00"})[0] == "Retired: it is missing from the latest API data (last seen 2026-10-05)."
    assert ss.status_sentence({"active": True, "commissioning": "not_electrified"})[1] == "neutral"
    assert ss.status_sentence({"active": True, "commissioning": "decommissioned"})[0].startswith("Decommissioned")
    assert ss.status_sentence({"active": True})[0] == "No monitor history yet."


def test_bluetooth_uses_the_verdict_and_never_talks_about_uptime():
    for verdict, tone in (("working", "good"), ("explained", "good"), ("path_problem", "warn"), ("faulty_high", "bad"),
                          ("faulty_likely", "bad"), ("no_evidence", "neutral")):
        text, t = ss.status_sentence({"active": True, "accountability": "supported"}, verdict, "Bluetooth")
        assert t == tone and "uptime" not in text.lower().replace("no uptime of their own", "")
    text, tone = ss.status_sentence({"active": True}, None, "Bluetooth")
    assert tone == "neutral" and "judged by their paths" in text
    text, _ = ss.status_sentence({"active": True, "accountability": "out_of_support"}, "faulty_high", "Bluetooth")
    assert text.endswith("Contract ended, so a failure is expected.")


def test_sentences_never_contain_the_id_marker_the_note_box_looks_for():
    for s in (sensor(hist(*["not_working"] * 30)), {"active": False}, {"active": True}):
        assert "ID:" not in ss.status_sentence(s)[0]
