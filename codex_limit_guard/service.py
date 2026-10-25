"""Recover a missing local monitor without blocking the calling chat."""

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .control import disabled


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a safe liveness probe on Windows.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def ensure_monitor(store, config, now=None, launcher=subprocess.Popen):
    now = time.time() if now is None else now
    if disabled(store.home) or not config.auto_start_monitor or not (store.home / "installation.json").is_file():
        return False
    # Reserve startup across simultaneous chat/MCP processes. The daemon has its own lease.
    with store.transaction() as db:
        lease = store._meta(db, "daemon_lease", {})
        dead = bool(lease.get("pid") and not process_alive(lease["pid"]))
        if lease.get("expires_at", 0) > now and not dead:
            return False
        if dead:
            db.execute("DELETE FROM meta WHERE key='daemon_lease'")
        if not dead and store._meta(db, "monitor_start_after", 0) > now:
            return False
        store._put(db, "monitor_start_after", now + 15)
    entry = Path(__file__).resolve().parent.parent / "guard.py"
    if not entry.is_file():
        entry = Path(__file__).with_name("_launcher.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        with (store.home / "daemon.stdout.log").open("ab") as out, (store.home / "daemon.stderr.log").open("ab") as err:
            child = launcher([sys.executable, str(entry), "--home", str(store.home), "daemon"],
                             stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env,
                             cwd=str(entry.parent), creationflags=flags, close_fds=True)
        store.put_meta("monitor_start", {"at": now, "pid": child.pid})
        threading.Thread(target=child.wait, daemon=True).start()
        return True
    except OSError as exc:
        store.put_meta("monitor_start_error", {"at": now, "error": str(exc)[:300]})
        return False
