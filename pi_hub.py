#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from html import escape
from typing import Any

from flask import Flask, jsonify, render_template_string, request


APP_DIR = os.path.dirname(__file__)
PLUGIN_CONFIG_FILE = os.path.join(APP_DIR, "plugins.json")
PI_HUB_UPDATE_SCRIPT = os.path.join(APP_DIR, "update_modules.sh")
HUB_UPDATE_CONFIG_FILE = os.path.join(APP_DIR, "hub_update.json")

DEFAULT_HUB_UPDATE_CONFIG = {
    "mode": "git",
    "repo_url": "",
    "branch": "main",
    "auto_deploy": True,
    "poll_seconds": 60,
}

DEFAULT_PLUGIN_MODULES = [
    "plugins.home_assistant_plugin",
    "plugins.bonsai_plugin",
    "plugins.pihole_plugin",
]

MODULE_META = {
    "master": {"tag": "MASTER", "hint": "Unified read-only overview", "icon": "dashboard"},
    "settings": {"tag": "SET", "hint": "Updater and hub controls", "icon": "settings"},
    "home_assistant": {"tag": "HA", "hint": "Lights, speakers, scenes", "icon": "home"},
    "bonsai": {"tag": "BONSAI", "hint": "Moisture, pump, OLED", "icon": "eco"},
    "pihole": {"tag": "DNS", "hint": "Blocking status and controls", "icon": "dns"},
}


def load_hub_update_config() -> dict[str, Any]:
    config: dict[str, Any] = DEFAULT_HUB_UPDATE_CONFIG.copy()
    if not os.path.isfile(HUB_UPDATE_CONFIG_FILE):
        return config

    try:
        with open(HUB_UPDATE_CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            mode = str(raw.get("mode", config["mode"])).strip().lower()
            if mode in {"git", "script"}:
                config["mode"] = mode
            config["repo_url"] = str(raw.get("repo_url", config["repo_url"])).strip()
            branch = str(raw.get("branch", config["branch"])).strip()
            config["branch"] = branch or "main"
            auto_deploy_raw = raw.get("auto_deploy", config["auto_deploy"])
            if isinstance(auto_deploy_raw, bool):
                config["auto_deploy"] = auto_deploy_raw
            else:
                config["auto_deploy"] = str(auto_deploy_raw).strip().lower() in {"1", "true", "yes", "on"}
            try:
                poll_seconds = int(raw.get("poll_seconds", config["poll_seconds"]))
            except Exception:
                poll_seconds = int(config["poll_seconds"])
            config["poll_seconds"] = max(30, min(3600, poll_seconds))
    except Exception:
        pass
    return config


def save_hub_update_config(config: dict[str, Any]) -> None:
    auto_deploy_raw = config.get("auto_deploy", True)
    if isinstance(auto_deploy_raw, bool):
        auto_deploy = auto_deploy_raw
    else:
        auto_deploy = str(auto_deploy_raw).strip().lower() in {"1", "true", "yes", "on"}
    try:
        poll_seconds = int(config.get("poll_seconds", 60))
    except Exception:
        poll_seconds = 60
    poll_seconds = max(30, min(3600, poll_seconds))

    cleaned = {
        "mode": str(config.get("mode", "git")).strip().lower(),
        "repo_url": str(config.get("repo_url", "")).strip(),
        "branch": (str(config.get("branch", "main")).strip() or "main"),
        "auto_deploy": bool(auto_deploy),
        "poll_seconds": int(poll_seconds),
    }
    if cleaned["mode"] not in {"git", "script"}:
        cleaned["mode"] = "git"

    with open(HUB_UPDATE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)


def safe_plugin_key(value: str) -> str:
    key = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in str(value))
    key = key.strip("_")
    return key or "plugin"


def render_module_nav_item(
    pane_id: str,
    module_name: str,
    module_meta: dict[str, str],
    active: bool,
    module_key: str,
) -> str:
    active_class = " active" if active else ""
    mod_tag = str(module_meta.get("tag", "MOD"))
    mod_icon = str(module_meta.get("icon", "widgets"))
    key = safe_plugin_key(module_key)
    return (
        f'<button class="side-link{active_class}" data-pane-id="{escape(pane_id)}" '
        f'data-module="{escape(key)}" '
        f'onclick="switchPane(\'{pane_id}\', this)">'
        f'<span class="side-link-top">'
        f'<span class="module-icon material-symbols-rounded" aria-hidden="true">{escape(mod_icon)}</span>'
        f'<span class="module-name-wrap">'
        f'<span class="module-tag">{escape(mod_tag)}</span>'
        f'<span class="module-name">{escape(module_name)}</span>'
        f"</span>"
        f"</span>"
        f"</button>"
    )


def render_module_pane(
    pane_id: str,
    module_name: str,
    module_meta: dict[str, str],
    module_html: str,
    active: bool,
) -> str:
    active_class = " active" if active else ""
    mod_icon = str(module_meta.get("icon", "widgets"))
    return (
        f'<section id="{pane_id}" class="plugin-pane{active_class}">'
        f'<div class="pane-header"><div class="pane-title-row">'
        f'<span class="pane-icon material-symbols-rounded" aria-hidden="true">{escape(mod_icon)}</span>'
        f"<div>"
        f'<div class="pane-title">{escape(module_name)}</div>'
        f"</div>"
        f"</div></div>"
        f"{module_html}"
        f"</section>"
    )


def master_dashboard_html() -> str:
    return """
  <div class="card master-overview-card">
    <div class="row" style="justify-content: space-between; align-items: center;">
      <div class="panel-title-row">
        <span class="material-symbols-rounded panel-title-icon">monitoring</span>
        <div class="panel-title" style="margin-bottom:0;">Whole-System Snapshot</div>
      </div>
      <div class="row" style="gap:8px; justify-content:flex-end;">
        <span id="hubPiLink" class="status-pill status-warn">Pi Link: Checking...</span>
        <span id="masterUpdated" class="small muted">Waiting for first refresh...</span>
      </div>
    </div>
    <div class="row" style="margin-top:10px; gap:8px;">
      <span id="masterConnHa" class="status-pill status-warn">HA: Pending</span>
      <span id="masterConnBonsai" class="status-pill status-warn">Bonsai: Pending</span>
      <span id="masterConnPihole" class="status-pill status-warn">Pi-hole: Pending</span>
    </div>
    <div id="masterActionMsg" class="small muted master-action-msg">Quick controls are live on this page.</div>
  </div>

  <div class="master-kpi-grid">
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">water_drop</span>Soil Moisture</div>
      <div id="masterMoistureKpi" class="master-kpi-value">--</div>
      <div id="masterMoistureState" class="master-kpi-sub muted">No data yet</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">bolt</span>Pump</div>
      <div id="masterPumpKpi" class="master-kpi-value">--</div>
      <div id="masterPumpMeta" class="master-kpi-sub muted">No data yet</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">water_drop</span>Auto Watering</div>
      <div id="masterAutoWaterKpi" class="master-kpi-value">--</div>
      <div id="masterLastWatered" class="master-kpi-sub muted">Last: --</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">lightbulb</span>Lamps</div>
      <div id="masterLampKpi" class="master-kpi-value">--</div>
      <div id="masterLampMeta" class="master-kpi-sub muted">Preset: --</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">speaker</span>Speakers</div>
      <div id="masterSpeakerKpi" class="master-kpi-value">--</div>
      <div id="masterSpeakerMeta" class="master-kpi-sub muted">No data yet</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">shield</span>DNS Blocking</div>
      <div id="masterDnsKpi" class="master-kpi-value">--</div>
      <div id="masterDnsMeta" class="master-kpi-sub muted">No data yet</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">query_stats</span>Queries Today</div>
      <div id="masterQueriesKpi" class="master-kpi-value">--</div>
      <div class="master-kpi-sub muted">Pi-hole</div>
    </div>
    <div class="card master-kpi-card">
      <div class="master-kpi-label"><span class="material-symbols-rounded label-icon">percent</span>Blocked %</div>
      <div id="masterBlockedPctKpi" class="master-kpi-value">--</div>
      <div id="masterBlockedCountMeta" class="master-kpi-sub muted">Blocked: --</div>
    </div>
  </div>

  <div class="master-detail-grid">
    <div class="card master-detail-card">
      <div class="panel-title"><span class="material-symbols-rounded label-icon">home</span>Home Assistant</div>
      <div class="master-list">
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">link</span>Connection</span>
          <span id="masterHaConnection" class="status-pill status-warn">Pending</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">power_settings_new</span>Smart plug</span>
          <span id="masterHaPlug" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">lightbulb</span>Main light</span>
          <span id="masterHaLight" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">speaker</span>Speaker left</span>
          <span id="masterHaSpeakerLeft" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">speaker</span>Speaker right</span>
          <span id="masterHaSpeakerRight" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">lightbulb</span>Lamp left</span>
          <button id="masterHaLampLeft" type="button" class="master-state master-state-btn neutral" onclick="masterToggleHaLamp('left')">--</button>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">lightbulb</span>Lamp right</span>
          <button id="masterHaLampRight" type="button" class="master-state master-state-btn neutral" onclick="masterToggleHaLamp('right')">--</button>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">tune</span>Dimmer</span>
          <span id="masterHaDimmer" class="master-state neutral">--</span>
        </div>
      </div>
      <div class="master-controls">
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">speaker</span>Speakers</span>
          <button id="masterHaSpeakersToggleBtn" class="btn master-mini-btn state-action" onclick="masterToggleHaSpeakers()">--</button>
        </div>
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">lightbulb</span>Lamps</span>
          <button id="masterHaLampsToggleBtn" class="btn master-mini-btn state-action" onclick="masterToggleHaLamps()">--</button>
        </div>
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">palette</span>Lamp colors</span>
          <button class="btn master-mini-btn preset-btn preset-cool" onclick="masterSetHaLampPalette('cool')">COOL</button>
          <button class="btn master-mini-btn preset-btn preset-money" onclick="masterSetHaLampPalette('money')">MONEY</button>
          <button class="btn master-mini-btn preset-btn preset-warm" onclick="masterSetHaLampPalette('warm')">WARM</button>
          <button class="btn master-mini-btn preset-btn preset-candle" onclick="masterSetHaLampPalette('candle')">CANDLE</button>
        </div>
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">tune</span>Dimmer</span>
          <input id="masterLampDimmer" type="range" min="1" max="100" step="1" value="80" oninput="masterLampDimmerInputChanged()">
          <span id="masterLampDimmerValue" class="status-pill status-warn">80%</span>
        </div>
      </div>
    </div>

    <div class="card master-detail-card">
      <div class="panel-title"><span class="material-symbols-rounded label-icon">eco</span>Bonsai</div>
      <div class="master-list">
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">water_drop</span>Soil moisture</span>
          <span id="masterBonsaiMoisture" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">water_drop</span>Moisture state</span>
          <span id="masterBonsaiState" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">bolt</span>Pump mode</span>
          <span id="masterBonsaiPumpMode" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">hourglass_top</span>Pump remaining</span>
          <span id="masterBonsaiPumpRemaining" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">water_drop</span>Auto watering</span>
          <span id="masterBonsaiAuto" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">view_in_ar</span>OLED</span>
          <button id="masterBonsaiOled" type="button" class="master-state master-state-btn neutral" onclick="masterToggleBonsaiOled()">--</button>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">memory</span>GPIO</span>
          <span id="masterBonsaiGpio" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">tune</span>Thresholds</span>
          <span id="masterBonsaiThresholds" class="master-state neutral">--</span>
        </div>
      </div>
      <div class="master-controls">
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">water_drop</span>Auto Watering</span>
          <button id="masterBonsaiAutoToggleBtn" class="btn master-mini-btn state-action" onclick="masterToggleBonsaiAuto()">--</button>
        </div>
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">play_circle</span>Manual Pump</span>
          <button id="masterBonsaiManualToggleBtn" class="btn master-mini-btn state-action" onclick="masterToggleBonsaiManual()">--</button>
        </div>
      </div>
    </div>

    <div class="card master-detail-card">
      <div class="panel-title"><span class="material-symbols-rounded label-icon">dns</span>Pi-hole</div>
      <div class="master-list">
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">link</span>Connection</span>
          <span id="masterPiholeConnection" class="status-pill status-warn">Pending</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">settings</span>Mode</span>
          <span id="masterPiholeMode" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">shield</span>Blocking</span>
          <span id="masterPiholeBlocking" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">query_stats</span>Queries today</span>
          <span id="masterPiholeQueries" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">block</span>Blocked today</span>
          <span id="masterPiholeBlocked" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">percent</span>Blocked percent</span>
          <span id="masterPiholePercent" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">lock</span>TLS verify</span>
          <span id="masterPiholeTls" class="master-state neutral">--</span>
        </div>
        <div class="master-item">
          <span class="master-item-name"><span class="material-symbols-rounded">chat</span>Message</span>
          <span id="masterPiholeMsg" class="master-msg muted">--</span>
        </div>
      </div>
      <div class="master-controls">
        <div class="master-control-group">
          <span class="master-control-label"><span class="material-symbols-rounded label-icon">shield</span>DNS Blocking</span>
          <button id="masterPiholeBlockingToggleBtn" class="btn master-mini-btn state-action" onclick="masterTogglePiholeBlocking()">--</button>
        </div>
      </div>
    </div>
  </div>
"""


