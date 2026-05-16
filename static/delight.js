/* ================================================================
   Pi Control Hub — Delight Layer JS
   Toast system, ring gauge, area chart, KPI animations,
   ambient glow, stat rings, toggle helpers.
   ================================================================ */

/* ── Toast notification system ── */
const Toast = {
  _container: null,
  _getContainer() {
    if (!this._container) this._container = document.getElementById('toastContainer');
    return this._container;
  },
  show(message, type = 'info', duration = 3000) {
    const container = this._getContainer();
    if (!container) return;
    const icons = { success: 'check_circle', error: 'error', info: 'info' };
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML =
      '<span class="material-symbols-rounded toast-icon">' + (icons[type] || 'info') + '</span>' +
      '<div class="toast-body">' +
        '<div>' + message + '</div>' +
        '<div class="toast-progress" style="animation-duration:' + duration + 'ms"></div>' +
      '</div>';
    container.appendChild(toast);
    setTimeout(() => {
      toast.classList.add('removing');
      setTimeout(() => toast.remove(), 300);
    }, duration);
  },
  success(msg) { this.show(msg, 'success'); },
  error(msg) { this.show(msg, 'error', 5000); },
  info(msg) { this.show(msg, 'info'); },
};

/* ── KPI number animation ── */
function animateValue(el, newText, direction) {
  if (!el) return;
  const old = el.textContent;
  if (old === newText) return;
  el.textContent = newText;
  if (direction === 'up') {
    el.classList.add('kpi-flash-up');
    setTimeout(() => el.classList.remove('kpi-flash-up'), 500);
  } else if (direction === 'down') {
    el.classList.add('kpi-flash-down');
    setTimeout(() => el.classList.remove('kpi-flash-down'), 500);
  }
}

/* ── Moisture ring gauge renderer (per-instance) ── */
function renderMoistureRing(containerId, moisture, low, high) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (moisture === null || moisture === undefined) {
    el.innerHTML = '<div class="moisture-ring-wrap"><div class="ring-center"><div class="ring-value" style="color:var(--sub)">--</div></div></div>';
    el._ringPrev = null;
    return;
  }
  const pct = Math.max(0, Math.min(100, Number(moisture)));
  const r = 52, cx = 65, cy = 65, viewBox = 130;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;

  let color, colorGlow;
  if (pct < low) {
    color = '#e87474'; colorGlow = 'rgba(232,116,116,0.5)';
  } else if (pct < high) {
    color = '#e0ac53'; colorGlow = 'rgba(224,172,83,0.4)';
  } else {
    color = '#42c58f'; colorGlow = 'rgba(66,197,143,0.4)';
  }

  const danger = pct < low ? ' danger' : '';
  const label = pct < low ? 'DRY' : pct > high ? 'WET' : 'OK';
  const labelClass = pct < low ? 'label-dry' : pct > high ? 'label-wet' : 'label-ok';

  const existing = el.querySelector('.ring-fill');
  if (existing && el._ringPrev !== undefined && el._ringPrev !== null) {
    existing.setAttribute('stroke', color);
    existing.setAttribute('stroke-dashoffset', offset.toFixed(1));
    existing.style.filter = 'drop-shadow(0 0 8px ' + colorGlow + ')';
    const valEl = el.querySelector('.ring-value');
    if (valEl) valEl.textContent = Math.round(pct) + '%';
    const labEl = el.querySelector('.ring-label');
    if (labEl) { labEl.textContent = label; labEl.className = 'ring-label ' + labelClass; }
    const wrap = el.querySelector('.moisture-ring-wrap');
    if (wrap) wrap.className = 'moisture-ring-wrap' + danger;
    el._ringPrev = offset;
    return;
  }

  el.innerHTML =
    '<div class="moisture-ring-wrap' + danger + '">' +
      '<svg viewBox="0 0 ' + viewBox + ' ' + viewBox + '">' +
        '<circle class="ring-bg" cx="' + cx + '" cy="' + cy + '" r="' + r + '"/>' +
        '<circle class="ring-fill" cx="' + cx + '" cy="' + cy + '" r="' + r + '"' +
          ' stroke="' + color + '"' +
          ' style="filter:drop-shadow(0 0 8px ' + colorGlow + ')"' +
          ' stroke-dasharray="' + circ.toFixed(1) + '"' +
          ' stroke-dashoffset="' + circ.toFixed(1) + '"/>' +
      '</svg>' +
      '<div class="ring-center">' +
        '<div class="ring-value">' + Math.round(pct) + '%</div>' +
        '<div class="ring-label ' + labelClass + '">' + label + '</div>' +
      '</div>' +
    '</div>';

  requestAnimationFrame(() => {
    const fill = el.querySelector('.ring-fill');
    if (fill) fill.setAttribute('stroke-dashoffset', offset.toFixed(1));
  });
  el._ringPrev = offset;
}

