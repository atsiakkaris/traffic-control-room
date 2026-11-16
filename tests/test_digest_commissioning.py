"""The digest must exclude sensors that aren't expected to be working.

An unpowered VMS is published by the API and reports not_working forever. The
dashboard has always excluded these; the digest did not, so the weekly email
named 38 awaiting-power VMS under "No good runs this week", burying real faults.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))


def test_dashboard_and_digest_share_one_exclusion_rule():
    import report, digest, stability
    assert report._EXCLUDED_COMMISSIONING is stability.EXCLUDED_COMMISSIONING
    assert digest.EXCLUDED_COMMISSIONING is stability.EXCLUDED_COMMISSIONING


def test_excluded_states_are_exactly_awaiting_power_and_decommissioned():
    from stability import EXCLUDED_COMMISSIONING
    assert EXCLUDED_COMMISSIONING == {"not_electrified", "decommissioned"}
    assert "active" not in EXCLUDED_COMMISSIONING


def test_fetch_excluded_commissioning_picks_up_both_states(monkeypatch):
    import digest
    monkeypatch.setattr(digest, "fetch_sensor_projects", lambda: {
        "VMS": {
            "1": {"commissioning": "not_electrified"},
            "2": {"commissioning": "decommissioned"},
            "3": {"commissioning": "active"},
        },
        "Traffic Detection": {"9": {"commissioning": "active"}},
    })
    assert digest.fetch_excluded_commissioning() == {("VMS", "1"), ("VMS", "2")}


def test_an_active_sensor_is_never_excluded(monkeypatch):
    import digest
    monkeypatch.setattr(digest, "fetch_sensor_projects", lambda: {
        "VMS": {"7": {"commissioning": "active"}, "8": {}},
    })
    assert digest.fetch_excluded_commissioning() == set()


def test_week_tier_uses_raw_counts_not_the_rounded_percentage():
    """98.6% is Healthy, as on the dashboard; rounding it to 99 would call it Always on."""
    import digest
    assert digest._tier(99, (986, 1000)).key == "healthy"
    assert digest._tier(0, (1, 500)).key == "critical"      # one good run is not "no good runs"
    assert digest._tier(0, (0, 28)).key == "offline"
    assert digest._tier(99).key == "always_on"              # no counts: falls back to the percentage


def test_legend_ranges_come_from_the_tier_definitions():
    import digest
    from stability import STABILITY_TIERS
    page = digest.build_html({
        "week_start": "", "week_end": "", "run_count": 0, "check_freq": "every 6 hours", "total": 0,
        **{t.key: 0 for t in STABILITY_TIERS}, "groups": {}, "bt": {"total": 0, **{t.key: 0 for t in STABILITY_TIERS}},
        "always_off": [], "persistent": [], "degraded": [], "recovered": [], "retired": [], "census": [],
        "sensor_names": {}})
    assert "Healthy — 90–98%" in page and "90–99%" not in page
    assert "&mdash." not in page and "EEST</p>" not in page
