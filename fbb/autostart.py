"""Start the dashboard automatically at Windows login, hidden in the background.

    python -m fbb autostart install   # adds a shortcut to your Startup folder
    python -m fbb autostart remove
    python -m fbb autostart restart   # e.g. after editing .env; works whether or not it's installed

The shortcut runs `pythonw -m fbb.autostart run`: no console window, output goes to
data/server.log, and it exits immediately if the server is already running.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import config

PORT = 8000
LOG = config.DATA_DIR / "server.log"
MAX_LOG_BYTES = 5_000_000
SHORTCUT = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/Fantasy Hoops HQ.lnk"


def _running() -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def run():
    if _running():
        return
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
        LOG.replace(LOG.with_suffix(".log.old"))
    # pythonw has no console: sys.stdout/stderr are None. Point them at the log before
    # uvicorn configures logging, so its handlers (which resolve sys.stderr) write there.
    log = open(LOG, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = log
    import logging
    logging.basicConfig(level=logging.INFO, stream=log,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn
    uvicorn.run("fbb.server:app", host="127.0.0.1", port=PORT)


def _pythonw() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        raise SystemExit(f"pythonw.exe not found next to {sys.executable}")
    return pythonw


def _launch():
    subprocess.Popen([str(_pythonw()), "-m", "fbb.autostart", "run"], cwd=config.ROOT,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def _wait(up: bool, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _running() == up:
            return True
        time.sleep(0.5)
    return False


def _listener() -> tuple[int, str] | None:
    """PID and image name of whatever is listening on PORT, or None."""
    ps = (f"$c = Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | "
          f"Select-Object -First 1; if ($c) {{ \"$($c.OwningProcess) $((Get-Process -Id $c.OwningProcess).Name)\" }}")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True).stdout.strip()
    if not out:
        return None
    pid, name = out.split(maxsplit=1)
    return int(pid), name


def restart():
    """Stop the server on PORT (hidden or `fbb serve`) and start it hidden again, so it
    re-reads .env. Only kills a python process, so it won't take out some other app on 8000."""
    if _running():
        found = _listener()
        if found is None:
            raise SystemExit(f"Something is on port {PORT} but its process couldn't be found; stop it manually.")
        pid, name = found
        if not name.lower().startswith("python"):
            raise SystemExit(f"Port {PORT} is held by {name} (PID {pid}), not the dashboard; not killing it.")
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=True, capture_output=True)
        if not _wait(up=False, timeout=10):
            raise SystemExit(f"Stopped PID {pid} but port {PORT} is still in use.")
        print(f"Stopped server (PID {pid}).")
    _launch()
    if not _wait(up=True, timeout=30):
        raise SystemExit(f"Server didn't come up within 30s; check {LOG}")
    print(f"Running at http://localhost:{PORT} (log: {LOG})")


def install():
    pythonw = _pythonw()
    ps = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{SHORTCUT}')
$s.TargetPath = '{pythonw}'
$s.Arguments = '-m fbb.autostart run'
$s.WorkingDirectory = '{config.ROOT}'
$s.WindowStyle = 7
$s.Description = 'Fantasy Hoops HQ dashboard (http://localhost:{PORT})'
$s.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print(f"Installed: {SHORTCUT}")
    print(f"Starting it now; dashboard at http://localhost:{PORT}")
    _launch()


def remove():
    if SHORTCUT.exists():
        SHORTCUT.unlink()
        print(f"Removed {SHORTCUT}. A server that's already running keeps running until you log out "
              f"(or stop it in Task Manager: pythonw.exe).")
    else:
        print("Autostart wasn't installed.")


if __name__ == "__main__":
    {"run": run, "install": install, "remove": remove, "restart": restart}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
