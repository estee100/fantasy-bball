"""Start the dashboard automatically at Windows login, hidden in the background.

    python -m fbb autostart install   # adds a shortcut to your Startup folder
    python -m fbb autostart remove

The shortcut runs `pythonw -m fbb.autostart run`: no console window, output goes to
data/server.log, and it exits immediately if the server is already running.
"""
import os
import socket
import subprocess
import sys
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


def install():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        raise SystemExit(f"pythonw.exe not found next to {sys.executable}")
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
    subprocess.Popen([str(pythonw), "-m", "fbb.autostart", "run"], cwd=config.ROOT,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def remove():
    if SHORTCUT.exists():
        SHORTCUT.unlink()
        print(f"Removed {SHORTCUT}. A server that's already running keeps running until you log out "
              f"(or stop it in Task Manager: pythonw.exe).")
    else:
        print("Autostart wasn't installed.")


if __name__ == "__main__":
    {"run": run, "install": install, "remove": remove}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
