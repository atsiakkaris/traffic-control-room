"""The Control Room's local Live page: shown only with the QA workbook, and warns when the stored projects are older than it."""
import sys
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "tools"))

import control_room as cr  # noqa: E402
import live_local  # noqa: E402


def test_stale_note_only_when_the_workbook_is_newer_than_the_stored_projects():
    upd = datetime(2026, 11, 16, 7, 0, 0, tzinfo=timezone.utc)
    mtime = lambda dt: dt.timestamp()
    assert live_local.stale_note(mtime(upd + timedelta(hours=8)), upd.isoformat()).startswith("QA Locations.xlsx was saved after the contracts")
    assert "update_projects.bat" in live_local.stale_note(mtime(upd + timedelta(days=2)), upd.isoformat())
    assert live_local.stale_note(mtime(upd - timedelta(hours=1)), upd.isoformat()) is None          # projects updated after the last save: fine
    assert live_local.stale_note(mtime(upd + timedelta(seconds=60)), upd.isoformat()) is None       # within two minutes: the same save
    assert live_local.stale_note(mtime(upd), None) is None and live_local.stale_note(mtime(upd), "garbage") is None


def test_workbook_presence_decides(tmp_path):
    assert live_local.workbook_present(tmp_path / "missing.xlsx") is False
    f = tmp_path / "QA Locations.xlsx"
    f.write_bytes(b"x")
    assert live_local.workbook_present(f) is True


def test_the_live_route_serves_the_page_caches_it_and_the_tab_is_in_the_shell(tmp_path):
    calls = []
    db = tmp_path / "h.db"
    db.write_text("a")
    srv = cr.make_server(port=0, review_file=tmp_path / "r.json", delete_log=tmp_path / "l.csv", page_builder=lambda: ("", {}),
                         paths_provider=dict, views={}, db_file=db, choose_path=None, downloads_dir=tmp_path / "dl",
                         live_builder=lambda: calls.append(1) or "<html><body>LIVE PAGE</body></html>")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda u: urllib.request.urlopen(base + u).read().decode("utf-8")
    try:
        assert "LIVE PAGE" in get("/live") and "LIVE PAGE" in get("/live")
        assert len(calls) == 1                                  # cached until the database (or the workbook) changes
        get("/live?refresh=1")
        assert len(calls) == 2
        shell = get("/")
        assert 'data-t="live"' in shell and 'id="f-live"' in shell and "t === 'live'" in shell
    finally:
        srv.shutdown()
