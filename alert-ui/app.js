/* Fridge Alert Manager — app.js
 *
 * Auth: Grafana Basic auth credentials stored in sessionStorage.
 * All /alerts/api/* calls include "Authorization: Basic <base64>" automatically.
 * On 401, the login modal is shown and the credential is cleared.
 *
 * All fetch paths are absolute from the site root so they work behind Caddy.
 */

// ── State ──────────────────────────────────────────────────────────────────
let authHeader = sessionStorage.getItem('fridge_auth') || '';
let metricsData = { metrics: [], fridges: [], operators: [] };
let refreshTimer = null;

// ── Auth helpers ────────────────────────────────────────────────────────────

function showLogin(errorMsg) {
  document.getElementById('login-modal').classList.add('visible');
  document.getElementById('login-error').textContent = errorMsg || '';
  document.getElementById('header-username').textContent = '';
}

function hideLogin() {
  document.getElementById('login-modal').classList.remove('visible');
}

function setAuth(username, password) {
  authHeader = 'Basic ' + btoa(username + ':' + password);
  sessionStorage.setItem('fridge_auth', authHeader);
  document.getElementById('header-username').textContent = username;
}

function clearAuth() {
  authHeader = '';
  sessionStorage.removeItem('fridge_auth');
}

// Decode stored auth to get username (for display only)
function storedUsername() {
  if (!authHeader.startsWith('Basic ')) return '';
  try {
    const decoded = atob(authHeader.slice(6));
    return decoded.split(':')[0];
  } catch (_) {
    return '';
  }
}

// ── Fetch wrapper ───────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
    'Authorization': authHeader,
  };
  const resp = await fetch('/alerts/api' + path, { ...options, headers });

  if (resp.status === 401) {
    clearAuth();
    showLogin('Session expired — please sign in again.');
    throw new Error('Unauthenticated');
  }

  return resp;
}

// ── Toast ───────────────────────────────────────────────────────────────────

let toastTimer = null;

function toast(msg, type = 'success') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3500);
}

// ── Login form ──────────────────────────────────────────────────────────────

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  btn.textContent = 'Signing in…';

  // Validate credentials by hitting a protected endpoint
  const testHeader = 'Basic ' + btoa(username + ':' + password);
  try {
    const resp = await fetch('/alerts/api/alerts', {
      headers: { Authorization: testHeader, 'Content-Type': 'application/json' },
    });
    if (resp.status === 401) {
      document.getElementById('login-error').textContent = 'Incorrect username or password.';
    } else {
      setAuth(username, password);
      hideLogin();
      document.getElementById('login-username').value = '';
      document.getElementById('login-password').value = '';
      await loadAll();
    }
  } catch (_) {
    document.getElementById('login-error').textContent = 'Could not reach the server.';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
});

document.getElementById('btn-signout').addEventListener('click', () => {
  clearAuth();
  showLogin();
  stopAutoRefresh();
  document.getElementById('alerts-body').innerHTML =
    '<tr class="empty-row"><td colspan="9">Sign in to view alerts.</td></tr>';
});

// ── Bootstrap ───────────────────────────────────────────────────────────────

async function loadAll() {
  await Promise.all([loadMetrics(), loadAlerts(), loadRecipients()]);
  startAutoRefresh();
}

// ── Metrics / dropdown population ───────────────────────────────────────────

async function loadMetrics() {
  try {
    // /api/metrics is public — no auth header needed, but including is harmless
    const resp = await fetch('/alerts/api/metrics');
    if (!resp.ok) return;
    metricsData = await resp.json();
    populateFridgeDropdown();
    populateOperatorDropdown();
  } catch (_) {}
}

function populateFridgeDropdown() {
  const sel = document.getElementById('f-fridge');
  // Keep placeholder
  sel.innerHTML = '<option value="">— select —</option>';
  for (const f of metricsData.fridges) {
    const opt = document.createElement('option');
    opt.value = f.id;
    opt.textContent = f.label;
    sel.appendChild(opt);
  }
}

