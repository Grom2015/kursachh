from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

HOST = "127.0.0.1"
PORT = 8000
HEALTH_URL = f"http://{HOST}:{PORT}/health"


def main() -> int:
    _stop_existing_app_server()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            HOST,
            "--port",
            str(PORT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    payload = _wait_for_health()
    print(json.dumps({"pid": proc.pid, "url": f"http://{HOST}:{PORT}/app", "health": payload}, ensure_ascii=False))
    return 0


def _stop_existing_app_server() -> None:
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*uvicorn app.main:app*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_health() -> dict:
    last_error: Exception | None = None
    for _ in range(50):
        try:
            with urlopen(HEALTH_URL, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Server did not become healthy on {HEALTH_URL}: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