/* ── Stat ring gauge (smaller, for Pi-hole blocked %, etc.) ── */
function renderStatRing(containerId, value, maxVal, color, label) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const pct = (value === null || value === undefined || maxVal <= 0)
    ? 0 : Math.max(0, Math.min(100, (Number(value) / maxVal) * 100));
  const r = 38, cx = 48, cy = 48, viewBox = 96;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;

  const displayVal = value === null || value === undefined ? '--'
    : (Number(value) >= 1000 ? (Number(value) / 1000).toFixed(1) + 'k' : String(Math.round(Number(value))));

  const colorMap = {
    ok: { stroke: '#42c58f', glow: 'rgba(66,197,143,0.35)' },
    warn: { stroke: '#e0ac53', glow: 'rgba(224,172,83,0.3)' },
    bad: { stroke: '#e87474', glow: 'rgba(232,116,116,0.35)' },
    primary: { stroke: '#6b84e7', glow: 'rgba(107,132,231,0.35)' },
  };
  const c = colorMap[color] || colorMap.primary;

  const existing = el.querySelector('.stat-ring-fill');
  if (existing && el._statRingPrev !== undefined) {
    existing.setAttribute('stroke', c.stroke);
    existing.setAttribute('stroke-dashoffset', offset.toFixed(1));
    existing.style.filter = 'drop-shadow(0 0 6px ' + c.glow + ')';
    const valEl = el.querySelector('.stat-ring-value');
    if (valEl) valEl.textContent = displayVal;
    el._statRingPrev = offset;
    return;
  }

  el.innerHTML =
    '<div class="stat-ring-wrap">' +
      '<svg viewBox="0 0 ' + viewBox + ' ' + viewBox + '">' +
        '<circle class="stat-ring-bg" cx="' + cx + '" cy="' + cy + '" r="' + r + '"/>' +
        '<circle class="stat-ring-fill" cx="' + cx + '" cy="' + cy + '" r="' + r + '"' +
          ' stroke="' + c.stroke + '"' +
          ' style="filter:drop-shadow(0 0 6px ' + c.glow + ')"' +
          ' stroke-dasharray="' + circ.toFixed(1) + '"' +
          ' stroke-dashoffset="' + circ.toFixed(1) + '"/>' +
      '</svg>' +
      '<div class="stat-ring-center">' +
        '<div class="stat-ring-value">' + displayVal + '</div>' +
        (label ? '<div class="stat-ring-label">' + label + '</div>' : '') +
      '</div>' +
    '</div>';

  requestAnimationFrame(() => {
    const fill = el.querySelector('.stat-ring-fill');
    if (fill) fill.setAttribute('stroke-dashoffset', offset.toFixed(1));
  });
  el._statRingPrev = offset;
}

