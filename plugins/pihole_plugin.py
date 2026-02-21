from __future__ import annotations

import json
import os
import ssl
from typing import Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

DEFAULT_CONFIG = {
    "pihole_enabled": False,
    "pihole_base_url": "http://pi.hole",
    "pihole_mode": "auto",  # auto | v6 | legacy
    "pihole_password": "",  # Pi-hole v6 app password / login password
    "pihole_legacy_api_token": "",  # v5 API token (optional if no password)
    "pihole_verify_tls": False,
}


class PiholePlugin:
    plugin_id = "pihole"
    display_name = "Pi-hole"

    def __init__(self, app_dir: str) -> None:
        self.app_dir = app_dir
        self.config_file = os.path.join(app_dir, "pihole_config.json")
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

    def _normalize_base(self) -> str:
        base = str(self.config.get("pihole_base_url", "")).strip().rstrip("/")
        return base

    def _ssl_context(self):
        verify_tls = bool(self.config.get("pihole_verify_tls", False))
        if verify_tls:
            return None
        return ssl._create_unverified_context()

    def _request_json(
        self,
        method: str,
        url: str,
        payload: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> tuple[bool, dict]:
        body = None
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        req = urlrequest.Request(url, data=body, headers=req_headers, method=method.upper())
        try:
            with urlrequest.urlopen(req, timeout=6, context=self._ssl_context()) as resp:
                raw = resp.read().decode("utf-8", errors="ignore").strip()
                if not raw:
                    return True, {}
                try:
                    return True, json.loads(raw)
                except json.JSONDecodeError:
                    return True, {"raw": raw}
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            msg = f"HTTP {exc.code}"
            if body:
                msg = f"{msg}: {body}"
            return False, {"error": msg}
        except Exception as exc:
            return False, {"error": str(exc)}

    @staticmethod
    def _find_sid(obj) -> Optional[str]:
        if isinstance(obj, dict):
            if "sid" in obj and isinstance(obj["sid"], str) and obj["sid"].strip():
                return obj["sid"].strip()
            for value in obj.values():
                found = PiholePlugin._find_sid(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = PiholePlugin._find_sid(item)
                if found:
                    return found
        return None

    def _v6_api_root(self) -> str:
        base = self._normalize_base()
        if not base:
            return ""
        if base.endswith("/api"):
            return base
        if base.endswith("/admin"):
            return f"{base}/api"
        if base.endswith("/admin/"):
            return f"{base.rstrip('/')}/api"
        return f"{base}/api"

    def _v6_login(self) -> tuple[bool, str, str]:
        api_root = self._v6_api_root()
        if not api_root:
            return False, "", "Set Pi-hole base URL first."

        password = str(self.config.get("pihole_password", "")).strip()
        if not password:
            return False, "", "Set Pi-hole password/app password first."

        ok, data = self._request_json("POST", f"{api_root}/auth", payload={"password": password})
        if not ok:
            return False, "", data.get("error", "Login failed")

        sid = self._find_sid(data)
        if not sid:
            return False, "", "No session ID (sid) in Pi-hole auth response."

        return True, sid, "OK"

    def _v6_get_blocking(self, sid: str) -> tuple[bool, Optional[bool], str]:
        api_root = self._v6_api_root()
        sid_q = urlparse.quote_plus(sid)
        ok, data = self._request_json("GET", f"{api_root}/dns/blocking?sid={sid_q}")
        if not ok:
            return False, None, data.get("error", "Failed to read blocking state")

        if "blocking" in data:
            return True, bool(data.get("blocking")), "OK"
        return False, None, "Pi-hole v6 response missing 'blocking' field."

    def _v6_set_blocking(self, sid: str, enabled: bool) -> tuple[bool, str]:
        api_root = self._v6_api_root()
        sid_q = urlparse.quote_plus(sid)
        ok, data = self._request_json(
            "POST",
            f"{api_root}/dns/blocking?sid={sid_q}",
            payload={"blocking": bool(enabled)},
        )
        if not ok:
            return False, data.get("error", "Failed to set blocking state")
        return True, "OK"

    def _v6_get_summary(self, sid: str) -> dict:
        api_root = self._v6_api_root()
        sid_q = urlparse.quote_plus(sid)

        # Endpoint names can differ across versions; try common candidates.
        for path in ("/stats/summary", "/stats/queries", "/stats"):
            ok, data = self._request_json("GET", f"{api_root}{path}?sid={sid_q}")
            if ok and isinstance(data, dict) and data:
                return data
        return {}

    def _legacy_api_url(self) -> str:
        base = self._normalize_base()
        if not base:
            return ""

        if base.endswith("/admin/api.php"):
            return base
        if base.endswith("/admin"):
            return f"{base}/api.php"
        if base.endswith("/api.php"):
            return base
        return f"{base}/admin/api.php"

    def _legacy_auth_q(self) -> str:
        token = str(self.config.get("pihole_legacy_api_token", "")).strip()
        if token:
            return f"&auth={urlparse.quote_plus(token)}"
        return ""

    def _legacy_get_status_and_summary(self) -> tuple[bool, dict, str]:
        api = self._legacy_api_url()
        if not api:
            return False, {}, "Set Pi-hole base URL first."

        auth_q = self._legacy_auth_q()
        ok_sum, summary = self._request_json("GET", f"{api}?summaryRaw{auth_q}")
        if not ok_sum:
            return False, {}, summary.get("error", "Failed to read summary")

        ok_status, status = self._request_json("GET", f"{api}?status{auth_q}")
        if not ok_status:
            return False, {}, status.get("error", "Failed to read status")

        combined = {"summary": summary, "status": status}
        return True, combined, "OK"

    def _legacy_set_blocking(self, enabled: bool) -> tuple[bool, str]:
        api = self._legacy_api_url()
        auth_q = self._legacy_auth_q()
        action = "enable" if enabled else "disable"
        ok, data = self._request_json("GET", f"{api}?{action}{auth_q}")
        if not ok:
            return False, data.get("error", f"Failed to {action} Pi-hole")
        return True, "OK"

    @staticmethod
    def _pick_number(obj: dict, keys: tuple[str, ...]) -> Optional[float]:
        for key in keys:
            if key in obj and isinstance(obj[key], (int, float)):
                return float(obj[key])
        return None

    def _status_from_v6(self) -> dict:
        ok_login, sid, msg = self._v6_login()
        if not ok_login:
            return {
                "enabled": bool(self.config.get("pihole_enabled", False)),
                "mode": "v6",
                "connected": False,
                "message": msg,
                "blocking": None,
                "queries_today": None,
                "blocked_today": None,
                "blocked_percent": None,
            }

        ok_block, blocking, bmsg = self._v6_get_blocking(sid)
        summary = self._v6_get_summary(sid)

        queries_today = self._pick_number(summary, ("queries", "queries_today", "total_queries"))
        blocked_today = self._pick_number(summary, ("blocked", "ads_blocked_today", "blocked_queries"))
        blocked_pct = self._pick_number(summary, ("blocked_percent", "ads_percentage_today", "percent_blocked"))

        return {
            "enabled": bool(self.config.get("pihole_enabled", False)),
            "mode": "v6",
            "connected": ok_block,
            "message": "Connected" if ok_block else bmsg,
            "blocking": blocking if ok_block else None,
            "queries_today": queries_today,
            "blocked_today": blocked_today,
            "blocked_percent": blocked_pct,
        }

    def _status_from_legacy(self) -> dict:
        ok, combined, msg = self._legacy_get_status_and_summary()
        if not ok:
            return {
                "enabled": bool(self.config.get("pihole_enabled", False)),
                "mode": "legacy",
                "connected": False,
                "message": msg,
                "blocking": None,
                "queries_today": None,
                "blocked_today": None,
                "blocked_percent": None,
            }

        summary = combined.get("summary", {}) if isinstance(combined, dict) else {}
        status_obj = combined.get("status", {}) if isinstance(combined, dict) else {}

        status_val = str(status_obj.get("status", "")).strip().lower()
        blocking = True if status_val == "enabled" else False if status_val == "disabled" else None

        queries_today = self._pick_number(summary, ("dns_queries_today", "queries_today", "total_queries"))
        blocked_today = self._pick_number(summary, ("ads_blocked_today", "blocked_queries"))
        blocked_pct = self._pick_number(summary, ("ads_percentage_today", "blocked_percent"))

        return {
            "enabled": bool(self.config.get("pihole_enabled", False)),
            "mode": "legacy",
            "connected": True,
            "message": "Connected",
            "blocking": blocking,
            "queries_today": queries_today,
            "blocked_today": blocked_today,
            "blocked_percent": blocked_pct,
        }

    def get_status(self) -> dict:
        enabled = bool(self.config.get("pihole_enabled", False))
        base = self._normalize_base()
        mode = str(self.config.get("pihole_mode", "auto")).strip().lower()
        verify_tls = bool(self.config.get("pihole_verify_tls", False))

        base_status = {
            "enabled": enabled,
            "base_url": base,
            "mode": mode,
            "verify_tls": verify_tls,
            "connected": False,
            "message": "Pi-hole integration disabled.",
            "blocking": None,
            "queries_today": None,
            "blocked_today": None,
            "blocked_percent": None,
        }

        if not enabled:
            return base_status

        if not base:
            base_status["message"] = "Set Pi-hole base URL."
            return base_status

        if mode == "v6":
            return self._status_from_v6()
        if mode == "legacy":
            return self._status_from_legacy()

        # Auto mode: try v6 first, then legacy.
        v6 = self._status_from_v6()
        if v6.get("connected"):
            v6["mode"] = "v6"
            return v6

        legacy = self._status_from_legacy()
        if legacy.get("connected"):
            legacy["mode"] = "legacy"
            return legacy

        # Keep the most informative message.
        msg = legacy.get("message") or v6.get("message") or "Connection failed."
        return {
            **base_status,
            "message": msg,
            "mode": "auto",
        }

    def set_blocking(self, enabled: bool) -> tuple[bool, str]:
        mode = str(self.config.get("pihole_mode", "auto")).strip().lower()

        if mode == "v6":
            ok, sid, msg = self._v6_login()
            if not ok:
                return False, msg
            return self._v6_set_blocking(sid, enabled)

        if mode == "legacy":
            return self._legacy_set_blocking(enabled)

        # Auto mode: try v6 then legacy.
        ok, sid, _ = self._v6_login()
        if ok:
            set_ok, set_msg = self._v6_set_blocking(sid, enabled)
            if set_ok:
                return True, set_msg

        return self._legacy_set_blocking(enabled)

    def start(self) -> None:
        return

    def shutdown(self) -> None:
        return

    def register_routes(self, app) -> None:
        from flask import jsonify, request

        @app.route("/api/pihole/status")
        def pihole_status():
            return jsonify(self.get_status())

        @app.route("/api/pihole/config", methods=["POST"])
        def pihole_config():
            payload = request.get_json(force=True)

            if "pihole_enabled" in payload:
                self.config["pihole_enabled"] = bool(payload["pihole_enabled"])
            if "pihole_base_url" in payload:
                self.config["pihole_base_url"] = str(payload["pihole_base_url"]).strip()
            if "pihole_mode" in payload:
                mode = str(payload["pihole_mode"]).strip().lower()
                if mode in {"auto", "v6", "legacy"}:
                    self.config["pihole_mode"] = mode
            if "pihole_verify_tls" in payload:
                self.config["pihole_verify_tls"] = bool(payload["pihole_verify_tls"])
            if "pihole_password" in payload and str(payload["pihole_password"]).strip():
                self.config["pihole_password"] = str(payload["pihole_password"]).strip()
            if "pihole_legacy_api_token" in payload and str(payload["pihole_legacy_api_token"]).strip():
                self.config["pihole_legacy_api_token"] = str(payload["pihole_legacy_api_token"]).strip()

            self._save_config(self.config)
            return jsonify({"ok": True, "status": self.get_status()})

        @app.route("/api/pihole/blocking", methods=["POST"])
        def pihole_blocking():
            payload = request.get_json(force=True)
            enabled = bool(payload.get("enabled", True))
            ok, msg = self.set_blocking(enabled)
            code = 200 if ok else 502
            return jsonify({"ok": ok, "message": msg, "status": self.get_status()}), code

    def dashboard_html(self) -> str:
        return """
  <div class="card">
    <div class="row" style="justify-content: space-between;">
      <div>
        <div class="panel-title-row">
          <span class="material-symbols-rounded panel-title-icon">dns</span>
          <div class="panel-title" style="margin-bottom:0;">Pi-hole DNS Control</div>
        </div>
        <div class="panel-meta">DNS health, blocking state, and query stats.</div>
      </div>
      <span id="piholeConn" class="status-pill status-warn">Not checked yet.</span>
    </div>

    <div class="row" style="margin-top:10px;">
      <label><input id="piholeEnabled" class="switch" type="checkbox"> <span class="material-symbols-rounded label-icon">dns</span>Enable Pi-hole integration</label>
      <label><input id="piholeVerifyTls" class="switch" type="checkbox"> <span class="material-symbols-rounded label-icon">lock</span>Verify TLS cert</label>
    </div>

    <div class="grid" style="margin-top:12px;">
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">link</span>Pi-hole base URL</div>
        <input id="piholeBaseUrl" class="wide" type="text" placeholder="http://pi.hole">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">settings</span>Mode</div>
        <select id="piholeMode" class="wide">
          <option value="auto">auto (try v6, then legacy)</option>
          <option value="v6">v6 API (session auth)</option>
          <option value="legacy">legacy api.php (v5)</option>
        </select>
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">password</span>Pi-hole password / app password (v6)</div>
        <input id="piholePassword" class="wide" type="password" placeholder="Leave blank to keep current">
      </div>
      <div>
        <div class="small muted"><span class="material-symbols-rounded label-icon">key</span>Legacy API token (optional)</div>
        <input id="piholeToken" class="wide" type="password" placeholder="Leave blank to keep current">
      </div>
    </div>

    <div class="row" style="margin-top:12px;">
      <button class="btn" onclick="piholeSaveConfig()">Save Pi-hole Settings</button>
      <button class="btn gray" onclick="piholeRefreshStatus()">Refresh Pi-hole</button>
      <span id="piholeSaveMsg" class="small muted"></span>
    </div>

    <div class="row" style="margin-top:12px;">
      <span class="small muted"><span class="material-symbols-rounded label-icon">shield</span>DNS Blocking:</span>
      <button id="piholeBlockingBtn" class="btn control-btn" onclick="piholeToggleBlocking()">Loading...</button>
      <span id="piholeBlockingState" class="small muted">n/a</span>
    </div>

    <div class="grid" style="margin-top:12px;">
      <div class="card sub-card" style="margin:0;">
        <div class="small muted"><span class="material-symbols-rounded label-icon">query_stats</span>Queries Today</div>
        <div id="piholeQueries" style="font-size:24px;font-weight:800;">--</div>
      </div>
      <div class="card sub-card" style="margin:0;">
        <div class="small muted"><span class="material-symbols-rounded label-icon">block</span>Blocked Today</div>
        <div id="piholeBlocked" style="font-size:24px;font-weight:800;">--</div>
      </div>
      <div class="card sub-card" style="margin:0;">
        <div class="small muted"><span class="material-symbols-rounded label-icon">percent</span>Blocked %</div>
        <div id="piholePercent" style="font-size:24px;font-weight:800;">--</div>
      </div>
    </div>
  </div>
"""

    def dashboard_js(self) -> str:
        return """
let piholeState = null;

function piholeFmt(value, digits=0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

async function piholeRefreshStatus() {
  try {
    const st = await api('/api/pihole/status');
    piholeState = st;

    document.getElementById('piholeEnabled').checked = !!st.enabled;
    document.getElementById('piholeVerifyTls').checked = !!st.verify_tls;

    const setIfIdle = (id, value) => {
      const el = document.getElementById(id);
      if (document.activeElement !== el) el.value = value || '';
    };

    setIfIdle('piholeBaseUrl', st.base_url);
    setIfIdle('piholeMode', st.mode || 'auto');

    const conn = document.getElementById('piholeConn');
    if (st.connected) {
      conn.textContent = `Connected (${st.mode})`;
      conn.className = 'status-pill status-ok';
    } else if (st.enabled) {
      conn.textContent = st.message || 'Not connected';
      conn.className = 'status-pill status-bad';
    } else {
      conn.textContent = st.message || 'Pi-hole integration disabled.';
      conn.className = 'status-pill status-warn';
    }
    document.getElementById('piholeQueries').textContent = piholeFmt(st.queries_today, 0);
    document.getElementById('piholeBlocked').textContent = piholeFmt(st.blocked_today, 0);
    document.getElementById('piholePercent').textContent = st.blocked_percent === null || st.blocked_percent === undefined ? '--' : (Number(st.blocked_percent).toFixed(1) + '%');

    const btn = document.getElementById('piholeBlockingBtn');
    if (st.blocking === true) {
      btn.textContent = 'TURN BLOCKING OFF';
      btn.classList.add('state-danger');
      btn.classList.remove('state-action');
      document.getElementById('piholeBlockingState').textContent = 'Blocking: ON';
    } else if (st.blocking === false) {
      btn.textContent = 'TURN BLOCKING ON';
      btn.classList.add('state-action');
      btn.classList.remove('state-danger');
      document.getElementById('piholeBlockingState').textContent = 'Blocking: OFF';
    } else {
      btn.textContent = 'UNAVAILABLE';
      btn.classList.remove('state-action');
      btn.classList.remove('state-danger');
      document.getElementById('piholeBlockingState').textContent = 'Blocking: Unknown';
    }
  } catch (err) {
    document.getElementById('piholeConn').textContent = 'Pi-hole status error: ' + err.message;
  }
}

async function piholeSaveConfig() {
  const payload = {
    pihole_enabled: document.getElementById('piholeEnabled').checked,
    pihole_verify_tls: document.getElementById('piholeVerifyTls').checked,
    pihole_base_url: document.getElementById('piholeBaseUrl').value.trim(),
    pihole_mode: document.getElementById('piholeMode').value,
  };

  const password = document.getElementById('piholePassword').value.trim();
  const token = document.getElementById('piholeToken').value.trim();
  if (password) payload.pihole_password = password;
  if (token) payload.pihole_legacy_api_token = token;

  const r = await api('/api/pihole/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });

  document.getElementById('piholePassword').value = '';
  document.getElementById('piholeToken').value = '';
  document.getElementById('piholeSaveMsg').textContent = r.ok ? 'Pi-hole settings saved.' : 'Save failed.';
  setTimeout(() => document.getElementById('piholeSaveMsg').textContent = '', 2000);
  await piholeRefreshStatus();
}

async function piholeToggleBlocking() {
  if (!piholeState || piholeState.blocking === null || piholeState.blocking === undefined) {
    document.getElementById('piholeSaveMsg').textContent = 'Blocking state unavailable.';
    setTimeout(() => document.getElementById('piholeSaveMsg').textContent = '', 2000);
    return;
  }

  const target = !piholeState.blocking;
  const r = await api('/api/pihole/blocking', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled: target}),
  });

  document.getElementById('piholeSaveMsg').textContent = r.message || 'Done';
  setTimeout(() => document.getElementById('piholeSaveMsg').textContent = '', 2200);
  await piholeRefreshStatus();
}
"""

    def dashboard_init_js(self) -> str:
        return """
  await piholeRefreshStatus();
  setInterval(piholeRefreshStatus, 5000);
"""


def create_plugin(app_dir: str) -> PiholePlugin:
    return PiholePlugin(app_dir)
