#!/usr/bin/env python3
"""
Bonsai Pi Control Hub

Features:
- Moisture polling and logging always enabled
- Auto-watering toggle (on/off)
- Manual pump toggle with immediate stop
- Manual run hard timeout (30s default)
- Active-low relay support (Waveshare relay HAT)
- OLED status updates
- Web dashboard for control and history
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import board
import busio
import RPi.GPIO as GPIO
from adafruit_seesaw.seesaw import Seesaw

# ------------------------
# File paths / constants
# ------------------------
APP_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DB_FILE = os.path.join(APP_DIR, "bonsai_data.db")

# Relay CH1 on Waveshare board
RELAY_PIN = 26

# Waveshare relay board is active-low for relay ON
PUMP_ON_LEVEL = GPIO.LOW
PUMP_OFF_LEVEL = GPIO.HIGH

# Sensor I2C address
SOIL_SENSOR_ADDR = 0x36

# ------------------------
# Defaults
# ------------------------
DEFAULT_CONFIG = {
    "moisture_threshold_low": 35,
    "moisture_threshold_high": 65,
    "watering_duration_seconds": 60,
    "min_water_interval_seconds": 28800,
    "sensor_read_interval_seconds": 300,
    "pump_max_runtime_seconds": 120,
    "manual_max_runtime_seconds": 30,
    "auto_watering_enabled": True,
    "ha_enabled": False,
    "ha_base_url": "http://homeassistant.local:8123",
    "ha_token": "",
    "ha_switch_entity": "",
    "ha_light_entity": "",
}


@dataclass
class PumpState:
    running: bool = False
    mode: str = "idle"  # idle | auto | manual
    started_at: float = 0.0
    ends_at: float = 0.0
    stop_reason: str = ""


class BonsaiController:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.config = self._load_config()
        self._save_config(self.config)

        self.current_moisture: Optional[float] = None
        self.last_watered: Optional[str] = None
        self.last_water_time: float = 0.0

        self.manual_toggle_on: bool = False
        self.pump = PumpState()

        self._shutdown = threading.Event()
        self._pump_stop_requested = threading.Event()

        self.sensor: Optional[Seesaw] = None
        self.display = None

        self._init_db()
        self._setup_gpio()
        self._setup_display()

    # ------------------------
    # Config / persistence
    # ------------------------
    def _load_config(self) -> dict:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(saved)
            return merged
        return DEFAULT_CONFIG.copy()

    def _save_config(self, config: dict) -> None:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    # ------------------------
    # Database
    # ------------------------
    def _init_db(self) -> None:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS moisture_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                moisture_percent REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS watering_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                moisture_before REAL,
                moisture_after REAL,
                mode TEXT NOT NULL,
                stop_reason TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _log_moisture(self, moisture_percent: float) -> None:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO moisture_readings (timestamp, moisture_percent) VALUES (?, ?)",
            (datetime.now().isoformat(), moisture_percent),
        )
        conn.commit()
        conn.close()

    def _log_watering(
        self,
        duration_seconds: float,
        moisture_before: Optional[float],
        moisture_after: Optional[float],
        mode: str,
        stop_reason: str,
    ) -> None:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO watering_events
            (timestamp, duration_seconds, moisture_before, moisture_after, mode, stop_reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                duration_seconds,
                moisture_before,
                moisture_after,
                mode,
                stop_reason,
            ),
        )
        conn.commit()
        conn.close()

    def get_recent_readings(self, hours: int = 48) -> list[dict]:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        c.execute(
            "SELECT timestamp, moisture_percent FROM moisture_readings WHERE timestamp > ? ORDER BY timestamp",
            (cutoff,),
        )
        rows = c.fetchall()
        conn.close()
        return [{"timestamp": r[0], "moisture": r[1]} for r in rows]

    def get_recent_waterings(self, count: int = 20) -> list[dict]:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            """
            SELECT timestamp, duration_seconds, moisture_before, moisture_after, mode, stop_reason
            FROM watering_events
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (count,),
        )
        rows = c.fetchall()
        conn.close()
        return [
            {
                "timestamp": r[0],
                "duration": r[1],
                "before": r[2],
                "after": r[3],
                "mode": r[4],
                "stop_reason": r[5],
            }
            for r in rows
        ]

    # ------------------------
    # Hardware setup
    # ------------------------
    def _setup_gpio(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(RELAY_PIN, GPIO.OUT)
        GPIO.output(RELAY_PIN, PUMP_OFF_LEVEL)

    def _setup_display(self) -> None:
        try:
            import adafruit_ssd1306

            i2c = busio.I2C(board.SCL, board.SDA)
            for addr in (0x3C, 0x3D):
                try:
                    self.display = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=addr)
                    self.display.fill(0)
                    self.display.show()
                    print(f"[DISPLAY] OLED ready at 0x{addr:02X}")
                    return
                except Exception:
                    continue
            print("[DISPLAY] OLED not found (0x3C/0x3D). Continuing without display.")
        except Exception as exc:
            print(f"[DISPLAY] init failed: {exc}")

    def _get_sensor(self) -> Optional[Seesaw]:
        # Lazily create / recover sensor object if disconnected.
        if self.sensor is not None:
            return self.sensor
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self.sensor = Seesaw(i2c, addr=SOIL_SENSOR_ADDR)
            print("[SENSOR] Connected at 0x36")
            return self.sensor
        except Exception:
            return None

    # ------------------------
    # Sensor / display
    # ------------------------
    @staticmethod
    def _convert_moisture(raw: int) -> float:
        # Placeholder calibration; user should replace after calibration.
        dry_value = 600
        wet_value = 300
        pct = (dry_value - raw) / (dry_value - wet_value) * 100
        pct = max(0, min(100, pct))
        return round(pct, 1)

    def _read_moisture(self) -> Optional[float]:
        sensor = self._get_sensor()
        if sensor is None:
            return None
        try:
            raw = sensor.moisture_read()
            return self._convert_moisture(raw)
        except Exception:
            # Drop sensor handle; next cycle will retry connection.
            self.sensor = None
            return None

    def _update_display(self, status: str) -> None:
        if self.display is None:
            return
        try:
            from PIL import Image, ImageDraw, ImageFont

            moisture_text = "--" if self.current_moisture is None else f"{self.current_moisture}%"
            watered = self.last_watered if self.last_watered else "Never"
            auto_mode = "AUTO ON" if self.config.get("auto_watering_enabled", True) else "AUTO OFF"

            img = Image.new("1", (128, 64))
            d = ImageDraw.Draw(img)
            font = ImageFont.load_default()
            d.text((0, 0), "BONSAI HUB", font=font, fill=255)
            d.line((0, 12, 128, 12), fill=255)
            d.text((0, 16), f"Moist: {moisture_text}", font=font, fill=255)
            d.text((0, 28), f"State: {status}", font=font, fill=255)
            d.text((0, 40), f"{auto_mode}", font=font, fill=255)
            d.text((0, 52), f"Last: {watered}", font=font, fill=255)

            self.display.image(img)
            self.display.show()
        except Exception:
            pass

    # ------------------------
    # Pump control
    # ------------------------
    def _set_pump_output(self, on: bool) -> None:
        GPIO.output(RELAY_PIN, PUMP_ON_LEVEL if on else PUMP_OFF_LEVEL)

    def _pump_worker(self, mode: str, requested_seconds: int, moisture_before: Optional[float]) -> None:
        max_auto = int(self.config["pump_max_runtime_seconds"])
        max_manual = int(self.config["manual_max_runtime_seconds"])

        if mode == "manual":
            max_allowed = max_manual
        else:
            max_allowed = max_auto

        run_seconds = max(1, min(int(requested_seconds), max_allowed))

        with self.lock:
            self.pump.running = True
            self.pump.mode = mode
            self.pump.started_at = time.time()
            self.pump.ends_at = self.pump.started_at + run_seconds
            self.pump.stop_reason = ""

        self._set_pump_output(True)
        print(f"[PUMP] {mode.upper()} start, target {run_seconds}s")

        started = time.time()
        stop_reason = "completed"

        try:
            while not self._shutdown.is_set():
                if self._pump_stop_requested.is_set():
                    stop_reason = "manual_stop"
                    break
                if time.time() >= started + run_seconds:
                    # For manual mode this is the 30s hard cap by design.
                    stop_reason = "safety_timeout" if mode == "manual" else "completed"
                    break
                time.sleep(0.1)
        finally:
            self._set_pump_output(False)
            elapsed = round(time.time() - started, 1)

            moisture_after = self._read_moisture()
            self._log_watering(elapsed, moisture_before, moisture_after, mode, stop_reason)

            with self.lock:
                self.last_watered = datetime.now().strftime("%H:%M")
                self.last_water_time = time.time()
                self.pump.running = False
                self.pump.mode = "idle"
                self.pump.started_at = 0.0
                self.pump.ends_at = 0.0
                self.pump.stop_reason = stop_reason
                self.manual_toggle_on = False
                self._pump_stop_requested.clear()

            print(f"[PUMP] stop ({stop_reason}), ran {elapsed}s")

    def start_pump(self, mode: str, seconds: int) -> tuple[bool, str]:
        with self.lock:
            if self.pump.running:
                return False, "Pump already running"
            moisture_before = self.current_moisture

        t = threading.Thread(
            target=self._pump_worker,
            args=(mode, seconds, moisture_before),
            daemon=True,
        )
        t.start()
        return True, "Pump started"

    def stop_pump(self) -> None:
        self._pump_stop_requested.set()

    # ------------------------
    # Home Assistant API
    # ------------------------
    def _ha_request(self, method: str, path: str, payload: Optional[dict] = None) -> tuple[bool, dict]:
        with self.lock:
            base_url = str(self.config.get("ha_base_url", "")).strip().rstrip("/")
            token = str(self.config.get("ha_token", "")).strip()

        if not base_url:
            return False, {"error": "Home Assistant base URL is empty."}
        if not token:
            return False, {"error": "Home Assistant token is not set."}

        url = f"{base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = urlrequest.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlrequest.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8").strip()
                if not body:
                    return True, {}
                try:
                    return True, json.loads(body)
                except json.JSONDecodeError:
                    return True, {"raw": body}
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            msg = f"HTTP {exc.code}"
            if body:
                msg = f"{msg}: {body}"
            return False, {"error": msg}
        except Exception as exc:
            return False, {"error": str(exc)}

    def _ha_entity_state(self, entity_id: str) -> tuple[bool, str]:
        ok, data = self._ha_request("GET", f"/api/states/{entity_id}")
        if not ok:
            return False, data.get("error", "Request failed")
        state = data.get("state", "unknown")
        return True, str(state)

    def _ha_call_service(self, domain: str, service: str, entity_id: str) -> tuple[bool, str]:
        ok, data = self._ha_request(
            "POST",
            f"/api/services/{domain}/{service}",
            {"entity_id": entity_id},
        )
        if not ok:
            return False, data.get("error", "Request failed")
        return True, "OK"

    def get_ha_status(self) -> dict:
        with self.lock:
            enabled = bool(self.config.get("ha_enabled", False))
            base_url = str(self.config.get("ha_base_url", "")).strip()
            token_set = bool(str(self.config.get("ha_token", "")).strip())
            switch_entity = str(self.config.get("ha_switch_entity", "")).strip()
            light_entity = str(self.config.get("ha_light_entity", "")).strip()

        status = {
            "enabled": enabled,
            "base_url": base_url,
            "token_set": token_set,
            "switch_entity": switch_entity,
            "light_entity": light_entity,
            "connected": False,
            "message": "",
            "switch_state": "n/a",
            "light_state": "n/a",
        }

        if not enabled:
            status["message"] = "Home Assistant integration is disabled."
            return status
        if not base_url:
            status["message"] = "Set Home Assistant base URL."
            return status
        if not token_set:
            status["message"] = "Set Home Assistant long-lived token."
            return status

        ok, data = self._ha_request("GET", "/api/")
        if not ok:
            status["message"] = data.get("error", "Connection failed")
            return status

        status["connected"] = True
        status["message"] = "Connected"

        if switch_entity:
            s_ok, s_state = self._ha_entity_state(switch_entity)
            status["switch_state"] = s_state if s_ok else f"error ({s_state})"
        if light_entity:
            l_ok, l_state = self._ha_entity_state(light_entity)
            status["light_state"] = l_state if l_ok else f"error ({l_state})"

        return status

    def set_ha_switch(self, on: bool) -> tuple[bool, str]:
        with self.lock:
            entity_id = str(self.config.get("ha_switch_entity", "")).strip()
        if not entity_id:
            return False, "Set ha_switch_entity first."
        return self._ha_call_service("switch", "turn_on" if on else "turn_off", entity_id)

    def set_ha_light(self, on: bool) -> tuple[bool, str]:
        with self.lock:
            entity_id = str(self.config.get("ha_light_entity", "")).strip()
        if not entity_id:
            return False, "Set ha_light_entity first."
        return self._ha_call_service("light", "turn_on" if on else "turn_off", entity_id)

    # ------------------------
    # Main monitor loop
    # ------------------------
    def monitor_loop(self) -> None:
        print("[SYSTEM] monitor loop started")
        while not self._shutdown.is_set():
            with self.lock:
                cfg = dict(self.config)

            moisture = self._read_moisture()
            if moisture is not None:
                with self.lock:
                    self.current_moisture = moisture
                self._log_moisture(moisture)
                print(f"[SENSOR] Moisture: {moisture}%")

            # Determine status text for OLED
            status = "WAIT"
            with self.lock:
                m = self.current_moisture
                auto_enabled = bool(cfg.get("auto_watering_enabled", True))
                pump_running = self.pump.running

            if m is not None:
                if m < cfg["moisture_threshold_low"]:
                    status = "DRY"
                elif m > cfg["moisture_threshold_high"]:
                    status = "WET"
                else:
                    status = "OK"

            if pump_running:
                status = "PUMP"

            self._update_display(status)

            # Auto-watering gate
            if m is not None and auto_enabled:
                now = time.time()
                with self.lock:
                    interval_ok = (now - self.last_water_time) >= cfg["min_water_interval_seconds"]
                    idle = not self.pump.running
                needs_water = m < cfg["moisture_threshold_low"]
                if needs_water and interval_ok and idle:
                    self.start_pump("auto", int(cfg["watering_duration_seconds"]))

            # Moisture logging runs regardless of auto mode.
            sleep_seconds = max(30, int(cfg["sensor_read_interval_seconds"]))
            for _ in range(sleep_seconds):
                if self._shutdown.is_set():
                    break
                time.sleep(1)

    def shutdown(self) -> None:
        print("[SYSTEM] shutting down")
        self._shutdown.set()
        self.stop_pump()
        time.sleep(0.2)
        self._set_pump_output(False)
        GPIO.cleanup()
        if self.display:
            try:
                self.display.fill(0)
                self.display.show()
            except Exception:
                pass


