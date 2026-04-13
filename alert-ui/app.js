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
let alertSort = { key: 'status', dir: 'desc' };

const ALERT_TEMPLATES = [
  { name: 'Cooling Water too Hot',    fridge: 'fridge-dodo', metric: 'cpatempwi_celsius', operator: '>',  threshold: 26 },
  { name: 'Dodo Mixing chamber test', fridge: 'fridge-dodo', metric: 'ch6_t_kelvin',      operator: '>',  threshold: 0.01 },
];
let templateIndex = -1;

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
  toastTimer = setTimeout(() => { el.className = ''; }, 6000);
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
  closeAssignmentPanel();
  document.getElementById('alerts-body').innerHTML =
    '<tr class="empty-row"><td colspan="8">Sign in to view alerts.</td></tr>';
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
        `<tr class="empty-row"><td colspan="8">Error loading alerts (${resp.status}).</td></tr>`;
      return;
    }
    const alerts = await resp.json();
    // Cache alerts for use by assignment panel
    window._alertsCache = alerts;
    renderAlerts(alerts);
    document.getElementById('refresh-indicator').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      document.getElementById('alerts-body').innerHTML =
        '<tr class="empty-row"><td colspan="8">Failed to load alerts.</td></tr>';
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

function statusRank(alert) {
  if (!alert.enabled) return 0;
  if (alert.state === 'firing') return 3;
  if (alert.state === 'pending') return 2;
  if (alert.state === 'normal') return 1;
  return 0;
}

function sortAlerts(alerts) {
  const sorted = [...alerts];
  const dir = alertSort.dir === 'asc' ? 1 : -1;

  sorted.sort((a, b) => {
    if (alertSort.key === 'status') {
      const diff = (statusRank(a) - statusRank(b)) * dir;
      if (diff !== 0) return diff;
      return a.title.localeCompare(b.title);
    }
    if (alertSort.key === 'fridge') {
      const diff = a.fridge.localeCompare(b.fridge) * dir;
      if (diff !== 0) return diff;
      return a.title.localeCompare(b.title);
    }
    return 0;
  });

  return sorted;
}

function renderSortHeaders() {
  const statusBtn = document.getElementById('sort-status');
  const fridgeBtn = document.getElementById('sort-fridge');
  const statusInd = document.getElementById('sort-status-ind');
  const fridgeInd = document.getElementById('sort-fridge-ind');
  if (!statusBtn || !fridgeBtn || !statusInd || !fridgeInd) return;

  statusBtn.classList.remove('active');
  fridgeBtn.classList.remove('active');
  statusInd.textContent = '';
  fridgeInd.textContent = '';

  const arrow = alertSort.dir === 'asc' ? '↑' : '↓';
  if (alertSort.key === 'status') {
    statusBtn.classList.add('active');
    statusInd.textContent = arrow;
  } else if (alertSort.key === 'fridge') {
    fridgeBtn.classList.add('active');
    fridgeInd.textContent = arrow;
  }
}

function setAlertSort(key) {
  if (alertSort.key === key) {
    alertSort.dir = alertSort.dir === 'desc' ? 'asc' : 'desc';
  } else {
    alertSort.key = key;
    alertSort.dir = 'desc';
  }
  renderSortHeaders();
  renderAlerts(window._alertsCache || []);
}

function renderAlerts(alerts) {
  const tbody = document.getElementById('alerts-body');
  const sortedAlerts = sortAlerts(alerts);
  renderSortHeaders();
  if (sortedAlerts.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No alerts configured.</td></tr>';
    return;
  }

  tbody.innerHTML = sortedAlerts.map((a) => {
    const stateBadge = !a.enabled
      ? '<span class="badge badge-disabled">&#9646;&#9646; Disabled</span>'
      : ({
          normal:  '<span class="badge badge-normal">&#9679; Normal</span>',
          pending: '<span class="badge badge-pending">&#9679; Pending</span>',
          firing:  '<span class="badge badge-firing">&#9679; Firing</span>',
        }[a.state] || `<span class="badge badge-unknown">${escHtml(a.state)}</span>`);

    const currentCell = a.current_value !== null && a.current_value !== undefined
      ? `<td class="col-current${a.state === 'firing' && a.enabled ? ' current-firing' : ''}">${fmtValue(a.current_value, a.metric)}</td>`
      : '<td class="col-current">—</td>';

    const condition = a.operator ? `${escHtml(a.operator)} ${a.threshold}` : '—';

    const toggleBtn = `<button class="btn btn-sm ${a.enabled ? 'btn-warn' : 'btn-primary'}" onclick="event.stopPropagation(); toggleAlert('${escHtml(a.uid)}', ${!a.enabled}, this)">${a.enabled ? 'Disable' : 'Enable'}</button>`;

    const deleteBtn = a.provisioned
      ? ''
      : `<button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); deleteAlert('${escHtml(a.uid)}', this)">Delete</button>`;

    const pencilIcon = `<span class="inline-pencil">✎</span>`;
    const titleWithIcon = `<span class="alert-title-cell">${escHtml(a.title)}${pencilIcon}</span>`;

    return `<tr onclick="editAlert(${escHtml(JSON.stringify(a))})" class="alert-row-editable">
      <td>${titleWithIcon}</td>
      <td>${stateBadge}</td>
      <td>${escHtml(a.fridge)}</td>
      <td class="col-metric">${escHtml(a.metric)}</td>
      <td class="col-operator">${condition}</td>
      ${currentCell}
      <td>${toggleBtn}</td>
      <td>${deleteBtn}</td>
    </tr>`;
  }).join('');
}

