import importlib.util
import shlex
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pi_hub", ROOT / "pi_hub.py")
pi_hub = importlib.util.module_from_spec(spec)
sys.modules["pi_hub"] = pi_hub
spec.loader.exec_module(pi_hub)


class HubUpdateRestartTests(TestCase):
    def test_auto_deploy_default_is_disabled(self):
        self.assertFalse(pi_hub.DEFAULT_HUB_UPDATE_CONFIG["auto_deploy"])

    def test_systemd_update_restart_does_not_spawn_second_hub_process(self):
        command = pi_hub.build_hub_restart_shell_command(
            app_dir="/opt/bonsai-water",
            python_cmd=shlex.quote("/usr/bin/python3"),
            script_cmd=shlex.quote("/opt/bonsai-water/pi_hub.py"),
            update_cmd="git pull --ff-only",
            systemd_managed=True,
        )
        self.assertIn("git pull --ff-only", command)
        self.assertNotIn("python3 -u", command)
        self.assertNotIn("pi_hub.py", command.split("git pull --ff-only", 1)[-1])

    def test_manual_restart_keeps_fallback_process_launch(self):
        command = pi_hub.build_hub_restart_shell_command(
            app_dir="/opt/bonsai-water",
            python_cmd=shlex.quote("/usr/bin/python3"),
            script_cmd=shlex.quote("/opt/bonsai-water/pi_hub.py"),
            update_cmd=None,
            systemd_managed=False,
        )
        self.assertIn("python3 -u", command)
        self.assertIn("pi_hub.py", command)
