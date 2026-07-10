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
