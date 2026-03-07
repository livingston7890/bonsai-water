from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

try:
    import board
    import busio
    import RPi.GPIO as GPIO
    from adafruit_seesaw.seesaw import Seesaw
except Exception:
    board = None
    busio = None
    GPIO = None
    Seesaw = None


# Relay HAT silk labels are wiringPi-style (P25/P24/P23).
# CH1 jumper on P25 maps to BCM GPIO26.
RELAY_PIN = 26
PUMP_ON_LEVEL = GPIO.LOW if GPIO else 0
PUMP_OFF_LEVEL = GPIO.HIGH if GPIO else 1
SOIL_SENSOR_ADDR = 0x36

DEFAULT_CONFIG = {
    "moisture_threshold_low": 35,
    "moisture_threshold_high": 65,
    "moisture_raw_dry": 300,
    "moisture_raw_wet": 1000,
    "moisture_sample_count": 7,
    "moisture_sample_delay_ms": 40,
    "moisture_ema_alpha": 0.35,
    "watering_duration_seconds": 60,
    "auto_pulse_seconds": 15,
    "auto_soak_seconds": 90,
    "min_water_interval_seconds": 28800,
    "sensor_read_interval_seconds": 300,
    "pump_max_runtime_seconds": 120,
    "manual_max_runtime_seconds": 30,
    "auto_watering_enabled": True,
    "office_hours_enabled": False,
    "office_hours_start_hour": 17,
    "office_hours_end_hour": 2,
    "oled_enabled": True,
}


@dataclass
class PumpState:
    running: bool = False
    mode: str = "idle"
    started_at: float = 0.0
    ends_at: float = 0.0
    stop_reason: str = ""