async function toggleAlert(uid, enabled, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const resp = await apiFetch(`/alerts/${uid}/enabled`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    });
    if (resp.ok) {
      // Poll until Grafana confirms the change, then update the table.
      btn.textContent = 'Updating…';
      const confirmed = await pollAlertEnabled(uid, enabled);
      if (confirmed) {
        toast(`Alert ${enabled ? 'enabled' : 'disabled'}.`);
      } else {
        toast('State may not have applied — check Grafana.', 'error');
        await loadAlerts();
      }
    } else {
      const body = await resp.json().catch(() => ({}));
      toast(body.detail || `Error ${resp.status}`, 'error');
      btn.disabled = false;
      btn.textContent = enabled ? 'Enable' : 'Disable';
    }
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      toast('Toggle failed.', 'error');
      btn.disabled = false;
      btn.textContent = enabled ? 'Enable' : 'Disable';
    }
  }
}

// Poll /api/alerts until the named alert's enabled field matches the expected
// value, then re-render the table. Returns true if confirmed within timeout.
async function pollAlertEnabled(uid, expected, { intervalMs = 600, maxAttempts = 8 } = {}) {
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(r => setTimeout(r, intervalMs));
    try {
      const resp = await apiFetch('/alerts');
      if (!resp.ok) break;
      const alerts = await resp.json();
      window._alertsCache = alerts;
      const match = alerts.find((a) => a.uid === uid);
      if (match && match.enabled === expected) {
        renderAlerts(alerts);
        document.getElementById('refresh-indicator').textContent =
          'Updated ' + new Date().toLocaleTimeString();
        return true;
      }
    } catch (_) {
      break;
    }
  }
  return false;
}

