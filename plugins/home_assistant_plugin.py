from __future__ import annotations

import json
import os
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

DEFAULT_CONFIG = {
    "ha_enabled": True,
    "ha_base_url": "http://homeassistant.local:8123",
    "ha_token": "",
    "ha_switch_entity": "",
    "ha_light_entity": "",
    "ha_speaker_left_entity": "",
    "ha_speaker_right_entity": "",
    "ha_lamp_left_entity": "",
    "ha_lamp_right_entity": "",
    "ha_lamp_palette_last": "",
    "ha_lamp_brightness_last": 80,
}


class HomeAssistantPlugin:
    plugin_id = "home_assistant"
    display_name = "Home Assistant"

    def __init__(self, app_dir: str) -> None:
        self.app_dir = app_dir
        self.config_file = os.path.join(app_dir, "home_assistant_config.json")
        self.config = self._load_config()
        self._save_config(self.config)

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

    def _ha_request(self, method: str, path: str, payload: Optional[dict] = None) -> tuple[bool, dict]:
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
            message = f"HTTP {exc.code}"
            if body:
                message = f"{message}: {body}"
            return False, {"error": message}
        except Exception as exc:
            return False, {"error": str(exc)}

    def _entity_data(self, entity_id: str) -> tuple[bool, dict]:
        ok, data = self._ha_request("GET", f"/api/states/{entity_id}")
        if not ok:
            return False, {"error": data.get("error", "Request failed")}
        if not isinstance(data, dict):
            return False, {"error": "Invalid entity response"}
        return True, data

    def _entity_state(self, entity_id: str) -> tuple[bool, str]:
        ok, data = self._entity_data(entity_id)
        if not ok:
            return False, data.get("error", "Request failed")
        return True, str(data.get("state", "unknown"))

    def _call_service(self, domain: str, service: str, entity_id: str, extra: Optional[dict] = None) -> tuple[bool, str]:
        payload = {"entity_id": entity_id}
        if extra:
            payload.update(extra)
        ok, data = self._ha_request(
            "POST",
            f"/api/services/{domain}/{service}",
            payload,
        )
        if not ok:
            return False, data.get("error", "Request failed")
        return True, "OK"

    @staticmethod
    def _clamp_brightness(value: object, default: int = 80) -> int:
        try:
            level = int(round(float(value)))
        except Exception:
            level = int(default)
        return max(1, min(100, level))

    def get_status(self) -> dict:
        enabled = bool(self.config.get("ha_enabled", True))
        base_url = str(self.config.get("ha_base_url", "")).strip()
        token_set = bool(str(self.config.get("ha_token", "")).strip())
        switch_entity = str(self.config.get("ha_switch_entity", "")).strip()
        light_entity = str(self.config.get("ha_light_entity", "")).strip()
        speaker_left_entity = str(self.config.get("ha_speaker_left_entity", "")).strip()
        speaker_right_entity = str(self.config.get("ha_speaker_right_entity", "")).strip()
        lamp_left_entity = str(self.config.get("ha_lamp_left_entity", "")).strip()
        lamp_right_entity = str(self.config.get("ha_lamp_right_entity", "")).strip()

        # Backward compatibility with earlier single-light config.
        if not lamp_left_entity and not lamp_right_entity and light_entity:
            lamp_left_entity = light_entity

        status = {
            "enabled": enabled,
            "base_url": base_url,
            "token_set": token_set,
            "switch_entity": switch_entity,
            "light_entity": light_entity,
            "speaker_left_entity": speaker_left_entity,
            "speaker_right_entity": speaker_right_entity,
            "lamp_left_entity": lamp_left_entity,
            "lamp_right_entity": lamp_right_entity,
            "connected": False,
            "message": "",
            "switch_state": "n/a",
            "light_state": "n/a",
            "speaker_left_state": "n/a",
            "speaker_right_state": "n/a",
            "lamp_left_state": "n/a",
            "lamp_right_state": "n/a",
            "lamp_palette_last": str(self.config.get("ha_lamp_palette_last", "")).strip(),
            "lamp_brightness_last": self._clamp_brightness(self.config.get("ha_lamp_brightness_last", 80)),
            "lamp_primary_entity": "",
            "lamp_effect_current": "",
            "lamp_effect_list": [],
            "lamp_color_mode": "",
            "lamp_rgb_color": [],
        }

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
            s_ok, s_state = self._entity_state(switch_entity)
            status["switch_state"] = s_state if s_ok else f"error ({s_state})"
        if light_entity:
            l_ok, l_state = self._entity_state(light_entity)
            status["light_state"] = l_state if l_ok else f"error ({l_state})"
        if speaker_left_entity:
            sl_ok, sl_state = self._entity_state(speaker_left_entity)
            status["speaker_left_state"] = sl_state if sl_ok else f"error ({sl_state})"
        if speaker_right_entity:
            sr_ok, sr_state = self._entity_state(speaker_right_entity)
            status["speaker_right_state"] = sr_state if sr_ok else f"error ({sr_state})"
        if lamp_left_entity:
            ll_ok, ll_state = self._entity_state(lamp_left_entity)
            status["lamp_left_state"] = ll_state if ll_ok else f"error ({ll_state})"
        if lamp_right_entity:
            lr_ok, lr_state = self._entity_state(lamp_right_entity)
            status["lamp_right_state"] = lr_state if lr_ok else f"error ({lr_state})"

        lamp_entities = self._resolve_lamp_entities()
        primary_lamp = lamp_entities[0] if lamp_entities else ""
        status["lamp_primary_entity"] = primary_lamp
        if primary_lamp:
            detail_ok, detail = self._entity_data(primary_lamp)
            if detail_ok:
                attrs = detail.get("attributes") if isinstance(detail.get("attributes"), dict) else {}
                effect = attrs.get("effect")
                if effect is not None:
                    status["lamp_effect_current"] = str(effect)
                effect_list = attrs.get("effect_list", [])
                if isinstance(effect_list, list):
                    status["lamp_effect_list"] = [str(item) for item in effect_list if str(item).strip()]
                color_mode = attrs.get("color_mode")
                if color_mode is not None:
                    status["lamp_color_mode"] = str(color_mode)
                rgb_color = attrs.get("rgb_color")
                if isinstance(rgb_color, list) and len(rgb_color) >= 3:
                    try:
                        status["lamp_rgb_color"] = [int(rgb_color[0]), int(rgb_color[1]), int(rgb_color[2])]
                    except Exception:
                        status["lamp_rgb_color"] = []

        return status

    def set_switch(self, on: bool) -> tuple[bool, str]:
        entity_id = str(self.config.get("ha_switch_entity", "")).strip()
        if not entity_id:
            return False, "Set ha_switch_entity first."
        return self._call_service("switch", "turn_on" if on else "turn_off", entity_id)

    def set_light(self, on: bool) -> tuple[bool, str]:
        entity_id = str(self.config.get("ha_light_entity", "")).strip()
        if not entity_id:
            return False, "Set ha_light_entity first."
        return self._call_service("light", "turn_on" if on else "turn_off", entity_id)

    def set_speaker(self, side: str, on: bool) -> tuple[bool, str]:
        side_norm = str(side).strip().lower()
        if side_norm == "left":
            entity_id = str(self.config.get("ha_speaker_left_entity", "")).strip()
            label = "left speaker"
        elif side_norm == "right":
            entity_id = str(self.config.get("ha_speaker_right_entity", "")).strip()
            label = "right speaker"
        else:
            return False, "Speaker side must be 'left' or 'right'."

        if not entity_id:
            return False, f"Set {label} entity first."
        return self._call_service("switch", "turn_on" if on else "turn_off", entity_id)

    def set_lamp(self, side: str, on: bool) -> tuple[bool, str]:
        side_norm = str(side).strip().lower()
        if side_norm == "left":
            entity_id = str(self.config.get("ha_lamp_left_entity", "")).strip()
            right_entity = str(self.config.get("ha_lamp_right_entity", "")).strip()
            if not entity_id and not right_entity:
                entity_id = str(self.config.get("ha_light_entity", "")).strip()
            label = "left lamp"
        elif side_norm == "right":
            entity_id = str(self.config.get("ha_lamp_right_entity", "")).strip()
            label = "right lamp"
        else:
            return False, "Lamp side must be 'left' or 'right'."

        if not entity_id:
            return False, f"Set {label} entity first."
        return self._call_service("light", "turn_on" if on else "turn_off", entity_id)

    def set_lamps(self, on: bool) -> tuple[bool, str]:
        entities = self._resolve_lamp_entities()
        if not entities:
            return False, "Set floor lamp entity IDs first."
        failures: list[str] = []
        for entity_id in entities:
            ok, message = self._call_service("light", "turn_on" if on else "turn_off", entity_id)
            if not ok:
                failures.append(f"{entity_id}: {message}")
        if failures:
            return False, "; ".join(failures)
        return True, "Lamps updated."

    def _resolve_lamp_entities(self) -> list[str]:
        left = str(self.config.get("ha_lamp_left_entity", "")).strip()
        right = str(self.config.get("ha_lamp_right_entity", "")).strip()
        fallback = str(self.config.get("ha_light_entity", "")).strip()

        entities: list[str] = []
        if left:
            entities.append(left)
        if right:
            entities.append(right)
        if not entities and fallback:
            entities.append(fallback)

        # Preserve order and remove duplicates.
        seen = set()
        unique: list[str] = []
        for entity in entities:
            if entity in seen:
                continue
            unique.append(entity)
            seen.add(entity)
        return unique

    def set_lamp_palette(self, palette: str) -> tuple[bool, str]:
        palette_name = str(palette).strip().lower()
        entities = self._resolve_lamp_entities()
        if not entities:
            return False, "Set floor lamp entity IDs first."

        brightness = self._clamp_brightness(self.config.get("ha_lamp_brightness_last", 80))
        if palette_name == "warm":
            colors = [(255, 80, 20), (255, 170, 45), (255, 120, 0), (255, 210, 80)]
            label = "Warm"
            use_color_temp = False
        elif palette_name == "cool":
            colors = [(130, 70, 255), (40, 120, 255), (20, 200, 170), (70, 180, 255)]
            label = "Cool"
            use_color_temp = False
        elif palette_name == "candle":
            colors = []
            label = "Candle"
            use_color_temp = True
        else:
            return False, "Palette must be 'warm', 'cool', or 'candle'."

        failures: list[str] = []
        for idx, entity_id in enumerate(entities):
            if use_color_temp:
                extra = {
                    "color_temp": 454,
                    "brightness_pct": brightness,
                    "transition": 0.6,
                }
            else:
                color = colors[idx % len(colors)]
                extra = {
                    "rgb_color": [int(color[0]), int(color[1]), int(color[2])],
                    "brightness_pct": brightness,
                    "transition": 0.6,
                }
            ok, message = self._call_service(
                "light",
                "turn_on",
                entity_id,
                extra=extra,
            )
            if not ok:
                failures.append(f"{entity_id}: {message}")

        if failures:
            return False, "; ".join(failures)

        self.config["ha_lamp_palette_last"] = palette_name
        self._save_config(self.config)
        return True, f"{label} palette applied to lamps."

    def set_lamp_effect(self, effect: str) -> tuple[bool, str]:
        effect_name = str(effect).strip()
        if not effect_name:
            return False, "Choose a gradient/effect first."

        entities = self._resolve_lamp_entities()
        if not entities:
            return False, "Set floor lamp entity IDs first."

        failures: list[str] = []
        for entity_id in entities:
            ok, message = self._call_service(
                "light",
                "turn_on",
                entity_id,
                extra={"effect": effect_name, "transition": 0.4},
            )
            if not ok:
                failures.append(f"{entity_id}: {message}")

        if failures:
            return False, "; ".join(failures)
        return True, f"Effect '{effect_name}' applied."

    def set_lamp_brightness(self, brightness_pct: int) -> tuple[bool, str]:
        entities = self._resolve_lamp_entities()
        if not entities:
            return False, "Set floor lamp entity IDs first."

        brightness = self._clamp_brightness(brightness_pct)
        failures: list[str] = []
        for entity_id in entities:
            ok, message = self._call_service(
                "light",
                "turn_on",
                entity_id,
                extra={"brightness_pct": brightness, "transition": 0.4},
            )
            if not ok:
                failures.append(f"{entity_id}: {message}")

        if failures:
            return False, "; ".join(failures)

        self.config["ha_lamp_brightness_last"] = brightness
        self._save_config(self.config)
        return True, f"Lamp brightness set to {brightness}%."

    def start(self) -> None:
        return

    def shutdown(self) -> None:
        return

    def register_routes(self, app) -> None:
        from flask import jsonify, request

        @app.route("/api/ha/status")
        def ha_status():
            return jsonify(self.get_status())

        @app.route("/api/ha/config", methods=["POST"])
        def ha_config():
            payload = request.get_json(force=True)

            if "ha_enabled" in payload:
                self.config["ha_enabled"] = bool(payload["ha_enabled"])
            if "ha_base_url" in payload:
                self.config["ha_base_url"] = str(payload["ha_base_url"]).strip()
            if "ha_switch_entity" in payload:
                self.config["ha_switch_entity"] = str(payload["ha_switch_entity"]).strip()
            if "ha_light_entity" in payload:
                self.config["ha_light_entity"] = str(payload["ha_light_entity"]).strip()
            if "ha_speaker_left_entity" in payload:
                self.config["ha_speaker_left_entity"] = str(payload["ha_speaker_left_entity"]).strip()
            if "ha_speaker_right_entity" in payload:
                self.config["ha_speaker_right_entity"] = str(payload["ha_speaker_right_entity"]).strip()
            if "ha_lamp_left_entity" in payload:
                self.config["ha_lamp_left_entity"] = str(payload["ha_lamp_left_entity"]).strip()
            if "ha_lamp_right_entity" in payload:
                self.config["ha_lamp_right_entity"] = str(payload["ha_lamp_right_entity"]).strip()
            if "ha_lamp_brightness_last" in payload:
                self.config["ha_lamp_brightness_last"] = self._clamp_brightness(payload["ha_lamp_brightness_last"])
            if "ha_token" in payload and str(payload["ha_token"]).strip():
                self.config["ha_token"] = str(payload["ha_token"]).strip()

            self._save_config(self.config)
            return jsonify({"ok": True, "ha_status": self.get_status()})

        @app.route("/api/ha/switch", methods=["POST"])
        def ha_switch():
            payload = request.get_json(force=True)
            on = bool(payload.get("on", False))
            ok, message = self.set_switch(on)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/light", methods=["POST"])
        def ha_light():
            payload = request.get_json(force=True)
            on = bool(payload.get("on", False))
            ok, message = self.set_light(on)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/speaker", methods=["POST"])
        def ha_speaker():
            payload = request.get_json(force=True)
            side = str(payload.get("side", "")).strip().lower()
            on = bool(payload.get("on", False))
            ok, message = self.set_speaker(side, on)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/lamp", methods=["POST"])
        def ha_lamp():
            payload = request.get_json(force=True)
            side = str(payload.get("side", "")).strip().lower()
            on = bool(payload.get("on", False))
            ok, message = self.set_lamp(side, on)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/lamps", methods=["POST"])
        def ha_lamps():
            payload = request.get_json(force=True)
            on = bool(payload.get("on", False))
            ok, message = self.set_lamps(on)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/lamp_palette", methods=["POST"])
        def ha_lamp_palette():
            payload = request.get_json(force=True)
            palette = str(payload.get("palette", "")).strip().lower()
            ok, message = self.set_lamp_palette(palette)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/lamp_effect", methods=["POST"])
        def ha_lamp_effect():
            payload = request.get_json(force=True)
            effect = str(payload.get("effect", "")).strip()
            ok, message = self.set_lamp_effect(effect)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

        @app.route("/api/ha/lamp_brightness", methods=["POST"])
        def ha_lamp_brightness():
            payload = request.get_json(force=True)
            brightness = self._clamp_brightness(payload.get("brightness_pct", payload.get("brightness", 80)))
            ok, message = self.set_lamp_brightness(brightness)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": message, "ha_status": self.get_status()}), code

    def dashboard_html(self) -> str:
        return """
  <div class="card">
    <div class="row" style="justify-content: space-between; align-items: flex-start;">
      <div>
        <div class="panel-title-row">
          <span class="material-symbols-rounded panel-title-icon">home</span>
          <div class="panel-title" style="margin-bottom:0;">Home Assistant Bridge</div>
        </div>
        <div class="panel-meta">Control speakers, lamps, and scenes from one panel.</div>
      </div>
      <a
        id="haOpenLink"
        class="btn gray small"
        href="#"
        target="_blank"
        rel="noopener noreferrer"
      >Open HA</a>
    </div>
    <div class="row" style="margin-top:12px;">
      <label><input id="haEnabled" class="switch" type="checkbox"> Enable HA integration</label>
      <span id="haConn" class="status-pill status-warn">Not checked yet.</span>
    </div>
  </div>

  <div class="card">
    <div class="panel-title"><span class="material-symbols-rounded label-icon">speaker</span>Speakers</div>
    <div class="row" style="margin-top:8px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">speaker</span>Speaker L:</span>
      <button class="btn control-btn" onclick="haSetSpeaker('left', true)">ON</button>
      <button class="btn gray control-btn" onclick="haSetSpeaker('left', false)">OFF</button>
      <span id="haSpeakerLeftState" class="small muted">n/a</span>
    </div>

    <div class="row" style="margin-top:8px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">speaker</span>Speaker R:</span>
      <button class="btn control-btn" onclick="haSetSpeaker('right', true)">ON</button>
      <button class="btn gray control-btn" onclick="haSetSpeaker('right', false)">OFF</button>
      <span id="haSpeakerRightState" class="small muted">n/a</span>
    </div>

    <div class="row" style="margin-top:8px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">surround_sound</span>Both speakers:</span>
      <button id="haBothSpeakersBtn" class="btn control-btn" onclick="haToggleBothSpeakers()">Toggle</button>
      <span id="haBothSpeakersState" class="small muted">n/a</span>
    </div>
  </div>

  <div class="card">
    <div class="panel-title"><span class="material-symbols-rounded label-icon">lightbulb</span>Lamps, Scenes, and Dimmer</div>
    <div class="row" style="margin-top:8px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">lightbulb</span>Lamp L:</span>
      <button id="haLampLeftBtn" class="btn control-btn" onclick="haToggleLamp('left')">N/A</button>
      <span id="haLampLeftState" class="small muted">n/a</span>
    </div>

    <div class="row" style="margin-top:8px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">lightbulb</span>Lamp R:</span>
      <button id="haLampRightBtn" class="btn control-btn" onclick="haToggleLamp('right')">N/A</button>
      <span id="haLampRightState" class="small muted">n/a</span>
    </div>

    <div class="row" style="margin-top:8px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">wb_incandescent</span>Both lamps:</span>
      <button id="haBothLampsBtn" class="btn control-btn" onclick="haToggleBothLamps()">Toggle</button>
      <span id="haBothLampsState" class="small muted">n/a</span>
    </div>

    <div class="row" style="margin-top:12px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">palette</span>Lamp colors:</span>
      <button
        class="btn control-btn preset-btn preset-cool"
        onclick="haSetLampPalette('cool')"
      >COOL</button>
      <button
        class="btn control-btn preset-btn preset-warm"
        onclick="haSetLampPalette('warm')"
      >WARM</button>
      <button
        class="btn control-btn preset-btn preset-candle"
        onclick="haSetLampPalette('candle')"
      >CANDLE</button>
      <span id="haLampPaletteMsg" class="small muted"></span>
    </div>
    <div id="haLampPaletteLast" class="small muted" style="margin-top:6px;"></div>

    <div class="row" style="margin-top:12px; align-items:center;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">gradient</span>Gradient effect:</span>
      <select id="haLampEffect" class="wide" style="max-width:320px;"></select>
      <button id="haLampEffectBtn" class="btn control-btn" onclick="haApplyLampEffect()">Apply Effect</button>
    </div>
    <div id="haLampEffectCurrent" class="small muted" style="margin-top:6px;">Current effect: --</div>
    <div id="haLampEffectMsg" class="small muted" style="margin-top:6px;"></div>

    <div class="row" style="margin-top:12px; align-items:center;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">tune</span>Dimmer:</span>
      <input id="haLampDimmer" type="range" min="1" max="100" step="1" value="80" oninput="haLampDimmerInputChanged()">
      <span id="haLampDimmerValue" class="status-pill status-warn">80%</span>
      <button class="btn control-btn" onclick="haApplyLampBrightness()">Apply Dimmer</button>
    </div>
    <div id="haLampDimmerMsg" class="small muted" style="margin-top:8px;"></div>
  </div>

  <div class="card">
    <div class="panel-title"><span class="material-symbols-rounded label-icon">settings_ethernet</span>Connection & Entities</div>
    <div class="grid">
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">link</span>HA base URL</div>
        <input id="haBaseUrl" class="wide" type="text" placeholder="http://homeassistant.local:8123">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">key</span>Long-lived access token</div>
        <input id="haToken" class="wide" type="password" placeholder="Paste token (leave blank to keep current)">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">power_settings_new</span>Smart plug entity</div>
        <input id="haSwitchEntity" class="wide" type="text" placeholder="switch.office_plug">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">lightbulb</span>Smart light entity</div>
        <input id="haLightEntity" class="wide" type="text" placeholder="light.desk_lamp">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">speaker</span>Speaker left plug entity</div>
        <input id="haSpeakerLeftEntity" class="wide" type="text" placeholder="switch.speaker_left">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">speaker</span>Speaker right plug entity</div>
        <input id="haSpeakerRightEntity" class="wide" type="text" placeholder="switch.speaker_right">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">light</span>Floor lamp left entity</div>
        <input id="haLampLeftEntity" class="wide" type="text" placeholder="light.floor_lamp_left">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">light</span>Floor lamp right entity</div>
        <input id="haLampRightEntity" class="wide" type="text" placeholder="light.floor_lamp_right">
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="haSaveConfig()">Save HA Settings</button>
      <button class="btn gray" onclick="haRefreshStatus()">Refresh HA</button>
      <span id="haSaveMsg" class="small muted"></span>
    </div>
  </div>
"""

    def dashboard_js(self) -> str:
        return """
function haNormalizeBinaryState(value) {
  const text = String(value || '').toLowerCase();
  if (text === 'on') return true;
  if (text === 'off') return false;
  return null;
}

function haSetBinaryToggleButton(buttonId, state, onLabel='ON', offLabel='OFF', unknownLabel='N/A') {
  const btn = document.getElementById(buttonId);
  if (!btn) return;
  btn.classList.remove('state-on', 'state-off', 'state-action', 'state-danger', 'gray');
  btn.disabled = false;
  if (state === true) {
    btn.textContent = onLabel;
    btn.classList.add('state-on');
  } else if (state === false) {
    btn.textContent = offLabel;
    btn.classList.add('state-off');
  } else {
    btn.textContent = unknownLabel;
    btn.classList.add('gray');
    btn.disabled = true;
  }
}

function haSyncLampEffectControls(st) {
  const select = document.getElementById('haLampEffect');
  const applyBtn = document.getElementById('haLampEffectBtn');
  const currentEl = document.getElementById('haLampEffectCurrent');
  if (!select || !applyBtn || !currentEl) return;

  const effects = Array.isArray(st.lamp_effect_list) ? st.lamp_effect_list.filter(Boolean).map(String) : [];
  const current = String(st.lamp_effect_current || '').trim();
  const activeValue = String(select.value || '').trim();

  select.innerHTML = '';
  if (!effects.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'No gradient effects reported by lamp';
    select.appendChild(opt);
    select.disabled = true;
    applyBtn.disabled = true;
    currentEl.textContent = current ? ('Current effect: ' + current) : 'Current effect: --';
    return;
  }

  for (const effectName of effects) {
    const opt = document.createElement('option');
    opt.value = effectName;
    opt.textContent = effectName;
    select.appendChild(opt);
  }

  const next = effects.includes(current)
    ? current
    : (effects.includes(activeValue) ? activeValue : effects[0]);
  select.value = next;
  select.disabled = false;
  applyBtn.disabled = false;
  currentEl.textContent = current ? ('Current effect: ' + current) : 'Current effect: (none)';
}

async function haRefreshStatus() {
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
    setIfIdle('haSpeakerLeftEntity', st.speaker_left_entity);
    setIfIdle('haSpeakerRightEntity', st.speaker_right_entity);
    setIfIdle('haLampLeftEntity', st.lamp_left_entity);
    setIfIdle('haLampRightEntity', st.lamp_right_entity);
    document.getElementById('haOpenLink').href = st.base_url || '#';

    const conn = document.getElementById('haConn');
    if (st.connected) {
      conn.textContent = 'Connected';
      conn.className = 'status-pill status-ok';
    } else if (st.enabled) {
      conn.textContent = st.message || 'Connection error';
      conn.className = 'status-pill status-bad';
    } else {
      conn.textContent = st.message || 'HA integration disabled.';
      conn.className = 'status-pill status-warn';
    }

    document.getElementById('haSpeakerLeftState').textContent = 'State: ' + (st.speaker_left_state || 'n/a');
    document.getElementById('haSpeakerRightState').textContent = 'State: ' + (st.speaker_right_state || 'n/a');
    document.getElementById('haLampLeftState').textContent = 'State: ' + (st.lamp_left_state || 'n/a');
    document.getElementById('haLampRightState').textContent = 'State: ' + (st.lamp_right_state || 'n/a');
    const lampLeftState = haNormalizeBinaryState(st.lamp_left_state);
    const lampRightState = haNormalizeBinaryState(st.lamp_right_state);
    haSetBinaryToggleButton('haLampLeftBtn', lampLeftState, 'ON', 'OFF', 'N/A');
    haSetBinaryToggleButton('haLampRightBtn', lampRightState, 'ON', 'OFF', 'N/A');

    const dimmer = document.getElementById('haLampDimmer');
    const brightness = Number(st.lamp_brightness_last || 80);
    if (document.activeElement !== dimmer) dimmer.value = brightness;
    document.getElementById('haLampDimmerValue').textContent = brightness + '%';
    document.getElementById('haLampPaletteLast').textContent = st.lamp_palette_last
      ? ('Last preset: ' + String(st.lamp_palette_last).toUpperCase())
      : 'No lamp color preset applied yet.';

    const bothSpeakersOn = String(st.speaker_left_state).toLowerCase() === 'on' && String(st.speaker_right_state).toLowerCase() === 'on';
    const bothSpeakersBtn = document.getElementById('haBothSpeakersBtn');
    bothSpeakersBtn.textContent = bothSpeakersOn ? 'TURN BOTH OFF' : 'TURN BOTH ON';
    bothSpeakersBtn.classList.toggle('state-danger', bothSpeakersOn);
    bothSpeakersBtn.classList.toggle('state-action', !bothSpeakersOn);
    document.getElementById('haBothSpeakersState').textContent = bothSpeakersOn ? 'Both ON' : 'One/Both OFF';

    const bothLampsBtn = document.getElementById('haBothLampsBtn');
    const lampAnyOn = lampLeftState === true || lampRightState === true;
    bothLampsBtn.textContent = lampAnyOn ? 'TURN BOTH OFF' : 'TURN BOTH ON';
    bothLampsBtn.classList.toggle('state-danger', lampAnyOn);
    bothLampsBtn.classList.toggle('state-action', !lampAnyOn);
    if (lampLeftState === null && lampRightState === null) {
      document.getElementById('haBothLampsState').textContent = 'State unavailable';
    } else {
      document.getElementById('haBothLampsState').textContent = lampAnyOn ? 'One/Both ON' : 'Both OFF';
    }

    haSyncLampEffectControls(st);
  } catch (err) {
    const conn = document.getElementById('haConn');
    conn.textContent = 'HA status error';
    conn.className = 'status-pill status-bad';
  }
}

async function haSaveConfig() {
  const payload = {
    ha_enabled: document.getElementById('haEnabled').checked,
    ha_base_url: document.getElementById('haBaseUrl').value.trim(),
    ha_switch_entity: document.getElementById('haSwitchEntity').value.trim(),
    ha_light_entity: document.getElementById('haLightEntity').value.trim(),
    ha_speaker_left_entity: document.getElementById('haSpeakerLeftEntity').value.trim(),
    ha_speaker_right_entity: document.getElementById('haSpeakerRightEntity').value.trim(),
    ha_lamp_left_entity: document.getElementById('haLampLeftEntity').value.trim(),
    ha_lamp_right_entity: document.getElementById('haLampRightEntity').value.trim(),
    ha_lamp_brightness_last: parseInt(document.getElementById('haLampDimmer').value, 10) || 80,
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
  await haRefreshStatus();
}

function haLampDimmerInputChanged() {
  const value = parseInt(document.getElementById('haLampDimmer').value, 10) || 80;
  document.getElementById('haLampDimmerValue').textContent = value + '%';
}

async function haSetSpeaker(side, on, silent=false) {
  const r = await api('/api/ha/speaker', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({side, on}),
  });
  if (!silent) {
    document.getElementById('haSaveMsg').textContent = r.message || 'Done';
    setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  }
  await haRefreshStatus();
}

async function haToggleBothSpeakers() {
  const st = await api('/api/ha/status');
  const bothOn = String(st.speaker_left_state).toLowerCase() === 'on' && String(st.speaker_right_state).toLowerCase() === 'on';
  const targetOn = !bothOn;
  const left = haSetSpeaker('left', targetOn, true);
  const right = haSetSpeaker('right', targetOn, true);
  await Promise.allSettled([left, right]);
  document.getElementById('haSaveMsg').textContent = targetOn ? 'Both speakers ON.' : 'Both speakers OFF.';
  setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  await haRefreshStatus();
}

async function haSetLamp(side, on, silent=false) {
  const r = await api('/api/ha/lamp', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({side, on}),
  });
  if (!silent) {
    document.getElementById('haSaveMsg').textContent = r.message || 'Done';
    setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  }
  await haRefreshStatus();
}

async function haToggleLamp(side) {
  const st = await api('/api/ha/status');
  const current = side === 'left'
    ? haNormalizeBinaryState(st.lamp_left_state)
    : haNormalizeBinaryState(st.lamp_right_state);
  if (current === null) {
    document.getElementById('haSaveMsg').textContent = 'Lamp state unavailable.';
    setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2200);
    return;
  }
  await haSetLamp(side, !current);
}

async function haToggleBothLamps() {
  const st = await api('/api/ha/status');
  const left = haNormalizeBinaryState(st.lamp_left_state);
  const right = haNormalizeBinaryState(st.lamp_right_state);
  const anyOn = left === true || right === true;
  const targetOn = !anyOn;
  const r = await api('/api/ha/lamps', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({on: targetOn}),
  });
  if (!r.ok) {
    document.getElementById('haSaveMsg').textContent = r.message || 'Lamp update failed.';
    setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2500);
    await haRefreshStatus();
    return;
  }
  document.getElementById('haSaveMsg').textContent = targetOn ? 'Both lamps ON.' : 'Both lamps OFF.';
  setTimeout(() => document.getElementById('haSaveMsg').textContent = '', 2000);
  await haRefreshStatus();
}

async function haSetLampPalette(palette) {
  const r = await api('/api/ha/lamp_palette', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({palette}),
  });
  document.getElementById('haLampPaletteMsg').textContent = r.message || 'Palette applied.';
  setTimeout(() => document.getElementById('haLampPaletteMsg').textContent = '', 2500);
  await haRefreshStatus();
}

async function haApplyLampEffect() {
  const select = document.getElementById('haLampEffect');
  const effect = String((select && select.value) || '').trim();
  if (!effect) {
    document.getElementById('haLampEffectMsg').textContent = 'Choose a gradient effect first.';
    setTimeout(() => document.getElementById('haLampEffectMsg').textContent = '', 2200);
    return;
  }
  const r = await api('/api/ha/lamp_effect', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({effect}),
  });
  document.getElementById('haLampEffectMsg').textContent = r.message || 'Effect applied.';
  setTimeout(() => document.getElementById('haLampEffectMsg').textContent = '', 2600);
  await haRefreshStatus();
}

async function haApplyLampBrightness() {
  const brightness = parseInt(document.getElementById('haLampDimmer').value, 10) || 80;
  const r = await api('/api/ha/lamp_brightness', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({brightness_pct: brightness}),
  });
  document.getElementById('haLampDimmerMsg').textContent = r.message || 'Dimmer updated.';
  setTimeout(() => document.getElementById('haLampDimmerMsg').textContent = '', 2500);
  await haRefreshStatus();
}
"""

    def dashboard_init_js(self) -> str:
        return """
  await haRefreshStatus();
  setInterval(haRefreshStatus, 5000);
"""


def create_plugin(app_dir: str) -> HomeAssistantPlugin:
    return HomeAssistantPlugin(app_dir)