/* ── Smooth area chart renderer (per-instance) ── */
function renderMoistureChart(containerId, readings, low, high) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!readings || readings.length < 2) { el.innerHTML = ''; return; }

  const w = 600, h = 90, padX = 30, padY = 6;
  const vals = readings.map(r => Number(r.moisture));
  const times = readings.map(r => new Date(r.timestamp).getTime());
  const mn = Math.max(0, Math.min(...vals) - 5);
  const mx = Math.min(100, Math.max(...vals) + 5);
  const range = mx - mn || 1;
  const tMin = times[0], tMax = times[times.length - 1], tRange = tMax - tMin || 1;

  function x(i) { return padX + ((times[i] - tMin) / tRange) * (w - padX * 2); }
  function y(v) { return h - padY - ((v - mn) / range) * (h - padY * 2); }

  let path = 'M' + x(0).toFixed(1) + ',' + y(vals[0]).toFixed(1);
  for (let i = 1; i < vals.length; i++) {
    const x0 = x(i - 1), y0 = y(vals[i - 1]);
    const x1 = x(i), y1 = y(vals[i]);
    const cpx = (x0 + x1) / 2;
    path += ' C' + cpx.toFixed(1) + ',' + y0.toFixed(1) + ' ' + cpx.toFixed(1) + ',' + y1.toFixed(1) + ' ' + x1.toFixed(1) + ',' + y1.toFixed(1);
  }

  const areaPath = path + ' L' + x(vals.length - 1).toFixed(1) + ',' + (h - padY) + ' L' + x(0).toFixed(1) + ',' + (h - padY) + ' Z';

  const cs = getComputedStyle(document.documentElement);
  const primaryColor = cs.getPropertyValue('--primary').trim();
  const badColor = cs.getPropertyValue('--bad').trim();
  const okColor = cs.getPropertyValue('--ok').trim();

  const yLow = y(Number(low));
  const yHigh = y(Number(high));

  // Unique gradient ID per container
  const gradId = 'chartGrad_' + containerId;

  const hours = Math.round((tMax - tMin) / 3600000);
  let labels = '';
  if (hours >= 6) {
    for (let hh = 6; hh <= hours; hh += 6) {
      const lx = padX + (hh / hours) * (w - padX * 2);
      labels += '<text class="chart-axis-label" x="' + lx.toFixed(0) + '" y="' + (h - 1) + '" text-anchor="middle">' + hh + 'h</text>';
    }
  }

  el.innerHTML =
    '<div class="moisture-chart-wrap">' +
    '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
      '<defs><linearGradient id="' + gradId + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0%" stop-color="' + primaryColor + '" stop-opacity="0.35"/>' +
        '<stop offset="100%" stop-color="' + primaryColor + '" stop-opacity="0.03"/>' +
      '</linearGradient></defs>' +
      '<rect class="chart-thresh-band" x="' + padX + '" y="' + Math.min(yLow, yHigh).toFixed(1) + '" width="' + (w - padX * 2) + '" height="' + Math.abs(yHigh - yLow).toFixed(1) + '" fill="' + okColor + '"/>' +
      '<line x1="' + padX + '" y1="' + yLow.toFixed(1) + '" x2="' + (w - padX) + '" y2="' + yLow.toFixed(1) + '" stroke="' + badColor + '" stroke-width="0.8" stroke-dasharray="4,3" opacity="0.5"/>' +
      '<line x1="' + padX + '" y1="' + yHigh.toFixed(1) + '" x2="' + (w - padX) + '" y2="' + yHigh.toFixed(1) + '" stroke="' + okColor + '" stroke-width="0.8" stroke-dasharray="4,3" opacity="0.5"/>' +
      '<path class="chart-area" d="' + areaPath + '" fill="url(#' + gradId + ')"/>' +
      '<path d="' + path + '" fill="none" stroke="' + primaryColor + '" stroke-width="2.5" stroke-linejoin="round"/>' +
      '<circle cx="' + x(vals.length - 1).toFixed(1) + '" cy="' + y(vals[vals.length - 1]).toFixed(1) + '" r="5" fill="' + primaryColor + '" stroke="#fff" stroke-width="2" style="filter:drop-shadow(0 0 4px ' + primaryColor + ')"/>' +
      labels +
    '</svg></div>';
}

/* ── Ambient header glow for lamp palette ── */
const PALETTE_GLOWS = {
  cool: 'rgba(99,124,221,0.2)',
  money: 'rgba(56,172,88,0.18)',
  warm: 'rgba(249,149,76,0.2)',
  candle: 'rgba(236,178,98,0.18)',
  ice_fire: 'rgba(255,80,50,0.2)',
  aurora: 'rgba(120,255,170,0.18)',
  cyber_orchid: 'rgba(230,65,255,0.2)',
  ember_forest: 'rgba(255,130,40,0.18)',
  moon_grove: 'rgba(95,180,255,0.18)',
  miami_vice: 'rgba(255,63,164,0.22)',
  tokyo_night: 'rgba(150,70,255,0.22)',
  deep_ocean: 'rgba(0,191,166,0.2)',
  golden_hour: 'rgba(255,179,71,0.2)',
  jade_temple: 'rgba(23,130,70,0.18)',
};

