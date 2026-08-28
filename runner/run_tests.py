"""
run_tests.py  –  Entry point for the daily SWARCO API test suite.

Usage:
    python runner/run_tests.py

Environment variables (set as GitHub Secrets):
    BASE_URL      – Base URL, e.g. https://datex.example.com/
    SWARCO        – Path segment, e.g. swarco/
    GMAIL_USER    – Gmail address for outgoing email
    GMAIL_APP_PW  – Gmail App Password (NOT your regular password)
    NOTIFY_EMAIL  – Recipient address for the daily report
"""

import os
import sys
import uuid
import time
import yaml
import httpx
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from db import (init_db, insert_run, update_run_totals, insert_result, insert_sensor_result,
                upsert_sensor_coords, upsert_bt_path_coords,
                retire_missing_sensors, retire_missing_bt_paths,
                set_feed_measurement_timestamp)
from tests import REGISTRY
from geo import extract_measurement_site_coords, extract_vms_coords, extract_bt_path_coords
from report import generate_report

class _CyprusFormatter(logging.Formatter):
    _tz = ZoneInfo("Asia/Nicosia")
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=self._tz)
        return ct.strftime(datefmt or "%H:%M:%S")

_handler = logging.StreamHandler()
_handler.setFormatter(_CyprusFormatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "endpoints.yaml"

_STATUS_KEY  = {"pass": "passed", "fail": "failed", "error": "errored"}
_STATUS_ICON = {"pass": "✓", "fail": "✗", "error": "⚠"}

# Maps coords_extract YAML value → (extract_fn, upsert_fn, retire_fn, entity_label)
_COORDS_HANDLERS = {
    "measurement_site": (
        extract_measurement_site_coords,
        lambda coords, group: upsert_sensor_coords(group, coords),
        lambda coords, group: retire_missing_sensors(group, set(coords.keys())),
        "sensors",
    ),
    "vms": (
        extract_vms_coords,
        lambda coords, group: upsert_sensor_coords(group, coords),
        lambda coords, group: retire_missing_sensors(group, set(coords.keys())),
        "controllers",
    ),
    "bt_paths": (
        extract_bt_path_coords,
        lambda coords, _group: upsert_bt_path_coords(coords),
        lambda coords, _group: retire_missing_bt_paths(set(coords.keys())),
        "paths",
    ),
}


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def build_url(base_url: str, swarco: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{swarco.strip('/')}/{path}"


def run_single(endpoint_def: dict, base_url: str, swarco: str) -> dict:
    url = build_url(base_url, swarco, endpoint_def["path"])
    expected_status = endpoint_def.get("expected_status", 200)
    max_ms = endpoint_def.get("max_response_ms", 5000)
    check_names = endpoint_def.get("checks", ["valid_xml"])

    result = {
        "url": url,
        "method": "GET",
        "expected_code": expected_status,
        "status_code": None,
        "response_ms": None,
        "status": "error",
        "failure_reason": None,
        "check_details": [],
    }

    for attempt in range(2):
        try:
            t0 = time.perf_counter()
            resp = httpx.get(url, timeout=max_ms / 1000 + 10, follow_redirects=True)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            result["status_code"] = resp.status_code
            result["response_ms"] = round(elapsed_ms, 1)
            result["response_text"] = resp.text

            failures = []

            if resp.status_code != expected_status:
                failures.append(f"Expected HTTP {expected_status}, got {resp.status_code}")

            if elapsed_ms > max_ms:
                failures.append(f"Response time {elapsed_ms:.0f}ms > {max_ms}ms limit")

            for check_name in check_names:
                fn = REGISTRY.get(check_name)
                if fn is None:
                    log.warning("Unknown check '%s' — skipping", check_name)
                    continue
                try:
                    check_result = fn(resp.text)
                    result["check_details"].append(
                        f"[{'✓' if check_result['passed'] else '✗'}] {check_name}: {check_result['detail']}"
                    )
                    if not check_result["passed"]:
                        failures.append(f"{check_name}: {check_result['detail']}")
                    if check_result.get("sensors"):
                        result.setdefault("sensors", {}).update(check_result["sensors"])
                    if check_result.get("measurements"):
                        result.setdefault("measurements", {}).update(check_result["measurements"])
                    if check_result.get("common_measurement_timestamp"):
                        result["common_measurement_timestamp"] = check_result["common_measurement_timestamp"]
                except Exception as e:
                    failures.append(f"{check_name} raised exception: {e}")

            result["status"] = "fail" if failures else "pass"
            if failures:
                result["failure_reason"] = " | ".join(failures)

            break  # success — no retry needed

        except httpx.TimeoutException:
            if attempt == 0:
                log.warning("  ↻  Timeout on attempt 1, retrying…")
                continue
            result["status"] = "error"
            result["failure_reason"] = f"Request timed out after {max_ms/1000+5:.0f}s (2 attempts)"
        except Exception as e:
            if attempt == 0:
                log.warning("  ↻  Error on attempt 1 (%s), retrying…", e)
                continue
            result["status"] = "error"
            result["failure_reason"] = str(e)

    result.setdefault("response_text", "")
    return result


def _process_coords(coords_type, response_text, group_name, run_status):
    """Extract, upsert, and retire coordinates for a passed inventory endpoint.

    Returns the extracted {id: ...} dict (or None) so callers can reconcile it
    against a paired live endpoint's reported sensor IDs — see
    _reconcile_missing_sensors().
    """
    handler = _COORDS_HANDLERS.get(coords_type)
    if handler is None:
        return None
    extract_fn, upsert_fn, retire_fn, entity_label = handler
    coords = extract_fn(response_text)
    if run_status == "pass" and coords:
        upsert_fn(coords, group_name)
        retire_fn(coords, group_name)
        return coords
    elif run_status == "pass" and not coords:
        log.warning("[%s] Inventory passed but returned no %s — skipping retire", group_name, entity_label)
    return None


def _reconcile_missing_sensors(run_id, run_at, sensor_group, inventory_ids, live_ids):
    """Insert a status="missing" row for every inventory sensor absent from the
    live feed this run — a sensor that silently dropped out of the live
    response otherwise leaves no trace at all (see ITS review roadmap item #1:
    a shrinking denominator can raise a group's health % with nothing fixed).

    Only called when the live endpoint's own fetch succeeded — if the whole
    feed is down, every sensor would look "missing", which is the
    feed-freshness/error check's job to report, not this one's.
    """
    missing = set(inventory_ids) - set(live_ids)
    for sensor_id in sorted(missing):
        insert_sensor_result(run_id, run_at, sensor_group, sensor_id, "missing", None)
    if missing:
        log.info("[%s] %d sensor(s) missing from live feed: %s",
                  sensor_group, len(missing), ", ".join(sorted(missing)))


def run_all():
    base_url = os.environ.get("BASE_URL", "").strip()
    swarco   = os.environ.get("SWARCO", "").strip()

    if not base_url or not swarco:
        log.error("BASE_URL and SWARCO must be set as environment variables.")
        sys.exit(1)

    config = load_config()
    init_db()

    run_id = str(uuid.uuid4())
    run_at = datetime.now(timezone.utc).isoformat()
    live_mode = os.environ.get("LIVE_MODE", "").lower() in ("1", "true", "yes")

    log.info("=" * 60)
    log.info("Run ID : %s", run_id)
    log.info("Started: %s", run_at)
    log.info("=" * 60)

    totals = {"total": 0, "passed": 0, "failed": 0, "errored": 0}
    all_results = []

    # Written up front (with zero totals, updated once the run finishes) so
    # test_results/sensor_results always have a real parent row to point at
    # — if the process dies partway through, those rows survive as part of
    # a genuine (if incomplete) run instead of becoming orphans.
    insert_run(run_id, run_at, totals)

    for group in config.get("groups", []):
        group_name = group["name"]
        inventory_ids_by_endpoint = {}   # endpoint name -> ids seen this run (only inventory endpoints)
        reconcile_todo = []              # [(sensor_group, inventory_endpoint_name, live_ids)]

        for ep in group.get("endpoints", []):
            log.info("[%s] %s …", group_name, ep["name"])
            r = run_single(ep, base_url, swarco)

            totals["total"] += 1
            totals[_STATUS_KEY.get(r["status"], "errored")] += 1

            insert_result(
                run_id=run_id,
                group_name=group_name,
                test_name=ep["name"],
                endpoint=r["url"],
                method=r["method"],
                status=r["status"],
                status_code=r["status_code"],
                expected_code=r["expected_code"],
                response_ms=r["response_ms"],
                failure_reason=r["failure_reason"],
                check_summary=" | ".join(r.get("check_details", [])),
            )

            sensor_group = ep.get("sensor_group", group_name)
            live_ids_seen = r.get("sensors", {})
            for sensor_id, s_status in live_ids_seen.items():
                mdata = r.get("measurements", {}).get(sensor_id) if live_mode else None
                insert_sensor_result(run_id, run_at, sensor_group, sensor_id, s_status, mdata)

            if r.get("common_measurement_timestamp"):
                set_feed_measurement_timestamp(sensor_group, r["common_measurement_timestamp"])

            # Extract and store coordinates from inventory endpoints.
            # Only retire when the feed explicitly passed — an empty coord set from a
            # failed fetch must not wipe active sensors.
            coords_type = ep.get("coords_extract")
            if r.get("response_text") and coords_type:
                coords = _process_coords(coords_type, r["response_text"], group_name, r["status"])
                if coords:
                    inventory_ids_by_endpoint[ep["name"]] = set(coords.keys())

            # Queue live-feed reconciliation (see _reconcile_missing_sensors) — only
            # when this endpoint's own fetch got a real response, so a fully-down
            # feed doesn't flag every sensor as "missing" (that's the freshness/
            # error check's job, not this one's).
            reconcile_against = ep.get("reconcile_against")
            if reconcile_against and r.get("response_text"):
                reconcile_todo.append((sensor_group, reconcile_against, set(live_ids_seen.keys())))

            icon = _STATUS_ICON.get(r["status"], "?")
            log.info("  %s  %s  (%s ms)", icon, r["status"].upper(), r["response_ms"])
            if r["failure_reason"]:
                log.info("     %s", r["failure_reason"])
            for detail in r.get("check_details", []):
                log.info("     %s", detail)
            log.info("")

            all_results.append({**ep, "group": group_name, **r})

        for sensor_group, inventory_endpoint_name, live_ids in reconcile_todo:
            inventory_ids = inventory_ids_by_endpoint.get(inventory_endpoint_name)
            if inventory_ids is None:
                # Inventory endpoint failed or returned nothing this run — can't
                # tell a real dropout from an inventory hiccup, so skip.
                continue
            _reconcile_missing_sensors(run_id, run_at, sensor_group, inventory_ids, live_ids)

    update_run_totals(run_id, totals)

    log.info("=" * 60)
    log.info("SUMMARY  passed=%d  failed=%d  errored=%d  total=%d",
             totals["passed"], totals["failed"], totals["errored"], totals["total"])
    log.info("=" * 60)

    report_path = generate_report()
    log.info("Report written to %s", report_path)

    if totals["failed"] > 0 or totals["errored"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