function populateOperatorDropdown() {
  const sel = document.getElementById('f-operator');
  sel.innerHTML = '<option value="">— select —</option>';
  for (const op of metricsData.operators) {
    const opt = document.createElement('option');
    opt.value = op.symbol;
    opt.textContent = op.symbol;
    sel.appendChild(opt);
  }
}

function populateMetricDropdown(fridgeId) {
  const sel = document.getElementById('f-metric');
  sel.innerHTML = '';

  const available = metricsData.metrics.filter((m) => {
    // If a metric has a fridges restriction, only show it for matching fridges
    if (!m.fridges) return true;
    return m.fridges.includes(fridgeId);
  });

  if (available.length === 0) {
    sel.innerHTML = '<option value="">No metrics for this fridge</option>';
    return;
  }

  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '— select metric —';
  sel.appendChild(placeholder);

  for (const m of available) {
    const opt = document.createElement('option');
    opt.value = m.name;
    opt.textContent = `${m.label} (${m.unit})`;
    sel.appendChild(opt);
  }
}

document.getElementById('f-fridge').addEventListener('change', (e) => {
  populateMetricDropdown(e.target.value);
});

// ── Alert table ─────────────────────────────────────────────────────────────

async function loadAlerts() {
  if (!authHeader) return;
  try {
    const resp = await apiFetch('/alerts');
    if (!resp.ok) {
      document.getElementById('alerts-body').innerHTML =
        `<tr class="empty-row"><td colspan="9">Error loading alerts (${resp.status}).</td></tr>`;
      return;
    }
    const alerts = await resp.json();
    renderAlerts(alerts);
    document.getElementById('refresh-indicator').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      document.getElementById('alerts-body').innerHTML =
        '<tr class="empty-row"><td colspan="9">Failed to load alerts.</td></tr>';
    }
  }
}

function fmtValue(val, metricName) {
  if (val === null || val === undefined) return '—';
  const meta = metricsData.metrics.find((m) => m.name === metricName);
  const unit = meta ? meta.unit : '';
  const abs = Math.abs(val);
  const formatted = (abs < 0.01 && abs > 0)
    ? val.toExponential(2)
    : parseFloat(val.toPrecision(4)).toString();
  return `${formatted} ${unit}`.trim();
}

function renderAlerts(alerts) {
  const tbody = document.getElementById('alerts-body');
  if (alerts.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No alerts configured.</td></tr>';
    return;
  }

  tbody.innerHTML = alerts.map((a) => {
    const stateBadge = {
      normal:  '<span class="badge badge-normal">&#9679; Normal</span>',
      pending: '<span class="badge badge-pending">&#9679; Pending</span>',
      firing:  '<span class="badge badge-firing">&#9679; Firing</span>',
    }[a.state] || `<span class="badge badge-unknown">${escHtml(a.state)}</span>`;

    const tierBadge = a.provisioned
      ? '<span class="badge badge-baseline">&#128274; Baseline</span>'
      : '<span class="badge badge-custom">Custom</span>';

    const deleteBtn = a.provisioned
      ? ''
      : `<button class="btn btn-danger btn-sm" onclick="deleteAlert('${escHtml(a.uid)}', this)">Delete</button>`;

    const condition = a.operator
      ? `${escHtml(a.operator)} ${a.threshold}`
      : '—';

    const currentCell = a.current_value !== null && a.current_value !== undefined
      ? `<td class="col-current${a.state === 'firing' ? ' current-firing' : ''}">${fmtValue(a.current_value, a.metric)}</td>`
      : '<td class="col-current">—</td>';

    return `<tr>
      <td>${escHtml(a.title)}</td>
      <td>${escHtml(a.fridge)}</td>
      <td class="col-metric">${escHtml(a.metric)}</td>
      <td class="col-operator">${condition}</td>
      ${currentCell}
      <td>${escHtml(a.severity)}</td>
      <td>${stateBadge}</td>
      <td>${tierBadge}</td>
      <td>${deleteBtn}</td>
    </tr>`;
  }).join('');
}

