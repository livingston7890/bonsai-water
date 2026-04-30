import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bonsai_hub_watchdog", ROOT / "scripts" / "bonsai_hub_watchdog.py")
bonsai_hub_watchdog = importlib.util.module_from_spec(spec)
sys.modules["bonsai_hub_watchdog"] = bonsai_hub_watchdog
spec.loader.exec_module(bonsai_hub_watchdog)


class BonsaiHubWatchdogAlertStateTests(TestCase):
    def setUp(self):
        self.temp_dir_handle = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_handle.name)
        self.state_file = self.temp_dir / "watchdog_state.json"
        self.sent = []
        self.original_state_file = bonsai_hub_watchdog.STATE_FILE
        self.original_check_hub = bonsai_hub_watchdog.check_hub
        self.original_send_telegram = bonsai_hub_watchdog.send_telegram
        self.original_collect_diagnostics = bonsai_hub_watchdog.collect_diagnostics
        self.original_maybe_ssh_restart = bonsai_hub_watchdog.maybe_ssh_restart
        self.original_cfg = bonsai_hub_watchdog.cfg
        bonsai_hub_watchdog.STATE_FILE = self.state_file
        bonsai_hub_watchdog.send_telegram = self.sent.append
        bonsai_hub_watchdog.collect_diagnostics = lambda: "diagnostics"
        bonsai_hub_watchdog.maybe_ssh_restart = lambda: "ssh restart disabled"
        bonsai_hub_watchdog.cfg = lambda name, default="": {"BONSAI_WATCHDOG_FAILURE_THRESHOLD": "2"}.get(name, default)

    def tearDown(self):
        bonsai_hub_watchdog.STATE_FILE = self.original_state_file
        bonsai_hub_watchdog.check_hub = self.original_check_hub
        bonsai_hub_watchdog.send_telegram = self.original_send_telegram
        bonsai_hub_watchdog.collect_diagnostics = self.original_collect_diagnostics
        bonsai_hub_watchdog.maybe_ssh_restart = self.original_maybe_ssh_restart
        bonsai_hub_watchdog.cfg = self.original_cfg
        self.temp_dir_handle.cleanup()

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_transient_failure_below_threshold_does_not_send_recovery_alert(self):
        bonsai_hub_watchdog.check_hub = lambda: (False, "synthetic connection refused")
        self.assertEqual(bonsai_hub_watchdog.main(), 1)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state()["alert_sent"])

        bonsai_hub_watchdog.check_hub = lambda: (True, "synthetic ok")
        self.assertEqual(bonsai_hub_watchdog.main(), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state()["alert_sent"])

    def test_recovery_alert_is_sent_after_matching_down_alert(self):
        bonsai_hub_watchdog.check_hub = lambda: (False, "synthetic timeout")
        self.assertEqual(bonsai_hub_watchdog.main(), 1)
        self.assertEqual(self.sent, [])

        self.assertEqual(bonsai_hub_watchdog.main(), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Project Bonsai hub is down", self.sent[0])
        self.assertTrue(self.state()["alert_sent"])

        bonsai_hub_watchdog.check_hub = lambda: (True, "synthetic ok")
        self.assertEqual(bonsai_hub_watchdog.main(), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Project Bonsai hub recovered", self.sent[1])
        self.assertFalse(self.state()["alert_sent"])