def master_dashboard_js() -> str:
    return """
function masterNum(value, digits=0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

function masterNormalize(state) {
  const text = String(state || '').trim().toLowerCase();
  if (text === 'on' || text === 'enabled' || text === 'true') return 'on';
  if (text === 'off' || text === 'disabled' || text === 'false') return 'off';
  return 'unknown';
}

function masterSetReadOnlyState(id, value, onLabel='ON', offLabel='OFF', unknownLabel='N/A') {
  const el = document.getElementById(id);
  if (!el) return;
  const normalized = masterNormalize(value);
  el.classList.remove('on', 'off', 'neutral');
  if (normalized === 'on') {
    el.classList.add('on');
    el.textContent = onLabel;
    return;
  }
  if (normalized === 'off') {
    el.classList.add('off');
    el.textContent = offLabel;
    return;
  }
  el.classList.add('neutral');
  el.textContent = unknownLabel;
}

function masterNormalizeBoolean(value) {
  const normalized = masterNormalize(value);
  if (normalized === 'on') return true;
  if (normalized === 'off') return false;
  return null;
}

function masterExtractMessage(raw) {
  if (raw === null || raw === undefined) return '';
  const text = String(raw).trim();
  if (!text) return '';
  const messageMatch = text.match(/"message"\\s*:\\s*"([^"]+)"/i);
  if (messageMatch && messageMatch[1]) return messageMatch[1];
  return text;
}

function masterBriefMessage(raw, maxLen=54) {
  let text = masterExtractMessage(raw).replace(/\s+/g, ' ').trim();
  if (!text) return '';

  const lower = text.toLowerCase();
  if (lower.includes('api seats exceeded')) text = 'API seats exceeded';
  if (lower.includes('home assistant integration is disabled')) text = 'HA integration disabled';
  if (lower.includes('pi-hole integration disabled')) text = 'Pi-hole integration disabled';

  if (text.length > maxLen) {
    return text.slice(0, Math.max(0, maxLen - 1)) + '…';
  }
  return text;
}

function masterSetConnPill(id, connected, fallbackMessage='Disconnected') {
  const el = document.getElementById(id);
  if (!el) return;
  const shortMsg = masterBriefMessage(fallbackMessage);
  if (connected === true) {
    el.textContent = 'Connected';
    el.className = 'status-pill status-ok';
    el.title = 'Connected';
    return;
  }
  if (connected === false) {
    el.textContent = shortMsg || 'Disconnected';
    el.className = 'status-pill status-bad';
    el.title = String(fallbackMessage || '');
    return;
  }
  el.textContent = 'Pending';
  el.className = 'status-pill status-warn';
  el.title = '';
}

function masterSetToggleButton(id, state, onLabel='ON', offLabel='OFF', unknownLabel='N/A') {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.classList.remove('state-on', 'state-off', 'state-action', 'state-danger');
  if (state === true) {
    btn.textContent = onLabel;
    btn.classList.add('state-on');
    return;
  }
  if (state === false) {
    btn.textContent = offLabel;
    btn.classList.add('state-off');
    return;
  }
  btn.textContent = unknownLabel;
  btn.classList.add('state-action');
}

function masterSetStatePillButton(id, state, onLabel='ON', offLabel='OFF', unknownLabel='N/A') {
  const btn = document.getElementById(id);
  if (!btn) return;
  const normalized = masterNormalizeBoolean(state);
  masterSetReadOnlyState(id, state, onLabel, offLabel, unknownLabel);
  btn.disabled = normalized === null;
}

function masterSetManualToggleButton(id, running) {
  const btn = document.getElementById(id);
  if (!btn) return;
  btn.classList.remove('state-on', 'state-off', 'state-action', 'state-danger');
  if (running === true) {
    btn.textContent = 'STOP';
    btn.classList.add('state-danger');
    return;
  }
  btn.textContent = 'START';
  btn.classList.add('state-action');
}

async function masterFetchStatus(path) {
  try {
    return await api(path);
  } catch (err) {
    return null;
  }
}

function masterBonsaiText(st) {
  if (!st || !st.config) return 'No data';
  if (!st.gpio_ready) return 'GPIO unavailable';
  if (st.moisture === null || st.moisture === undefined) return 'Sensor offline';
  if (st.pump && st.pump.running) return 'Pump running';
  if (Number(st.moisture) < Number(st.config.moisture_threshold_low)) return 'Dry';
  if (Number(st.moisture) > Number(st.config.moisture_threshold_high)) return 'Wet';
  return 'OK';
}

function masterSetText(id, text) {
  const el = document.getElementById(id);
  if (!el) return;
  const value = String(text ?? '');
  el.textContent = value;
  if (value.length > 80) {
    el.title = value;
  } else {
    el.title = '';
  }
}

let masterActionTimer = null;
const masterUiState = {
  haSpeakers: null,
  haLamps: null,
  haLampLeft: null,
  haLampRight: null,
  bonsaiAuto: null,
  bonsaiManual: null,
  bonsaiOled: null,
  piholeBlocking: null,
};
let masterDimmerDebounceTimer = null;
let masterDimmerLastSent = null;

function masterNotify(message, isError=false) {
  const el = document.getElementById('masterActionMsg');
  if (!el) return;
  const full = String(message || '');
  el.textContent = masterBriefMessage(full, 120) || full;
  el.title = full;
  el.classList.toggle('error', !!isError);
  if (masterActionTimer) clearTimeout(masterActionTimer);
  masterActionTimer = setTimeout(() => {
    el.classList.remove('error');
  }, 3200);
}

async function masterPost(path, payload) {
  return api(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
}

async function masterRunAction(label, actionFn) {
  try {
    const response = await actionFn();
    const msg = response && (response.message || response.msg || response.error);
    masterNotify(msg || (label + ' updated.'));
  } catch (err) {
    masterNotify(label + ' failed: ' + err.message, true);
  }
  masterCachedPihole = null;
  await masterRefresh();
}

async function masterSetHaBothSpeakers(on) {
  await masterRunAction(on ? 'Speakers ON' : 'Speakers OFF', async () => {
    await Promise.all([
      masterPost('/api/ha/speaker', {side: 'left', on: !!on}),
      masterPost('/api/ha/speaker', {side: 'right', on: !!on}),
    ]);
    return {message: on ? 'Both speakers ON.' : 'Both speakers OFF.'};
  });
}

async function masterSetHaBothLamps(on) {
  await masterRunAction(on ? 'Lamps ON' : 'Lamps OFF', async () => {
    await masterPost('/api/ha/lamps', {on: !!on});
    return {message: on ? 'Both lamps ON.' : 'Both lamps OFF.'};
  });
}

function masterLampDimmerInputChanged() {
  const input = document.getElementById('masterLampDimmer');
  if (!input) return;
  const value = parseInt(input.value, 10) || 80;
  masterSetText('masterLampDimmerValue', value + '%');
  masterScheduleHaLampBrightness();
}

async function masterSetHaLampPalette(palette) {
  await masterRunAction(
    'Lamp color',
    () => masterPost('/api/ha/lamp_palette', {palette: String(palette || '').toLowerCase()})
  );
}

function masterScheduleHaLampBrightness() {
  if (masterDimmerDebounceTimer) clearTimeout(masterDimmerDebounceTimer);
  masterDimmerDebounceTimer = setTimeout(() => {
    void masterApplyHaLampBrightness(true);
  }, 240);
}

async function masterApplyHaLampBrightness(fromSlider=false) {
  const input = document.getElementById('masterLampDimmer');
  const brightness = input ? (parseInt(input.value, 10) || 80) : 80;
  if (fromSlider && masterDimmerLastSent === brightness) return;
  try {
    const response = await masterPost('/api/ha/lamp_brightness', {brightness_pct: brightness});
    masterDimmerLastSent = brightness;
    if (!fromSlider) {
      const msg = response && (response.message || response.msg || response.error);
      masterNotify(msg || 'Lamp dimmer updated.');
    }
  } catch (err) {
    masterNotify('Lamp dimmer failed: ' + err.message, true);
  }
  masterCachedPihole = null;
  await masterRefresh();
}

async function masterSetBonsaiAuto(enabled) {
  await masterRunAction(
    enabled ? 'Auto watering ON' : 'Auto watering OFF',
    () => masterPost('/api/bonsai/auto_mode', {enabled: !!enabled})
  );
}

async function masterSetBonsaiManual(enabled) {
  await masterRunAction(
    enabled ? 'Manual pump START' : 'Manual pump STOP',
    () => masterPost('/api/bonsai/manual_toggle', {enabled: !!enabled})
  );
}

async function masterSetBonsaiOled(enabled) {
  await masterRunAction(
    enabled ? 'OLED ON' : 'OLED OFF',
    () => masterPost('/api/bonsai/oled', {enabled: !!enabled})
  );
}

async function masterSetPiholeBlocking(enabled) {
  await masterRunAction(
    enabled ? 'DNS blocking ON' : 'DNS blocking OFF',
    () => masterPost('/api/pihole/blocking', {enabled: !!enabled})
  );
}

async function masterToggleHaSpeakers() {
  const target = masterUiState.haSpeakers === true ? false : true;
  await masterSetHaBothSpeakers(target);
}

async function masterToggleHaLamps() {
  const target = masterUiState.haLamps === true ? false : true;
  await masterSetHaBothLamps(target);
}

async function masterToggleHaLamp(side) {
  const sideNorm = String(side || '').toLowerCase();
  const current = sideNorm === 'left' ? masterUiState.haLampLeft : sideNorm === 'right' ? masterUiState.haLampRight : null;
  if (current === null) {
    masterNotify('Lamp state unavailable.', true);
    return;
  }
  await masterRunAction(
    current ? ('Lamp ' + sideNorm + ' OFF') : ('Lamp ' + sideNorm + ' ON'),
    () => masterPost('/api/ha/lamp', {side: sideNorm, on: !current})
  );
}

async function masterToggleBonsaiAuto() {
  const target = masterUiState.bonsaiAuto === true ? false : true;
  await masterSetBonsaiAuto(target);
}

async function masterToggleBonsaiManual() {
  const target = masterUiState.bonsaiManual === true ? false : true;
  await masterSetBonsaiManual(target);
}

async function masterToggleBonsaiOled() {
  const target = masterUiState.bonsaiOled === true ? false : true;
  await masterSetBonsaiOled(target);
}

async function masterTogglePiholeBlocking() {
  const target = masterUiState.piholeBlocking === true ? false : true;
  await masterSetPiholeBlocking(target);
}

let masterCachedPihole = null;
let masterLastPiholePollMs = 0;

async function masterRefresh() {
  const [ha, bonsai] = await Promise.all([
    masterFetchStatus('/api/ha/status'),
    masterFetchStatus('/api/bonsai/status'),
  ]);
  const nowMs = Date.now();
  let pihole = masterCachedPihole;
  if (!masterCachedPihole || (nowMs - masterLastPiholePollMs) > 15000) {
    pihole = await masterFetchStatus('/api/pihole/status');
    masterCachedPihole = pihole;
    masterLastPiholePollMs = nowMs;
  }

  const now = new Date();
  masterSetText('masterUpdated', 'Updated ' + now.toLocaleTimeString());

  if (ha) {
    const haMsg = ha.connected ? 'Connected' : (ha.message || 'Unavailable');
    masterSetConnPill('masterConnHa', !!ha.connected, haMsg);
    masterSetConnPill('masterHaConnection', !!ha.connected, haMsg);

    masterSetReadOnlyState('masterHaPlug', ha.switch_state);
    masterSetReadOnlyState('masterHaLight', ha.light_state);
    masterSetReadOnlyState('masterHaSpeakerLeft', ha.speaker_left_state);
    masterSetReadOnlyState('masterHaSpeakerRight', ha.speaker_right_state);
    masterSetStatePillButton('masterHaLampLeft', ha.lamp_left_state);
    masterSetStatePillButton('masterHaLampRight', ha.lamp_right_state);

    const speakerLeftBool = masterNormalizeBoolean(ha.speaker_left_state);
    const speakerRightBool = masterNormalizeBoolean(ha.speaker_right_state);
    const lampLeftBool = masterNormalizeBoolean(ha.lamp_left_state);
    const lampRightBool = masterNormalizeBoolean(ha.lamp_right_state);
    const boolText = (value) => value === true ? 'ON' : (value === false ? 'OFF' : 'N/A');

    const speakerAnyOn = speakerLeftBool === true || speakerRightBool === true;
    const speakersBothOff = speakerLeftBool === false && speakerRightBool === false;
    const lampAnyOn = lampLeftBool === true || lampRightBool === true;
    const lampsBothOff = lampLeftBool === false && lampRightBool === false;

    masterSetText('masterSpeakerKpi', speakerAnyOn ? 'ON' : (speakersBothOff ? 'OFF' : 'N/A'));
    masterSetText('masterSpeakerMeta', 'L ' + boolText(speakerLeftBool) + ' | R ' + boolText(speakerRightBool));
    masterSetText('masterLampKpi', lampAnyOn ? 'ON' : (lampsBothOff ? 'OFF' : 'N/A'));
    masterSetText(
      'masterLampMeta',
      String(ha.lamp_palette_last || 'none').toUpperCase()
      + ' | L ' + boolText(lampLeftBool)
      + ' | R ' + boolText(lampRightBool)
    );

    const brightness = Number(ha.lamp_brightness_last || 0);
    masterSetText('masterHaDimmer', brightness > 0 ? (brightness + '%') : '--');

    const speakersBothState = (speakerLeftBool === true && speakerRightBool === true)
      ? true
      : (speakerLeftBool === false && speakerRightBool === false ? false : null);
    const lampsBothState = (lampLeftBool === true && lampRightBool === true)
      ? true
      : (lampLeftBool === false && lampRightBool === false ? false : null);

    masterUiState.haSpeakers = speakersBothState;
    masterUiState.haLamps = lampsBothState;
    masterUiState.haLampLeft = lampLeftBool;
    masterUiState.haLampRight = lampRightBool;
    masterSetToggleButton('masterHaSpeakersToggleBtn', speakersBothState, 'BOTH ON', 'BOTH OFF', 'ONE/BOTH OFF');
    masterSetToggleButton('masterHaLampsToggleBtn', lampsBothState, 'BOTH ON', 'BOTH OFF', 'ONE/BOTH OFF');

    const dimmer = document.getElementById('masterLampDimmer');
    const dimmerBrightness = Number(ha.lamp_brightness_last || 80);
    if (dimmer && document.activeElement !== dimmer) {
      dimmer.value = String(dimmerBrightness);
    }
    const clampedDimmer = Math.max(1, Math.min(100, dimmerBrightness));
    masterSetText('masterLampDimmerValue', clampedDimmer + '%');
    masterDimmerLastSent = clampedDimmer;
  } else {
    masterSetConnPill('masterConnHa', false, 'Unavailable');
    masterSetConnPill('masterHaConnection', false, 'Unavailable');
    masterSetText('masterSpeakerKpi', 'N/A');
    masterSetText('masterSpeakerMeta', 'L N/A | R N/A');
    masterSetText('masterLampKpi', 'N/A');
    masterSetText('masterLampMeta', 'NONE | L N/A | R N/A');
    masterUiState.haSpeakers = null;
    masterUiState.haLamps = null;
    masterUiState.haLampLeft = null;
    masterUiState.haLampRight = null;
    masterSetToggleButton('masterHaSpeakersToggleBtn', null, 'BOTH ON', 'BOTH OFF', 'ONE/BOTH OFF');
    masterSetToggleButton('masterHaLampsToggleBtn', null, 'BOTH ON', 'BOTH OFF', 'ONE/BOTH OFF');
    masterSetStatePillButton('masterHaLampLeft', null);
    masterSetStatePillButton('masterHaLampRight', null);
    masterSetText('masterLampDimmerValue', '--');
    masterDimmerLastSent = null;
  }

  if (bonsai) {
    masterSetConnPill('masterConnBonsai', true, 'Connected');

    const moistureText = bonsai.moisture === null || bonsai.moisture === undefined ? '--' : (bonsai.moisture + '%');
    const bonsaiState = masterBonsaiText(bonsai);
    masterSetText('masterMoistureKpi', moistureText);
    masterSetText('masterMoistureState', bonsaiState);
    masterSetText('masterBonsaiMoisture', moistureText);
    masterSetText('masterBonsaiState', bonsaiState.toUpperCase());

    const pumpRunning = !!(bonsai.pump && bonsai.pump.running);
    const pumpMode = pumpRunning ? String(bonsai.pump.mode || 'run').toUpperCase() : 'OFF';
    const remaining = pumpRunning ? String(bonsai.pump.remaining_seconds || 0) + 's' : '--';
    masterSetText('masterPumpKpi', pumpMode);
    masterSetText('masterPumpMeta', pumpRunning ? ('Remaining: ' + remaining) : 'Idle');
    masterSetText('masterBonsaiPumpMode', pumpMode);
    masterSetText('masterBonsaiPumpRemaining', remaining);

    const autoOn = !!(bonsai.config && bonsai.config.auto_watering_enabled);
    masterSetText('masterAutoWaterKpi', autoOn ? 'ON' : 'OFF');
    masterSetText('masterLastWatered', 'Last: ' + String(bonsai.last_watered || '--'));
    masterSetReadOnlyState('masterBonsaiAuto', autoOn ? 'on' : 'off');
    masterSetStatePillButton('masterBonsaiOled', bonsai.oled_enabled ? 'on' : 'off');
    masterSetReadOnlyState('masterBonsaiGpio', bonsai.gpio_ready ? 'on' : 'off', 'READY', 'OFFLINE', 'UNKNOWN');

    const low = bonsai.config ? bonsai.config.moisture_threshold_low : '--';
    const high = bonsai.config ? bonsai.config.moisture_threshold_high : '--';
    masterSetText('masterBonsaiThresholds', low + '% / ' + high + '%');
    const manualRunning = pumpRunning && String(bonsai.pump.mode || '') === 'manual';
    masterUiState.bonsaiAuto = autoOn;
    masterUiState.bonsaiOled = !!bonsai.oled_enabled;
    masterUiState.bonsaiManual = manualRunning;
    masterSetToggleButton('masterBonsaiAutoToggleBtn', autoOn, 'ON', 'OFF', 'N/A');
    masterSetManualToggleButton('masterBonsaiManualToggleBtn', manualRunning);
  } else {
    masterSetConnPill('masterConnBonsai', false, 'Unavailable');
    masterUiState.bonsaiAuto = null;
    masterUiState.bonsaiOled = null;
    masterUiState.bonsaiManual = null;
    masterSetToggleButton('masterBonsaiAutoToggleBtn', null, 'ON', 'OFF', 'N/A');
    masterSetStatePillButton('masterBonsaiOled', null);
    masterSetManualToggleButton('masterBonsaiManualToggleBtn', false);
  }

  if (pihole) {
    const piholeMsg = pihole.connected ? 'Connected' : (pihole.message || 'Unavailable');
    masterSetConnPill('masterConnPihole', !!pihole.connected, piholeMsg);
    masterSetConnPill('masterPiholeConnection', !!pihole.connected, piholeMsg);
    const piholeMode = String(pihole.mode_active || pihole.mode || 'auto').toUpperCase();

    const blocking = pihole.blocking === true ? 'ON' : pihole.blocking === false ? 'OFF' : 'N/A';
    masterSetText('masterDnsKpi', blocking);
    masterSetText('masterDnsMeta', 'Mode: ' + piholeMode);
    masterSetText('masterQueriesKpi', masterNum(pihole.queries_today, 0));
    masterSetText('masterBlockedPctKpi', pihole.blocked_percent === null || pihole.blocked_percent === undefined ? '--' : (Number(pihole.blocked_percent).toFixed(1) + '%'));
    masterSetText('masterBlockedCountMeta', 'Blocked: ' + masterNum(pihole.blocked_today, 0));

    masterSetText('masterPiholeMode', piholeMode);
    masterSetText('masterPiholeBlocking', blocking);
    masterSetText('masterPiholeQueries', masterNum(pihole.queries_today, 0));
    masterSetText('masterPiholeBlocked', masterNum(pihole.blocked_today, 0));
    masterSetText(
      'masterPiholePercent',
      pihole.blocked_percent === null || pihole.blocked_percent === undefined
        ? '--'
        : (Number(pihole.blocked_percent).toFixed(1) + '%')
    );
    masterSetReadOnlyState('masterPiholeTls', pihole.verify_tls ? 'on' : 'off', 'ON', 'OFF', 'N/A');
    masterSetText('masterPiholeMsg', masterBriefMessage(pihole.message, 88) || '--');
    masterUiState.piholeBlocking = pihole.blocking === true ? true : pihole.blocking === false ? false : null;
    masterSetToggleButton('masterPiholeBlockingToggleBtn', masterUiState.piholeBlocking, 'ON', 'OFF', 'N/A');
  } else {
    masterSetConnPill('masterConnPihole', false, 'Unavailable');
    masterSetConnPill('masterPiholeConnection', false, 'Unavailable');
    masterUiState.piholeBlocking = null;
    masterSetToggleButton('masterPiholeBlockingToggleBtn', null, 'ON', 'OFF', 'N/A');
  }
}
"""