async function deleteAlert(uid, btn) {
  if (!confirm('Permanently delete this alert?')) return;
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

// ── Edit existing alert (populate form with its values) ────────────────────────

function editAlert(alert) {
  // Populate form fields with this alert's values
  document.getElementById('f-name').value = alert.title || '';
  document.getElementById('f-fridge').value = alert.fridge || '';
  populateMetricDropdown(alert.fridge);
  document.getElementById('f-metric').value = alert.metric || '';
  document.getElementById('f-operator').value = alert.operator || '';
  document.getElementById('f-threshold').value = alert.threshold || '';

  // Reset template selector since we're now using an existing rule as template
  templateIndex = -1;
  const tplBtn = document.getElementById('btn-use-template');
  tplBtn.textContent = 'Use Template';
  tplBtn.className = 'btn btn-secondary btn-sm';
}

// ── Alert templates ──────────────────────────────────────────────────────────

document.getElementById('btn-use-template').addEventListener('click', () => {
  const btn = document.getElementById('btn-use-template');
  templateIndex++;
  if (templateIndex >= ALERT_TEMPLATES.length) {
    templateIndex = -1;
    document.getElementById('f-name').value = '';
    document.getElementById('f-fridge').value = '';
    document.getElementById('f-metric').innerHTML = '<option value="">— select fridge first —</option>';
    document.getElementById('f-operator').value = '';
    document.getElementById('f-threshold').value = '';
    btn.textContent = 'Use Template';
    btn.className = 'btn btn-secondary btn-sm';
  } else {
    const t = ALERT_TEMPLATES[templateIndex];
    document.getElementById('f-fridge').value = t.fridge;
    populateMetricDropdown(t.fridge);
    document.getElementById('f-metric').value = t.metric;
    document.getElementById('f-name').value = t.name;
    document.getElementById('f-operator').value = t.operator;
    document.getElementById('f-threshold').value = t.threshold;
    btn.textContent = `Using Template ${templateIndex + 1}/${ALERT_TEMPLATES.length}`;
    btn.className = 'btn btn-primary btn-sm';
  }
});

// Increment or append a number to a name (e.g. "Cooling Water" → "Cooling Water 1", or "Cooling Water 1" → "Cooling Water 2")
function incrementAlertName(name) {
  const match = name.match(/^(.+?)\s+(\d+)$/);
  if (match) {
    const base = match[1];
    const num = parseInt(match[2], 10);
    return `${base} ${num + 1}`;
  }
  return `${name} 1`;
}

// Pick a conflict-free name based on current alert titles.
function nextAvailableAlertName(name, alerts = window._alertsCache || []) {
  const match = name.match(/^(.+?)\s+(\d+)$/);
  const base = match ? match[1] : name;
  let next = match ? (parseInt(match[2], 10) + 1) : 1;

  const taken = new Set();
  for (const a of alerts) {
    const title = (a && a.title) ? String(a.title) : '';
    if (title === base) {
      taken.add(0);
      continue;
    }
    const m = title.match(new RegExp(`^${base.replace(/[.*+?^${}()|[\\]\\]/g, '\\\\$&')}\\s+(\\d+)$`));
    if (m) taken.add(parseInt(m[1], 10));
  }

  while (taken.has(next)) next += 1;
  return `${base} ${next}`;
}

// ── Create alert form ────────────────────────────────────────────────────────

document.getElementById('create-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('btn-create');
  const status = document.getElementById('create-status');
  btn.disabled = true;
  status.textContent = 'Creating…';

  let body = {
    name: document.getElementById('f-name').value.trim(),
    fridge: document.getElementById('f-fridge').value,
    metric: document.getElementById('f-metric').value,
    operator: document.getElementById('f-operator').value,
    threshold: parseFloat(document.getElementById('f-threshold').value),
    for_duration: document.getElementById('f-duration').value,
  };

  // Try to create with original name; if conflict, retry with incremented name
  const attemptCreate = async (attemptBody, retryCount = 0) => {
    const MAX_RETRIES = 50;  // Allow up to 50 name increments
    try {
      const resp = await apiFetch('/alerts', {
        method: 'POST',
        body: JSON.stringify(attemptBody),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        toast(`Alert "${data.title}" created.`);
        e.target.reset();
        populateFridgeDropdown();
        document.getElementById('f-metric').innerHTML =
          '<option value="">— select fridge first —</option>';
        templateIndex = -1;
        const tplBtn = document.getElementById('btn-use-template');
        tplBtn.textContent = 'Use Template';
        tplBtn.className = 'btn btn-secondary btn-sm';
        status.textContent = '';
        await loadAlerts();
        return true;
      }

      const errText = `${data?.detail || ''} ${data?.message || ''}`.toLowerCase();
      const isNameConflict = errText.includes('should be unique') || errText.includes('alert-rule.conflict');
      if (isNameConflict && retryCount < MAX_RETRIES) {
        // Conflict: title already exists, increment and retry
        const newName = retryCount === 0
          ? nextAvailableAlertName(attemptBody.name)
          : incrementAlertName(attemptBody.name);
        status.textContent = `Name exists, trying "${newName}"…`;
        attemptBody.name = newName;
        return attemptCreate(attemptBody, retryCount + 1);
      }

      const msg = data.detail || data.message || `Error ${resp.status}`;
      status.textContent = msg;
      toast(msg, 'error');
      return false;
    } catch (err) {
      if (err.message !== 'Unauthenticated') {
        status.textContent = 'Request failed.';
        toast('Request failed.', 'error');
      }
      return false;
    }
  };

  try {
    await attemptCreate(body);
  } finally {
    btn.disabled = false;
  }
});

// ── Recipients ───────────────────────────────────────────────────────────────

// Cached recipients list (uid → name/type)
window._recipientsCache = [];

async function loadRecipients() {
  if (!authHeader) return;
  try {
    const resp = await apiFetch('/recipients');
    if (!resp.ok) return;
    const recipients = await resp.json();
    window._recipientsCache = recipients;
    renderRecipients(recipients);
  } catch (_) {}
}

function renderRecipients(recipients) {
  const ul = document.getElementById('recipient-list');
  if (recipients.length === 0) {
    ul.innerHTML = '<li>No recipients configured.</li>';
    return;
  }
  ul.innerHTML = recipients.map((r) =>
    `<li>
      <button class="btn-link recipient-btn" data-uid="${escHtml(r.uid)}" data-name="${escHtml(r.name)}">
        ${escHtml(r.name)}
      </button>
    </li>`
  ).join('');
  ul.querySelectorAll('.recipient-btn').forEach((btn) => {
    btn.addEventListener('click', () => openAssignmentPanel(btn.dataset.uid, btn.dataset.name));
  });
}

// ── Assignment panel ────────────────────────────────────────────────

let _assignmentContactUid = null;

function openAssignmentPanel(contactUid, contactName) {
  _assignmentContactUid = contactUid;
  document.getElementById('assignment-panel-title').textContent = `Alerts for \u201c${contactName}\u201d`;

  // Wire up delete-recipient button with current recipient context
  const deleteBtn = document.getElementById('assignment-delete-recipient');
  deleteBtn.onclick = () => deleteRecipient(contactUid, contactName, deleteBtn);
  deleteBtn.disabled = false;

  const alerts = window._alertsCache || [];
  const list = document.getElementById('assignment-list');
  if (alerts.length === 0) {
    list.innerHTML = '<li>No alerts loaded. Refresh first.</li>';
  } else {
    list.innerHTML = alerts.map((a) => {
      const checked = (a.notify_to.length === 0 || a.notify_to.includes(contactUid)) ? 'checked' : '';
      const label = `${escHtml(a.title)} — ${escHtml(a.fridge)}`;
      return `<li class="assignment-item">
        <label>
          <input type="checkbox" class="assignment-cb" data-uid="${escHtml(a.uid)}" ${checked}>
          ${label}
        </label>
      </li>`;
    }).join('');
    // Attach listeners
    list.querySelectorAll('.assignment-cb').forEach((cb) => {
      cb.addEventListener('change', (e) => saveAssignment(cb.dataset.uid, contactUid, e.target.checked));
    });
  }

  const panel = document.getElementById('assignment-panel-container');
  panel.hidden = false;
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeAssignmentPanel() {
  document.getElementById('assignment-panel-container').hidden = true;
  _assignmentContactUid = null;
}

async function saveAssignment(alertUid, contactUid, add) {
  const alert = (window._alertsCache || []).find((a) => a.uid === alertUid);
  if (!alert) return;

  const allUids = (window._recipientsCache || []).map((r) => r.uid);
  // If notify_to is empty it means "all recipients" — expand to full list before modifying
  let current = alert.notify_to.length === 0 ? [...allUids] : [...alert.notify_to];

  if (add) {
    if (!current.includes(contactUid)) current.push(contactUid);
    // If every recipient is now listed, collapse back to empty (= send to all)
    if (allUids.length > 0 && allUids.every((u) => current.includes(u))) current = [];
  } else {
    current = current.filter((u) => u !== contactUid);
  }

  try {
    const resp = await apiFetch(`/alerts/${alertUid}/recipients`, {
      method: 'PATCH',
      body: JSON.stringify({ contact_uids: current }),
    });
    if (resp.ok) {
      // Update cache in-place so checkboxes stay consistent
      alert.notify_to = current;
      toast('Assignment saved.');
    } else {
      const body = await resp.json().catch(() => ({}));
      toast(body.detail || `Error ${resp.status}`, 'error');
      // Revert checkbox
      const cb = document.querySelector(`.assignment-cb[data-uid="${alertUid}"]`);
      if (cb) cb.checked = !add;
    }
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      toast('Save failed.', 'error');
      const cb = document.querySelector(`.assignment-cb[data-uid="${alertUid}"]`);
      if (cb) cb.checked = !add;
    }
  }
}

document.getElementById('assignment-close').addEventListener('click', closeAssignmentPanel);

async function deleteRecipient(contactUid, contactName, btn) {
  if (!confirm(`Delete recipient "${contactName}"? This cannot be undone.`)) return;
  btn.disabled = true;
  try {
    const resp = await apiFetch(`/recipients/${contactUid}`, { method: 'DELETE' });
    if (resp.ok) {
      toast(`Recipient \u201c${contactName}\u201d deleted.`);
      closeAssignmentPanel();
      await loadRecipients();
    } else {
      const body = await resp.json().catch(() => ({}));
      toast(body.detail || `Error ${resp.status}`, 'error');
      btn.disabled = false;
    }
  } catch (err) {
    if (err.message !== 'Unauthenticated') {
      toast('Delete failed.', 'error');
      btn.disabled = false;
    }
  }
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
