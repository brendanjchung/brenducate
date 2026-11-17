/**
 * Brenducate — Margin Control Dashboard Engine
 * Connects directly to FastAPI backend (`/v1/*`) on Vercel or localhost:8000.
 */

const API_BASE =
  (['localhost', '127.0.0.1'].includes(window.location.hostname) &&
    window.location.port !== '8000')
    ? 'http://localhost:8000'
    : '';

const STORAGE_TOKEN_KEY = 'brenducate_auth_token';
const DEMO_KEY = 'br_live_demo_key_00001';

const appState = {
  token: localStorage.getItem(STORAGE_TOKEN_KEY) || '',
  fullApiKeyOnce: '',
  profile: null,
  policy: null,
  margins: null,
  traces: [],
  alerts: [],
};

// ── DOM & Accessibility Helpers ───────────────────────────────────────────────

function el(tag, attrs = {}, text = '') {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null) continue;
    if (k === 'className') node.className = v;
    else node.setAttribute(k, String(v));
  }
  if (text !== '') node.textContent = text;
  return node;
}

function showToast(msg, tone = '') {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const item = el('div', { className: `toast ${tone}`.trim() }, msg);
  container.appendChild(item);
  setTimeout(() => item.remove(), 3400);
}

function fmtUsd(n, digits = 2) {
  const val = Number(n || 0);
  if (val > 0 && val < 0.01) return `$${val.toFixed(4)}`;
  return `$${val.toFixed(digits)}`;
}

// ── Dialog Light-Dismiss Fallback (Safari / Older Browsers) ───────────────────

function initDialogLightDismiss() {
  for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
    if (!('closedBy' in HTMLDialogElement.prototype)) {
      dialog.addEventListener('click', (event) => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        const inside =
          rect.top <= event.clientY &&
          event.clientY <= rect.top + rect.height &&
          rect.left <= event.clientX &&
          event.clientX <= rect.left + rect.width;
        if (!inside) dialog.close();
      });
    }
  }
}

// ── Form Validation ARIA Sync (:user-invalid) ─────────────────────────────────

function initValidationFeedback() {
  const dirtyState = new WeakMap();
  const useFallback = typeof CSS !== 'undefined' && !CSS.supports('selector(:user-invalid)');

  const syncAria = (input) => {
    if (!input || !input.checkValidity) return;
    if (useFallback) {
      const ok = input.checkValidity();
      input.classList.toggle('user-invalid-fallback', !ok);
      input.setAttribute('aria-invalid', ok ? 'false' : 'true');
    } else {
      input.setAttribute('aria-invalid', input.matches(':user-invalid') ? 'true' : 'false');
    }
  };

  document.addEventListener(
    'blur',
    (e) => {
      const t = e.target;
      if (!t || !t.checkValidity) return;
      const st = dirtyState.get(t) || { interacted: false, blurred: false };
      st.blurred = true;
      dirtyState.set(t, st);
      if (!useFallback || st.interacted) syncAria(t);
    },
    true
  );

  document.addEventListener('input', (e) => {
    const t = e.target;
    if (!t || !t.checkValidity) return;
    const st = dirtyState.get(t) || { interacted: false, blurred: false };
    st.interacted = true;
    dirtyState.set(t, st);
    if (st.blurred || t.hasAttribute('aria-invalid')) syncAria(t);
  });
}

// ── HTTP Helper ───────────────────────────────────────────────────────────────