def master_dashboard_init_js() -> str:
    return """
  await masterRefresh();
  setInterval(masterRefresh, 5000);
"""


def settings_dashboard_html() -> str:
    return """
  <div class="card">
    <div class="panel-title-row">
      <span class="material-symbols-rounded panel-title-icon">settings</span>
      <div class="panel-title" style="margin-bottom:0;">Hub Settings</div>
    </div>
    <div class="panel-meta">Updater, restart controls, and source configuration.</div>
  </div>

  <div class="card">
    <div class="panel-title">Connection Controls</div>
    <div class="row" style="margin-top:10px;">
      <button id="hubUpdateBtn" class="btn chip-btn" onclick="updateHubModules()">
        <span id="hubUpdateIcon" class="material-symbols-rounded" aria-hidden="true">system_update_alt</span>
        <span id="hubUpdateText">Update Modules</span>
      </button>
      <button id="hubRestartBtn" class="btn gray chip-btn" onclick="restartHubConnection()">
        <span id="hubRestartIcon" class="material-symbols-rounded" aria-hidden="true">restart_alt</span>
        <span id="hubRestartText">Reset Connection</span>
      </button>
    </div>
    <div id="settingsActionMsg" class="small muted" style="margin-top:10px; min-height:20px;"></div>
  </div>

  <div class="card">
    <div class="panel-title">Updater Source</div>
    <div class="grid">
      <div>
        <div class="small muted">Mode</div>
        <select id="settingsUpdateMode" onchange="settingsUpdateModeChanged()">
          <option value="git">Git</option>
          <option value="script">Script</option>
        </select>
      </div>
      <div>
        <div class="small muted">Branch</div>
        <input id="settingsUpdateBranch" type="text" placeholder="main">
      </div>
      <div>
        <div class="small muted">Auto deploy</div>
        <label><input id="settingsAutoDeploy" class="switch" type="checkbox"> Pull + restart on new commits</label>
      </div>
      <div>
        <div class="small muted">Poll interval (sec)</div>
        <input id="settingsAutoDeployPoll" type="number" min="30" max="3600" step="10" value="60">
      </div>
      <div style="grid-column: 1 / -1;">
        <div class="small muted">Git repo URL (HTTPS or SSH)</div>
        <input id="settingsUpdateRepoUrl" class="wide" type="text" placeholder="https://github.com/you/repo.git">
      </div>
    </div>
    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="settingsSaveUpdaterConfig()">Save Updater Settings</button>
      <button class="btn gray" onclick="settingsRefreshUpdaterConfig()">Refresh</button>
      <span id="settingsSaveMsg" class="small muted"></span>
    </div>
    <div id="settingsUpdaterStatus" class="small muted" style="margin-top:8px;">Loading updater config...</div>
  </div>
"""


