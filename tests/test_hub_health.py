import importlib.util
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pi_hub", ROOT / "pi_hub.py")
pi_hub = importlib.util.module_from_spec(spec)
sys.modules["pi_hub"] = pi_hub
spec.loader.exec_module(pi_hub)


class HubHealthEndpointTests(TestCase):
    def test_root_health_endpoint_returns_json(self):
        app = pi_hub.create_app([])
        client = app.test_client()
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "bonsai-pi-hub")
        self.assertIn("build", data)
        self.assertIn("plugins", data)
        self.assertIn("timestamp", data)
