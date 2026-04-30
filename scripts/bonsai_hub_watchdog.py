#!/usr/bin/env python3
"""Project Bonsai hub watchdog for Miyagi.

Checks the Pi hub from the Mac and sends failure/recovery Telegram alerts.
Optionally attempts a fixed SSH restart command when explicitly enabled via
BONSAI_WATCHDOG_SSH_RESTART=1 and SSH auth is available.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
STATE_DIR = ROOT / "generated" / "watchdog"
STATE_FILE = STATE_DIR / "bonsai_hub_watchdog_state.json"
CHAT_FILE = ROOT / "generated" / "telegram" / "bonsai_bot_chat_id.txt"
DEFAULT_CHECK_PATH = "/api/hub/health"


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name) or load_dotenv().get(name) or default


def base_url() -> str:
    return cfg("BONSAI_HUB_BASE_URL", "http://maechinepi4.local:5100").rstrip("/")


def state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "unknown", "fail_count": 0, "last_error": ""}


def save_state(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def telegram_chat_id() -> str:
    explicit = cfg("BONSAI_WATCHDOG_CHAT_ID")
    if explicit:
        return explicit
    if CHAT_FILE.exists():
        value = CHAT_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    return cfg("BONSAI_TELEGRAM_ALLOWED_USER_ID")


def send_telegram(text: str) -> None:
    token = cfg("BONSAI_TELEGRAM_BOT_TOKEN")
    chat_id = telegram_chat_id()
    if not token or not chat_id:
        print("watchdog alert skipped: missing Telegram token/chat", file=sys.stderr)
        return
    payload = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
    req = urlrequest.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram send failed: {data}")


def check_hub() -> tuple[bool, str]:
    url = f"{base_url()}{cfg('BONSAI_WATCHDOG_CHECK_PATH', DEFAULT_CHECK_PATH)}"
    req = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=int(cfg("BONSAI_WATCHDOG_TIMEOUT_SECONDS", "8"))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = getattr(resp, "status", 200)
        if code < 200 or code >= 300:
            return False, f"HTTP {code} from {url}"
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {}
        level = data.get("level") or "ok"
        msg = data.get("message") or data.get("service") or "hub responded"
        return True, f"{level}: {msg} ({url})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc} ({url})"


def ssh_target() -> str:
    return cfg("BONSAI_PI_SSH_TARGET", "madmaestro@maechinepi4.local").strip()


def ssh_command(remote: str) -> list[str]:
    extra = cfg("BONSAI_PI_SSH_EXTRA_ARGS", "").split()
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *extra, ssh_target(), remote]


def run_ssh(remote: str, timeout: int = 20) -> tuple[int, str]:
    completed = subprocess.run(ssh_command(remote), text=True, capture_output=True, timeout=timeout, check=False)
    detail = "\n".join(part.strip() for part in [completed.stdout, completed.stderr] if part.strip())
    return completed.returncode, detail


def short(text: str, chars: int = 1200) -> str:
    text = text.strip()
    if len(text) <= chars:
        return text
    return "... truncated ...\n" + text[-chars:]


def collect_diagnostics() -> str:
    remote = r'''
set -uo pipefail
printf 'ssh: ok\n'
printf 'uptime: '; uptime -p 2>/dev/null || uptime
printf 'service-enabled: '; systemctl is-enabled bonsai-hub.service 2>/dev/null || true
printf 'service-active: '; systemctl is-active bonsai-hub.service 2>/dev/null || true
printf 'port-5100: '; if lsof -iTCP:5100 -sTCP:LISTEN -n -P >/dev/null 2>&1; then echo listening; else echo not-listening; fi
printf 'disk-root: '; df -h / | awk 'NR==2 {print $5 " used (" $4 " free)"}'
printf 'temp: '; { vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null | awk '{printf "%.1fC\n", $1/1000}'; } || true
printf 'local-health: '; curl -fsS --max-time 5 http://127.0.0.1:5100/api/hub/health 2>/dev/null || echo failed
printf '\nrecent-journal:\n'
journalctl -u bonsai-hub.service -n 18 --no-pager || true
'''
    try:
        code, detail = run_ssh(remote, timeout=25)
    except Exception as exc:
        return f"ssh diagnostics failed: {exc}"
    prefix = "diagnostics ok" if code == 0 else f"diagnostics exit={code}"
    return short(f"{prefix}\n{detail}", 1800)


def maybe_ssh_restart() -> str:
    if cfg("BONSAI_WATCHDOG_SSH_RESTART", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return "ssh restart disabled"
    remote = r'''
set -euo pipefail
sudo systemctl restart bonsai-hub.service
for i in $(seq 1 8); do
  if curl -fsS --max-time 5 http://127.0.0.1:5100/api/hub/health >/dev/null; then
    echo "systemd restart succeeded; health ok after $((i*5))s"
    exit 0
  fi
  sleep 5
done
echo "systemd restart attempted but health still failing"
systemctl --no-pager --full status bonsai-hub.service || true
exit 1
'''
    try:
        code, detail = run_ssh(remote, timeout=60)
    except Exception as exc:
        return f"ssh restart failed: {exc}"
    if code == 0:
        return short(detail or "systemd restart succeeded", 900)
    return "ssh systemd restart failed: " + short(detail, 900)


def main() -> int:
    previous = state()
    ok, detail = check_hub()
    threshold = max(1, int(cfg("BONSAI_WATCHDOG_FAILURE_THRESHOLD", "2")))
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    if ok:
        recovered = previous.get("status") == "fail" and bool(previous.get("alert_sent"))
        save_state({"status": "ok", "fail_count": 0, "last_ok": detail, "last_error": "", "alert_sent": False})
        print(f"OK {detail}")
        if recovered:
            send_telegram(f"✅ Project Bonsai hub recovered\n{detail}")
        return 0

    fail_count = int(previous.get("fail_count") or 0) + 1
    restart_note = ""
    diagnostics = ""
    if fail_count >= threshold:
        diagnostics = collect_diagnostics()
        restart_note = maybe_ssh_restart()

    alert_already_sent = previous.get("status") == "fail" and bool(previous.get("alert_sent"))
    should_alert = not alert_already_sent and fail_count >= threshold
    save_state(
        {
            "status": "fail",
            "fail_count": fail_count,
            "last_error": detail,
            "last_restart": restart_note,
            "last_diagnostics": diagnostics,
            "alert_sent": alert_already_sent or should_alert,
        }
    )
    print(f"FAIL count={fail_count} {detail}; {restart_note}; {diagnostics}", file=sys.stderr)

    if should_alert:
        send_telegram(
            "⚠️ Project Bonsai hub is down\n"
            f"Time: {now}\n"
            f"Source: {base_url()}\n"
            f"Error: {detail}\n"
            f"Repair: {restart_note}\n"
            f"Diagnostics:\n{diagnostics}\n"
            "Miyagi is still running, but hub commands may fail."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