def settings_dashboard_js() -> str:
    return """
function settingsSetMessage(text, isError=false) {
  const settingsMsg = document.getElementById('settingsActionMsg');
  const masterMsg = document.getElementById('masterActionMsg');
  [settingsMsg, masterMsg].forEach((el) => {
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('error', !!isError);
  });
}

function settingsUpdateModeChanged() {
  const mode = (document.getElementById('settingsUpdateMode')?.value || 'git').toLowerCase();
  const repoEl = document.getElementById('settingsUpdateRepoUrl');
  const branchEl = document.getElementById('settingsUpdateBranch');
  if (!repoEl || !branchEl) return;
  const usingGit = mode === 'git';
  repoEl.disabled = !usingGit;
  branchEl.disabled = !usingGit;
}

async function settingsRefreshUpdaterConfig() {
  try {
    const cfg = await getHubUpdateConfig();
    const mode = String(cfg.mode || 'git').toLowerCase();
    const repoUrl = String(cfg.repo_url || '');
    const branch = String(cfg.branch || 'main');
    const autoDeploy = !!cfg.auto_deploy;
    const pollSeconds = Number(cfg.poll_seconds || 60);

    const modeEl = document.getElementById('settingsUpdateMode');
    const repoEl = document.getElementById('settingsUpdateRepoUrl');
    const branchEl = document.getElementById('settingsUpdateBranch');
    const autoEl = document.getElementById('settingsAutoDeploy');
    const pollEl = document.getElementById('settingsAutoDeployPoll');
    if (modeEl) modeEl.value = mode;
    if (repoEl && document.activeElement !== repoEl) repoEl.value = repoUrl;
    if (branchEl && document.activeElement !== branchEl) branchEl.value = branch;
    if (autoEl) autoEl.checked = autoDeploy;
    if (pollEl && document.activeElement !== pollEl) {
      pollEl.value = String(Math.max(30, Math.min(3600, Number.isFinite(pollSeconds) ? Math.round(pollSeconds) : 60)));
    }
    settingsUpdateModeChanged();

    const statusEl = document.getElementById('settingsUpdaterStatus');
    if (statusEl) {
      if (mode === 'git') {
        statusEl.textContent = repoUrl
          ? ('Git source: ' + repoUrl + ' (' + branch + ') | Auto deploy: ' + (autoDeploy ? 'ON' : 'OFF'))
          : 'Git source not configured yet.';
      } else {
        statusEl.textContent = 'Script source: /home/madmaestro/bonsai-water/update_modules.sh';
      }
    }
  } catch (err) {
    const statusEl = document.getElementById('settingsUpdaterStatus');
    if (statusEl) statusEl.textContent = 'Updater config unavailable.';
  }
}

async function settingsSaveUpdaterConfig() {
  const mode = String(document.getElementById('settingsUpdateMode')?.value || 'git').toLowerCase();
  const repo_url = String(document.getElementById('settingsUpdateRepoUrl')?.value || '').trim();
  const branch = String(document.getElementById('settingsUpdateBranch')?.value || 'main').trim() || 'main';
  const autoDeploy = !!document.getElementById('settingsAutoDeploy')?.checked;
  const pollEl = document.getElementById('settingsAutoDeployPoll');
  let pollSeconds = parseInt(String(pollEl?.value || '60'), 10);
  if (!Number.isFinite(pollSeconds)) pollSeconds = 60;
  pollSeconds = Math.max(30, Math.min(3600, pollSeconds));
  if (pollEl) pollEl.value = String(pollSeconds);
  const payload = {mode, repo_url, branch, auto_deploy: autoDeploy, poll_seconds: pollSeconds};
  const msg = document.getElementById('settingsSaveMsg');
  try {
    const r = await saveHubUpdateConfig(payload);
    if (msg) {
      msg.textContent = r.ok ? 'Updater settings saved.' : 'Save failed.';
      setTimeout(() => { msg.textContent = ''; }, 2200);
    }
    await settingsRefreshUpdaterConfig();
  } catch (err) {
    if (msg) {
      msg.textContent = 'Save failed.';
      setTimeout(() => { msg.textContent = ''; }, 2200);
    }
  }
}
"""


def settings_dashboard_init_js() -> str:
    return """
  await settingsRefreshUpdaterConfig();
"""