function setAmbientGlow(palette) {
  const head = document.querySelector('.app-head');
  if (!head) return;
  const glow = PALETTE_GLOWS[String(palette || '').toLowerCase()] || 'transparent';
  head.style.setProperty('--ambient-glow', glow);
  head.classList.toggle('has-ambient', glow !== 'transparent');
}

/* ── Toggle switch helper ── */
function renderToggle(containerId, isOn, onClick, opts) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const o = opts || {};
  const onLabel = o.onLabel || 'ON';
  const offLabel = o.offLabel || 'OFF';
  const naLabel = o.naLabel || 'N/A';
  const size = o.size || '';
  const sizeClass = size === 'large' ? ' toggle-lg' : '';

  const state = isOn === true ? 'on' : isOn === false ? 'off' : 'off disabled';
  const labelText = isOn === true ? onLabel : isOn === false ? offLabel : naLabel;
  const ariaLabel = o.ariaLabel || labelText;
  el.innerHTML =
    '<div class="toggle-row">' +
      '<div class="toggle-switch ' + state + sizeClass + '" onclick="' + onClick + '" role="switch" aria-checked="' + (isOn === true) + '" aria-label="' + ariaLabel + '" tabindex="0">' +
        '<div class="toggle-knob"></div>' +
      '</div>' +
      '<span class="toggle-label">' + labelText + '</span>' +
    '</div>';
}

/* ── Pump card animation helper ── */
function setPumpCardActive(cardSelector, isActive) {
  const card = document.querySelector(cardSelector);
  if (!card) return;
  card.classList.toggle('pump-active', isActive);
}

/* ── Collapsible section (smooth) ── */
function toggleCollapsible(bodyId) {
  const body = document.getElementById(bodyId);
  const chev = document.getElementById(bodyId + 'Chev');
  if (!body) return;
  const isOpen = body.classList.contains('open');
  body.classList.toggle('open', !isOpen);
  if (chev) chev.classList.toggle('open', !isOpen);
}

/* ── Shared dry-estimate calculator ── */
function calcDryEstimate(readings, thresholdLow) {
  if (!readings || readings.length < 6) return null;
  const recent = readings.slice(-6);
  const vals = recent.map(r => Number(r.moisture));
  const n = vals.length;
  let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
  for (let i = 0; i < n; i++) { sumX += i; sumY += vals[i]; sumXY += i * vals[i]; sumXX += i * i; }
  const slope = (n * sumXY - sumX * sumY) / (n * sumXX - sumX * sumX);
  if (slope >= -0.05) return 'Stable';
  const current = vals[n - 1];
  if (current <= thresholdLow) return 'Below threshold';
  const stepsToThreshold = (current - thresholdLow) / Math.abs(slope);
  const t0 = new Date(recent[0].timestamp).getTime();
  const tN = new Date(recent[n - 1].timestamp).getTime();
  const stepMs = (tN - t0) / (n - 1);
  if (stepMs <= 0) return null;
  const hoursLeft = (stepsToThreshold * stepMs) / 3600000;
  if (hoursLeft > 72) return 'Stable';
  if (hoursLeft < 0.5) return '< 30min until dry';
  return '~' + Math.round(hoursLeft) + 'h until dry';
}

/* ── Speaker visual state indicator ── */
function renderSpeakerVisual(containerId, leftOn, rightOn) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const lc = leftOn === true ? 'var(--ok)' : 'var(--sub)';
  const rc = rightOn === true ? 'var(--ok)' : 'var(--sub)';
  const lo = leftOn === true ? '1' : '0.3';
  const ro = rightOn === true ? '1' : '0.3';
  el.innerHTML =
    '<svg viewBox="0 0 80 40" width="80" height="40">' +
      '<rect x="4" y="4" width="28" height="32" rx="6" fill="none" stroke="' + lc + '" stroke-width="2" opacity="' + lo + '"/>' +
      '<circle cx="18" cy="20" r="6" fill="' + lc + '" opacity="' + lo + '"/>' +
      '<rect x="48" y="4" width="28" height="32" rx="6" fill="none" stroke="' + rc + '" stroke-width="2" opacity="' + ro + '"/>' +
      '<circle cx="62" cy="20" r="6" fill="' + rc + '" opacity="' + ro + '"/>' +
    '</svg>';
}
