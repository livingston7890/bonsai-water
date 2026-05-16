#!/usr/bin/env python3
"""Deterministic Project Bonsai operations command surface.

This is intentionally not a shell bridge. It only accepts explicit allowlisted
commands and talks to the Bonsai Flask hub / SSH target configured in .env.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
DEFAULT_BASE_URL = "http://10.0.0.38:5100"
DEFAULT_TIMEOUT_SECONDS = 8


class OpsError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    aliases: tuple[str, ...]
    description: str
    mutates: bool = False
    destructive: bool = False


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("help", ("/start", "start", "help", "/help"), "show allowed Bonsai bot commands"),
    CommandSpec("status", ("status", "health", "snapshot"), "whole-system snapshot: hub, bonsai, HA, Pi-hole"),
    CommandSpec("moisture", ("moisture", "soil", "bonsai"), "current moisture/raw sensor/pump/auto state"),
    CommandSpec("read_now", ("read now", "read-now", "refresh moisture"), "force one moisture sensor read", mutates=True),
    CommandSpec("lights", ("lights", "lamps"), "Home Assistant lights/speakers summary"),
    CommandSpec("lights_on", ("lights on", "lamps on"), "turn both lamps on", mutates=True),
    CommandSpec("lights_off", ("lights off", "lamps off"), "turn both lamps off", mutates=True),
    CommandSpec("speakers_on", ("speakers on", "speaker on"), "turn both speaker smart plugs on", mutates=True),
    CommandSpec("speakers_off", ("speakers off", "speaker off"), "turn both speaker smart plugs off", mutates=True),
    CommandSpec("open_shop", ("open shop", "shop open"), "turn both lamps and both speakers on", mutates=True),
    CommandSpec("close_shop", ("close shop", "shop close"), "turn both lamps and both speakers off", mutates=True),
    CommandSpec("palette_cool", ("cool", "cool lights", "cool lamps"), "apply the cool lamp palette", mutates=True),
    CommandSpec("palette_warm", ("warm", "warm lights", "warm lamps"), "apply the warm lamp palette", mutates=True),
    CommandSpec("palette_money", ("money", "money lights", "money lamps"), "apply the money lamp palette", mutates=True),
    CommandSpec("palette_candle", ("candle", "candle lights", "candle lamps"), "apply the candle lamp palette", mutates=True),
    CommandSpec("palette_ice_fire", ("ice/fire", "ice fire", "icefire", "fire ice", "ice/fire lights", "ice fire lights"), "apply Ice/Fire: left cool, right red-warm", mutates=True),
    CommandSpec("palette_aurora", ("aurora", "aurora lights", "green purple", "green and purple"), "apply Aurora: green and purple", mutates=True),
    CommandSpec("palette_cyber_orchid", ("cyber orchid", "cyber-orchid", "orchid", "cyan magenta"), "apply Cyber Orchid: cyan and magenta", mutates=True),
    CommandSpec("palette_ember_forest", ("ember forest", "ember-forest", "orange green", "fire forest"), "apply Ember Forest: orange and green", mutates=True),
    CommandSpec("palette_moon_grove", ("moon grove", "moon-grove", "blue green", "moon garden"), "apply Moon Grove: blue and green", mutates=True),
    CommandSpec("palette_miami_vice", ("miami vice", "miami-vice", "vice", "pink cyan"), "apply Miami Vice: hot pink and cyan", mutates=True),
    CommandSpec("palette_tokyo_night", ("tokyo night", "tokyo-night", "tokyo", "indigo magenta"), "apply Tokyo Night: indigo and magenta", mutates=True),
    CommandSpec("palette_deep_ocean", ("deep ocean", "deep-ocean", "ocean", "teal blue"), "apply Deep Ocean: teal and royal blue", mutates=True),
    CommandSpec("palette_golden_hour", ("golden hour", "golden-hour", "golden", "amber peach"), "apply Golden Hour: amber and peach", mutates=True),
    CommandSpec("palette_jade_temple", ("jade temple", "jade-temple", "jade", "green ivory"), "apply Jade Temple: jade and ivory", mutates=True),
    CommandSpec("pump_on", ("pump on", "start pump", "manual pump on"), "start one bounded manual pump run", mutates=True),
    CommandSpec("pump_off", ("pump off", "stop pump", "manual pump off"), "stop manual/active pump run", mutates=True),
    CommandSpec("pihole", ("pihole", "dns"), "Pi-hole blocking/metrics summary"),
    CommandSpec("restart_app", ("restart app", "restart hub"), "restart only the Bonsai Flask app", mutates=True),
    CommandSpec("deploy_hub", ("deploy hub confirm", "update hub confirm"), "manually deploy latest GitHub main to the Pi hub; exact confirmation required", mutates=True, destructive=True),
    CommandSpec("reboot_pi", ("reboot pi confirm", "reset pi confirm"), "reboot the Raspberry Pi via SSH; exact confirmation required", mutates=True, destructive=True),
)


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


def config_value(name: str, default: str = "") -> str:
    return os.environ.get(name) or load_dotenv().get(name) or default


def base_url() -> str:
    return config_value("BONSAI_HUB_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def normalize_command(text: str) -> str:
    value = " ".join((text or "").strip().lower().split())
    if value.startswith("/bonsai "):
        value = value[len("/bonsai ") :]
    return value


def classify_command(text: str) -> str | None:
    value = normalize_command(text)
    for spec in COMMANDS:
        if value == spec.name or value in spec.aliases:
            return spec.name
    return None


def allowed_commands_text() -> str:
    lines = ["Project Bonsai commands:"]
    for spec in COMMANDS:
        shown = spec.aliases[0] if spec.aliases else spec.name
        marker = " [writes]" if spec.mutates else ""
        if spec.destructive:
            marker += " [requires exact confirm]"
        lines.append(f"- {shown}: {spec.description}{marker}")
    return "\n".join(lines)


def _json_request(path: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
    url = f"{base_url()}{path}"
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, data=body, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout or int(config_value("BONSAI_HTTP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw[:500]
        raise OpsError(f"HTTP {exc.code} from {path}: {detail}") from exc
    except Exception as exc:
        raise OpsError(f"Could not reach Bonsai hub at {url}: {exc}") from exc


def fmt_bool(value: Any) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    if value is None:
        return "unknown"
    return str(value)


def fmt_pct(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return str(value)


def _fmt_watering_value(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return str(value)


def _parse_iso_local(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _last_auto_run_summary(bonsai: dict[str, Any]) -> str:
    try:
        waterings = _json_request("/api/bonsai/waterings?count=30")
    except Exception:
        waterings = []
    auto_events: list[dict[str, Any]] = []
    if isinstance(waterings, list):
        for event in waterings:
            if not isinstance(event, dict):
                continue
            if str(event.get("mode", "")).lower() != "auto":
                continue
            parsed = _parse_iso_local(event.get("timestamp"))
            if parsed is None:
                continue
            item = dict(event)
            item["_dt"] = parsed
            auto_events.append(item)

    if auto_events:
        auto_events.sort(key=lambda event: event["_dt"], reverse=True)
        session = [auto_events[0]]
        for event in auto_events[1:]:
            gap_seconds = (session[-1]["_dt"] - event["_dt"]).total_seconds()
            if gap_seconds > 15 * 60:
                break
            session.append(event)

        session_chronological = list(reversed(session))
        first = session_chronological[0]
        last = session_chronological[-1]
        date_text = last["_dt"].strftime("%Y-%m-%d")
        if first["_dt"].date() == last["_dt"].date():
            window = f"{date_text} {first['_dt'].strftime('%H:%M')}–{last['_dt'].strftime('%H:%M')} local"
        else:
            window = f"{first['_dt'].strftime('%Y-%m-%d %H:%M')}–{last['_dt'].strftime('%Y-%m-%d %H:%M')} local"
        total_duration = 0.0
        for event in session:
            try:
                total_duration += float(event.get("duration") or 0)
            except Exception:
                pass
        pulse_word = "pulse" if len(session) == 1 else "pulses"
        before = _fmt_watering_value(first.get("before"))
        after = _fmt_watering_value(last.get("after"))
        reason = last.get("stop_reason") or "unknown reason"
        return (
            f"{window}, {len(session)} {pulse_word}, {total_duration:.0f}s watered, "
            f"moisture {before} → {after}, {reason}"
        )

    fallback = bonsai.get("last_auto_watered") or bonsai.get("last_auto_run") or bonsai.get("last_watered")
    if fallback:
        return f"{fallback} (time-only fallback; watering history unavailable)"
    return "unknown"


def compact_status() -> str:
    hub = _json_request("/api/hub/health")
    bonsai = _json_request("/api/bonsai/status")
    ha = _safe_request("/api/ha/status")
    pihole = _safe_request("/api/pihole/status")
    pump = bonsai.get("pump") or {}
    cfg = bonsai.get("config") or {}
    last_auto_run = _last_auto_run_summary(bonsai)
    lines = [
        "Project Bonsai status",
        f"Moisture: {fmt_pct(bonsai.get('moisture'))} raw={bonsai.get('moisture_raw', 'unknown')}",
        f"Auto watering: {fmt_bool(cfg.get('auto_watering_enabled'))}",
        f"Last auto run: {last_auto_run}",
        f"Pump: {fmt_bool(pump.get('running'))} mode={pump.get('mode', 'unknown')} remaining={pump.get('remaining_seconds', 0)}s",
        f"Lights: {_ha_lamp_summary(ha)}",
        f"Speakers: {_ha_speaker_summary(ha)}",
        f"Palette: {ha.get('lamp_palette_last', ha.get('palette', ha.get('active_palette', 'unknown')))}",
        f"Hub: {hub.get('level', 'unknown')} — {hub.get('message', 'no message')}",
        f"Pi-hole: {pihole.get('message') or fmt_bool(pihole.get('connected'))}; blocking={fmt_bool(pihole.get('blocking_enabled', pihole.get('blocking')))}",
        f"Source: {base_url()}",
    ]
    return "\n".join(lines)


def _safe_request(path: str) -> dict[str, Any]:
    try:
        data = _json_request(path)
        return data if isinstance(data, dict) else {"value": data}
    except Exception as exc:
        return {"connected": False, "message": str(exc)}


def _ha_lamp_summary(ha: dict[str, Any]) -> str:
    flat_left = ha.get("lamp_left_state")
    flat_right = ha.get("lamp_right_state")
    if flat_left is not None or flat_right is not None:
        return f"left:{flat_left or 'unknown'},right:{flat_right or 'unknown'}"
    lamps = ha.get("lamps") or ha.get("lights") or {}
    if isinstance(lamps, dict):
        parts = []
        for key in ("left", "right"):
            val = lamps.get(key)
            if isinstance(val, dict):
                parts.append(f"{key}:{val.get('state', val.get('on', 'unknown'))}")
            elif val is not None:
                parts.append(f"{key}:{val}")
        if parts:
            return ",".join(parts)
    return "unknown"


def _ha_speaker_summary(ha: dict[str, Any]) -> str:
    flat_left = ha.get("speaker_left_state")
    flat_right = ha.get("speaker_right_state")
    if flat_left is not None or flat_right is not None:
        return f"left:{flat_left or 'unknown'},right:{flat_right or 'unknown'}"
    speakers = ha.get("speakers") or {}
    if isinstance(speakers, dict):
        parts = []
        for key in ("left", "right"):
            val = speakers.get(key)
            if isinstance(val, dict):
                parts.append(f"{key}:{val.get('state', val.get('on', 'unknown'))}")
            elif val is not None:
                parts.append(f"{key}:{val}")
        if parts:
            return ",".join(parts)
    return "unknown"


def moisture_report() -> str:
    bonsai = _json_request("/api/bonsai/status")
    pump = bonsai.get("pump") or {}
    cfg = bonsai.get("config") or {}
    points = bonsai.get("calibration_points") or []
    return "\n".join([
        "Bonsai moisture",
        f"Moisture: {fmt_pct(bonsai.get('moisture'))}",
        f"Raw: {bonsai.get('moisture_raw', 'unknown')}",
        f"Calibration points: {len(points)}",
        f"Thresholds: low={cfg.get('moisture_threshold_low', 'unknown')} high={cfg.get('moisture_threshold_high', 'unknown')}",
        f"Pump: {fmt_bool(pump.get('running'))} mode={pump.get('mode', 'unknown')} remaining={pump.get('remaining_seconds', 0)}s",
        f"Auto watering: {fmt_bool(cfg.get('auto_watering_enabled'))}",
        f"Quiet hours blocking now: {fmt_bool(bonsai.get('office_hours_blocking_now'))}",
    ])


def read_now() -> str:
    result = _json_request("/api/bonsai/read_now", method="POST", payload={})
    return f"Read now: {result.get('message', 'done')} raw={result.get('moisture_raw', 'unknown')}"


def lights_report() -> str:
    ha = _json_request("/api/ha/status")
    return "\n".join([
        "Home Assistant",
        f"Connected: {fmt_bool(ha.get('connected'))}",
        f"Lamps: {_ha_lamp_summary(ha)}",
        f"Speakers: {_ha_speaker_summary(ha)}",
        f"Palette: {ha.get('palette', ha.get('active_palette', 'unknown'))}",
        f"Brightness: {ha.get('brightness', 'unknown')}",
    ])


def set_lights(on: bool) -> str:
    result = _json_request("/api/ha/lamps", method="POST", payload={"on": bool(on)})
    status = result.get("ha_status") if isinstance(result.get("ha_status"), dict) else _safe_request("/api/ha/status")
    return "\n".join([
        f"Lights {'on' if on else 'off'}: {result.get('message', 'requested')}",
        f"Lamps: {_ha_lamp_summary(status)}",
    ])


def set_speakers(on: bool) -> str:
    result = _json_request("/api/ha/speakers", method="POST", payload={"on": bool(on)})
    status = result.get("ha_status") if isinstance(result.get("ha_status"), dict) else _safe_request("/api/ha/status")
    return "\n".join([
        f"Speakers {'on' if on else 'off'}: {result.get('message', 'requested')}",
        f"Speakers: {_ha_speaker_summary(status)}",
    ])


def set_shop_open(open_shop: bool) -> str:
    """Open/close the shop by toggling lamps and speaker plugs together.

    Keep this as fixed, narrow Home Assistant operations only. Continue through
    all three endpoint calls so one side failing does not prevent the other
    devices from receiving the requested state.
    """
    target = bool(open_shop)
    failures: list[str] = []
    messages: list[str] = []

    operations = [
        ("lamps", "/api/ha/lamps", {"on": target}),
        ("speakers", "/api/ha/speakers", {"on": target}),
    ]
    latest_status: dict[str, Any] | None = None
    for label, path, payload in operations:
        try:
            result = _json_request(path, method="POST", payload=payload)
            if isinstance(result.get("ha_status"), dict):
                latest_status = result["ha_status"]
            messages.append(f"{label}: {result.get('message', 'requested')}")
        except Exception as exc:
            failures.append(f"{label}: {exc}")

    status = latest_status if latest_status is not None else _safe_request("/api/ha/status")
    lines = [
        f"Shop {'opened' if target else 'closed'}: lights and speakers {'on' if target else 'off'} requested.",
        f"Lamps: {_ha_lamp_summary(status)}",
        f"Speakers: {_ha_speaker_summary(status)}",
    ]
    if messages:
        lines.append("Actions: " + "; ".join(messages[:3]))
    if failures:
        lines.append("Failures: " + "; ".join(failures))
    return "\n".join(lines)


def set_lamp_palette(palette: str) -> str:
    palette_name = str(palette).strip().lower()
    allowed = {"cool", "warm", "money", "candle", "ice_fire", "aurora", "cyber_orchid", "ember_forest", "moon_grove", "miami_vice", "tokyo_night", "deep_ocean", "golden_hour", "jade_temple"}
    if palette_name not in allowed:
        raise OpsError("Palette must be one of: cool, warm, money, candle, ice_fire, aurora, cyber_orchid, ember_forest, moon_grove, miami_vice, tokyo_night, deep_ocean, golden_hour, jade_temple.")
    result = _json_request("/api/ha/lamp_palette", method="POST", payload={"palette": palette_name})
    raw_status = result.get("ha_status")
    status = raw_status if isinstance(raw_status, dict) else _safe_request("/api/ha/status")
    display_name = palette_name.replace("_", " ").title()
    return "\n".join([
        f"{display_name} lights: {result.get('message', 'palette requested')}",
        f"Lamps: {_ha_lamp_summary(status)}",
        f"Palette: {status.get('palette', status.get('active_palette', palette_name))}",
    ])


def set_pump(enabled: bool) -> str:
    result = _json_request("/api/bonsai/manual_toggle", method="POST", payload={"enabled": bool(enabled)})
    status = _safe_request("/api/bonsai/status")
    pump = status.get("pump") or {}
    cfg = status.get("config") or {}
    max_run = cfg.get("manual_max_runtime_seconds", "configured")
    return "\n".join([
        f"Pump {'started' if enabled else 'stop requested'}: {result.get('message', 'ok')}",
        f"Pump: {fmt_bool(pump.get('running'))} mode={pump.get('mode', 'unknown')} remaining={pump.get('remaining_seconds', 0)}s",
        f"Manual run max: {max_run}s",
    ])


def pihole_report() -> str:
    pihole = _json_request("/api/pihole/status")
    metrics = pihole.get("metrics") or {}
    return "\n".join([
        "Pi-hole",
        f"Connected: {fmt_bool(pihole.get('connected'))}",
        f"Blocking: {fmt_bool(pihole.get('blocking_enabled', pihole.get('blocking')))}",
        f"Mode: {pihole.get('mode', 'unknown')}",
        f"Queries today: {metrics.get('queries_today', metrics.get('dns_queries_today', pihole.get('queries_today'))) or 'unknown'}",
        f"Blocked today: {metrics.get('ads_blocked_today', metrics.get('blocked_today', pihole.get('blocked_today'))) or 'unknown'}",
    ])


def restart_app() -> str:
    result = _json_request("/api/hub/restart", method="POST", payload={})
    return f"App restart requested: {result.get('message', 'ok')}"


def _ssh_base_command() -> tuple[str, list[str]]:
    target = config_value("BONSAI_PI_SSH_TARGET", "").strip()
    if not target:
        raise OpsError("BONSAI_PI_SSH_TARGET is not configured. Set it in .env, e.g. madmaestro@10.0.0.38 or maechinepi4.local.")
    extra = shlex.split(config_value("BONSAI_PI_SSH_EXTRA_ARGS", ""))
    return target, ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", *extra, target]


def deploy_hub() -> str:
    """Manually deploy latest origin/main to the Pi hub via fixed SSH workflow."""
    target, ssh_cmd = _ssh_base_command()
    app_dir = shlex.quote(config_value("BONSAI_PI_APP_DIR", "/home/madmaestro/bonsai-water"))
    branch = shlex.quote(config_value("BONSAI_DEPLOY_BRANCH", "main") or "main")
    remote = f'''
set -euo pipefail
cd {app_dir}
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p deploy-backups
before=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
git fetch origin {branch}
if ! git merge-base --is-ancestor HEAD FETCH_HEAD; then
  if [ -f .git/shallow ]; then
    echo "shallow-history detected; deepening before fast-forward deploy"
    git fetch --deepen=1000 origin {branch}
  fi
fi
if ! git merge-base --is-ancestor HEAD FETCH_HEAD; then
  echo "refusing deploy: local HEAD is not an ancestor of origin/{branch}" >&2
  git log --oneline --decorate --graph --all -20 >&2 || true
  exit 42
fi
after=$(git rev-parse --short FETCH_HEAD)
changed=$(git diff --name-only HEAD FETCH_HEAD 2>/dev/null || true)
if [ -z "$changed" ]; then
  echo "already-current $before"
  cat hub_update.json 2>/dev/null || true
  exit 0
fi
printf '%s\n' "$changed" > "deploy-backups/manual-deploy-$stamp.files"
tar -czf "deploy-backups/manual-deploy-$stamp.tgz" -T "deploy-backups/manual-deploy-$stamp.files" 2>/dev/null || true
git merge --ff-only FETCH_HEAD
python3 - <<'PY'
import json
from pathlib import Path
p = Path('hub_update.json')
data = json.loads(p.read_text()) if p.exists() else {{}}
data.update({{"mode": "git", "branch": "main", "auto_deploy": False, "poll_seconds": max(300, int(data.get("poll_seconds", 300) or 300))}})
p.write_text(json.dumps(data, indent=2) + "\\n")
PY
python3 -m py_compile pi_hub.py scripts/bonsai_ops.py scripts/bonsai_hub_watchdog.py
sudo systemctl restart bonsai-hub.service
for i in $(seq 1 18); do
  if curl -fsS --max-time 5 http://127.0.0.1:5100/api/hub/health >/tmp/bonsai-deploy-health.json; then
    echo "deployed $before -> $after"
    echo "backup deploy-backups/manual-deploy-$stamp.tgz"
    cat /tmp/bonsai-deploy-health.json
    exit 0
  fi
  sleep 5
done
systemctl --no-pager --full status bonsai-hub.service || true
exit 1
'''
    completed = subprocess.run([*ssh_cmd, remote], capture_output=True, text=True, timeout=150, check=False)
    detail = "\n".join(part.strip() for part in [completed.stdout, completed.stderr] if part.strip())
    if completed.returncode != 0:
        raise OpsError(f"Hub deploy failed on {target}: {detail[:1200]}")
    return "Manual hub deploy complete.\n" + detail[:1600]


def reboot_pi() -> str:
    target, ssh_cmd = _ssh_base_command()
    command = [*ssh_cmd, "sudo", "/sbin/reboot"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ssh reboot failed").strip()
        raise OpsError(f"Pi reboot command failed: {detail[:500]}")
    return f"Pi reboot command accepted for {target}."


def apply_command(text: str) -> str:
    command = classify_command(text)
    if command is None:
        return "Unknown Bonsai command.\n\n" + allowed_commands_text()
    if command == "help":
        return allowed_commands_text()
    if command == "status":
        return compact_status()
    if command == "moisture":
        return moisture_report()
    if command == "read_now":
        return read_now()
    if command == "lights":
        return lights_report()
    if command == "lights_on":
        return set_lights(True)
    if command == "lights_off":
        return set_lights(False)
    if command == "speakers_on":
        return set_speakers(True)
    if command == "speakers_off":
        return set_speakers(False)
    if command == "open_shop":
        return set_shop_open(True)
    if command == "close_shop":
        return set_shop_open(False)
    if command == "palette_cool":
        return set_lamp_palette("cool")
    if command == "palette_warm":
        return set_lamp_palette("warm")
    if command == "palette_money":
        return set_lamp_palette("money")
    if command == "palette_candle":
        return set_lamp_palette("candle")
    if command == "palette_ice_fire":
        return set_lamp_palette("ice_fire")
    if command == "palette_aurora":
        return set_lamp_palette("aurora")
    if command == "palette_cyber_orchid":
        return set_lamp_palette("cyber_orchid")
    if command == "palette_ember_forest":
        return set_lamp_palette("ember_forest")
    if command == "palette_moon_grove":
        return set_lamp_palette("moon_grove")
    if command == "palette_miami_vice":
        return set_lamp_palette("miami_vice")
    if command == "palette_tokyo_night":
        return set_lamp_palette("tokyo_night")
    if command == "palette_deep_ocean":
        return set_lamp_palette("deep_ocean")
    if command == "palette_golden_hour":
        return set_lamp_palette("golden_hour")
    if command == "palette_jade_temple":
        return set_lamp_palette("jade_temple")
    if command == "pump_on":
        return set_pump(True)
    if command == "pump_off":
        return set_pump(False)
    if command == "pihole":
        return pihole_report()
    if command == "restart_app":
        return restart_app()
    if command == "deploy_hub":
        return deploy_hub()
    if command == "reboot_pi":
        return reboot_pi()
    raise OpsError(f"Unhandled command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an allowlisted Project Bonsai operation")
    parser.add_argument("command", nargs="*", help="command text, e.g. 'status' or 'reboot pi confirm'")
    parser.add_argument("--json", action="store_true", help="emit JSON wrapper")
    args = parser.parse_args(argv)
    text = " ".join(args.command).strip() or "help"
    try:
        message = apply_command(text)
        payload = {"ok": True, "command": classify_command(text), "message": message}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(message)
        return 0
    except Exception as exc:
        payload = {"ok": False, "command": classify_command(text), "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
