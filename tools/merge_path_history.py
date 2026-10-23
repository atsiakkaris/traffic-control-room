"""Move the history of re-created BT paths onto their new ids (see runner/path_history_merge.py).

    python tools/merge_path_history.py 250=661 251=662              dry run: shows what would move
    python tools/merge_path_history.py 250=661 251=662 --apply      backs up results/history.db, then moves it

Each pair is OLD=NEW. A path that OMNIA re-created under a new id keeps one history afterwards.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "runner"))

import path_history_merge as phm  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pairs", nargs="+", help="OLD=NEW path ids, e.g. 250=661")
    ap.add_argument("--db", default=str(ROOT / "results" / "history.db"))
    ap.add_argument("--apply", action="store_true", help="really move the history (a backup is made first)")
    args = ap.parse_args(argv)
    try:
        pairs = [tuple(p.split("=", 1)) for p in args.pairs]
        assert all(len(p) == 2 for p in pairs)
    except AssertionError:
        print("Each pair must look like OLD=NEW, for example 250=661.")
        return 2
    conn = sqlite3.connect(args.db)
    steps = phm.plan(conn, pairs)
    print("%-6s %-6s %10s %10s  %s" % ("old", "new", "old rows", "new rows", "note"))
    for s in steps:
        print("%-6s %-6s %10s %10s  %s" % (s["old"], s["new"], s["old_rows"], s["new_rows"], s["problem"] or "ok"))
    if not args.apply:
        print("Dry run: nothing was changed. Add --apply to move the history.")
        return 0
    if all(s["problem"] for s in steps):
        print("Nothing can be moved.")
        return 1
    backup = Path(args.db).with_name("%s.bak-%s" % (Path(args.db).name, datetime.now().strftime("%Y%m%d-%H%M%S")))
    dest = sqlite3.connect(backup)
    conn.backup(dest)
    dest.close()
    print("Backup written to", backup)
    for s in phm.apply(conn, pairs):
        print("%-6s -> %-6s moved %s rows%s" % (s["old"], s["new"], s["moved"], "  (skipped: %s)" % s["problem"] if s["problem"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