def create_app(ctrl: BonsaiController):
    from flask import Flask, jsonify, render_template_string, request

    app = Flask(__name__)

    HUB_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pi Control Hub</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel2: #1f2937;
      --txt: #e5e7eb;
      --sub: #9ca3af;
      --good: #34d399;
      --warn: #fbbf24;
      --bad: #f87171;
      --btn: #2563eb;
    }
    body { margin: 0; font-family: Arial, sans-serif; background: radial-gradient(circle at 10% 0%, #1e293b, var(--bg)); color: var(--txt); }
    .wrap { max-width: 920px; margin: 0 auto; padding: 20px; }
    .title { font-size: 28px; margin: 8px 0 16px; }
    .card { background: linear-gradient(160deg, var(--panel), var(--panel2)); border: 1px solid #2f3a4d; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .kpi { font-size: 44px; font-weight: 700; color: var(--good); }
    .muted { color: var(--sub); }
    .btn { background: var(--btn); color: white; border: 0; border-radius: 8px; padding: 10px 14px; cursor: pointer; }
    .btn.danger { background: #dc2626; }
    .btn.gray { background: #4b5563; }
    input[type=number], input[type=text], input[type=password] { width: 110px; padding: 8px; border-radius: 8px; border: 1px solid #334155; background: #0b1220; color: var(--txt); }
    input.wide { width: min(100%, 420px); }
    .switch { width: 20px; height: 20px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
    .small { font-size: 13px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  </style>
</head>
<body>
<div class="wrap">
  <div class="title">Pi Control Hub</div>

  <div class="card">
    <div class="row" style="justify-content: space-between;">
      <div>
        <div class="muted">Bonsai Moisture</div>
        <div class="kpi" id="moisture">--</div>
        <div id="stateText" class="muted">Loading...</div>
      </div>
      <div>
        <div class="muted">Pump</div>
        <div id="pumpState" style="font-size:22px;font-weight:700;">OFF</div>
        <div id="pumpMeta" class="small muted"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <label><input id="autoToggle" class="switch" type="checkbox"> Auto watering enabled</label>
      <button class="btn" onclick="saveAuto()">Save Auto Mode</button>
      <span id="autoMsg" class="small muted"></span>
    </div>
    <div class="small muted" style="margin-top:8px;">Moisture logging stays ON even when auto watering is OFF.</div>
  </div>

  <div class="card">
    <div class="row">
      <label><input id="manualToggle" class="switch" type="checkbox" onchange="manualToggleChanged()"> Manual pump run</label>
      <span class="small muted">Stops on toggle-off or at 30s safety max.</span>
    </div>
    <div id="manualMsg" class="small muted" style="margin-top:8px;"></div>
  </div>

  <div class="card">
    <div class="grid">
      <div>
        <div class="small muted">Low threshold (%)</div>
        <input id="low" type="number" min="5" max="95">
      </div>
      <div>
        <div class="small muted">High threshold (%)</div>
        <input id="high" type="number" min="5" max="95">
      </div>
      <div>
        <div class="small muted">Auto run duration (s)</div>
        <input id="dur" type="number" min="1" max="300">
      </div>
      <div>
        <div class="small muted">Min interval (s)</div>
        <input id="intv" type="number" min="60" max="86400">
      </div>
      <div>
        <div class="small muted">Read interval (s)</div>
        <input id="readi" type="number" min="30" max="3600">
      </div>
    </div>
    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="saveSettings()">Save Settings</button>
      <span id="saveMsg" class="small muted"></span>
    </div>
  </div>

  <div class="card">
    <div style="font-weight:700; margin-bottom:8px;">Recent waterings</div>
    <div id="waterings" class="small mono muted">Loading...</div>
  </div>

  <div class="card">
    <div class="row" style="justify-content: space-between;">
      <div style="font-weight:700;">Home Assistant</div>
      <a id="haOpenLink" class="small" href="#" target="_blank" rel="noopener noreferrer">Open Home Assistant</a>
    </div>

    <div class="row" style="margin-top:8px;">
      <label><input id="haEnabled" class="switch" type="checkbox"> Enable HA integration</label>
      <span id="haConn" class="small muted">Not checked yet.</span>
    </div>

    <div class="grid" style="margin-top:10px;">
      <div>
        <div class="small muted">HA base URL</div>
        <input id="haBaseUrl" class="wide" type="text" placeholder="http://homeassistant.local:8123">
      </div>
      <div>
        <div class="small muted">Long-lived access token</div>
        <input id="haToken" class="wide" type="password" placeholder="Paste token (leave blank to keep current)">
      </div>
      <div>
        <div class="small muted">Smart plug entity</div>
        <input id="haSwitchEntity" class="wide" type="text" placeholder="switch.office_plug">
      </div>
      <div>
        <div class="small muted">Smart light entity</div>
        <input id="haLightEntity" class="wide" type="text" placeholder="light.desk_lamp">
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="saveHAConfig()">Save HA Settings</button>
      <button class="btn gray" onclick="refreshHAStatus()">Refresh HA</button>
      <span id="haSaveMsg" class="small muted"></span>
    </div>

    <div class="row" style="margin-top:12px;">
      <span class="small muted">Plug:</span>
      <button class="btn" onclick="setHASwitch(true)">ON</button>
      <button class="btn gray" onclick="setHASwitch(false)">OFF</button>
      <span id="haSwitchState" class="small muted">n/a</span>
    </div>

    <div class="row" style="margin-top:8px;">
      <span class="small muted">Light:</span>
      <button class="btn" onclick="setHALight(true)">ON</button>
      <button class="btn gray" onclick="setHALight(false)">OFF</button>
      <span id="haLightState" class="small muted">n/a</span>
    </div>
  </div>

  <div class="card small muted">
    Future modules can be added here (lighting, camera, environment, etc.) under the same app.
  </div>
</div>

<script>
async function api(path, opts={}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function statusText(st) {
  if (st.moisture === null) return 'Sensor not connected yet';
  if (st.pump.running) return 'Pump running';
  if (st.moisture < st.config.moisture_threshold_low) return 'Dry';
  if (st.moisture > st.config.moisture_threshold_high) return 'Wet';
  return 'OK';
}

async function refreshStatus() {
  const st = await api('/api/status');

  document.getElementById('moisture').textContent = st.moisture === null ? '--' : (st.moisture + '%');
  document.getElementById('stateText').textContent = statusText(st);

  document.getElementById('autoToggle').checked = !!st.config.auto_watering_enabled;
  document.getElementById('manualToggle').checked = !!st.manual_toggle_on;

  const pumpState = st.pump.running ? 'ON (' + st.pump.mode + ')' : 'OFF';
  document.getElementById('pumpState').textContent = pumpState;
  let meta = '';
  if (st.pump.running) {
    meta = 'Remaining: ' + st.pump.remaining_seconds + 's';
  } else if (st.pump.stop_reason) {
    meta = 'Last stop: ' + st.pump.stop_reason;
  }
  document.getElementById('pumpMeta').textContent = meta;

  document.getElementById('low').value = st.config.moisture_threshold_low;
  document.getElementById('high').value = st.config.moisture_threshold_high;
  document.getElementById('dur').value = st.config.watering_duration_seconds;
  document.getElementById('intv').value = st.config.min_water_interval_seconds;
  document.getElementById('readi').value = st.config.sensor_read_interval_seconds;
}

async function refreshWaterings() {
  const list = await api('/api/waterings?count=15');
  const el = document.getElementById('waterings');
  if (!list.length) {
    el.textContent = 'No waterings yet.';
    return;
  }
  el.innerHTML = list.map(w =>
    `${new Date(w.timestamp).toLocaleString()} | ${w.mode} | ${w.duration}s | ${w.before ?? '--'}% -> ${w.after ?? '--'}% | ${w.stop_reason || ''}`
  ).join('<br>');
}

async function saveAuto() {
  const enabled = document.getElementById('autoToggle').checked;
  await api('/api/auto_mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled})
  });
  document.getElementById('autoMsg').textContent = 'Saved';
  setTimeout(() => document.getElementById('autoMsg').textContent = '', 1500);
  await refreshStatus();
}

async function manualToggleChanged() {
  const enabled = document.getElementById('manualToggle').checked;
  const r = await api('/api/manual_toggle', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled})
  });
  document.getElementById('manualMsg').textContent = r.message;
  setTimeout(() => document.getElementById('manualMsg').textContent = '', 2500);
  await refreshStatus();
  await refreshWaterings();
}

async function saveSettings() {
  const cfg = {
    moisture_threshold_low: parseInt(document.getElementById('low').value, 10),
    moisture_threshold_high: parseInt(document.getElementById('high').value, 10),
    watering_duration_seconds: parseInt(document.getElementById('dur').value, 10),
    min_water_interval_seconds: parseInt(document.getElementById('intv').value, 10),
    sensor_read_interval_seconds: parseInt(document.getElementById('readi').value, 10),
  };
  await api('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(cfg)
  });
  document.getElementById('saveMsg').textContent = 'Saved';
  setTimeout(() => document.getElementById('saveMsg').textContent = '', 1500);
}

async function refreshHAStatus() {
  try {
    const st = await api('/api/ha/status');
    document.getElementById('haEnabled').checked = !!st.enabled;
    const setIfIdle = (id, value) => {
      const el = document.getElementById(id);
      if (document.activeElement !== el) el.value = value || '';
    };
    setIfIdle('haBaseUrl', st.base_url);
    setIfIdle('haSwitchEntity', st.switch_entity);
    setIfIdle('haLightEntity', st.light_entity);
    document.getElementById('haOpenLink').href = st.base_url || '#';
    document.getElementById('haConn').textContent = st.connected ? 'Connected' : st.message;
    document.getElementById('haSwitchState').textContent = 'State: ' + (st.switch_state || 'n/a');
    document.getElementById('haLightState').textContent = 'State: ' + (st.light_state || 'n/a');
  } catch (err) {
    document.getElementById('haConn').textContent = 'HA status error: ' + err.message;
  }
}

async function saveHAConfig() {
  const payload = {
    ha_enabled: document.getElementById('haEnabled').checked,
    ha_base_url: document.getElementById('haBaseUrl').value.trim(),
    ha_switch_entity: document.getElementById('haSwitchEntity').value.trim(),
    ha_light_entity: document.getElementById('haLightEntity').value.trim(),
  };
  const token = document.getElementById('haToken').value.trim();
  if (token) payload.ha_token = token;

  const r = await api('/api/ha/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });

  document.getElementById('haToken').value = '';
  document.getElementById('haSaveMsg').textContent = r.ok ? 'HA settings saved.' : 'Save failed.';
  setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  await refreshHAStatus();
}

async function setHASwitch(on) {
  const r = await api('/api/ha/switch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({on}),
  });
  document.getElementById('haSaveMsg').textContent = r.message || 'Done';
  setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  await refreshHAStatus();
}

async function setHALight(on) {
  const r = await api('/api/ha/light', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({on}),
  });
  document.getElementById('haSaveMsg').textContent = r.message || 'Done';
  setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  await refreshHAStatus();
}

(async function init() {
  await refreshStatus();
  await refreshWaterings();
  await refreshHAStatus();
  setInterval(refreshStatus, 1000);
  setInterval(refreshWaterings, 5000);
  setInterval(refreshHAStatus, 5000);
})();
</script>
</body>
</html>
"""

    @app.route("/")
    def home():
        return render_template_string(HUB_HTML)

    @app.route("/api/status")
    def api_status():
        with ctrl.lock:
            now = time.time()
            remaining = 0
            if ctrl.pump.running:
                remaining = max(0, int(round(ctrl.pump.ends_at - now)))
            return jsonify(
                {
                    "moisture": ctrl.current_moisture,
                    "last_watered": ctrl.last_watered,
                    "manual_toggle_on": ctrl.manual_toggle_on,
                    "config": ctrl.config,
                    "pump": {
                        "running": ctrl.pump.running,
                        "mode": ctrl.pump.mode,
                        "remaining_seconds": remaining,
                        "stop_reason": ctrl.pump.stop_reason,
                    },
                }
            )

    @app.route("/api/config", methods=["POST"])
    def api_config():
        payload = request.get_json(force=True)
        allowed = {
            "moisture_threshold_low",
            "moisture_threshold_high",
            "watering_duration_seconds",
            "min_water_interval_seconds",
            "sensor_read_interval_seconds",
            "pump_max_runtime_seconds",
            "manual_max_runtime_seconds",
            "auto_watering_enabled",
        }
        with ctrl.lock:
            for k, v in payload.items():
                if k in allowed:
                    ctrl.config[k] = v
            ctrl._save_config(ctrl.config)
        return jsonify({"ok": True, "config": ctrl.config})

    @app.route("/api/auto_mode", methods=["POST"])
    def api_auto_mode():
        payload = request.get_json(force=True)
        enabled = bool(payload.get("enabled", True))
        with ctrl.lock:
            ctrl.config["auto_watering_enabled"] = enabled
            ctrl._save_config(ctrl.config)
        return jsonify({"ok": True, "auto_watering_enabled": enabled})

    @app.route("/api/manual_toggle", methods=["POST"])
    def api_manual_toggle():
        payload = request.get_json(force=True)
        enabled = bool(payload.get("enabled", False))

        if enabled:
            with ctrl.lock:
                ctrl.manual_toggle_on = True
            ok, msg = ctrl.start_pump("manual", int(ctrl.config["manual_max_runtime_seconds"]))
            if not ok:
                with ctrl.lock:
                    ctrl.manual_toggle_on = False
                return jsonify({"ok": False, "message": msg}), 409
            return jsonify({"ok": True, "message": "Manual pump run started (max 30s)."})

        ctrl.stop_pump()
        with ctrl.lock:
            ctrl.manual_toggle_on = False
        return jsonify({"ok": True, "message": "Manual pump stop requested."})

    @app.route("/api/readings")
    def api_readings():
        hours = request.args.get("hours", 48, type=int)
        return jsonify(ctrl.get_recent_readings(hours))

    @app.route("/api/waterings")
    def api_waterings():
        count = request.args.get("count", 20, type=int)
        return jsonify(ctrl.get_recent_waterings(count))

    @app.route("/api/ha/status")
    def api_ha_status():
        return jsonify(ctrl.get_ha_status())

    @app.route("/api/ha/config", methods=["POST"])
    def api_ha_config():
        payload = request.get_json(force=True)
        with ctrl.lock:
            if "ha_enabled" in payload:
                ctrl.config["ha_enabled"] = bool(payload["ha_enabled"])
            if "ha_base_url" in payload:
                ctrl.config["ha_base_url"] = str(payload["ha_base_url"]).strip()
            if "ha_switch_entity" in payload:
                ctrl.config["ha_switch_entity"] = str(payload["ha_switch_entity"]).strip()
            if "ha_light_entity" in payload:
                ctrl.config["ha_light_entity"] = str(payload["ha_light_entity"]).strip()
            # Keep existing token when empty string is sent.
            if "ha_token" in payload and str(payload["ha_token"]).strip():
                ctrl.config["ha_token"] = str(payload["ha_token"]).strip()
            ctrl._save_config(ctrl.config)
        return jsonify({"ok": True, "ha_status": ctrl.get_ha_status()})

    @app.route("/api/ha/switch", methods=["POST"])
    def api_ha_switch():
        payload = request.get_json(force=True)
        on = bool(payload.get("on", False))
        ok, msg = ctrl.set_ha_switch(on)
        code = 200 if ok else 502
        return jsonify({"ok": ok, "message": msg, "ha_status": ctrl.get_ha_status()}), code

    @app.route("/api/ha/light", methods=["POST"])
    def api_ha_light():
        payload = request.get_json(force=True)
        on = bool(payload.get("on", False))
        ok, msg = ctrl.set_ha_light(on)
        code = 200 if ok else 502
        return jsonify({"ok": ok, "message": msg, "ha_status": ctrl.get_ha_status()}), code

    return app


def main() -> None:
    print("=" * 56)
    print(" BONSAI PI CONTROL HUB")
    print("=" * 56)

    ctrl = BonsaiController()

    monitor_thread = threading.Thread(target=ctrl.monitor_loop, daemon=True)
    monitor_thread.start()

    app = create_app(ctrl)

    try:
        print("[WEB] http://0.0.0.0:5000")
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.shutdown()
        print("[SYSTEM] stopped")


if __name__ == "__main__":
    main()
