#!/usr/bin/env python3
"""Native messaging host: lets the extension start and stop the local backend.

The browser launches this script for each message. It reads one JSON request
({"action": "status" | "start" | "stop"}), replies with one JSON object, and exits.
Standard library only, so it runs before the backend virtual environment exists.
"""
import json
import os
import signal
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("HIM_STATE_DIR", Path.home() / ".local" / "share" / "him"))
PID_FILE = STATE_DIR / "backend.pid"
LOG_FILE = STATE_DIR / "backend.log"
SERVICE_NAME = "him-hiring-assistant.service"
UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME
HEALTH_URL = "http://127.0.0.1:8765/api/health"
START_TIMEOUT_SECONDS = 20


def read_message() -> dict:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return {}
    length = struct.unpack("=I", header)[0]
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send_message(payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(data)) + data)
    sys.stdout.buffer.flush()


def is_running() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def venv_python() -> str:
    candidates = [Path(os.environ["HIM_VENV_DIR"])] if os.environ.get("HIM_VENV_DIR") else []
    candidates += [Path.home() / ".venvs" / "him", PROJECT_DIR / ".venv"]
    for venv in candidates:
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if python.exists():
            return str(python)
    return sys.executable


def uses_systemd() -> bool:
    return sys.platform.startswith("linux") and UNIT_FILE.exists()


def systemctl(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, SERVICE_NAME], check=True, capture_output=True, timeout=30)


def server_host() -> str:
    """127.0.0.1 (this machine only) unless SERVER_HOST is set in settings.env, for example 0.0.0.0 to reach the dashboard from other machines."""
    if os.environ.get("HIM_SERVER_HOST"):
        return os.environ["HIM_SERVER_HOST"]
    try:
        for line in (STATE_DIR / "settings.env").read_text(encoding="utf-8").splitlines():
            if line.strip().upper().startswith("SERVER_HOST="):
                return line.split("=", 1)[1].strip() or "127.0.0.1"
    except OSError:
        pass
    return "127.0.0.1"


def spawn_backend() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = [venv_python(), "-m", "uvicorn", "app.main:app", "--host", server_host(), "--port", "8765"]
    options = {"cwd": PROJECT_DIR, "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    with open(LOG_FILE, "ab") as log:
        process = subprocess.Popen(command, stdout=log, **options)
    PID_FILE.write_text(str(process.pid))


def kill_spawned_backend() -> None:
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text())
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError):
        pass
    PID_FILE.unlink(missing_ok=True)


def start() -> dict:
    if is_running():
        return {"ok": True, "running": True, "message": "Server already running"}
    try:
        systemctl("start") if uses_systemd() else spawn_backend()
    except Exception as error:
        return {"ok": False, "running": False, "message": f"Could not start server: {error}"}
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_running():
            return {"ok": True, "running": True, "message": "Server started"}
        time.sleep(0.5)
    return {"ok": False, "running": False, "message": f"Server did not respond in time. See {LOG_FILE}"}


def stop() -> dict:
    try:
        systemctl("stop") if uses_systemd() else kill_spawned_backend()
    except Exception as error:
        return {"ok": False, "running": is_running(), "message": f"Could not stop server: {error}"}
    for _ in range(20):
        if not is_running():
            return {"ok": True, "running": False, "message": "Server stopped"}
        time.sleep(0.5)
    return {"ok": False, "running": True, "message": "Server is still running (not started by this helper?)"}


def main() -> None:
    action = read_message().get("action")
    if action == "start":
        send_message(start())
    elif action == "stop":
        send_message(stop())
    elif action == "status":
        send_message({"ok": True, "running": is_running()})
    else:
        send_message({"ok": False, "message": f"Unknown action: {action}"})


if __name__ == "__main__":
    main()