async function apiFetch(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (appState.token && !headers.Authorization) {
    headers.Authorization = `Bearer ${appState.token}`;
  }
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail = body?.detail || `HTTP ${res.status}`;
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ── Auth State & Forms ────────────────────────────────────────────────────────

function switchAuthMode(mode) {
  const isSignup = mode !== 'login';
  document.getElementById('modeSignupBtn').setAttribute('aria-selected', String(isSignup));
  document.getElementById('modeLoginBtn').setAttribute('aria-selected', String(!isSignup));
  document.getElementById('signupForm').classList.toggle('hidden', !isSignup);
  document.getElementById('loginForm').classList.toggle('hidden', isSignup);
}

function setAuthenticatedUI(isAuthed) {
  document.getElementById('authGate').classList.toggle('hidden', isAuthed);
  document.getElementById('dashApp').classList.toggle('hidden', !isAuthed);
  document.getElementById('dashNav').classList.toggle('hidden', !isAuthed);
  document.getElementById('workspaceBadge').classList.toggle('hidden', !isAuthed);
  document.getElementById('upgradeBtn').classList.toggle('hidden', !isAuthed);
  document.getElementById('logoutBtn').classList.toggle('hidden', !isAuthed);
}

async function bootstrapSession() {
  if (window.location.hash === '#login') switchAuthMode('login');
  if (window.location.hash === '#signup') switchAuthMode('signup');

  if (!appState.token) {
    setAuthenticatedUI(false);
    return;
  }
  try {
    appState.profile = await apiFetch('/v1/auth/me');
    setAuthenticatedUI(true);
    renderProfile();
    await loadAllDashboardData();
  } catch {
    localStorage.removeItem(STORAGE_TOKEN_KEY);
    appState.token = '';
    setAuthenticatedUI(false);
  }
}

function renderProfile() {
  const p = appState.profile;
  if (!p) return;

  const badge = document.getElementById('workspaceBadge');
  badge.textContent = `${p.team_name} · ${p.plan}`;

  const keyChip = document.getElementById('keyDisplayChip');
  const copyBtn = document.getElementById('copyFullKeyBtn');
  if (appState.fullApiKeyOnce) {
    keyChip.textContent = appState.fullApiKeyOnce;
    copyBtn.classList.remove('hidden');
  } else {
    keyChip.textContent = p.api_key_prefix;
    copyBtn.classList.add('hidden');
  }

  document.getElementById('sdkKeyPrefix').textContent = p.api_key_prefix;
  document.getElementById('slackWebhookInput').value = p.slack_webhook_url || '';

  const keyPlaceholder = appState.fullApiKeyOnce || p.api_key_prefix;
  document.getElementById('sdkSnippetPre').textContent =
`# pip install brenducate-sdk
from openai import OpenAI
from brenducate import Brenducate, CircuitBreaker, wrap_openai

sentinel = Brenducate(api_key="${keyPlaceholder}")
client = wrap_openai(
    OpenAI(),
    pulse=sentinel,
    circuit_breaker=CircuitBreaker(degrade_after_cost_usd=0.25, max_cost_usd=1.50),
)

# Tag requests with your end-customer ID
with sentinel.tenant("acme_corp"):
    res = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Analyze Q3 margin drivers"}],
        stream=True,
    )
    for chunk in res:
        pass`;
}

// ── Dashboard Data Sync & Safe DOM Rendering ──────────────────────────────────

async function loadAllDashboardData() {
  const [marginsRes, tracesRes, policyRes, alertsRes] = await Promise.all([
    apiFetch('/v1/dashboard/margins'),
    apiFetch(buildTracesQuery()),
    apiFetch('/v1/policies/sync'),
    apiFetch('/v1/alerts?resolved=false'),
  ]);
  appState.margins = marginsRes;
  appState.traces = tracesRes;
  appState.policy = policyRes.policy;
  appState.alerts = alertsRes;

  renderMargins(marginsRes);
  renderTracesTable(tracesRes);
  renderPolicyEditor(policyRes.policy);
  renderAlerts(alertsRes);
}

function buildTracesQuery() {
  const tenant = document.getElementById('traceTenantFilter')?.value?.trim();
  const statusVal = document.getElementById('traceStatusFilter')?.value;
  const params = new URLSearchParams({ limit: '60' });
  if (tenant) params.set('tenant_id', tenant);
  if (statusVal) params.set('status_filter', statusVal);
  return `/v1/telemetry/traces?${params.toString()}`;
}

function statusBadge(st) {
  const map = {
    ok: ['badge-ok', 'Within budget'],
    COMPLETED: ['badge-ok', 'Completed'],
    degrade: ['badge-warn', 'Smart Degraded'],
    DEGRADED: ['badge-warn', 'Smart Degraded'],
    blocked: ['badge-halt', 'Daily Cap Blocked'],
    BUDGET_BLOCKED: ['badge-halt', 'Budget Blocked'],
    CIRCUIT_KILLED: ['badge-halt', 'Loop Halted'],
    ERROR: ['badge-neutral', 'Provider Error'],
  };
  const [cls, label] = map[st] || ['badge-neutral', st || 'Unknown'];
  return el('span', { className: `badge ${cls}` }, label);
}

function renderMargins(m) {
  document.getElementById('kpiSaved30d').textContent = fmtUsd(m.net_margin_saved_30d_usd);
  document.getElementById('kpiSpend24h').textContent = fmtUsd(m.total_spend_24h_usd);
  document.getElementById('kpiSpend30dSub').textContent = `${fmtUsd(m.total_spend_30d_usd)} spent in last 30 days`;
  document.getElementById('kpiTenantsCount').textContent = `${m.active_tenants_count} tenants`;
  document.getElementById('kpiDegraded24h').textContent = String(m.degraded_calls_24h);
  document.getElementById('kpiKilled24h').textContent = String(m.killed_runs_24h);
  document.getElementById('kpiAlertsSub').textContent = `${m.open_alerts} open alerts`;

  const tbody = document.getElementById('tenantsTableBody');
  tbody.replaceChildren();

  if (!m.top_tenants || m.top_tenants.length === 0) {
    const tr = el('tr');
    const td = el('td', { colspan: '9', className: 'muted', style: 'text-align:center; padding: 2rem;' },
      'No tenant activity recorded yet. Click "Simulate Degraded Run" above to verify live telemetry.');
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  for (const row of m.top_tenants) {
    const tr = el('tr');

    const th = el('th', { scope: 'row', className: 'mono' }, row.tenant_id);
    tr.appendChild(th);

    tr.appendChild(el('td', { className: 'r', 'data-label': 'Spent Today' }, fmtUsd(row.spent_today_usd)));

    const tdSaved = el('td', { className: 'r', 'data-label': 'Saved Today', style: 'color: var(--ok); font-weight: 500;' },
      fmtUsd(row.saved_today_usd));
    tr.appendChild(tdSaved);

    tr.appendChild(el('td', { className: 'r', 'data-label': '30d Spend' }, fmtUsd(row.spent_30d_usd)));
    tr.appendChild(el('td', { className: 'r', 'data-label': 'Runs' }, String(row.run_count)));
    tr.appendChild(el('td', { className: 'r', 'data-label': 'Degraded' }, String(row.degraded_calls_count)));
    tr.appendChild(el('td', { className: 'r', 'data-label': 'Halted' }, String(row.killed_runs_count)));

    const tdStatus = el('td', { 'data-label': 'Status' });
    tdStatus.appendChild(statusBadge(row.status));
    tr.appendChild(tdStatus);

    const tdCtrl = el('td', { 'data-label': 'Manual Override' });
    const wrap = el('div', { style: 'display:flex; gap:0.375rem; flex-wrap:wrap;' });

    const btnAuto = el('button', {
      type: 'button',
      className: `btn btn-xs ${row.override_mode === 'auto' ? 'btn-primary' : 'btn-secondary'}`,
    }, 'Auto');
    btnAuto.addEventListener('click', () => setTenantMode(row.tenant_id, 'auto'));

    const btnDeg = el('button', {
      type: 'button',
      className: `btn btn-xs ${row.override_mode === 'force_degrade' ? 'btn-primary' : 'btn-secondary'}`,
    }, 'Force Step-Down');
    btnDeg.addEventListener('click', () => setTenantMode(row.tenant_id, 'force_degrade'));

    const btnBlock = el('button', {
      type: 'button',
      className: `btn btn-xs ${row.override_mode === 'blocked' ? 'btn-danger' : 'btn-secondary'}`,
    }, 'Block');
    btnBlock.addEventListener('click', () => setTenantMode(row.tenant_id, 'blocked'));

    wrap.append(btnAuto, btnDeg, btnBlock);
    tdCtrl.appendChild(wrap);
    tr.appendChild(tdCtrl);

    tbody.appendChild(tr);
  }
}

async function setTenantMode(tenantId, mode) {
  try {
    await apiFetch(`/v1/tenants/${encodeURIComponent(tenantId)}/mode`, {
      method: 'PATCH',
      body: JSON.stringify({ override_mode: mode }),
    });
    showToast(`Tenant '${tenantId}' set to ${mode}. SDKs sync within 60s.`, 'ok');
    await loadAllDashboardData();
  } catch (err) {
    showToast(err.message, 'warn');
  }
}

function renderTracesTable(traces) {
  const tbody = document.getElementById('tracesTableBody');
  tbody.replaceChildren();

  if (!traces || traces.length === 0) {
    const tr = el('tr');
    tr.appendChild(el('td', { colspan: '8', className: 'muted', style: 'text-align:center; padding: 2rem;' },
      'No matching traces found.'));
    tbody.appendChild(tr);
    return;
  }

  for (const t of traces) {
    const tr = el('tr', { className: 'clickable' });
    tr.addEventListener('click', () => openTraceDetail(t.id));

    tr.appendChild(el('th', { scope: 'row', className: 'mono' }, t.id));
    tr.appendChild(el('td', { 'data-label': 'Tenant', className: 'mono' }, t.tenant_id));
    tr.appendChild(el('td', { 'data-label': 'Agent' }, t.agent_id));

    const modelText = t.was_degraded
      ? `${t.original_model} → ${t.executed_model}`
      : t.executed_model;
    tr.appendChild(el('td', { 'data-label': 'Model Path', className: 'mono', style: 'font-size:0.75rem;' }, modelText));

    const tdSt = el('td', { 'data-label': 'Status' });
    tdSt.appendChild(statusBadge(t.status));
    tr.appendChild(tdSt);

    const tokLabel = `${(t.total_tokens || 0).toLocaleString()} (${(t.cached_input_tokens || 0).toLocaleString()} cached)`;
    tr.appendChild(el('td', { className: 'r', 'data-label': 'Tokens' }, tokLabel));
    tr.appendChild(el('td', { className: 'r', 'data-label': 'Cost' }, fmtUsd(t.cost_usd, 4)));
    tr.appendChild(el('td', {
      className: 'r',
      'data-label': 'Saved',
      style: t.estimated_savings_usd > 0 ? 'color: var(--ok); font-weight:500;' : '',
    }, fmtUsd(t.estimated_savings_usd, 4)));

    tbody.appendChild(tr);
  }
}

async function openTraceDetail(traceId) {
  try {
    const trace = await apiFetch(`/v1/telemetry/traces/${encodeURIComponent(traceId)}`);
    document.getElementById('traceDialogTitle').textContent = `${trace.agent_id} (${trace.tenant_id})`;
    document.getElementById('traceDialogMeta').textContent =
      `${trace.id} · ${trace.was_degraded ? `${trace.original_model} → ${trace.executed_model}` : trace.executed_model} · ${trace.latency_ms}ms`;

    const body = document.getElementById('traceDialogBody');
    body.replaceChildren();

    const stats = el('div', {
      style: 'display:grid; grid-template-columns:repeat(auto-fit, minmax(120px,1fr)); gap:0.75rem; margin-bottom:1.25rem;',
    });
    const metrics = [
      ['Uncached Input', trace.input_tokens.toLocaleString()],
      ['Cache Read Hits', trace.cached_input_tokens.toLocaleString()],
      ['Output (Reason)', `${trace.output_tokens.toLocaleString()} (${trace.reasoning_tokens})`],
      ['Actual Cost', fmtUsd(trace.cost_usd, 4)],
      ['Saved via Guardrail', fmtUsd(trace.estimated_savings_usd, 4)],
    ];
    for (const [k, v] of metrics) {
      const box = el('div', { style: 'border:1px solid var(--line); border-radius:8px; padding:0.625rem 0.75rem;' });
      box.appendChild(el('div', { className: 'subtle-text', style: 'font-size:0.75rem;' }, k));
      box.appendChild(el('strong', { className: 'tabular' }, v));
      stats.appendChild(box);
    }
    body.appendChild(stats);

    if (trace.guardrail_reason) {
      const reasonBox = el('div', {
        style: 'background: var(--accent-soft); color: var(--accent-ink); padding:0.75rem 1rem; border-radius:8px; margin-bottom:1.25rem; font-size:0.875rem;',
      });
      reasonBox.appendChild(el('strong', {}, 'Guardrail Reason: '));
      reasonBox.appendChild(document.createTextNode(trace.guardrail_reason));
      body.appendChild(reasonBox);
    }

    body.appendChild(el('h3', { className: 'card-title', style: 'margin-bottom:0.625rem;' }, 'Step Timeline'));
    const stepsList = el('div', { style: 'display:flex; flex-direction:column; gap:0.5rem;' });
    const steps = trace.steps || [];
    if (steps.length === 0) {
      stepsList.appendChild(el('p', { className: 'subtle-text' }, `Prompt: ${trace.prompt}`));
    } else {
      for (const s of steps) {
        const card = el('div', { style: 'border:1px solid var(--line); border-radius:8px; padding:0.75rem;' });
        const head = el('div', { style: 'display:flex; justify-content:space-between; gap:0.5rem; margin-bottom:0.25rem;' });
        head.appendChild(el('strong', { style: 'font-size:0.875rem;' }, `${s.step_index}. ${s.title}`));
        head.appendChild(el('span', { className: 'badge badge-neutral mono' }, s.step_type));
        card.appendChild(head);
        card.appendChild(el('p', { className: 'muted', style: 'font-size:0.8125rem; white-space:pre-wrap;' }, s.content));
        stepsList.appendChild(card);
      }
    }
    body.appendChild(stepsList);

    document.getElementById('traceDialog').showModal();
  } catch (err) {
    showToast(err.message, 'warn');
  }
}

function renderPolicyEditor(pol) {
  if (!pol) return;
  document.getElementById('polRunDegrade').value = pol.degrade_after_run_cost_usd;
  document.getElementById('polRunMax').value = pol.max_run_cost_usd;
  document.getElementById('polTenantDegrade').value = pol.tenant_daily_degrade_usd;
  document.getElementById('polTenantCap').value = pol.tenant_daily_cap_usd;
  document.getElementById('polMaxHops').value = pol.max_hops;
  document.getElementById('polDupCalls').value = pol.max_duplicate_calls;
  document.getElementById('polKillLoop').checked = Boolean(pol.kill_on_infinite_loop);

  const list = document.getElementById('fallbackMapList');
  list.replaceChildren();
  const entries = Object.entries(pol.model_fallback_map || {});
  for (const [src, dst] of entries) {
    const row = el('div', {
      style: 'display:flex; justify-content:space-between; align-items:center; padding:0.5625rem 0.75rem; border:1px solid var(--line); border-radius:8px; font-size:0.8125rem;',
    });
    row.appendChild(el('code', { className: 'mono' }, src));
    row.appendChild(el('span', { className: 'subtle-text' }, '→'));
    row.appendChild(el('code', { className: 'mono', style: 'color: var(--ok);' }, dst));
    list.appendChild(row);
  }
}

function renderAlerts(alerts) {
  const container = document.getElementById('alertsList');
  container.replaceChildren();

  if (!alerts || alerts.length === 0) {
    container.appendChild(el('p', { className: 'subtle-text' }, 'No active guardrail alerts.'));
    return;
  }

  for (const a of alerts) {
    const item = el('div', { className: 'alert-item' });
    const left = el('div');
    left.appendChild(statusBadge(a.severity === 'critical' ? 'CIRCUIT_KILLED' : 'DEGRADED'));
    left.appendChild(el('p', { style: 'font-size:0.875rem; margin-top:0.375rem;' }, a.message));
    left.appendChild(el('span', { className: 'subtle-text mono' }, `tenant: ${a.tenant_id} · agent: ${a.agent_id}`));

    const btn = el('button', { type: 'button', className: 'btn btn-xs btn-secondary' }, 'Resolve');
    btn.addEventListener('click', async () => {
      await apiFetch(`/v1/alerts/${a.id}/resolve`, { method: 'PATCH' });
      showToast('Alert resolved', 'ok');
      await loadAllDashboardData();
    });

    item.append(left, btn);
    container.appendChild(item);
  }
}

// ── Simulate Realistic Smart Degradation Run ──────────────────────────────────

async function simulateDegradedTrace() {
  const tenants = ['acme_corp', 'globex_inc', 'northstar_ops', 'whale_saas'];
  const tid = tenants[Math.floor(Math.random() * tenants.length)];
  const isLoop = Math.random() < 0.3;

  const payload = isLoop
    ? {
        tenant_id: tid,
        agent_id: 'sql-analyst',
        original_model: 'gpt-4o',
        executed_model: 'gpt-4o',
        was_degraded: false,
        prompt: 'Analyze monthly recurring cohort retention across Stripe tables',
        status: 'CIRCUIT_KILLED',
        input_tokens: 9400,
        cached_input_tokens: 4200,
        output_tokens: 1850,
        cost_usd: 0.0472,
        estimated_savings_usd: 0.48,
        latency_ms: 1420,
        hops: 4,
        circuit_tripped: true,
        guardrail_reason: "Repetitive tool loop detected (3x identical calls): sql_exec('SELECT * FROM subscriptions')",
        steps: [
          { step_index: 1, step_type: 'thought', title: 'Workflow Initiated', content: `Tenant=${tid} cohort query` },
          { step_index: 2, step_type: 'tool', title: 'sql_exec() timeout #1', content: 'Query timed out, retrying' },
          { step_index: 3, step_type: 'guardrail', title: 'Loop Halted in <1ms', content: 'Prevented $0.48 overnight burn spike.' },
        ],
      }
    : {
        tenant_id: tid,
        agent_id: 'support-copilot',
        original_model: 'claude-sonnet-4-20250514',
        executed_model: 'claude-3-5-haiku-latest',
        was_degraded: true,
        prompt: 'Resolve multi-step invoice refund dispute and cite billing SLA clauses',
        status: 'DEGRADED',
        input_tokens: 18500,
        cached_input_tokens: 14000,
        cache_creation_tokens: 0,
        output_tokens: 3400,
        cost_usd: 0.0295,
        estimated_savings_usd: 0.0812,
        latency_ms: 1890,
        hops: 6,
        circuit_tripped: false,
        guardrail_reason: 'Run crossed $0.25 soft cap; stepped down Sonnet -> Haiku sticky to preserve cache',
        steps: [
          { step_index: 1, step_type: 'tool', title: 'Anthropic (claude-sonnet-4)', content: 'Steps 1-3 analyzed invoice ledger.' },
          { step_index: 2, step_type: 'degrade', title: 'Smart Degradation Activated', content: 'Soft cap crossed. Stepped down Sonnet -> claude-3-5-haiku-latest.' },
          { step_index: 3, step_type: 'final', title: 'Anthropic (claude-3-5-haiku-latest)', content: 'Final reply streamed & cached. Saved $0.0812.' },
        ],
      };

  try {
    await apiFetch('/v1/telemetry/traces', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showToast(`Logged live trace for tenant '${tid}'!`, 'ok');
    await loadAllDashboardData();
  } catch (err) {
    showToast(err.message, 'warn');
  }
}

// ── Event Wiring ──────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  initDialogLightDismiss();
  initValidationFeedback();

  document.getElementById('modeSignupBtn').addEventListener('click', () => switchAuthMode('signup'));
  document.getElementById('modeLoginBtn').addEventListener('click', () => switchAuthMode('login'));

  document.getElementById('signupForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('signupError');
    errEl.textContent = '';
    const btn = document.getElementById('signupSubmitBtn');
    btn.disabled = true;
    try {
      const res = await apiFetch('/v1/auth/signup', {
        method: 'POST',
        body: JSON.stringify({
          team_name: document.getElementById('signupTeam').value,
          email: document.getElementById('signupEmail').value,
          password: document.getElementById('signupPassword').value,
        }),
      });
      appState.token = res.session_token;
      appState.fullApiKeyOnce = res.api_key || '';
      localStorage.setItem(STORAGE_TOKEN_KEY, res.session_token);
      showToast('Workspace created! Copy your live API key above.', 'ok');
      await bootstrapSession();
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('loginError');
    errEl.textContent = '';
    const btn = document.getElementById('loginSubmitBtn');
    btn.disabled = true;
    try {
      const res = await apiFetch('/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify({
          email: document.getElementById('loginEmail').value,
          password: document.getElementById('loginPassword').value,
        }),
      });
      appState.token = res.session_token;
      appState.fullApiKeyOnce = '';
      localStorage.setItem(STORAGE_TOKEN_KEY, res.session_token);
      await bootstrapSession();
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById('enterDemoSandboxBtn').addEventListener('click', async () => {
    appState.token = DEMO_KEY;
    appState.fullApiKeyOnce = DEMO_KEY;
    localStorage.setItem(STORAGE_TOKEN_KEY, DEMO_KEY);
    await bootstrapSession();
  });

  document.getElementById('logoutBtn').addEventListener('click', () => {
    localStorage.removeItem(STORAGE_TOKEN_KEY);
    appState.token = '';
    appState.fullApiKeyOnce = '';
    appState.profile = null;
    setAuthenticatedUI(false);
  });

  // Tab navigation
  for (const tabBtn of document.querySelectorAll('.dash-tab')) {
    tabBtn.addEventListener('click', () => {
      for (const b of document.querySelectorAll('.dash-tab')) b.classList.remove('active');
      for (const p of document.querySelectorAll('.dash-pane')) p.classList.remove('active');
      tabBtn.classList.add('active');
      document.getElementById(`pane-${tabBtn.dataset.pane}`).classList.add('active');
    });
  }

  document.getElementById('copyFullKeyBtn').addEventListener('click', async () => {
    if (!appState.fullApiKeyOnce) return;
    await navigator.clipboard.writeText(appState.fullApiKeyOnce);
    showToast('API Key copied to clipboard', 'ok');
  });

  document.getElementById('copySdkSnippetBtn').addEventListener('click', async () => {
    const txt = document.getElementById('sdkSnippetPre').textContent;
    await navigator.clipboard.writeText(txt);
    showToast('Python SDK snippet copied', 'ok');
  });

  document.getElementById('sendTestTraceBtn').addEventListener('click', simulateDegradedTrace);
  document.getElementById('refreshDataBtn').addEventListener('click', async () => {
    await loadAllDashboardData();
    showToast('Synced with backend');
  });

  document.getElementById('traceTenantFilter').addEventListener('change', async () => {
    appState.traces = await apiFetch(buildTracesQuery());
    renderTracesTable(appState.traces);
  });
  document.getElementById('traceStatusFilter').addEventListener('change', async () => {
    appState.traces = await apiFetch(buildTracesQuery());
    renderTracesTable(appState.traces);
  });

  document.getElementById('policyForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const payload = {
        degrade_after_run_cost_usd: parseFloat(document.getElementById('polRunDegrade').value),
        max_run_cost_usd: parseFloat(document.getElementById('polRunMax').value),
        tenant_daily_degrade_usd: parseFloat(document.getElementById('polTenantDegrade').value),
        tenant_daily_cap_usd: parseFloat(document.getElementById('polTenantCap').value),
        max_hops: parseInt(document.getElementById('polMaxHops').value, 10),
        max_duplicate_calls: parseInt(document.getElementById('polDupCalls').value, 10),
        kill_on_infinite_loop: document.getElementById('polKillLoop').checked,
        model_fallback_map: appState.policy?.model_fallback_map || {},
      };
      appState.policy = await apiFetch('/v1/policies', {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      renderPolicyEditor(appState.policy);
      showToast('Policies updated. Live SDKs receive changes on next sync.', 'ok');
    } catch (err) {
      showToast(err.message, 'warn');
    }
  });

  document.getElementById('rotateKeyBtn').addEventListener('click', async () => {
    try {
      const res = await apiFetch('/v1/auth/keys/rotate', { method: 'POST' });
      appState.fullApiKeyOnce = res.api_key;
      document.getElementById('rotatedKeyBox').classList.remove('hidden');
      document.getElementById('rotatedKeyVal').textContent = res.api_key;
      if (appState.profile) appState.profile.api_key_prefix = res.api_key_prefix;
      renderProfile();
      showToast('New API key issued', 'ok');
    } catch (err) {
      showToast(err.message, 'warn');
    }
  });

  document.getElementById('webhookForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const url = document.getElementById('slackWebhookInput').value.trim();
      await apiFetch('/v1/auth/webhook', {
        method: 'PATCH',
        body: JSON.stringify({ slack_webhook_url: url }),
      });
      showToast('Slack webhook saved', 'ok');
    } catch (err) {
      showToast(err.message, 'warn');
    }
  });

  document.getElementById('testWebhookBtn').addEventListener('click', async () => {
    try {
      await apiFetch('/v1/alerts/test-webhook', { method: 'POST' });
      showToast('Test ping delivered to Slack!', 'ok');
    } catch (err) {
      showToast(err.message, 'warn');
    }
  });

  document.getElementById('closeTraceDialogBtn').addEventListener('click', () => {
    document.getElementById('traceDialog').close();
  });

  document.getElementById('upgradeBtn').addEventListener('click', () => {
    document.getElementById('upgradeDialog').showModal();
  });
  document.getElementById('closeUpgradeDialogBtn').addEventListener('click', () => {
    document.getElementById('upgradeDialog').close();
  });

  for (const btn of document.querySelectorAll('.checkout-plan-btn')) {
    btn.addEventListener('click', async () => {
      if (!appState.profile) return;
      try {
        const res = await apiFetch('/v1/billing/create-checkout-session', {
          method: 'POST',
          body: JSON.stringify({
            plan: btn.dataset.plan,
            email: appState.profile.email,
            team_name: appState.profile.team_name,
            success_url: `${window.location.origin}/dashboard.html?status=success`,
            cancel_url: `${window.location.origin}/dashboard.html?status=cancelled`,
          }),
        });
        if (res.checkout_url && res.checkout_url.startsWith('https://checkout.stripe.com')) {
          window.location.href = res.checkout_url;
          return;
        }
        document.getElementById('upgradeDialog').close();
        showToast(`Workspace upgraded to ${res.plan.toUpperCase()} (sandbox mode)!`, 'ok');
        await bootstrapSession();
      } catch (err) {
        showToast(err.message, 'warn');
      }
    });
  }

  bootstrapSession();
});
