"""Resync the trusted Firefox profile copy (wco-profile-ff) that wco.py
drives, from the USER'S LIVE Firefox profile, so the automation always
carries the current Cloudflare clearance and WCOS session cookies.

Reading the live cookie store while Firefox is running is racy (SQLite
WAL), so Firefox is force-closed first when it is open.

Why this runs under WSL python3 and works on /tmp snapshots:
  * the bundled Windows Python's sqlite cannot open a modern Firefox
    cookie DB at all ("unable to open database file");
  * WSL sqlite CAN open one, but only reliably when the DB lives on a
    local filesystem - the -shm/-wal memory mapping breaks on the
    drive-mapped /mnt/c. So each DB (live read, copy write) is snap-
    copied to a local /tmp dir, processed there with sqlite, and the
    result is copied back over the /mnt/c file with plain file copies
    (no sqlite i/o on the mapped drive).

If invoked from Windows Python this module re-executes itself under WSL
automatically. Wired into server startup; also runnable standalone:

    python sync_ff_cookies.py
"""

import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

_SYNC_IN_WSL = os.environ.get("SYNC_IN_WSL") == "1"

_COOKIE_FILES = ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm")


def _in_wsl():
    return _SYNC_IN_WSL


def _shq(s):
    """Shell single-quote a string (safe for values with backslashes)."""
    return "'" + s.replace("'", "'\\''") + "'"


def _wsl_path(wpath):
    """Translate a Windows path (C:\\x\\y) to its WSL form (/mnt/c/x/y)."""
    if len(wpath) >= 2 and wpath[1] == ":":
        return "/mnt/" + wpath[0].lower() + "/" + wpath[2:].replace("\\", "/")
    return wpath.replace("\\", "/")


def _win_exe(name):
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", name)
    return _wsl_path(path) if _in_wsl() else path


def _profiles_dir():
    d = os.environ.get("APPDATA", "")
    if not d:
        raise RuntimeError("APPDATA not set")
    d = os.path.join(d, "Mozilla", "Firefox")
    return _wsl_path(d) if _in_wsl() else d


COPY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "wco-profile-ff")


