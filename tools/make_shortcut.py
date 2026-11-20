"""
make_shortcut.py - a "Control Room" shortcut with the app's icon (Windows).

    python tools/make_shortcut.py              # creates "Control Room.lnk" in the repo folder
    python tools/make_shortcut.py --desktop    # ... and a copy on your desktop

A .bat file cannot have its own icon, a shortcut can. The shortcut starts the app window directly (pythonw, no console) and
carries the same Windows app id as the window, so a shortcut pinned to the taskbar and the running window share one button
with the Control Room icon instead of Python's. Run it again after moving the repo folder or reinstalling Python.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def pythonw():
    """pythonw.exe next to the Python running this script (no console window)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.is_file() else exe


def make(dest, app_id, icon):
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(str(pythonw()))
    link.SetArguments('"%s"' % (ROOT / "tools" / "control_room.py"))
    link.SetWorkingDirectory(str(ROOT))
    link.SetIconLocation(str(icon), 0)
    link.SetDescription("Control Room: Bluetooth paths, wiring, site visits and live status")
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id))
    store.Commit()
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(dest), 0)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--desktop", action="store_true", help="also put a copy on the desktop")
    args = ap.parse_args()
    if sys.platform != "win32":
        sys.exit("Shortcuts are a Windows thing; nothing to do here.")
    import control_room as cr
    targets = [ROOT / "Control Room.lnk"]
    if args.desktop:
        targets.append(Path(os.path.expanduser("~")) / "Desktop" / "Control Room.lnk")
    for t in targets:
        print("Created", make(t, cr.APP_ID, cr.APP_ICON))
    print("To pin it: right-click the shortcut > Show more options > Pin to taskbar (or Pin to Start).")


if __name__ == "__main__":
    main()