def load_plugin_module_names() -> list[str]:
    if not os.path.exists(PLUGIN_CONFIG_FILE):
        return DEFAULT_PLUGIN_MODULES[:]

    try:
        with open(PLUGIN_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        print(f"[HUB] Failed to read plugins.json: {exc}")
        return DEFAULT_PLUGIN_MODULES[:]

    names = data.get("plugins", [])
    if not isinstance(names, list) or not names:
        return DEFAULT_PLUGIN_MODULES[:]

    cleaned: list[str] = []
    for name in names:
        if isinstance(name, str) and name.strip():
            cleaned.append(name.strip())
    return cleaned or DEFAULT_PLUGIN_MODULES[:]


def load_plugins(app_dir: str) -> list[Any]:
    plugins: list[Any] = []

    for mod_name in load_plugin_module_names():
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:
            print(f"[HUB] Failed to import {mod_name}: {exc}")
            continue

        create_plugin = getattr(module, "create_plugin", None)
        if create_plugin is None:
            print(f"[HUB] Module {mod_name} has no create_plugin(app_dir)")
            continue

        try:
            plugin = create_plugin(app_dir)
            plugins.append(plugin)
            plugin_id = getattr(plugin, "plugin_id", mod_name)
            print(f"[HUB] Loaded plugin: {plugin_id}")
        except Exception as exc:
            print(f"[HUB] Failed to initialize plugin {mod_name}: {exc}")

    return plugins


def create_app(plugins: list[Any]) -> Flask:
    app = Flask(__name__)
    plugin_map: dict[str, Any] = {}

    def _schedule_process_exit() -> None:
        def _terminate_current_process() -> None:
            time.sleep(0.2)
            os._exit(0)

        threading.Thread(target=_terminate_current_process, daemon=True).start()

    def _launch_hub_restart(update_cmd: str | None = None) -> tuple[bool, str]:
        script_path = os.path.join(APP_DIR, "pi_hub.py")
        python_cmd = shlex.quote(sys.executable)
        script_cmd = shlex.quote(script_path)

        if update_cmd:
            shell_cmd = (
                f"cd {shlex.quote(APP_DIR)} && "
                f"({update_cmd}) >>/tmp/pi_hub_update.log 2>&1; "
                f"(sleep 1; nohup {python_cmd} -u {script_cmd} >/tmp/pi_hub.log 2>&1 < /dev/null &)"
            )
        else:
            shell_cmd = (
                f"cd {shlex.quote(APP_DIR)} && "
                f"(sleep 1; nohup {python_cmd} -u {script_cmd} >/tmp/pi_hub.log 2>&1 < /dev/null &)"
            )

        try:
            subprocess.Popen(
                ["/bin/sh", "-c", shell_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            return False, f"Restart launch failed: {exc}"

        _schedule_process_exit()
        return True, "Restarting Pi Control Hub..."

    def _resolve_update_command() -> tuple[str | None, str, str | None, bool]:
        env_cmd = str(os.environ.get("PI_HUB_UPDATE_CMD", "")).strip()
        if env_cmd:
            return env_cmd, "PI_HUB_UPDATE_CMD", None, False

        update_cfg = load_hub_update_config()
        mode = str(update_cfg.get("mode", "git")).strip().lower()

        if mode == "script":
            if os.path.isfile(PI_HUB_UPDATE_SCRIPT):
                return f"/bin/sh {shlex.quote(PI_HUB_UPDATE_SCRIPT)}", "update_modules.sh", None, False
            return None, "", "Script mode selected but update_modules.sh is missing.", False

        if mode == "git":
            repo_url = str(update_cfg.get("repo_url", "")).strip()
            branch = str(update_cfg.get("branch", "main")).strip() or "main"
            if not repo_url:
                return None, "", "Git repo URL is not configured yet.", True

            git_cmd = (
                "set -e; "
                f"cd {shlex.quote(APP_DIR)}; "
                "export GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new'; "
                "if [ ! -d .git ]; then git init -q; fi; "
                "if git remote get-url origin >/dev/null 2>&1; then "
                f"git remote set-url origin {shlex.quote(repo_url)}; "
                "else "
                f"git remote add origin {shlex.quote(repo_url)}; "
                "fi; "
                f"git fetch --depth 1 origin {shlex.quote(branch)}; "
                "git reset --hard FETCH_HEAD"
            )
            return git_cmd, f"git ({branch})", None, False

        has_git = os.path.isdir(os.path.join(APP_DIR, ".git"))
        has_bundle = os.path.isfile(os.path.join(APP_DIR, "update_bundle.tgz"))
        has_incoming = False
        incoming_dir = os.path.join(APP_DIR, "_incoming")
        if os.path.isdir(incoming_dir):
            try:
                has_incoming = any(True for _ in os.scandir(incoming_dir))
            except Exception:
                has_incoming = False

        if os.path.isfile(PI_HUB_UPDATE_SCRIPT) and (has_git or has_bundle or has_incoming):
            return f"/bin/sh {shlex.quote(PI_HUB_UPDATE_SCRIPT)}", "update_modules.sh", None, False

        if has_git:
            return "git pull --ff-only", "git", None, False

        return None, "", "No updater source available.", False

    def _git_update_available(repo_url: str, branch: str) -> tuple[bool, str]:
        if not repo_url:
            return False, "Auto deploy skipped: Git repo URL is not configured."
        branch_name = branch or "main"
        probe_cmd = (
            "set -e; "
            f"cd {shlex.quote(APP_DIR)}; "
            "export GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new'; "
            "if [ ! -d .git ]; then git init -q; fi; "
            "if git remote get-url origin >/dev/null 2>&1; then "
            f"git remote set-url origin {shlex.quote(repo_url)}; "
            "else "
            f"git remote add origin {shlex.quote(repo_url)}; "
            "fi; "
            f"git fetch --depth 1 origin {shlex.quote(branch_name)}; "
            "if git rev-parse --verify HEAD >/dev/null 2>&1; then "
            "LOCAL=$(git rev-parse HEAD); REMOTE=$(git rev-parse FETCH_HEAD); "
            "if [ \"$LOCAL\" = \"$REMOTE\" ]; then echo NO_CHANGE; else echo UPDATE; fi; "
            "else echo UPDATE; fi"
        )
        try:
            completed = subprocess.run(
                ["/bin/sh", "-lc", probe_cmd],
                check=False,
                capture_output=True,
                text=True,
                timeout=40,
            )
        except Exception as exc:
            return False, f"Auto deploy probe failed: {exc}"

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip()
            short = details.splitlines()[-1] if details else "git probe failed"
            return False, f"Auto deploy probe failed: {short}"

        marker = ""
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        if lines:
            marker = lines[-1]
        return marker == "UPDATE", "update available" if marker == "UPDATE" else "no change"

    for plugin in plugins:
        plugin_id = safe_plugin_key(getattr(plugin, "plugin_id", plugin.__class__.__name__))
        plugin_map[plugin_id] = plugin
        try:
            plugin.register_routes(app)
        except Exception as exc:
            plugin_id = getattr(plugin, "plugin_id", plugin.__class__.__name__)
            print(f"[HUB] Failed to register routes for {plugin_id}: {exc}")

    @app.route("/")
    def home():
        nav_items: list[str] = []
        plugin_panes: list[str] = []

        master_pane_id = "pane-master"
        master_meta = MODULE_META.get("master", {"tag": "MASTER", "hint": "Unified read-only overview", "icon": "dashboard"})
        nav_items.append(
            render_module_nav_item(
                pane_id=master_pane_id,
                module_name="Master",
                module_meta=master_meta,
                active=True,
                module_key="master",
            )
        )
        plugin_panes.append(
            render_module_pane(
                pane_id=master_pane_id,
                module_name="Master",
                module_meta=master_meta,
                module_html=master_dashboard_html(),
                active=True,
            )
        )

        for plugin in plugins:
            plugin_id = safe_plugin_key(getattr(plugin, "plugin_id", plugin.__class__.__name__))
            plugin_name = str(getattr(plugin, "display_name", plugin.__class__.__name__))
            pane_id = f"pane-{plugin_id}"
            meta = MODULE_META.get(plugin_id, {"tag": "MOD", "hint": "Plugin module"})
            plugin_panes.append(
                render_module_pane(
                    pane_id=pane_id,
                    module_name=plugin_name,
                    module_meta=meta,
                    module_html=plugin.dashboard_html(),
                    active=False,
                )
            )
            nav_items.append(
                render_module_nav_item(
                    pane_id=pane_id,
                    module_name=plugin_name,
                    module_meta=meta,
                    active=False,
                    module_key=plugin_id,
                )
            )

        settings_pane_id = "pane-settings"
        settings_meta = MODULE_META.get("settings", {"tag": "SET", "hint": "Updater and hub controls", "icon": "settings"})
        nav_items.append(
            render_module_nav_item(
                pane_id=settings_pane_id,
                module_name="Settings",
                module_meta=settings_meta,
                active=False,
                module_key="settings",
            )
        )
        plugin_panes.append(
            render_module_pane(
                pane_id=settings_pane_id,
                module_name="Settings",
                module_meta=settings_meta,
                module_html=settings_dashboard_html(),
                active=False,
            )
        )

        plugin_nav_html = "\n".join(nav_items)
        plugin_html = "\n".join(plugin_panes)
        js_parts = [master_dashboard_js(), settings_dashboard_js()]
        js_parts.extend(plugin.dashboard_js() for plugin in plugins if plugin.dashboard_js().strip())
        plugin_js = "\n\n".join(js_parts)
        init_parts = [master_dashboard_init_js(), settings_dashboard_init_js()]
        init_parts.extend(plugin.dashboard_init_js() for plugin in plugins if plugin.dashboard_init_js().strip())
        plugin_init = "\n".join(init_parts)

        html = """
<!doctype html>
<html data-theme=\"dark\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Pi Control Hub</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Nunito+Sans:wght@500;600;700;800&family=Space+Grotesk:wght@500;600;700&family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@24,500,0,0&display=swap');
    :root {
      --bg: #0f141f;
      --bg2: #151d2c;
      --bg3: #1f2839;
      --card: #171f2d;
      --card-2: #1c2535;
      --txt: #edf2ff;
      --sub: #a9b4cc;
      --line: rgba(136, 154, 190, 0.22);
      --line-soft: rgba(136, 154, 190, 0.12);
      --primary: #6b84e7;
      --primary-2: #5a72d6;
      --primary-soft: rgba(107, 132, 231, 0.18);
      --ok: #42c58f;
      --warn: #e0ac53;
      --bad: #e87474;
      --shadow: 0 10px 26px rgba(6, 10, 20, 0.34);
    }
    html[data-theme='light'] {
      --bg: #ecf1f8;
      --bg2: #f4f7fc;
      --bg3: #dce4f1;
      --card: #ffffff;
      --card-2: #f8fafe;
      --txt: #1f293b;
      --sub: #62708a;
      --line: rgba(82, 102, 145, 0.2);
      --line-soft: rgba(82, 102, 145, 0.1);
      --primary: #4a67de;
      --primary-2: #3d57c0;
      --primary-soft: rgba(74, 103, 222, 0.15);
      --ok: #239666;
      --warn: #ba8426;
      --bad: #ce5757;
      --shadow: 0 10px 20px rgba(73, 90, 125, 0.18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: 'Nunito Sans', 'Avenir Next', 'Segoe UI', sans-serif;
      font-size: 17px;
      line-height: 1.45;
      color: var(--txt);
      background:
        radial-gradient(1300px 760px at -12% -20%, rgba(116, 145, 255, 0.14), transparent 62%),
        radial-gradient(940px 680px at 112% 0%, rgba(66, 208, 169, 0.08), transparent 56%),
        linear-gradient(165deg, var(--bg2) 0%, var(--bg) 72%);
      min-height: 100vh;
    }
    a { color: inherit; }
    .material-symbols-rounded {
      font-family: 'Material Symbols Rounded';
      font-weight: normal;
      font-style: normal;
      font-size: 24px;
      display: inline-block;
      line-height: 1;
      text-transform: none;
      letter-spacing: normal;
      white-space: nowrap;
      word-wrap: normal;
      direction: ltr;
      font-feature-settings: 'liga';
      -webkit-font-smoothing: antialiased;
    }
    .wrap { max-width: 1640px; margin: 0 auto; padding: 24px 22px 32px; }
    .card {
      background: linear-gradient(164deg, var(--card), var(--card-2));
      border: 1px solid var(--line-soft);
      border-radius: 20px;
      padding: 18px;
      margin-bottom: 14px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(3px);
    }
    .card.sub-card {
      border-radius: 14px;
      padding: 14px;
      box-shadow: none;
      border-color: var(--line-soft);
      background: linear-gradient(162deg, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0.01));
    }
    html[data-theme='light'] .card.sub-card {
      background: linear-gradient(162deg, #ffffff, #f6f9ff);
      border-color: rgba(82, 102, 145, 0.14);
    }
    .app-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 16px;
    }
    .eyebrow {
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--sub);
      font-weight: 700;
      margin-bottom: 8px;
    }
    .title {
      font-family: 'Space Grotesk', 'Nunito Sans', sans-serif;
      font-size: 40px;
      font-weight: 700;
      line-height: 1.1;
      margin: 0 0 4px;
    }
    .subtitle {
      margin: 0;
      font-size: 15px;
      color: var(--sub);
      max-width: 740px;
    }
    .head-actions {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: flex-end;
      flex-wrap: wrap;
    }
    .layout {
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    main { min-width: 0; }
    .sidebar {
      position: sticky;
      top: 16px;
      background: linear-gradient(166deg, var(--card), var(--card-2));
      border: 1px solid var(--line-soft);
      border-radius: 20px;
      padding: 12px;
      box-shadow: var(--shadow);
    }
    .side-head {
      margin: 3px 6px 10px;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.1em;
      color: var(--sub);
      text-transform: uppercase;
    }
    .side-link {
      --module-bg-a: rgba(76, 96, 142, 0.14);
      --module-bg-b: rgba(57, 80, 130, 0.08);
      --module-border: var(--line-soft);
      --module-hover-border: var(--line);
      --module-active-border: rgba(139, 165, 255, 0.42);
      --module-active-a: rgba(94, 121, 223, 0.16);
      --module-active-b: rgba(77, 102, 196, 0.08);
      --module-active-ring: rgba(139, 165, 255, 0.22);
      --module-icon-bg: var(--primary-soft);
      --module-icon-border: rgba(150, 173, 255, 0.38);
      --module-icon-color: #d4e0ff;
      --module-tag-bg: rgba(122, 146, 255, 0.18);
      --module-tag-border: rgba(144, 168, 255, 0.38);
      --module-tag-color: #d9e3ff;
      width: 100%;
      text-align: left;
      border: 1px solid var(--module-border);
      background: linear-gradient(140deg, var(--module-bg-a), var(--module-bg-b));
      color: var(--txt);
      border-radius: 14px;
      padding: 11px 11px;
      margin: 7px 0;
      cursor: pointer;
      transition: transform 130ms ease, border-color 130ms ease, box-shadow 130ms ease;
    }
    .side-link:hover {
      transform: translateY(-1px);
      border-color: var(--module-hover-border);
      box-shadow: 0 6px 14px rgba(8, 15, 28, 0.18);
    }
    .side-link.active {
      border-color: var(--module-active-border);
      background: linear-gradient(140deg, var(--module-active-a), var(--module-active-b));
      box-shadow: 0 0 0 1px var(--module-active-ring);
    }
    .side-link[data-module='master'] {
      --module-bg-a: rgba(102, 145, 243, 0.19);
      --module-bg-b: rgba(77, 115, 201, 0.1);
      --module-hover-border: rgba(144, 181, 255, 0.4);
      --module-active-border: rgba(154, 188, 255, 0.5);
      --module-active-a: rgba(108, 153, 251, 0.26);
      --module-active-b: rgba(81, 123, 214, 0.15);
      --module-active-ring: rgba(151, 184, 255, 0.28);
      --module-icon-bg: rgba(108, 153, 251, 0.24);
      --module-icon-border: rgba(149, 185, 255, 0.45);
      --module-icon-color: #e6efff;
      --module-tag-bg: rgba(108, 153, 251, 0.2);
      --module-tag-border: rgba(149, 185, 255, 0.42);
      --module-tag-color: #e2ecff;
    }
    .side-link[data-module='settings'] {
      --module-bg-a: rgba(119, 136, 168, 0.2);
      --module-bg-b: rgba(92, 108, 140, 0.11);
      --module-hover-border: rgba(165, 182, 216, 0.38);
      --module-active-border: rgba(175, 193, 228, 0.5);
      --module-active-a: rgba(127, 146, 180, 0.28);
      --module-active-b: rgba(100, 118, 151, 0.16);
      --module-active-ring: rgba(167, 186, 220, 0.27);
      --module-icon-bg: rgba(128, 147, 182, 0.24);
      --module-icon-border: rgba(173, 193, 228, 0.44);
      --module-icon-color: #ebf2ff;
      --module-tag-bg: rgba(129, 149, 185, 0.2);
      --module-tag-border: rgba(173, 193, 228, 0.42);
      --module-tag-color: #edf4ff;
    }
    .side-link[data-module='home_assistant'] {
      --module-bg-a: rgba(77, 166, 184, 0.18);
      --module-bg-b: rgba(56, 131, 150, 0.1);
      --module-hover-border: rgba(123, 212, 229, 0.38);
      --module-active-border: rgba(136, 220, 236, 0.46);
      --module-active-a: rgba(89, 180, 197, 0.24);
      --module-active-b: rgba(59, 145, 162, 0.14);
      --module-active-ring: rgba(128, 214, 231, 0.26);
      --module-icon-bg: rgba(90, 182, 200, 0.24);
      --module-icon-border: rgba(132, 223, 240, 0.42);
      --module-icon-color: #e1fbff;
      --module-tag-bg: rgba(90, 182, 200, 0.2);
      --module-tag-border: rgba(132, 223, 240, 0.4);
      --module-tag-color: #e0fbff;
    }
    .side-link[data-module='bonsai'] {
      --module-bg-a: rgba(74, 181, 123, 0.2);
      --module-bg-b: rgba(50, 141, 93, 0.1);
      --module-hover-border: rgba(116, 214, 158, 0.4);
      --module-active-border: rgba(131, 224, 172, 0.5);
      --module-active-a: rgba(82, 193, 132, 0.28);
      --module-active-b: rgba(56, 156, 103, 0.16);
      --module-active-ring: rgba(128, 219, 168, 0.28);
      --module-icon-bg: rgba(82, 193, 132, 0.25);
      --module-icon-border: rgba(132, 225, 173, 0.44);
      --module-icon-color: #e6fff0;
      --module-tag-bg: rgba(82, 193, 132, 0.2);
      --module-tag-border: rgba(132, 225, 173, 0.42);
      --module-tag-color: #e4ffef;
    }
    .side-link[data-module='pihole'] {
      --module-bg-a: rgba(232, 164, 68, 0.22);
      --module-bg-b: rgba(189, 125, 39, 0.11);
      --module-hover-border: rgba(247, 191, 112, 0.42);
      --module-active-border: rgba(251, 203, 132, 0.54);
      --module-active-a: rgba(236, 172, 82, 0.3);
      --module-active-b: rgba(196, 134, 51, 0.17);
      --module-active-ring: rgba(242, 192, 117, 0.3);
      --module-icon-bg: rgba(236, 172, 82, 0.24);
      --module-icon-border: rgba(249, 198, 125, 0.44);
      --module-icon-color: #fff5e4;
      --module-tag-bg: rgba(236, 172, 82, 0.2);
      --module-tag-border: rgba(249, 198, 125, 0.42);
      --module-tag-color: #fff4df;
    }
    .side-link-top {
      display: flex;
      align-items: center;
      gap: 11px;
      margin-bottom: 4px;
    }
    .module-icon {
      width: 36px;
      height: 36px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      background: var(--module-icon-bg);
      color: var(--module-icon-color);
      border: 1px solid var(--module-icon-border);
      flex: none;
    }
    .module-name-wrap {
      display: flex;
      flex-direction: column;
      gap: 1px;
      min-width: 0;
    }
    .module-tag {
      display: inline-flex;
      width: fit-content;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--module-tag-color);
      background: var(--module-tag-bg);
      border: 1px solid var(--module-tag-border);
    }
    .module-name {
      font-family: 'Space Grotesk', 'Nunito Sans', sans-serif;
      font-size: 20px;
      font-weight: 700;
      line-height: 1.15;
    }
    .module-hint {
      color: var(--sub);
      font-size: 14px;
      line-height: 1.3;
    }
    .plugin-pane { display: none; }
    .plugin-pane.active { display: block; }
    .pane-header {
      margin: 2px 0 14px;
      padding: 0 2px;
    }
    .pane-title-row {
      display: flex;
      align-items: center;
      gap: 11px;
    }
    .pane-icon {
      width: 42px;
      height: 42px;
      border-radius: 12px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      color: #d9e2ff;
      background: linear-gradient(140deg, rgba(104, 132, 245, 0.2), rgba(84, 111, 224, 0.14));
      border: 1px solid rgba(151, 173, 255, 0.4);
      flex: none;
    }
    html[data-theme='light'] .pane-icon {
      color: var(--primary-2);
    }
    .pane-title {
      font-family: 'Space Grotesk', 'Nunito Sans', sans-serif;
      font-size: 34px;
      font-weight: 700;
      line-height: 1.12;
      margin: 0;
    }
    .pane-subtitle {
      margin-top: 3px;
      font-size: 15px;
      color: var(--sub);
    }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .panel-title {
      font-size: 16px;
      font-weight: 800;
      letter-spacing: 0.01em;
      margin-bottom: 9px;
    }
    .label-icon {
      font-size: 16px;
      color: var(--sub);
      vertical-align: -2px;
      margin-right: 5px;
    }
    .panel-title .label-icon {
      color: var(--primary);
      margin-right: 7px;
    }
    .panel-title-row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .panel-title-icon {
      font-size: 20px;
      color: var(--primary);
    }
    .panel-meta {
      margin-top: 5px;
      font-size: 13px;
      color: var(--sub);
    }
    .kpi { font-size: 44px; font-weight: 800; color: var(--ok); line-height: 1.05; }
    .muted { color: var(--sub); }
    label { font-size: 15px; font-weight: 700; color: var(--txt); }
    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      border: 1px solid transparent;
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 13.5px;
      font-weight: 800;
      letter-spacing: 0.01em;
      cursor: pointer;
      color: #f8fbff;
      text-decoration: none;
      background: linear-gradient(145deg, var(--primary), var(--primary-2));
      box-shadow: 0 6px 12px rgba(17, 28, 54, 0.22);
      transition: transform 110ms ease, filter 110ms ease, border-color 110ms ease;
      min-height: 40px;
    }
    .btn:hover { transform: translateY(-1px); filter: brightness(1.04); }
    .btn:active { transform: translateY(0); }
    .btn:disabled {
      opacity: 0.62;
      cursor: not-allowed;
      transform: none;
      filter: none;
    }
    .btn.gray {
      background: linear-gradient(145deg, rgba(111, 127, 162, 0.36), rgba(95, 109, 141, 0.28));
      border-color: var(--line-soft);
      color: var(--txt);
      box-shadow: none;
    }
    .btn.control-btn { min-width: 146px; }
    .btn.state-on { background: linear-gradient(145deg, #37b986, #2aa877); }
    .btn.state-off { background: linear-gradient(145deg, #73809a, #64708a); }
    .btn.state-action { background: linear-gradient(145deg, var(--primary), var(--primary-2)); }
    .btn.state-danger { background: linear-gradient(145deg, #e27373, #d96262); }
    .btn.preset-btn {
      border-color: rgba(255, 255, 255, 0.15);
      box-shadow: none;
    }
    .btn.preset-warm {
      background: linear-gradient(145deg, rgba(249, 149, 76, 0.38), rgba(232, 103, 54, 0.3));
      color: #fff6eb;
    }
    .btn.preset-cool {
      background: linear-gradient(145deg, rgba(99, 124, 221, 0.4), rgba(73, 117, 212, 0.32));
      color: #eef3ff;
    }
    .btn.preset-money {
      background: linear-gradient(145deg, rgba(56, 172, 88, 0.4), rgba(112, 224, 136, 0.32));
      color: #effff2;
    }
    .btn.preset-candle {
      background: linear-gradient(145deg, rgba(236, 178, 98, 0.4), rgba(207, 133, 65, 0.32));
      color: #fff7ea;
    }
    input[type=number], input[type=text], input[type=password], select {
      width: 136px;
      padding: 10px 11px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(10, 16, 26, 0.36);
      color: var(--txt);
      font-size: 15px;
      font-family: inherit;
      min-height: 40px;
    }
    html[data-theme='light'] input[type=number],
    html[data-theme='light'] input[type=text],
    html[data-theme='light'] input[type=password],
    html[data-theme='light'] select {
      background: rgba(255, 255, 255, 0.8);
    }
    input.wide { width: min(100%, 460px); }
    input[type=range] {
      accent-color: var(--primary);
      width: min(100%, 320px);
    }
    .switch {
      width: 19px;
      height: 19px;
      transform: translateY(1px);
      accent-color: var(--primary);
    }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    .small { font-size: 14px; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      padding: 5px 10px;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.03em;
    }
    .status-ok {
      color: #8be7bd;
      background: rgba(63, 193, 136, 0.14);
      border-color: rgba(96, 219, 161, 0.28);
    }
    .status-warn {
      color: #efcf93;
      background: rgba(224, 172, 83, 0.14);
      border-color: rgba(224, 172, 83, 0.26);
    }
    .status-bad {
      color: #f3b2b2;
      background: rgba(232, 116, 116, 0.15);
      border-color: rgba(232, 116, 116, 0.3);
    }
    button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 2px solid var(--primary);
      outline-offset: 2px;
      box-shadow: 0 0 0 3px rgba(97, 124, 235, 0.24);
    }
    .chip-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 11px;
      min-width: 116px;
      justify-content: center;
    }
    .foot-note {
      margin-top: 8px;
      font-size: 13px;
      color: var(--sub);
    }
    .master-overview-card {
      margin-bottom: 12px;
    }
    .master-kpi-grid {
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .master-kpi-card {
      margin: 0;
      min-height: 124px;
      padding: 14px 15px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .master-kpi-label {
      font-size: 12px;
      color: var(--sub);
      letter-spacing: 0.07em;
      text-transform: uppercase;
      font-weight: 800;
    }
    .master-kpi-value {
      font-family: 'Space Grotesk', 'Nunito Sans', sans-serif;
      font-size: 28px;
      line-height: 1.12;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .master-kpi-sub {
      font-size: 13px;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .master-detail-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      align-items: start;
    }
    .master-detail-card {
      margin: 0;
      min-height: 100%;
    }
    .master-list {
      display: grid;
      gap: 8px;
    }
    .master-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      border: 1px solid var(--line-soft);
      background: linear-gradient(150deg, rgba(120, 140, 190, 0.1), rgba(100, 120, 165, 0.05));
      border-radius: 12px;
      padding: 8px 9px;
      font-size: 12.5px;
      line-height: 1.2;
    }
    .master-item-name {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }
    .master-item-name .material-symbols-rounded {
      font-size: 16px;
      color: var(--sub);
    }
    html[data-theme='light'] .master-item {
      background: linear-gradient(150deg, rgba(232, 240, 255, 0.82), rgba(225, 235, 250, 0.65));
      border-color: rgba(82, 102, 145, 0.14);
    }
    .master-state {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 86px;
      padding: 5px 10px;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 11px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      font-weight: 800;
    }
    .master-state-btn {
      cursor: pointer;
      appearance: none;
      -webkit-appearance: none;
      font-family: inherit;
      line-height: 1;
      transition: transform 120ms ease, filter 150ms ease, opacity 150ms ease;
    }
    .master-state-btn:hover:not(:disabled) {
      filter: brightness(1.08);
    }
    .master-state-btn:active:not(:disabled) {
      transform: translateY(1px) scale(0.995);
    }
    .master-state-btn:disabled {
      cursor: default;
      opacity: 0.8;
    }
    .master-state.on {
      color: #91edc4;
      background: rgba(63, 193, 136, 0.14);
      border-color: rgba(96, 219, 161, 0.28);
    }
    .master-state.off {
      color: #bdc8df;
      background: rgba(116, 131, 166, 0.2);
      border-color: rgba(145, 160, 194, 0.28);
    }
    .master-state.neutral {
      color: #e2cb97;
      background: rgba(224, 172, 83, 0.14);
      border-color: rgba(224, 172, 83, 0.26);
    }
    .master-action-msg {
      margin-top: 10px;
      min-height: 20px;
    }
    .master-action-msg.error {
      color: #f3b2b2;
    }
    .master-controls {
      margin-top: 11px;
      padding-top: 10px;
      border-top: 1px solid var(--line-soft);
      display: grid;
      gap: 8px;
    }
    .master-control-group {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .master-control-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      color: var(--sub);
      font-weight: 800;
      min-width: 100px;
    }
    .master-mini-btn {
      min-height: 34px;
      padding: 7px 11px;
      font-size: 12px;
      line-height: 1;
      letter-spacing: 0.03em;
      box-shadow: none;
    }
    .master-msg {
      display: inline-block;
      max-width: 65%;
      text-align: right;
      font-size: 12px;
      line-height: 1.2;
      word-break: break-word;
      overflow-wrap: anywhere;
    }
    html[data-theme='light'] .master-state.off {
      color: #495a78;
      background: rgba(82, 102, 145, 0.12);
      border-color: rgba(82, 102, 145, 0.22);
    }
    @media (prefers-reduced-motion: reduce) {
      * {
        animation: none !important;
        transition: none !important;
        scroll-behavior: auto !important;
      }
    }
    @media (max-width: 1360px) {
      .master-kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .master-detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
        display: flex;
        flex-wrap: wrap;
        align-items: stretch;
        gap: 8px;
      }
      .side-head {
        width: 100%;
      }
      .side-link {
        display: inline-flex;
        width: auto;
        min-width: 236px;
        margin: 0;
      }
      .title { font-size: 34px; }
      .pane-title { font-size: 30px; }
      .master-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .master-detail-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      .wrap { padding: 18px 14px 28px; }
      .app-head { flex-direction: column; }
      .title { font-size: 30px; }
      .side-link { min-width: 100%; width: 100%; }
      .btn {
        min-height: 44px;
        padding: 10px 12px;
      }
      .btn.control-btn {
        min-width: 128px;
      }
      input[type=number], input[type=text], input[type=password], select {
        min-height: 44px;
      }
      input[type=range] {
        width: 100%;
      }
      .kpi { font-size: 36px; }
      input.wide { width: 100%; }
      .master-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .master-kpi-card { min-height: 108px; padding: 12px; }
      .master-kpi-value { font-size: 23px; }
      .master-item { padding: 8px 9px; }
      .master-state { min-width: 80px; font-size: 10px; }
      .master-control-label { min-width: 100%; }
      .master-mini-btn { min-height: 40px; }
    }
  </style>
</head>
<body>
<div class=\"wrap\">
  <header class=\"app-head card\">
    <div>
      <div class=\"title\">Pi Control Hub</div>
    </div>
    <div class=\"head-actions\">
      <button id=\"themeToggleBtn\" class=\"btn gray chip-btn\" onclick=\"toggleTheme()\">
        <span id=\"themeToggleIcon\" class=\"material-symbols-rounded\" aria-hidden=\"true\">dark_mode</span>
        <span id=\"themeToggleText\">Dark</span>
      </button>
    </div>
  </header>

  <div class=\"layout\">
    <aside class=\"sidebar\">
      <div class=\"side-head\">Modules</div>
      {{ plugin_nav_html|safe }}
    </aside>
    <main>
      {{ plugin_html|safe }}
      <div class=\"card foot-note\">Plugin-based hub active. Add modules by updating plugins.json.</div>
    </main>
  </div>
</div>

<script>
async function api(path, opts={}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function normalizeTheme(theme) {
  return theme === 'light' ? 'light' : 'dark';
}

function setTheme(theme) {
  const next = normalizeTheme(theme);
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('pi_hub_theme', next); } catch (err) {}
  const icon = document.getElementById('themeToggleIcon');
  const label = document.getElementById('themeToggleText');
  if (icon) icon.textContent = next === 'dark' ? 'dark_mode' : 'light_mode';
  if (label) label.textContent = next === 'dark' ? 'Dark' : 'Light';
}

function restoreThemePreference() {
  let saved = 'dark';
  try { saved = localStorage.getItem('pi_hub_theme') || 'dark'; } catch (err) {}
  setTheme(saved);
}

function toggleTheme() {
  const current = normalizeTheme(document.documentElement.getAttribute('data-theme') || 'dark');
  setTheme(current === 'dark' ? 'light' : 'dark');
}

function switchPane(paneId, btn) {
  document.querySelectorAll('.plugin-pane').forEach((pane) => pane.classList.remove('active'));
  document.querySelectorAll('.side-link').forEach((link) => link.classList.remove('active'));

  const pane = document.getElementById(paneId);
  if (pane) pane.classList.add('active');
  if (btn) btn.classList.add('active');
  try { localStorage.setItem('pi_hub_active_pane', paneId); } catch (err) {}
}

function restorePanePreference() {
  let saved = null;
  try { saved = localStorage.getItem('pi_hub_active_pane'); } catch (err) {}
  if (!saved) return;

  const pane = document.getElementById(saved);
  const btn = Array.from(document.querySelectorAll('.side-link')).find((b) => b.dataset.paneId === saved);
  if (pane && btn) switchPane(saved, btn);
}

function bindModuleHotkeys() {
  document.addEventListener('keydown', (ev) => {
    if (!ev.altKey || ev.repeat) return;
    const idx = Number(ev.key);
    if (!Number.isInteger(idx) || idx < 1) return;
    const btns = Array.from(document.querySelectorAll('.side-link'));
    const btn = btns[idx - 1];
    if (!btn) return;
    const paneId = btn.dataset.paneId;
    if (!paneId) return;
    ev.preventDefault();
    switchPane(paneId, btn);
  });
}

function setPiLinkPill(level, message) {
  const pill = document.getElementById('hubPiLink');
  if (!pill) return;
  pill.classList.remove('status-ok', 'status-warn', 'status-bad');
  if (level === 'ok') pill.classList.add('status-ok');
  else if (level === 'bad') pill.classList.add('status-bad');
  else pill.classList.add('status-warn');
  pill.textContent = 'Pi Link: ' + (message || 'Checking...');
}

async function refreshHubHealth() {
  try {
    const health = await api('/api/hub/health');
    const level = health.level || (health.connected ? 'ok' : 'bad');
    const message = health.message || (health.connected ? 'Connected' : 'Unavailable');
    setPiLinkPill(level, message);
  } catch (err) {
    setPiLinkPill('bad', 'Unavailable');
  }
}

function setHubRestartButtonBusy(busy) {
  const btn = document.getElementById('hubRestartBtn');
  const icon = document.getElementById('hubRestartIcon');
  const text = document.getElementById('hubRestartText');
  if (!btn || !icon || !text) return;
  btn.disabled = !!busy;
  icon.textContent = busy ? 'hourglass_top' : 'restart_alt';
  text.textContent = busy ? 'Restarting...' : 'Reset Connection';
}

function setHubUpdateButtonBusy(busy) {
  const btn = document.getElementById('hubUpdateBtn');
  const icon = document.getElementById('hubUpdateIcon');
  const text = document.getElementById('hubUpdateText');
  if (!btn || !icon || !text) return;
  btn.disabled = !!busy;
  icon.textContent = busy ? 'hourglass_top' : 'system_update_alt';
  text.textContent = busy ? 'Updating...' : 'Update Modules';
}

function setHubActionsDisabled(disabled) {
  const restartBtn = document.getElementById('hubRestartBtn');
  const updateBtn = document.getElementById('hubUpdateBtn');
  if (restartBtn) restartBtn.disabled = !!disabled;
  if (updateBtn) updateBtn.disabled = !!disabled;
}

async function waitForHubAndReload(maxTries=25, intervalMs=1000) {
  for (let i = 0; i < maxTries; i += 1) {
    try {
      const r = await fetch('/api/hub/health', {cache: 'no-store'});
      if (r.ok) {
        window.location.reload();
        return;
      }
    } catch (err) {}
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  window.location.reload();
}

async function postJsonWithStatus(path, payload={}) {
  const r = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  let data = {};
  try {
    data = await r.json();
  } catch (err) {
    data = {};
  }
  if (!r.ok) {
    const error = new Error((data && data.message) ? data.message : ('HTTP ' + r.status));
    error.status = r.status;
    error.payload = data || {};
    throw error;
  }
  return data;
}

async function getHubUpdateConfig() {
  const r = await fetch('/api/hub/update_config', {cache: 'no-store'});
  if (!r.ok) throw new Error('Could not load updater config');
  return r.json();
}

async function saveHubUpdateConfig(config) {
  return postJsonWithStatus('/api/hub/update_config', config || {});
}

async function promptHubUpdateConfig() {
  let current = {mode: 'git', repo_url: '', branch: 'main'};
  try {
    current = await getHubUpdateConfig();
  } catch (err) {}

  const repoDefault = String(current.repo_url || '').trim();
  const branchDefault = String(current.branch || 'main').trim() || 'main';
  const repoUrl = window.prompt('Enter git repo URL for one-click updates (SSH or HTTPS):', repoDefault);
  if (repoUrl === null) return false;
  const cleanRepo = String(repoUrl || '').trim();
  if (!cleanRepo) return false;

  const branch = window.prompt('Enter update branch:', branchDefault);
  if (branch === null) return false;
  const cleanBranch = String(branch || '').trim() || 'main';

  await saveHubUpdateConfig({
    mode: 'git',
    repo_url: cleanRepo,
    branch: cleanBranch,
  });
  return true;
}

async function restartHubConnection() {
  setHubActionsDisabled(true);
  setHubRestartButtonBusy(true);
  settingsSetMessage('Restarting Pi Control Hub...', false);
  setPiLinkPill('warn', 'Restarting...');
  try {
    const r = await postJsonWithStatus('/api/hub/restart', {});
    settingsSetMessage(r.message || 'Restart requested.', false);
  } catch (err) {
    settingsSetMessage('Restart failed: ' + (err.message || 'Request failed'), true);
    setPiLinkPill('bad', 'Restart failed');
    setHubRestartButtonBusy(false);
    setHubActionsDisabled(false);
    return;
  }
  waitForHubAndReload();
}

async function updateHubModules(alreadyPrompted=false) {
  setHubActionsDisabled(true);
  setHubUpdateButtonBusy(true);
  settingsSetMessage('Updating modules...', false);
  setPiLinkPill('warn', 'Updating...');
  try {
    const r = await postJsonWithStatus('/api/hub/update', {});
    settingsSetMessage(r.message || 'Update started.', false);
  } catch (err) {
    const configureRequired = err && err.payload && err.payload.configure_required === true;
    if (configureRequired && !alreadyPrompted) {
      try {
        settingsSetMessage('Updater not configured. Entering setup...', false);
        const configured = await promptHubUpdateConfig();
        if (configured) {
          return updateHubModules(true);
        }
        settingsSetMessage('Update setup canceled.', true);
      } catch (setupErr) {
        settingsSetMessage('Update setup failed: ' + (setupErr.message || 'Request failed'), true);
        setPiLinkPill('bad', 'Update setup failed');
      }
    } else {
      settingsSetMessage('Update failed: ' + (err.message || 'Request failed'), true);
    }
    setPiLinkPill('bad', 'Update failed');
    setHubUpdateButtonBusy(false);
    setHubActionsDisabled(false);
    return;
  }
  waitForHubAndReload(40, 1000);
}

{{ plugin_js|safe }}

(async function init() {
  restoreThemePreference();
  restorePanePreference();
  bindModuleHotkeys();
  await refreshHubHealth();
  setInterval(refreshHubHealth, 6000);
{{ plugin_init|safe }}
})();
</script>
</body>
</html>
"""
        return render_template_string(
            html,
            plugin_html=plugin_html,
            plugin_nav_html=plugin_nav_html,
            plugin_js=plugin_js,
            plugin_init=plugin_init,
        )

    @app.route("/api/plugins")
    def api_plugins():
        items = []
        for plugin in plugins:
            items.append(
                {
                    "id": getattr(plugin, "plugin_id", plugin.__class__.__name__),
                    "name": getattr(plugin, "display_name", plugin.__class__.__name__),
                }
            )
        return jsonify(items)

    @app.route("/api/hub/health")
    def api_hub_health():
        bonsai = plugin_map.get("bonsai")
        if bonsai is None:
            return jsonify(
                {
                    "connected": False,
                    "level": "bad",
                    "message": "Bonsai module missing",
                }
            )

        try:
            status = bonsai.get_status() if callable(getattr(bonsai, "get_status", None)) else {}
        except Exception as exc:
            return jsonify(
                {
                    "connected": False,
                    "level": "bad",
                    "message": f"Status error: {exc}",
                }
            )

        monitor_thread = getattr(bonsai, "monitor_thread", None)
        monitor_alive = bool(monitor_thread and callable(getattr(monitor_thread, "is_alive", None)) and monitor_thread.is_alive())
        gpio_ready = bool(status.get("gpio_ready"))
        display_ready = bool(status.get("display_ready"))
        moisture_ok = status.get("moisture") is not None

        if monitor_alive and (gpio_ready or display_ready):
            message = "Connected"
            if moisture_ok:
                message = "Connected (sensor live)"
            return jsonify(
                {
                    "connected": True,
                    "level": "ok",
                    "message": message,
                    "monitor_alive": monitor_alive,
                    "gpio_ready": gpio_ready,
                    "display_ready": display_ready,
                }
            )

        if monitor_alive:
            return jsonify(
                {
                    "connected": False,
                    "level": "warn",
                    "message": "Controller running, GPIO unavailable",
                    "monitor_alive": monitor_alive,
                    "gpio_ready": gpio_ready,
                    "display_ready": display_ready,
                }
            )

        return jsonify(
            {
                "connected": False,
                "level": "bad",
                "message": "Controller offline",
                "monitor_alive": monitor_alive,
                "gpio_ready": gpio_ready,
                "display_ready": display_ready,
            }
        )

    @app.route("/api/hub/restart", methods=["POST"])
    def api_hub_restart():
        ok, message = _launch_hub_restart()
        if not ok:
            return jsonify({"ok": False, "message": message}), 500
        return jsonify({"ok": True, "message": message})

    @app.route("/api/hub/update_config")
    def api_hub_update_config_get():
        return jsonify(load_hub_update_config())

    @app.route("/api/hub/update_config", methods=["POST"])
    def api_hub_update_config_set():
        payload = request.get_json(silent=True) or {}
        current = load_hub_update_config()
        mode = str(payload.get("mode", current.get("mode", "git"))).strip().lower()
        if mode not in {"git", "script"}:
            mode = "git"
        repo_url = str(payload.get("repo_url", current.get("repo_url", ""))).strip()
        branch = str(payload.get("branch", current.get("branch", "main"))).strip() or "main"
        auto_deploy_raw = payload.get("auto_deploy", current.get("auto_deploy", True))
        if isinstance(auto_deploy_raw, bool):
            auto_deploy = auto_deploy_raw
        else:
            auto_deploy = str(auto_deploy_raw).strip().lower() in {"1", "true", "yes", "on"}
        try:
            poll_seconds = int(payload.get("poll_seconds", current.get("poll_seconds", 60)))
        except Exception:
            poll_seconds = int(current.get("poll_seconds", 60))
        poll_seconds = max(30, min(3600, poll_seconds))
        updated = {
            "mode": mode,
            "repo_url": repo_url,
            "branch": branch,
            "auto_deploy": bool(auto_deploy),
            "poll_seconds": int(poll_seconds),
        }
        save_hub_update_config(updated)
        return jsonify({"ok": True, "config": updated})

    @app.route("/api/hub/update", methods=["POST"])
    def api_hub_update():
        update_cmd, source, reason, configure_required = _resolve_update_command()
        if not update_cmd:
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": reason or "No updater configured.",
                        "configure_required": bool(configure_required),
                    }
                ),
                409,
            )

        ok, message = _launch_hub_restart(update_cmd=update_cmd)
        if not ok:
            return jsonify({"ok": False, "message": message}), 500
        return jsonify({"ok": True, "message": f"Updating from {source} and restarting..."})

    def _start_auto_deploy_worker() -> None:
        env_raw = str(os.environ.get("PI_HUB_AUTO_DEPLOY", "")).strip().lower()
        env_override: bool | None = None
        if env_raw:
            env_override = env_raw in {"1", "true", "yes", "on"}

        def _worker() -> None:
            time.sleep(20)
            while True:
                try:
                    cfg = load_hub_update_config()
                    mode = str(cfg.get("mode", "git")).strip().lower()
                    cfg_enabled_raw = cfg.get("auto_deploy", True)
                    if isinstance(cfg_enabled_raw, bool):
                        cfg_enabled = cfg_enabled_raw
                    else:
                        cfg_enabled = str(cfg_enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
                    enabled = env_override if env_override is not None else cfg_enabled

                    try:
                        poll_seconds = int(cfg.get("poll_seconds", 60))
                    except Exception:
                        poll_seconds = 60
                    poll_seconds = max(30, min(3600, poll_seconds))

                    if enabled and mode == "git":
                        repo_url = str(cfg.get("repo_url", "")).strip()
                        branch = str(cfg.get("branch", "main")).strip() or "main"
                        has_update, detail = _git_update_available(repo_url, branch)
                        if has_update:
                            print(f"[HUB][AUTO] Update found on {branch}; applying and restarting...")
                            update_cmd, source, reason, configure_required = _resolve_update_command()
                            if not update_cmd:
                                print(f"[HUB][AUTO] Update command unavailable: {reason or 'not configured'}")
                            elif configure_required:
                                print("[HUB][AUTO] Update requires configuration in Settings.")
                            else:
                                ok, message = _launch_hub_restart(update_cmd=update_cmd)
                                if ok:
                                    print("[HUB][AUTO] Update launched.")
                                    return
                                print(f"[HUB][AUTO] Restart launch failed: {message}")
                        elif detail.startswith("Auto deploy probe failed"):
                            print(f"[HUB][AUTO] {detail}")

                    time.sleep(poll_seconds)
                except Exception as exc:
                    print(f"[HUB][AUTO] Worker error: {exc}")
                    time.sleep(60)

        threading.Thread(target=_worker, daemon=True).start()
        print("[HUB][AUTO] Auto-deploy worker started.")

    _start_auto_deploy_worker()

    return app


def main() -> None:
    print("=" * 56)
    print(" PI CONTROL HUB (PLUGIN MODE)")
    print("=" * 56)

    plugins = load_plugins(APP_DIR)
    if not plugins:
        print("[HUB] No plugins loaded. Exiting.")
        return

    for plugin in plugins:
        try:
            plugin.start()
        except Exception as exc:
            plugin_id = getattr(plugin, "plugin_id", plugin.__class__.__name__)
            print(f"[HUB] Failed to start {plugin_id}: {exc}")

    app = create_app(plugins)

    try:
        print("[HUB] Web UI at http://0.0.0.0:5000")
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        for plugin in reversed(plugins):
            try:
                plugin.shutdown()
            except Exception:
                pass
        print("[HUB] stopped")


if __name__ == "__main__":
    main()
