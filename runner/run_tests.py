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
import smtplib
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Make sure runner/ is on the path when called from repo root
sys.path.insert(0, str(Path(__file__).parent))

from db import init_db, insert_run, insert_result
from checks import REGISTRY
from report import generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "endpoints.yaml"


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

    try:
        t0 = time.perf_counter()
        resp = httpx.get(url, timeout=max_ms / 1000 + 5, follow_redirects=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        result["status_code"] = resp.status_code
        result["response_ms"] = round(elapsed_ms, 1)

        failures = []

        # Status code check
        if resp.status_code != expected_status:
            failures.append(f"Expected HTTP {expected_status}, got {resp.status_code}")

        # Response time check
        if elapsed_ms > max_ms:
            failures.append(f"Response time {elapsed_ms:.0f}ms > {max_ms}ms limit")

        # Domain checks
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
            except Exception as e:
                failures.append(f"{check_name} raised exception: {e}")

        if failures:
            result["status"] = "fail"
            result["failure_reason"] = " | ".join(failures)
        else:
            result["status"] = "pass"

    except httpx.TimeoutException:
        result["status"] = "error"
        result["failure_reason"] = f"Request timed out after {max_ms/1000+5:.0f}s"
    except Exception as e:
        result["status"] = "error"
        result["failure_reason"] = str(e)

    return result


def run_all():
    base_url = os.environ.get("BASE_URL", "").strip()
    swarco = os.environ.get("SWARCO", "").strip()

    if not base_url or not swarco:
        log.error("BASE_URL and SWARCO must be set as environment variables.")
        sys.exit(1)

    config = load_config()
    init_db()

    run_id = str(uuid.uuid4())
    run_at = datetime.now(timezone.utc).isoformat()

    log.info("=" * 60)
    log.info("Run ID : %s", run_id)
    log.info("Started: %s", run_at)
    log.info("=" * 60)

    totals = {"total": 0, "passed": 0, "failed": 0, "errored": 0}
    all_results = []

    for group in config.get("groups", []):
        group_name = group["name"]
        for ep in group.get("endpoints", []):
            log.info("[%s] %s …", group_name, ep["name"])
            r = run_single(ep, base_url, swarco)

            totals["total"] += 1
            totals[{"pass": "passed", "fail": "failed", "error": "errored"}[r["status"]]] += 1

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
            )

            icon = {"pass": "✓", "fail": "✗", "error": "⚠"}.get(r["status"], "?")
            log.info("  %s  %s  (%s ms)", icon, r["status"].upper(), r["response_ms"])
            if r["failure_reason"]:
                log.info("     %s", r["failure_reason"])
            for detail in r.get("check_details", []):
                log.info("     %s", detail)

            all_results.append({**ep, "group": group_name, **r})

    insert_run(run_id, run_at, totals)

    log.info("=" * 60)
    log.info("SUMMARY  passed=%d  failed=%d  errored=%d  total=%d",
             totals["passed"], totals["failed"], totals["errored"], totals["total"])
    log.info("=" * 60)

    # Generate HTML report
    report_path = generate_report()
    log.info("Report written to %s", report_path)

    # Send email
    send_email(run_id, run_at, totals, all_results, report_path)

    # Exit non-zero if any failures (makes GitHub Actions mark the run red)
    if totals["failed"] > 0 or totals["errored"] > 0:
        sys.exit(1)


def send_email(run_id, run_at, totals, results, report_path):
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pw = os.environ.get("GMAIL_APP_PW", "")
    recipient = os.environ.get("NOTIFY_EMAIL", gmail_user)

    if not gmail_user or not gmail_pw:
        log.warning("GMAIL_USER / GMAIL_APP_PW not set — skipping email.")
        return

    all_passed = totals["failed"] == 0 and totals["errored"] == 0
    subject = (
        f"✅ SWARCO API Tests — All {totals['total']} passed"
        if all_passed
        else f"❌ SWARCO API Tests — {totals['failed']} failed, {totals['errored']} errored"
    )

    # Build HTML email body
    rows = ""
    for r in results:
        colour = {"pass": "#d4edda", "fail": "#f8d7da", "error": "#fff3cd"}.get(r["status"], "#fff")
        icon = {"pass": "✅", "fail": "❌", "error": "⚠️"}.get(r["status"], "")
        rows += f"""
        <tr style="background:{colour}">
            <td>{r['group']}</td>
            <td>{r['name']}</td>
            <td>{icon} {r['status'].upper()}</td>
            <td>{r.get('status_code','—')}</td>
            <td>{r.get('response_ms','—')} ms</td>
            <td style="font-size:12px;color:#555">{r.get('failure_reason') or ''}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:sans-serif;font-size:14px">
    <h2>SWARCO API Test Report</h2>
    <p><b>Run:</b> {run_at}<br>
       <b>Passed:</b> {totals['passed']} &nbsp;
       <b>Failed:</b> {totals['failed']} &nbsp;
       <b>Errored:</b> {totals['errored']} &nbsp;
       <b>Total:</b> {totals['total']}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
        <tr style="background:#343a40;color:white">
            <th>Group</th><th>Test</th><th>Result</th>
            <th>HTTP</th><th>Time</th><th>Reason</th>
        </tr>
        {rows}
    </table>
    <p style="color:#888;font-size:12px">Full historical report available in the repository under reports/latest.html</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pw)
            server.sendmail(gmail_user, recipient, msg.as_string())
        log.info("Email sent to %s", recipient)
    except Exception as e:
        log.error("Failed to send email: %s", e)


if __name__ == "__main__":
    run_all()