class BonsaiPlugin:
    plugin_id = "bonsai"
    display_name = "Bonsai"

    def __init__(self, app_dir: str) -> None:
        self.app_dir = app_dir
        self.config_file = os.path.join(app_dir, "bonsai_config.json")
        self.db_file = os.path.join(app_dir, "bonsai_data.db")

        self.lock = threading.RLock()
        self.config = self._load_config()
        self._save_config(self.config)

        self.current_moisture: Optional[float] = None
        self.current_moisture_raw: Optional[int] = None
        self.last_watered: Optional[str] = None
        self.last_water_time: float = 0.0

        self.manual_toggle_on: bool = False
        self.pump = PumpState()

        self._shutdown = threading.Event()
        self._pump_stop_requested = threading.Event()

        self.monitor_thread: Optional[threading.Thread] = None
        self.pump_thread: Optional[threading.Thread] = None
        self.sensor: Optional[object] = None
        self.display = None
        self.gpio_ready = False

        self._init_db()
        self._setup_gpio()
        self._setup_display()

    def _load_config(self) -> dict:
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(saved)
            return merged
        return DEFAULT_CONFIG.copy()

    def _save_config(self, config: dict) -> None:
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_file)
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
        self._migrate_db(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        c = conn.cursor()
        c.execute(f"PRAGMA table_info({table})")
        existing = {str(row[1]) for row in c.fetchall()}
        if column in existing:
            return
        c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def _migrate_db(self, conn: sqlite3.Connection) -> None:
        # Backward compatibility for older watering_events schema.
        self._ensure_column(conn, "watering_events", "duration_seconds", "duration_seconds REAL NOT NULL DEFAULT 0")
        self._ensure_column(conn, "watering_events", "moisture_before", "moisture_before REAL")
        self._ensure_column(conn, "watering_events", "moisture_after", "moisture_after REAL")
        self._ensure_column(conn, "watering_events", "mode", "mode TEXT NOT NULL DEFAULT 'manual'")
        self._ensure_column(conn, "watering_events", "stop_reason", "stop_reason TEXT")

    def _log_moisture(self, moisture_percent: float) -> None:
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
        try:
            self._migrate_db(conn)
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
        except Exception as exc:
            print(f"[BONSAI] Failed to log watering event: {exc}")
        finally:
            conn.close()

    def get_recent_readings(self, hours: int = 48) -> list[dict]:
        conn = sqlite3.connect(self.db_file)
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
        conn = sqlite3.connect(self.db_file)
        try:
            self._migrate_db(conn)
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
        except Exception as exc:
            print(f"[BONSAI] Failed to fetch watering events: {exc}")
            rows = []
        finally:
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

    def _reset_pump_state(self, stop_reason: str = "") -> None:
        with self.lock:
            self.pump.running = False
            self.pump.mode = "idle"
            self.pump.started_at = 0.0
            self.pump.ends_at = 0.0
            self.pump.stop_reason = stop_reason
            self.manual_toggle_on = False
            self._pump_stop_requested.clear()

    def _reconcile_pump_worker_state(self) -> None:
        with self.lock:
            running = bool(self.pump.running)
            thread = self.pump_thread

        if running and thread is not None and not thread.is_alive():
            print("[BONSAI] Pump worker was not alive while marked running; resetting state.")
            self._set_pump_output(False)
            self._reset_pump_state("worker_reset")

    def _setup_gpio(self) -> None:
        if GPIO is None:
            print("[BONSAI] GPIO unavailable; pump control disabled.")
            self.gpio_ready = False
            return
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(RELAY_PIN, GPIO.OUT)
            GPIO.output(RELAY_PIN, PUMP_OFF_LEVEL)
            self.gpio_ready = True
        except Exception as exc:
            print(f"[BONSAI] GPIO setup failed: {exc}")
            self.gpio_ready = False

    def _setup_display(self) -> None:
        if board is None or busio is None:
            return
        try:
            import adafruit_ssd1306

            i2c = busio.I2C(board.SCL, board.SDA)
            for addr in (0x3C, 0x3D):
                try:
                    self.display = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=addr)
                    self.display.fill(0)
                    self.display.show()
                    print(f"[BONSAI] OLED ready at 0x{addr:02X}")
                    return
                except Exception:
                    continue
            print("[BONSAI] OLED not found (0x3C/0x3D); continuing without display.")
        except Exception as exc:
            print(f"[BONSAI] OLED init failed: {exc}")

    def _get_sensor(self) -> Optional[object]:
        if self.sensor is not None:
            return self.sensor
        if board is None or busio is None or Seesaw is None:
            return None
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            self.sensor = Seesaw(i2c, addr=SOIL_SENSOR_ADDR)
            print("[BONSAI] Sensor connected at 0x36")
            return self.sensor
        except Exception:
            return None

    def _convert_moisture(self, raw: int) -> float:
        dry_value = float(self.config.get("moisture_raw_dry", 300))
        wet_value = float(self.config.get("moisture_raw_wet", 1000))
        if wet_value == dry_value:
            return 0.0

        # Support either orientation:
        # - Seesaw defaults: larger raw value = wetter
        # - Some sensors: smaller raw value = wetter
        if wet_value > dry_value:
            pct = (float(raw) - dry_value) / (wet_value - dry_value) * 100.0
        else:
            pct = (dry_value - float(raw)) / (dry_value - wet_value) * 100.0
        pct = max(0, min(100, pct))
        return round(pct, 1)

    @staticmethod
    def _trimmed_average(values: list[int]) -> int:
        if not values:
            raise ValueError("No values to average.")
        ordered = sorted(int(v) for v in values)
        if len(ordered) >= 5:
            ordered = ordered[1:-1]
        return int(round(sum(ordered) / len(ordered)))

    def _read_moisture(self, use_smoothing: bool = True) -> Optional[float]:
        sensor = self._get_sensor()
        if sensor is None:
            return None
        try:
            sample_count = max(1, min(25, int(self.config.get("moisture_sample_count", 7))))
            sample_delay = max(0.0, min(0.3, float(self.config.get("moisture_sample_delay_ms", 40)) / 1000.0))
            samples: list[int] = []
            for idx in range(sample_count):
                try:
                    samples.append(int(sensor.moisture_read()))
                except Exception:
                    # Keep best-effort sample set; if none collected we surface failure below.
                    pass
                if idx + 1 < sample_count and sample_delay > 0:
                    time.sleep(sample_delay)

            if not samples:
                raise RuntimeError("No moisture samples received.")

            raw_filtered = self._trimmed_average(samples)
            self.current_moisture_raw = raw_filtered
            measured_pct = self._convert_moisture(raw_filtered)

            if not use_smoothing:
                return measured_pct

            alpha = max(0.05, min(1.0, float(self.config.get("moisture_ema_alpha", 0.35))))
            with self.lock:
                previous = self.current_moisture

            if previous is None:
                return measured_pct

            delta = abs(measured_pct - previous)
            if delta >= 20:
                alpha = max(alpha, 0.8)
            elif delta >= 10:
                alpha = max(alpha, 0.6)

            smoothed = previous + (measured_pct - previous) * alpha
            return round(smoothed, 1)
        except Exception:
            self.sensor = None
            self.current_moisture_raw = None
            return None

    def _record_moisture_sample(self, moisture: Optional[float]) -> None:
        if moisture is None:
            return
        with self.lock:
            self.current_moisture = moisture
        self._log_moisture(moisture)

    def _update_display(self, status: str) -> None:
        if self.display is None:
            return
        if not bool(self.config.get("oled_enabled", True)):
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

    def set_oled_enabled(self, enabled: bool) -> tuple[bool, str]:
        with self.lock:
            self.config["oled_enabled"] = bool(enabled)
            self._save_config(self.config)

        if self.display is None:
            return True, "OLED setting saved. No OLED detected right now."

        try:
            if enabled:
                self._update_display("WAIT")
                return True, "OLED turned ON."
            self.display.fill(0)
            self.display.show()
            return True, "OLED turned OFF."
        except Exception as exc:
            return False, f"OLED update failed: {exc}"

    def _set_pump_output(self, on: bool) -> None:
        if not self.gpio_ready or GPIO is None:
            return
        GPIO.output(RELAY_PIN, PUMP_ON_LEVEL if on else PUMP_OFF_LEVEL)

    def _pump_worker(self, mode: str, requested_seconds: int, moisture_before: Optional[float]) -> None:
        max_auto = int(self.config["pump_max_runtime_seconds"])
        max_manual = int(self.config["manual_max_runtime_seconds"])
        max_allowed = max_manual if mode == "manual" else max_auto
        run_seconds = max(1, min(int(requested_seconds), max_allowed))

        with self.lock:
            self.pump.running = True
            self.pump.mode = mode
            self.pump.started_at = time.time()
            self.pump.ends_at = self.pump.started_at + run_seconds
            self.pump.stop_reason = ""

        started = time.time()
        stop_reason = "completed"

        try:
            self._set_pump_output(True)
            print(f"[BONSAI] Pump {mode.upper()} start, target {run_seconds}s")
            while not self._shutdown.is_set():
                if self._pump_stop_requested.is_set():
                    stop_reason = "manual_stop"
                    break
                if time.time() >= started + run_seconds:
                    stop_reason = "safety_timeout" if mode == "manual" else "completed"
                    break
                time.sleep(0.1)
        except Exception as exc:
            stop_reason = f"error:{exc.__class__.__name__}"
            print(f"[BONSAI] Pump worker error: {exc}")
        finally:
            try:
                self._set_pump_output(False)
            except Exception as exc:
                print(f"[BONSAI] Pump output reset failed: {exc}")
            elapsed = round(time.time() - started, 1)
            moisture_after = self._read_moisture(use_smoothing=False)
            self._record_moisture_sample(moisture_after)
            self._log_watering(elapsed, moisture_before, moisture_after, mode, stop_reason)
            with self.lock:
                self.last_watered = datetime.now().strftime("%H:%M")
                self.last_water_time = time.time()
            self._reset_pump_state(stop_reason)

            print(f"[BONSAI] Pump stop ({stop_reason}), ran {elapsed}s")

    def start_pump(self, mode: str, seconds: int) -> tuple[bool, str]:
        if not self.gpio_ready:
            return False, "GPIO not ready; pump control unavailable."

        self._reconcile_pump_worker_state()
        with self.lock:
            if self.pump.running:
                return False, "Pump already running"
            moisture_before = self.current_moisture
            self._pump_stop_requested.clear()

        t = threading.Thread(
            target=self._pump_worker,
            args=(mode, seconds, moisture_before),
            daemon=True,
        )
        with self.lock:
            self.pump_thread = t
        t.start()
        return True, "Pump started"

    def start_auto_cycle(self, total_budget_seconds: int) -> tuple[bool, str]:
        if not self.gpio_ready:
            return False, "GPIO not ready; pump control unavailable."

        self._reconcile_pump_worker_state()
        with self.lock:
            if self.pump.running:
                return False, "Pump already running"
            moisture_before = self.current_moisture
            self._pump_stop_requested.clear()

        t = threading.Thread(
            target=self._auto_pulse_worker,
            args=(total_budget_seconds, moisture_before),
            daemon=True,
        )
        with self.lock:
            self.pump_thread = t
        t.start()
        return True, "Auto watering session started"

    def stop_pump(self) -> None:
        self._pump_stop_requested.set()

    def _sleep_interruptible(self, seconds: int) -> bool:
        deadline = time.time() + max(0, int(seconds))
        while not self._shutdown.is_set() and time.time() < deadline:
            if self._pump_stop_requested.is_set():
                return False
            time.sleep(0.1)
        return not self._shutdown.is_set()

    def _auto_pulse_worker(self, total_budget_seconds: int, moisture_before: Optional[float]) -> None:
        max_auto = int(self.config["pump_max_runtime_seconds"])
        total_budget = max(1, min(int(total_budget_seconds), max_auto))
        pulse_seconds_default = max(1, int(self.config.get("auto_pulse_seconds", 15)))
        soak_seconds = max(10, int(self.config.get("auto_soak_seconds", 90)))
        target_moisture = float(self.config.get("moisture_threshold_high", 65))

        with self.lock:
            self.pump.running = True
            self.pump.mode = "auto-pulse"
            self.pump.started_at = time.time()
            self.pump.ends_at = self.pump.started_at + min(total_budget, pulse_seconds_default)
            self.pump.stop_reason = ""

        remaining_budget = total_budget
        pulse_count = 0
        total_watered_seconds = 0.0
        final_reason = "session_budget_reached"
        last_moisture = moisture_before

        try:
            while remaining_budget > 0 and not self._shutdown.is_set():
                if self._pump_stop_requested.is_set():
                    final_reason = "manual_stop"
                    break
                if self._is_office_hours_blocked():
                    final_reason = "office_hours_blocked"
                    break
                if last_moisture is not None and last_moisture >= target_moisture:
                    final_reason = "target_reached"
                    break

                pulse_count += 1
                pulse_seconds = min(remaining_budget, pulse_seconds_default)

                with self.lock:
                    now = time.time()
                    self.pump.mode = f"auto-pulse {pulse_count}"
                    self.pump.ends_at = now + pulse_seconds
                    self.pump.stop_reason = ""

                pulse_started = time.time()
                pulse_reason = "pulse_complete"

                try:
                    self._set_pump_output(True)
                    print(
                        f"[BONSAI] Auto pulse {pulse_count} start, "
                        f"{pulse_seconds}s (remaining budget before pulse: {remaining_budget}s)"
                    )
                    while not self._shutdown.is_set():
                        if self._pump_stop_requested.is_set():
                            pulse_reason = "manual_stop"
                            final_reason = "manual_stop"
                            break
                        if time.time() >= pulse_started + pulse_seconds:
                            break
                        time.sleep(0.1)
                finally:
                    try:
                        self._set_pump_output(False)
                    except Exception as exc:
                        print(f"[BONSAI] Pump output reset failed: {exc}")

                elapsed = round(time.time() - pulse_started, 1)
                remaining_budget = max(0, remaining_budget - pulse_seconds)
                total_watered_seconds += elapsed
                moisture_after_pulse = self._read_moisture(use_smoothing=False)
                self._record_moisture_sample(moisture_after_pulse)

                if moisture_after_pulse is None:
                    pulse_reason = "sensor_read_failed"
                    final_reason = "sensor_read_failed"
                elif moisture_after_pulse >= target_moisture:
                    pulse_reason = "target_reached"
                    final_reason = "target_reached"
                elif remaining_budget <= 0:
                    pulse_reason = "session_budget_reached"
                    final_reason = "session_budget_reached"

                self._log_watering(elapsed, last_moisture, moisture_after_pulse, "auto", pulse_reason)
                if moisture_after_pulse is not None:
                    last_moisture = moisture_after_pulse

                if final_reason != "session_budget_reached" or remaining_budget <= 0:
                    if final_reason == "session_budget_reached" and remaining_budget <= 0:
                        break
                    if final_reason != "session_budget_reached":
                        break

                if remaining_budget <= 0:
                    break

                with self.lock:
                    now = time.time()
                    self.pump.mode = f"auto-soak {pulse_count}"
                    self.pump.ends_at = now + soak_seconds

                print(f"[BONSAI] Auto soak {pulse_count} for {soak_seconds}s")
                if not self._sleep_interruptible(soak_seconds):
                    final_reason = "manual_stop" if self._pump_stop_requested.is_set() else "shutdown"
                    break

                moisture_after_soak = self._read_moisture(use_smoothing=False)
                self._record_moisture_sample(moisture_after_soak)
                if moisture_after_soak is None:
                    final_reason = "sensor_read_failed"
                    break
                last_moisture = moisture_after_soak
                if moisture_after_soak >= target_moisture:
                    final_reason = "target_reached"
                    break
                if self._is_office_hours_blocked():
                    final_reason = "office_hours_blocked"
                    break
        except Exception as exc:
            final_reason = f"error:{exc.__class__.__name__}"
            print(f"[BONSAI] Auto pulse worker error: {exc}")
        finally:
            try:
                self._set_pump_output(False)
            except Exception as exc:
                print(f"[BONSAI] Pump output reset failed: {exc}")
            if total_watered_seconds > 0:
                with self.lock:
                    self.last_watered = datetime.now().strftime("%H:%M")
                    self.last_water_time = time.time()
            self._reset_pump_state(final_reason)
            print(f"[BONSAI] Auto pulse session stop ({final_reason})")

    def _is_office_hours_blocked(self, at: Optional[datetime] = None) -> bool:
        with self.lock:
            enabled = bool(self.config.get("office_hours_enabled", False))
            try:
                start_hour = int(self.config.get("office_hours_start_hour", 17)) % 24
            except Exception:
                start_hour = 17
            try:
                end_hour = int(self.config.get("office_hours_end_hour", 2)) % 24
            except Exception:
                end_hour = 2

        if not enabled:
            return False
        if start_hour == end_hour:
            return True

        now = at or datetime.now()
        hour_value = now.hour + (now.minute / 60.0) + (now.second / 3600.0)
        if start_hour < end_hour:
            return start_hour <= hour_value < end_hour
        return hour_value >= start_hour or hour_value < end_hour

    def monitor_loop(self) -> None:
        print("[BONSAI] monitor loop started")
        while not self._shutdown.is_set():
            with self.lock:
                cfg = dict(self.config)

            moisture = self._read_moisture()
            if moisture is not None:
                self._record_moisture_sample(moisture)
                print(f"[BONSAI] Moisture: {moisture}%")

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

            if m is not None and auto_enabled:
                now = time.time()
                with self.lock:
                    interval_ok = (now - self.last_water_time) >= cfg["min_water_interval_seconds"]
                    idle = not self.pump.running
                needs_water = m < cfg["moisture_threshold_low"]
                blocked_by_hours = self._is_office_hours_blocked()
                if needs_water and interval_ok and idle and not blocked_by_hours:
                    self.start_auto_cycle(int(cfg["watering_duration_seconds"]))

            sleep_seconds = max(30, int(cfg["sensor_read_interval_seconds"]))
            for _ in range(sleep_seconds):
                if self._shutdown.is_set():
                    break
                time.sleep(1)

    def start(self) -> None:
        if self.monitor_thread and self.monitor_thread.is_alive():
            return
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

    def shutdown(self) -> None:
        print("[BONSAI] shutting down")
        self._shutdown.set()
        self.stop_pump()
        time.sleep(0.2)
        self._set_pump_output(False)

        if GPIO is not None and self.gpio_ready:
            try:
                GPIO.cleanup()
            except Exception:
                pass

        if self.display:
            try:
                self.display.fill(0)
                self.display.show()
            except Exception:
                pass

    def register_routes(self, app) -> None:
        from flask import jsonify, request

        @app.route("/api/bonsai/status")
        def bonsai_api_status():
            self._reconcile_pump_worker_state()
            with self.lock:
                now = time.time()
                remaining = 0
                if self.pump.running:
                    remaining = max(0, int(round(self.pump.ends_at - now)))
                return jsonify(
                    {
                        "moisture": self.current_moisture,
                        "moisture_raw": self.current_moisture_raw,
                        "last_watered": self.last_watered,
                        "manual_toggle_on": self.manual_toggle_on,
                        "config": self.config,
                        "gpio_ready": self.gpio_ready,
                        "display_ready": self.display is not None,
                        "oled_enabled": bool(self.config.get("oled_enabled", True)),
                        "office_hours_blocking_now": self._is_office_hours_blocked(),
                        "pump": {
                            "running": self.pump.running,
                            "mode": self.pump.mode,
                            "remaining_seconds": remaining,
                            "stop_reason": self.pump.stop_reason,
                        },
                    }
                )

        @app.route("/api/bonsai/config", methods=["POST"])
        def bonsai_api_config():
            payload = request.get_json(force=True)
            allowed = {
                "moisture_threshold_low",
                "moisture_threshold_high",
                "moisture_raw_dry",
                "moisture_raw_wet",
                "moisture_sample_count",
                "moisture_sample_delay_ms",
                "moisture_ema_alpha",
                "watering_duration_seconds",
                "auto_pulse_seconds",
                "auto_soak_seconds",
                "min_water_interval_seconds",
                "sensor_read_interval_seconds",
                "pump_max_runtime_seconds",
                "manual_max_runtime_seconds",
                "auto_watering_enabled",
                "office_hours_enabled",
                "office_hours_start_hour",
                "office_hours_end_hour",
            }
            with self.lock:
                for key, value in payload.items():
                    if key in allowed:
                        self.config[key] = value
                self._save_config(self.config)
            return jsonify({"ok": True, "config": self.config})

        @app.route("/api/bonsai/auto_mode", methods=["POST"])
        def bonsai_api_auto_mode():
            payload = request.get_json(force=True)
            enabled = bool(payload.get("enabled", True))
            with self.lock:
                self.config["auto_watering_enabled"] = enabled
                self._save_config(self.config)
            return jsonify({"ok": True, "auto_watering_enabled": enabled})

        @app.route("/api/bonsai/office_hours", methods=["POST"])
        def bonsai_api_office_hours():
            payload = request.get_json(force=True)
            enabled = bool(payload.get("enabled", False))
            with self.lock:
                self.config["office_hours_enabled"] = enabled
                self._save_config(self.config)
            return jsonify(
                {
                    "ok": True,
                    "office_hours_enabled": enabled,
                    "office_hours_blocking_now": self._is_office_hours_blocked(),
                    "window": "5PM to 2AM",
                }
            )

        @app.route("/api/bonsai/manual_toggle", methods=["POST"])
        def bonsai_api_manual_toggle():
            payload = request.get_json(force=True)
            enabled = bool(payload.get("enabled", False))

            if enabled:
                with self.lock:
                    pump_running = bool(self.pump.running)
                if pump_running:
                    self.stop_pump()
                    with self.lock:
                        self.manual_toggle_on = False
                    return jsonify({"ok": True, "message": "Pump stop requested."})
                with self.lock:
                    self.manual_toggle_on = True
                ok, message = self.start_pump("manual", int(self.config["manual_max_runtime_seconds"]))
                if not ok:
                    with self.lock:
                        self.manual_toggle_on = False
                    return jsonify({"ok": False, "message": message}), 409
                return jsonify({"ok": True, "message": "Manual pump run started (max 30s)."})

            self.stop_pump()
            with self.lock:
                self.manual_toggle_on = False
            return jsonify({"ok": True, "message": "Manual pump stop requested."})

        @app.route("/api/bonsai/read_now", methods=["POST"])
        def bonsai_api_read_now():
            moisture = self._read_moisture(use_smoothing=False)
            self._record_moisture_sample(moisture)
            if moisture is None:
                return jsonify({"ok": False, "message": "Moisture read failed. Check sensor wiring.", "moisture": None})
            return jsonify(
                {
                    "ok": True,
                    "message": f"Moisture refreshed: {moisture}%",
                    "moisture": moisture,
                    "moisture_raw": self.current_moisture_raw,
                }
            )

        @app.route("/api/bonsai/readings")
        def bonsai_api_readings():
            hours = request.args.get("hours", 48, type=int)
            return jsonify(self.get_recent_readings(hours))

        @app.route("/api/bonsai/oled", methods=["POST"])
        def bonsai_api_oled():
            payload = request.get_json(force=True)
            enabled = bool(payload.get("enabled", True))
            ok, message = self.set_oled_enabled(enabled)
            code = 200 if ok else 500
            return (
                jsonify(
                    {
                        "ok": ok,
                        "message": message,
                        "oled_enabled": bool(self.config.get("oled_enabled", True)),
                        "display_ready": self.display is not None,
                    }
                ),
                code,
            )

        @app.route("/api/bonsai/waterings")
        def bonsai_api_waterings():
            count = request.args.get("count", 20, type=int)
            return jsonify(self.get_recent_waterings(count))

    def dashboard_html(self) -> str:
        return """
  <div class="card">
    <div class="row" style="justify-content: space-between; align-items: flex-start;">
      <div>
        <div class="panel-title-row">
          <span class="material-symbols-rounded panel-title-icon">eco</span>
          <div class="panel-title" style="margin-bottom:0;">Bonsai Monitor</div>
        </div>
        <div class="panel-meta">Soil moisture sensing, pump safety, and OLED sync.</div>
        <div class="kpi" id="bonsaiMoisture">--</div>
        <div id="bonsaiStateText" class="small muted">Loading...</div>
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">bolt</span>Pump</div>
        <div id="bonsaiPumpState" style="font-size:22px;font-weight:800;">OFF</div>
        <div id="bonsaiPumpMeta" class="small muted"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="row">
      <span class="panel-title" style="margin-bottom:0;"><span class="material-symbols-rounded label-icon">water_drop</span>Auto Watering</span>
      <button id="bonsaiAutoBtn" class="btn control-btn" onclick="bonsaiToggleAuto()">Loading...</button>
      <span id="bonsaiAutoMsg" class="small muted"></span>
    </div>
    <div class="small muted" style="margin-top:8px;">Moisture logging stays ON even when auto watering is OFF.</div>
  </div>

  <div class="card">
    <div class="row">
      <span class="panel-title" style="margin-bottom:0;"><span class="material-symbols-rounded label-icon">play_circle</span>Manual Pump Run</span>
      <button id="bonsaiManualBtn" class="btn control-btn" onclick="bonsaiToggleManual()">Loading...</button>
      <span class="small muted">Stops any active run, including auto, or starts a manual run.</span>
    </div>
    <div id="bonsaiManualMsg" class="small muted" style="margin-top:8px;"></div>
  </div>

  <div class="card">
    <div class="row">
      <span class="panel-title" style="margin-bottom:0;"><span class="material-symbols-rounded label-icon">sensors</span>Moisture Read</span>
      <button id="bonsaiReadNowBtn" class="btn control-btn state-action" onclick="bonsaiReadNow()">READ NOW</button>
      <span id="bonsaiReadMsg" class="small muted"></span>
    </div>
    <div class="small muted" style="margin-top:8px;">Forces an immediate sample without waiting for the next interval.</div>
  </div>

  <div class="card">
    <div class="row">
      <span class="panel-title" style="margin-bottom:0;"><span class="material-symbols-rounded label-icon">view_in_ar</span>OLED Display</span>
      <button id="bonsaiOledBtn" class="btn control-btn" onclick="bonsaiToggleOled()">Loading...</button>
      <span id="bonsaiOledMsg" class="small muted"></span>
    </div>
    <div id="bonsaiOledState" class="small muted" style="margin-top:8px;">Checking OLED...</div>
  </div>

  <div class="card">
    <div class="panel-title"><span class="material-symbols-rounded label-icon">tune</span>Thresholds & Timing</div>
    <div class="grid">
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">south</span>Low threshold (%)</div>
        <input id="bonsaiLow" type="number" min="5" max="95">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">north</span>High threshold (%)</div>
        <input id="bonsaiHigh" type="number" min="5" max="95">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">timer</span>Auto run duration (s)</div>
        <input id="bonsaiDur" type="number" min="1" max="300">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">schedule</span>Min interval (s)</div>
        <input id="bonsaiIntv" type="number" min="60" max="86400">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">sensors</span>Read interval (s)</div>
        <input id="bonsaiReadi" type="number" min="30" max="3600">
      </div>
    </div>
    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="bonsaiSaveSettings()">Save Settings</button>
      <span id="bonsaiSaveMsg" class="small muted"></span>
    </div>
  </div>

  <div class="card">
    <div class="panel-title" style="margin-bottom:8px;"><span class="material-symbols-rounded label-icon">history</span>Recent Waterings</div>
    <div id="bonsaiWaterings" class="small mono muted">Loading...</div>
  </div>

  <div class="card">
    <div class="panel-title"><span class="material-symbols-rounded label-icon">schedule</span>Office Hours</div>
    <div class="small muted">Block auto watering during the selected time window when Office Hours is enabled.</div>
    <div class="grid" style="margin-top:12px;">
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">login</span>Start time</div>
        <div class="row">
          <select id="bonsaiOfficeStartHour">
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4">4</option>
            <option value="5">5</option>
            <option value="6">6</option>
            <option value="7">7</option>
            <option value="8">8</option>
            <option value="9">9</option>
            <option value="10">10</option>
            <option value="11">11</option>
            <option value="12">12</option>
          </select>
          <select id="bonsaiOfficeStartAmPm">
            <option value="AM">AM</option>
            <option value="PM">PM</option>
          </select>
        </div>
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">logout</span>End time</div>
        <div class="row">
          <select id="bonsaiOfficeEndHour">
            <option value="1">1</option>
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4">4</option>
            <option value="5">5</option>
            <option value="6">6</option>
            <option value="7">7</option>
            <option value="8">8</option>
            <option value="9">9</option>
            <option value="10">10</option>
            <option value="11">11</option>
            <option value="12">12</option>
          </select>
          <select id="bonsaiOfficeEndAmPm">
            <option value="AM">AM</option>
            <option value="PM">PM</option>
          </select>
        </div>
      </div>
    </div>
    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="bonsaiSaveOfficeHours()">Save Office Hours</button>
      <span id="bonsaiOfficeHoursMsg" class="small muted"></span>
    </div>
  </div>
"""

    def dashboard_js(self) -> str:
        return """
let bonsaiState = null;

function bonsaiStatusText(st) {
  if (!st.gpio_ready) return 'GPIO not ready';
  if (st.moisture === null) return 'Sensor not connected yet';
  if (st.pump.running) return 'Pump running';
  if (st.moisture < st.config.moisture_threshold_low) return 'Dry';
  if (st.moisture > st.config.moisture_threshold_high) return 'Wet';
  return 'OK';
}

function bonsaiHour24ToParts(hour24) {
  const normalized = ((Number(hour24) % 24) + 24) % 24;
  const ampm = normalized >= 12 ? 'PM' : 'AM';
  let hour12 = normalized % 12;
  if (hour12 === 0) hour12 = 12;
  return {hour: String(hour12), ampm};
}

function bonsaiHourPartsTo24(hour12, ampm) {
  const hour = Math.max(1, Math.min(12, parseInt(hour12, 10) || 12));
  const upper = String(ampm || 'AM').toUpperCase() === 'PM' ? 'PM' : 'AM';
  if (upper === 'AM') return hour === 12 ? 0 : hour;
  return hour === 12 ? 12 : hour + 12;
}

function bonsaiSetOfficeHoursSelectors(st) {
  const active = document.activeElement ? document.activeElement.id : '';
  const officeIds = new Set([
    'bonsaiOfficeStartHour',
    'bonsaiOfficeStartAmPm',
    'bonsaiOfficeEndHour',
    'bonsaiOfficeEndAmPm',
  ]);
  if (officeIds.has(active)) return;

  const start = bonsaiHour24ToParts(st.config.office_hours_start_hour);
  const end = bonsaiHour24ToParts(st.config.office_hours_end_hour);
  document.getElementById('bonsaiOfficeStartHour').value = start.hour;
  document.getElementById('bonsaiOfficeStartAmPm').value = start.ampm;
  document.getElementById('bonsaiOfficeEndHour').value = end.hour;
  document.getElementById('bonsaiOfficeEndAmPm').value = end.ampm;
}

async function bonsaiRefreshStatus() {
  const st = await api('/api/bonsai/status');
  bonsaiState = st;

  document.getElementById('bonsaiMoisture').textContent = st.moisture === null ? '--' : (st.moisture + '%');
  document.getElementById('bonsaiStateText').textContent = bonsaiStatusText(st);

  const pumpState = st.pump.running ? 'ON (' + st.pump.mode + ')' : 'OFF';
  document.getElementById('bonsaiPumpState').textContent = pumpState;
  let meta = '';
  if (st.pump.running) {
    meta = 'Remaining: ' + st.pump.remaining_seconds + 's';
  } else if (st.pump.stop_reason) {
    meta = 'Last stop: ' + st.pump.stop_reason;
  }
  document.getElementById('bonsaiPumpMeta').textContent = meta;

  document.getElementById('bonsaiLow').value = st.config.moisture_threshold_low;
  document.getElementById('bonsaiHigh').value = st.config.moisture_threshold_high;
  document.getElementById('bonsaiDur').value = st.config.watering_duration_seconds;
  document.getElementById('bonsaiIntv').value = st.config.min_water_interval_seconds;
  document.getElementById('bonsaiReadi').value = st.config.sensor_read_interval_seconds;
  bonsaiSetOfficeHoursSelectors(st);

  const oledEnabled = !!st.oled_enabled;
  const oledDetected = !!st.display_ready;
  document.getElementById('bonsaiOledState').textContent = oledDetected
    ? (oledEnabled ? 'OLED is ON.' : 'OLED is OFF.')
    : 'OLED not detected.';

  const autoOn = !!st.config.auto_watering_enabled;
  const autoBtn = document.getElementById('bonsaiAutoBtn');
  autoBtn.textContent = autoOn ? 'ON' : 'OFF';
  autoBtn.classList.toggle('state-on', autoOn);
  autoBtn.classList.toggle('state-off', !autoOn);

  const manualRunning = !!(st.pump && st.pump.running);
  const manualBtn = document.getElementById('bonsaiManualBtn');
  manualBtn.textContent = manualRunning ? 'STOP' : 'START';
  manualBtn.classList.toggle('state-danger', manualRunning);
  manualBtn.classList.toggle('state-action', !manualRunning);

  const oledBtn = document.getElementById('bonsaiOledBtn');
  oledBtn.textContent = oledEnabled ? 'ON' : 'OFF';
  oledBtn.classList.toggle('state-on', oledEnabled);
  oledBtn.classList.toggle('state-off', !oledEnabled);
}

async function bonsaiRefreshWaterings() {
  const list = await api('/api/bonsai/waterings?count=15');
  const el = document.getElementById('bonsaiWaterings');
  if (!list.length) {
    el.textContent = 'No waterings yet.';
    return;
  }
  el.innerHTML = list.map(w =>
    `${new Date(w.timestamp).toLocaleString()} | ${w.mode} | ${w.duration}s | ${w.before ?? '--'}% -> ${w.after ?? '--'}% | ${w.stop_reason || ''}`
  ).join('<br>');
}

async function bonsaiToggleAuto() {
  const current = !!(bonsaiState && bonsaiState.config && bonsaiState.config.auto_watering_enabled);
  const enabled = !current;
  await api('/api/bonsai/auto_mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled})
  });
  document.getElementById('bonsaiAutoMsg').textContent = enabled ? 'Auto watering ON' : 'Auto watering OFF';
  setTimeout(() => document.getElementById('bonsaiAutoMsg').textContent = '', 1500);
  await bonsaiRefreshStatus();
}

async function bonsaiToggleManual() {
  const running = !!(bonsaiState && bonsaiState.pump && bonsaiState.pump.running);
  const enabled = !running;
  const r = await api('/api/bonsai/manual_toggle', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled})
  });
  document.getElementById('bonsaiManualMsg').textContent = r.message;
  setTimeout(() => document.getElementById('bonsaiManualMsg').textContent = '', 2500);
  await bonsaiRefreshStatus();
  await bonsaiRefreshWaterings();
}

async function bonsaiReadNow() {
  try {
    const r = await api('/api/bonsai/read_now', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    document.getElementById('bonsaiReadMsg').textContent = r.message || 'Moisture refreshed.';
  } catch (err) {
    const raw = String((err && err.message) ? err.message : (err || 'Unknown error'));
    const msg = (raw.includes('404') || raw.includes('Not Found'))
      ? 'Read endpoint missing on running server. Update Modules and restart.'
      : ('Read failed: ' + raw.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim());
    document.getElementById('bonsaiReadMsg').textContent = msg;
  }
  setTimeout(() => document.getElementById('bonsaiReadMsg').textContent = '', 2200);
  await bonsaiRefreshStatus();
}

async function bonsaiSaveSettings() {
  const cfg = {
    moisture_threshold_low: parseInt(document.getElementById('bonsaiLow').value, 10),
    moisture_threshold_high: parseInt(document.getElementById('bonsaiHigh').value, 10),
    watering_duration_seconds: parseInt(document.getElementById('bonsaiDur').value, 10),
    min_water_interval_seconds: parseInt(document.getElementById('bonsaiIntv').value, 10),
    sensor_read_interval_seconds: parseInt(document.getElementById('bonsaiReadi').value, 10),
  };
  await api('/api/bonsai/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(cfg)
  });
  document.getElementById('bonsaiSaveMsg').textContent = 'Saved';
  setTimeout(() => document.getElementById('bonsaiSaveMsg').textContent = '', 1500);
}

async function bonsaiSaveOfficeHours() {
  const startHour24 = bonsaiHourPartsTo24(
    document.getElementById('bonsaiOfficeStartHour').value,
    document.getElementById('bonsaiOfficeStartAmPm').value
  );
  const endHour24 = bonsaiHourPartsTo24(
    document.getElementById('bonsaiOfficeEndHour').value,
    document.getElementById('bonsaiOfficeEndAmPm').value
  );

  await api('/api/bonsai/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      office_hours_start_hour: startHour24,
      office_hours_end_hour: endHour24,
    })
  });
  document.getElementById('bonsaiOfficeHoursMsg').textContent = 'Office Hours saved';
  setTimeout(() => document.getElementById('bonsaiOfficeHoursMsg').textContent = '', 1800);
  await bonsaiRefreshStatus();
}

async function bonsaiToggleOled() {
  const enabled = !((bonsaiState && bonsaiState.oled_enabled) ? true : false);
  const r = await api('/api/bonsai/oled', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled})
  });
  document.getElementById('bonsaiOledMsg').textContent = r.message || 'Saved';
  setTimeout(() => document.getElementById('bonsaiOledMsg').textContent = '', 2000);
  await bonsaiRefreshStatus();
}
"""

    def dashboard_init_js(self) -> str:
        return """
  await bonsaiRefreshStatus();
  await bonsaiRefreshWaterings();
  setInterval(bonsaiRefreshStatus, 1000);
  setInterval(bonsaiRefreshWaterings, 5000);
"""


def create_plugin(app_dir: str) -> BonsaiPlugin:
    return BonsaiPlugin(app_dir)