async function deleteAlert(uid, btn) {
  if (!confirm('Delete this alert? This cannot be undone.')) return;
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const resp = await apiFetch(`/alerts/${uid}`, { method: 'DELETE' });
    if (resp.ok) {
      toast('Alert deleted.');
      await loadAlerts();
    } else {
      const body = await resp.json().catch(() => ({}));
      toast(body.detail || `Error ${resp.status}`, 'error');
      btn.disabled = false;
      btn.textContent = 'Delete';
    }
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      toast('Delete failed.', 'error');
      btn.disabled = false;
      btn.textContent = 'Delete';
    }
  }
}

// ── Create alert form ────────────────────────────────────────────────────────

document.getElementById('create-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('btn-create');
  const status = document.getElementById('create-status');
  btn.disabled = true;
  status.textContent = 'Creating…';

  const body = {
    name: document.getElementById('f-name').value.trim(),
    fridge: document.getElementById('f-fridge').value,
    metric: document.getElementById('f-metric').value,
    operator: document.getElementById('f-operator').value,
    threshold: parseFloat(document.getElementById('f-threshold').value),
    for_duration: document.getElementById('f-duration').value,
    severity: document.getElementById('f-severity').value,
  };

  try {
    const resp = await apiFetch('/alerts', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      toast(`Alert "${data.title}" created.`);
      e.target.reset();
      populateFridgeDropdown();
      document.getElementById('f-metric').innerHTML =
        '<option value="">— select fridge first —</option>';
      status.textContent = '';
      await loadAlerts();
    } else {
      const msg = data.detail || `Error ${resp.status}`;
      status.textContent = msg;
      toast(msg, 'error');
    }
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      status.textContent = 'Request failed.';
      toast('Request failed.', 'error');
    }
  } finally {
    btn.disabled = false;
  }
});

// ── Recipients ───────────────────────────────────────────────────────────────

async function loadRecipients() {
  if (!authHeader) return;
  try {
    const resp = await apiFetch('/recipients');
    if (!resp.ok) return;
    const recipients = await resp.json();
    renderRecipients(recipients);
  } catch (_) {}
}

function renderRecipients(recipients) {
  const ul = document.getElementById('recipient-list');
  if (recipients.length === 0) {
    ul.innerHTML = '<li style="color:#6b7280">No recipients configured.</li>';
    return;
  }
  ul.innerHTML = recipients.map((r) =>
    `<li>
      <span class="type-badge">${escHtml(r.type)}</span>
      ${escHtml(r.name)}
    </li>`
  ).join('');
}

// Toggle add-recipient form
document.getElementById('recipient-toggle').addEventListener('click', function () {
  const wrap = document.getElementById('recipient-form-wrap');
  const open = wrap.classList.toggle('open');
  this.setAttribute('aria-expanded', open ? 'true' : 'false');
  this.textContent = open ? '✕ Cancel' : '+ Add recipient';
});

document.getElementById('recipient-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]');
  const statusEl = document.getElementById('recipient-status');
  btn.disabled = true;
  statusEl.textContent = 'Adding…';

  const body = {
    name: document.getElementById('r-name').value.trim(),
    email: document.getElementById('r-email').value.trim(),
  };

  try {
    const resp = await apiFetch('/recipients', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      toast(`Recipient "${data.name}" added.`);
      e.target.reset();
      statusEl.textContent = '';
      await loadRecipients();
    } else {
      const msg = data.detail || `Error ${resp.status}`;
      statusEl.textContent = msg;
      toast(msg, 'error');
    }
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      statusEl.textContent = 'Request failed.';
      toast('Request failed.', 'error');
    }
  } finally {
    btn.disabled = false;
  }
});

// ── Auto-refresh ─────────────────────────────────────────────────────────────

function startAutoRefresh() {
  stopAutoRefresh();
  refreshTimer = setInterval(loadAlerts, 30_000);
}

function stopAutoRefresh() {
  clearInterval(refreshTimer);
  refreshTimer = null;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Init ─────────────────────────────────────────────────────────────────────

(async function init() {
  // Always load public metrics config so dropdowns are ready
  await loadMetrics();

  if (authHeader) {
    // Show username from stored token
    document.getElementById('header-username').textContent = storedUsername();
    await loadAll();
  } else {
    showLogin();
  }
})();