def find_live_profile():
    """Return the path of the live Firefox profile (with cookies.sqlite),
    or None. Uses profiles.ini; falls back to a .default-release glob."""
    pdir = _profiles_dir()
    ini = os.path.join(pdir, "profiles.ini")
    if os.path.exists(ini):
        entries = []
        cur = None
        try:
            with open(ini, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("[") and line.endswith("]"):
                        cur = {}
                        entries.append(cur)
                    elif "=" in line and cur is not None:
                        k, v = line.split("=", 1)
                        cur[k.strip()] = v.strip()
        except OSError:
            entries = []
        for ent in entries:
            default = (ent.get("Default") == "1"
                       or ent.get("Name", "").endswith("default-release"))
            if not default or not ent.get("Path"):
                continue
            p = ent["Path"]
            ap = p if os.path.isabs(p) else os.path.join(pdir, p)
            if os.path.exists(os.path.join(ap, "cookies.sqlite")):
                return ap
    hits = glob.glob(os.path.join(pdir, "Profiles", "*.default-release"))
    hits = [h for h in hits if os.path.exists(os.path.join(h, "cookies.sqlite"))]
    if hits:
        return max(hits, key=os.path.getmtime)
    return None


def firefox_running():
    try:
        out = subprocess.run([_win_exe("tasklist.exe"),
                              "/FI", "IMAGENAME eq firefox.exe", "/NH"],
                             capture_output=True, text=True, timeout=30)
        return "firefox.exe" in out.stdout.lower()
    except Exception:
        return False


def close_firefox(wait_s=12):
    """Force-close Firefox if it is running. Returns True if it needed
    closing (and finished closing), False if it wasn't running."""
    if not firefox_running():
        return False
    try:
        subprocess.run([_win_exe("taskkill.exe"), "/F", "/IM", "firefox.exe"],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    for _ in range(int(wait_s * 2)):
        if not firefox_running():
            return True
        time.sleep(0.5)
    return not firefox_running()


def _snap(dirpath, tmp):
    """Copy the cookie DB (main + wal + shm) of dirpath into tmp and
    return the copied main path. Works on local fs so WAL shm mmap is
    not an issue."""
    main = os.path.join(dirpath, "cookies.sqlite")
    dst = os.path.join(tmp, "cookies.sqlite")
    shutil.copy2(main, dst)
    for f in _COOKIE_FILES[1:]:
        src = os.path.join(dirpath, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp, f))
    return dst


def _read_wco_rows(main_path):
    """Open a LOCAL snapshot and return (columns, rows) for cookies whose
    host mentions wco / wcostream."""
    for _ in range(6):
        try:
            con = sqlite3.connect(main_path, timeout=5)
            try:
                cols = [c[1] for c in con.execute("PRAGMA table_info(moz_cookies)")]
                rows = [tuple(r) for r in con.execute("SELECT * FROM moz_cookies")
                        if "wco" in (r[cols.index("host")] or "")]
                return cols, rows
            finally:
                con.close()
        except sqlite3.Error:
            time.sleep(0.5)


def _apply_wco_rows(main_path, cols, rows):
    """Rewrite a LOCAL snapshot's moz_cookies with the given wco rows."""
    for _ in range(6):
        try:
            con = sqlite3.connect(main_path, timeout=5)
            try:
                con.execute("PRAGMA journal_mode=DELETE")
                tcols = [c[1] for c in con.execute("PRAGMA table_info(moz_cookies)")]
                names = [c for c in cols if c in tcols]
                con.execute("DELETE FROM moz_cookies WHERE host LIKE '%wco%' "
                            "OR host LIKE '%wcostream%'")
                for r in rows:
                    con.execute("INSERT INTO moz_cookies (%s) VALUES (%s)"
                                % (",".join(names), ",".join("?" * len(names))),
                                [r[cols.index(c)] for c in names])
                con.commit()
                return
            finally:
                con.close()
        except sqlite3.Error:
            time.sleep(0.5)


def _strip(dirs):
    """Remove stale WAL/shm files so a later sqlite/Firefox open is clean."""
    for d in dirs:
        for f in _COOKIE_FILES[1:]:
            p = os.path.join(d, f)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _sync():
    live = find_live_profile()
    if not live:
        return "no live Firefox profile found (%s)" % _profiles_dir()
    copy = COPY_DIR
    if not os.path.isdir(copy) or not os.path.exists(os.path.join(copy, "cookies.sqlite")):
        return "automation profile missing: %s" % copy

    closed = close_firefox()

    ltmp = tempfile.mkdtemp(prefix="ffsync-live-")
    ctmp = tempfile.mkdtemp(prefix="ffsync-copy-")
    try:
        cols, rows = _read_wco_rows(_snap(live, ltmp))
        if not rows:
            return "sync: no wco cookies to copy (found %d)" % len(rows)
        snap = _snap(copy, ctmp)
        _apply_wco_rows(snap, cols, rows)
        dst = os.path.join(copy, "cookies.sqlite")
        tmpdst = os.path.join(ctmp, "cookies.sqlite")
        shutil.copy2(tmpdst, dst)
        report = "resynced %d wco cookies from %s" % (len(rows), os.path.basename(live))
        if closed:
            report = "firefox was closed; " + report
        return report
    finally:
        _strip((live, copy))
        shutil.rmtree(ltmp, ignore_errors=True)
        shutil.rmtree(ctmp, ignore_errors=True)


def sync_cookies():
    """Entry point used by both server.py and the standalone script.

    On Windows Python, delegate to WSL python3 (its sqlite / local-fs
    snapshots can handle the cookie DB; the Windows one cannot) and
    return its report.
    """
    if sys.platform == "win32" and not _in_wsl():
        script = _wsl_path(os.path.abspath(__file__))
        appdata = os.environ.get("APPDATA", "")
        cmd = ("export SYNC_IN_WSL=1%s; python3 %s"
               % ((" APPDATA=" + _shq(appdata)) if appdata else "",
                  _shq(script)))
        out = subprocess.run(["wsl.exe", "bash", "-lc", cmd],
                             capture_output=True, text=True, timeout=300,
                             cwd=os.environ.get("SystemDrive", "C:") + "\\")
        if out.returncode != 0:
            detail = (out.stderr or out.stdout).strip()
            detail = "\n".join(ln for ln in detail.splitlines()
                               if not ln.startswith("wsl:"))
            raise RuntimeError(detail)
        lines = [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
        return lines[-1] if lines else out.stdout.strip()
    return _sync()


def main():
    print("SouthparkDS cookie resync")
    print("  live profile dir : %s" % find_live_profile())
    print("  automation copy  : %s" % COPY_DIR)
    try:
        print("  %s" % sync_cookies())
    except Exception as ex:  # noqa: BLE001
        import traceback
        print("  FAILED: %s" % ex)
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())