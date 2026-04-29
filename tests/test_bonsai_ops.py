import importlib.util
import sys
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bonsai_ops", ROOT / "scripts" / "bonsai_ops.py")
bonsai_ops = importlib.util.module_from_spec(spec)
sys.modules["bonsai_ops"] = bonsai_ops
spec.loader.exec_module(bonsai_ops)


class BonsaiOpsTests(TestCase):
    def test_classifier_allowlist(self):
        self.assertEqual(bonsai_ops.classify_command("status"), "status")
        self.assertEqual(bonsai_ops.classify_command("/bonsai moisture"), "moisture")
        self.assertEqual(bonsai_ops.classify_command("reboot pi confirm"), "reboot_pi")
        self.assertEqual(bonsai_ops.classify_command("lights off"), "lights_off")
        self.assertEqual(bonsai_ops.classify_command("speakers on"), "speakers_on")
        self.assertEqual(bonsai_ops.classify_command("open shop"), "open_shop")
        self.assertEqual(bonsai_ops.classify_command("shop close"), "close_shop")
        self.assertEqual(bonsai_ops.classify_command("cool"), "palette_cool")
        self.assertEqual(bonsai_ops.classify_command("warm lights"), "palette_warm")
        self.assertEqual(bonsai_ops.classify_command("money"), "palette_money")
        self.assertEqual(bonsai_ops.classify_command("candle lamps"), "palette_candle")
        self.assertEqual(bonsai_ops.classify_command("pump off"), "pump_off")
        self.assertIsNone(bonsai_ops.classify_command("reboot pi"))
        self.assertIsNone(bonsai_ops.classify_command("run rm -rf /"))

    def test_unknown_returns_help_not_shell(self):
        msg = bonsai_ops.apply_command("uname -a")
        self.assertIn("Unknown Bonsai command", msg)
        self.assertIn("Project Bonsai commands", msg)

    def test_status_format_uses_api_data(self):
        responses = {
            "/api/hub/health": {"level": "ok", "message": "Connected"},
            "/api/bonsai/status": {"moisture": 78.5, "moisture_raw": 1001, "pump": {"running": False, "mode": "idle", "remaining_seconds": 0}, "config": {"auto_watering_enabled": False}, "last_watered": None},
            "/api/bonsai/waterings?count=30": [
                {
                    "timestamp": "2026-04-28T04:33:20.419030",
                    "duration": 20.0,
                    "before": 51.3,
                    "after": 55.3,
                    "mode": "auto",
                    "stop_reason": "target_reached",
                },
                {
                    "timestamp": "2026-04-28T04:28:59.342999",
                    "duration": 20.0,
                    "before": 50.7,
                    "after": 49.7,
                    "mode": "auto",
                    "stop_reason": "pulse_complete",
                },
                {
                    "timestamp": "2026-04-28T04:24:38.280923",
                    "duration": 20.0,
                    "before": 48.8,
                    "after": 45.6,
                    "mode": "auto",
                    "stop_reason": "pulse_complete",
                },
            ],
            "/api/ha/status": {
                "connected": True,
                "lamps": {"left": {"state": "on"}, "right": {"state": "on"}},
                "speakers": {"left": "on", "right": "on"},
                "lamp_palette_last": "cool",
            },
            "/api/pihole/status": {"connected": True, "blocking_enabled": True, "mode": "v6", "metrics": {}},
        }
        with mock.patch.object(bonsai_ops, "_json_request", side_effect=lambda path, *a, **kw: responses[path]):
            msg = bonsai_ops.compact_status()
        self.assertIn("Moisture: 78.5%", msg)
        self.assertIn("Auto watering: off", msg)
        self.assertIn("Last auto run: 2026-04-28 04:24–04:33 local, 3 pulses, 60s watered, moisture 48.8% → 55.3%, target_reached", msg)
        self.assertIn("Lights: left:on,right:on", msg)
        self.assertIn("Speakers: left:on,right:on", msg)
        self.assertIn("Palette: cool", msg)
        self.assertIn("Pi-hole", msg)

    def test_light_speaker_and_pump_controls_call_fixed_endpoints(self):
        calls = []

        def fake_request(path, method="GET", payload=None, timeout=None):
            calls.append((path, method, payload))
            if path == "/api/ha/lamps":
                return {"message": "lamps set", "ha_status": {"lamp_left_state": "off", "lamp_right_state": "off"}}
            if path == "/api/ha/speaker":
                return {"message": f"speaker {payload['side']} set", "ha_status": {"speaker_left_state": "off", "speaker_right_state": "off"}}
            if path == "/api/bonsai/manual_toggle":
                return {"message": "Manual pump stop requested."}
            if path == "/api/bonsai/status":
                return {"pump": {"running": False, "mode": "idle", "remaining_seconds": 0}, "config": {"manual_max_runtime_seconds": 30}}
            raise AssertionError(path)

        with mock.patch.object(bonsai_ops, "_json_request", side_effect=fake_request):
            lights_msg = bonsai_ops.apply_command("lights off")
            speakers_msg = bonsai_ops.apply_command("speakers off")
            pump_msg = bonsai_ops.apply_command("pump off")

        self.assertIn("Lights off", lights_msg)
        self.assertIn("Speakers off", speakers_msg)
        self.assertIn("Pump stop requested", pump_msg)
        self.assertIn(("/api/ha/lamps", "POST", {"on": False}), calls)
        self.assertIn(("/api/ha/speaker", "POST", {"side": "left", "on": False}), calls)
        self.assertIn(("/api/ha/speaker", "POST", {"side": "right", "on": False}), calls)
        self.assertIn(("/api/bonsai/manual_toggle", "POST", {"enabled": False}), calls)

    def test_open_and_close_shop_toggle_lights_and_speakers_together(self):
        calls = []

        def fake_request(path, method="GET", payload=None, timeout=None):
            calls.append((path, method, payload))
            state = "on" if payload and payload.get("on") else "off"
            return {
                "message": f"{path} {state}",
                "ha_status": {
                    "lamp_left_state": state,
                    "lamp_right_state": state,
                    "speaker_left_state": state,
                    "speaker_right_state": state,
                },
            }

        with mock.patch.object(bonsai_ops, "_json_request", side_effect=fake_request):
            open_msg = bonsai_ops.apply_command("open shop")
            close_msg = bonsai_ops.apply_command("close shop")

        self.assertIn("Shop opened", open_msg)
        self.assertIn("lights and speakers on", open_msg)
        self.assertIn("Shop closed", close_msg)
        self.assertIn("lights and speakers off", close_msg)
        self.assertEqual(calls[:3], [
            ("/api/ha/lamps", "POST", {"on": True}),
            ("/api/ha/speaker", "POST", {"side": "left", "on": True}),
            ("/api/ha/speaker", "POST", {"side": "right", "on": True}),
        ])
        self.assertEqual(calls[3:6], [
            ("/api/ha/lamps", "POST", {"on": False}),
            ("/api/ha/speaker", "POST", {"side": "left", "on": False}),
            ("/api/ha/speaker", "POST", {"side": "right", "on": False}),
        ])

    def test_open_shop_continues_if_one_device_fails(self):
        calls = []

        def fake_request(path, method="GET", payload=None, timeout=None):
            calls.append((path, method, payload))
            if path == "/api/ha/lamps":
                raise bonsai_ops.OpsError("lamp relay failed")
            return {"message": "speaker set", "ha_status": {"speaker_left_state": "on", "speaker_right_state": "on"}}

        with mock.patch.object(bonsai_ops, "_json_request", side_effect=fake_request):
            msg = bonsai_ops.apply_command("open shop")

        self.assertIn("Failures: lamps", msg)
        self.assertEqual(len(calls), 3)

    def test_palette_commands_call_fixed_lamp_palette_endpoint(self):
        calls = []

        def fake_request(path, method="GET", payload=None, timeout=None):
            calls.append((path, method, payload))
            return {
                "message": f"{payload['palette'].title()} palette applied to lamps.",
                "ha_status": {
                    "lamp_left_state": "on",
                    "lamp_right_state": "on",
                    "palette": payload["palette"],
                },
            }

        with mock.patch.object(bonsai_ops, "_json_request", side_effect=fake_request):
            cool_msg = bonsai_ops.apply_command("cool")
            warm_msg = bonsai_ops.apply_command("warm lights")
            money_msg = bonsai_ops.apply_command("money")
            candle_msg = bonsai_ops.apply_command("candle lamps")

        self.assertIn("Cool lights", cool_msg)
        self.assertIn("Warm lights", warm_msg)
        self.assertIn("Money lights", money_msg)
        self.assertIn("Candle lights", candle_msg)
        self.assertEqual(calls, [
            ("/api/ha/lamp_palette", "POST", {"palette": "cool"}),
            ("/api/ha/lamp_palette", "POST", {"palette": "warm"}),
            ("/api/ha/lamp_palette", "POST", {"palette": "money"}),
            ("/api/ha/lamp_palette", "POST", {"palette": "candle"}),
        ])

    def test_reboot_requires_configured_ssh_target(self):
        with mock.patch.object(bonsai_ops, "config_value", return_value=""):
            with self.assertRaises(bonsai_ops.OpsError):
                bonsai_ops.reboot_pi()

    def test_reboot_command_is_fixed_allowlist(self):
        vals = {"BONSAI_PI_SSH_TARGET": "pi@example", "BONSAI_PI_SSH_EXTRA_ARGS": ""}
        with mock.patch.object(bonsai_ops, "config_value", side_effect=lambda k, d="": vals.get(k, d)):
            with mock.patch.object(bonsai_ops.subprocess, "run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = ""
                run.return_value.stderr = ""
                msg = bonsai_ops.reboot_pi()
        self.assertIn("accepted", msg)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-3:], ["pi@example", "sudo", "/sbin/reboot"])
