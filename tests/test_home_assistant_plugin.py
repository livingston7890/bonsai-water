import importlib.util
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("home_assistant_plugin", ROOT / "plugins" / "home_assistant_plugin.py")
ha_module = importlib.util.module_from_spec(spec)
sys.modules["home_assistant_plugin"] = ha_module
spec.loader.exec_module(ha_module)


class HomeAssistantLampControlTests(TestCase):
    def make_plugin(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plugin = ha_module.HomeAssistantPlugin(tmp.name)
        plugin.config.update({
            "ha_base_url": "http://ha.local:8123",
            "ha_token": "test-token",
            "ha_lamp_left_entity": "light.left",
            "ha_lamp_right_entity": "light.right",
            "ha_speaker_left_entity": "switch.left_speaker",
            "ha_speaker_right_entity": "switch.right_speaker",
            "ha_lamp_brightness_last": 50,
        })
        return plugin

    def test_set_lamps_sends_one_group_light_call(self):
        plugin = self.make_plugin()
        calls = []

        def fake_call(domain, service, entity_id, extra=None):
            calls.append((domain, service, entity_id, extra))
            return True, "OK"

        plugin._call_service = fake_call
        plugin._verify_light_result = lambda entity, **kwargs: (True, "OK")

        ok, message = plugin.set_lamps(False)

        self.assertTrue(ok)
        self.assertEqual(message, "Lamps updated.")
        self.assertEqual(calls, [
            ("light", "turn_off", ["light.left", "light.right"], {"transition": 0}),
        ])

    def test_palette_uses_same_payload_for_both_lamps_in_one_group_call(self):
        plugin = self.make_plugin()
        calls = []

        def fake_call(domain, service, entity_id, extra=None):
            calls.append((domain, service, entity_id, extra))
            return True, "OK"

        plugin._call_service = fake_call
        plugin._verify_light_result = lambda entity, **kwargs: (True, "OK")

        ok, message = plugin.set_lamp_palette("money")

        self.assertTrue(ok)
        self.assertIn("Money palette", message)
        self.assertEqual(len(calls), 1)
        domain, service, entity_id, extra = calls[0]
        self.assertEqual((domain, service), ("light", "turn_on"))
        self.assertEqual(entity_id, ["light.left", "light.right"])
        self.assertEqual(extra["rgb_color"], [70, 210, 95])
        self.assertEqual(extra["transition"], 0)

    def test_brightness_uses_one_group_light_call(self):
        plugin = self.make_plugin()
        calls = []
        plugin._call_service = lambda domain, service, entity_id, extra=None: (calls.append((domain, service, entity_id, extra)) or (True, "OK"))
        plugin._verify_light_result = lambda entity, **kwargs: (True, "OK")

        ok, message = plugin.set_lamp_brightness(33)

        self.assertTrue(ok)
        self.assertIn("33%", message)
        self.assertEqual(calls[0], (
            "light",
            "turn_on",
            ["light.left", "light.right"],
            {"brightness_pct": 33, "transition": 0},
        ))

    def test_removed_palette_last_is_hidden_from_status(self):
        plugin = self.make_plugin()
        plugin.config["ha_base_url"] = ""
        plugin.config["ha_lamp_palette_last"] = "golden_hour"

        self.assertEqual(plugin.get_status()["lamp_palette_last"], "")

    def test_set_speakers_sends_one_group_switch_call(self):
        plugin = self.make_plugin()
        calls = []
        plugin._call_service = lambda domain, service, entity_id, extra=None: (calls.append((domain, service, entity_id, extra)) or (True, "OK"))

        ok, _ = plugin.set_speakers(True)

        self.assertTrue(ok)
        self.assertEqual(calls, [
            ("switch", "turn_on", ["switch.left_speaker", "switch.right_speaker"], None),
        ])
    def test_split_palette_sends_different_payloads_to_left_and_right(self):
        plugin = self.make_plugin()
        calls = []

        def fake_call(domain, service, entity_id, extra=None):
            calls.append((domain, service, entity_id, extra))
            return True, "OK"

        plugin._call_service = fake_call
        plugin._verify_light_result = lambda entity, **kwargs: (True, "OK")

        ok, message = plugin.set_lamp_palette("ice_fire")

        self.assertTrue(ok)
        self.assertIn("Ice/Fire palette", message)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], ("light", "turn_on", "light.left"))
        self.assertEqual(calls[0][3]["rgb_color"], [80, 150, 255])
        self.assertEqual(calls[1][:3], ("light", "turn_on", "light.right"))
        self.assertEqual(calls[1][3]["rgb_color"], [255, 32, 18])

    def test_new_scene_palette_sends_different_payloads_to_left_and_right(self):
        plugin = self.make_plugin()
        calls = []

        def fake_call(domain, service, entity_id, extra=None):
            calls.append((domain, service, entity_id, extra))
            return True, "OK"

        plugin._call_service = fake_call
        plugin._verify_light_result = lambda entity, **kwargs: (True, "OK")

        ok, message = plugin.set_lamp_palette("miami vice")

        self.assertTrue(ok)
        self.assertIn("Miami Vice palette", message)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], ("light", "turn_on", "light.left"))
        self.assertEqual(calls[0][3]["rgb_color"], [255, 63, 164])
        self.assertEqual(calls[1][:3], ("light", "turn_on", "light.right"))
        self.assertEqual(calls[1][3]["rgb_color"], [0, 217, 255])

        for removed in ("golden hour", "jade temple"):
            calls.clear()
            ok, message = plugin.set_lamp_palette(removed)
            self.assertFalse(ok)
            self.assertIn("Palette must be one of", message)
            self.assertEqual(calls, [])

    def test_palette_catalog_ui_uses_compact_stacked_grid(self):
        html = self.make_plugin().dashboard_html()
        css = (ROOT / "static" / "delight.css").read_text()
        expected = {
            "miami_vice": "MIAMI",
            "tokyo_night": "TOKYO",
            "deep_ocean": "OCEAN",
        }
        for palette, label in expected.items():
            self.assertIn(palette, ha_module.LAMP_PALETTES)
            self.assertIn(f"haSetLampPalette('{palette}')", html)
            self.assertIn(label, html)
        for removed_palette, removed_label in {
            "golden_hour": "GOLDEN HOUR",
            "jade_temple": "JADE TEMPLE",
        }.items():
            self.assertNotIn(removed_palette, ha_module.LAMP_PALETTES)
            self.assertNotIn(f"haSetLampPalette('{removed_palette}')", html)
            self.assertNotIn(removed_label, html)
        expected_order = [
            "haPaletteCandle", "haPaletteCool", "haPaletteWarm", "haPaletteMoney",
            "haPaletteIceFire", "haPaletteAurora", "haPaletteEmberForest", "haPaletteCyberOrchid",
            "haPaletteMiamiVice", "haPaletteTokyoNight", "haPaletteDeepOcean", "haPaletteMoonGrove",
        ]
        self.assertEqual([html.index(item) for item in expected_order], sorted(html.index(item) for item in expected_order))

        hub_html = (ROOT / "pi_hub.py").read_text()
        hub_block = hub_html[hub_html.index('aria-label=\\"Lamp color presets\\"'):]
        hub_block = hub_block[:hub_block.index('</div>')]
        expected_head_order = [item.replace("haPalette", "headPalette") for item in expected_order]
        self.assertEqual(
            [hub_block.index(item) for item in expected_head_order],
            sorted(hub_block.index(item) for item in expected_head_order),
        )
        self.assertEqual(html.count('head-palette-row palette-rail'), 1)
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', css)
        self.assertIn('overflow: hidden !important', css)
        self.assertNotIn('overflow-x: auto !important', css)
        self.assertNotIn('min-width: 96px !important', css)
        self.assertNotIn('repeat(2, minmax(0, 1fr))', html + css)


