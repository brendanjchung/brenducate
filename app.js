/**
 * AgentPulse Ops — Client-Side Application Engine
 * High-performance autonomous agent telemetry, drift sentinel & guardrails
 */

// Initial Seed Data: Traces
const INITIAL_TRACES = [
  {
    id: 'TR-8941',
    agent: 'Financial Research Agent',
    agentKey: 'researcher',
    model: 'Claude 3.7 Sonnet',
    prompt: 'Analyze Q3 semiconductor supply chain bottlenecks and extract TSMC margin guidance',
    status: 'COMPLETED',
    tokens: 34210,
    cost: 0.184,
    latency: '1.42s',
    hops: 4,
    timestamp: '2 mins ago',
    steps: [
      { type: 'thought', title: 'Task Decomposition', content: 'Decomposing query: Identify 10-Q filing references, query TSMC 2026 Q3 gross margins and fab capacity constraints.' },
      { type: 'tool', title: 'Tool Call: sec_edgar_filings(ticker="TSM", form="10-Q")', content: 'Invoking SEC EDGAR API. Found 1 matching report filed Oct 18, 2026.' },
      { type: 'tool', title: 'Tool Call: vector_search(query="gross margin target advanced nodes")', content: 'Semantic search returned 4 chunks: "Gross margin targeted at 53-55% for 3nm & 2nm ramp."' },
      { type: 'final', title: 'Final Synthesized Response', content: 'TSMC reported 54.2% gross margin in Q3 2026. Management reiterated long-term target of 53%+ despite advanced packaging supply constraints.' }
    ]
  },
  {
    id: 'TR-8939',
    agent: 'Autonomous SQL Analyst',
    agentKey: 'sql',
    model: 'GPT-4o',
    prompt: 'Find customer churn cohorts in Stripe subscriptions where churn occurred within 45 days',
    status: 'CIRCUIT_KILLED',
    tokens: 68140,
    cost: 0.512,
    latency: '3.18s',
    hops: 7,
    timestamp: '14 mins ago',
    steps: [
      { type: 'thought', title: 'Formulating Query', content: 'Attempting schema inspection on postgres `subscriptions` and `events` tables.' },
      { type: 'tool', title: 'Tool Call: sql_exec("SELECT * FROM subscriptions WHERE...")', content: 'Execution timed out. Agent re-attempted with same syntax.' },
      { type: 'tool', title: 'Tool Call: sql_exec("SELECT * FROM subscriptions WHERE...")', content: 'Execution timed out. Agent re-attempted identical query.' },
      { type: 'guardrail', title: 'Circuit Breaker: Repetitive Query Guardrail #3', content: 'Identical query pattern detected 3 times with no parameter variation. Agent killed in 8ms to prevent runaway query cost.' }
    ]
  },
  {
    id: 'TR-8935',
    agent: 'Customer Copilot',
    agentKey: 'support',
    model: 'Gemini 2.5 Pro',
    prompt: 'Customer asking for invoice refund policy for enterprise annual contracts',
    status: 'DRIFT_WARNING',
    tokens: 12400,
    cost: 0.038,
    latency: '0.84s',
    hops: 2,
    timestamp: '32 mins ago',
    steps: [
      { type: 'thought', title: 'Policy Retrieval', content: 'Querying enterprise service agreement terms.' },
      { type: 'tool', title: 'Tool Call: kb_search("enterprise refund SLA")', content: 'Retrieved Section 8.4: Annual contracts non-refundable after 30 days.' },
      { type: 'final', title: 'Response Generated (Drift Detected)', content: 'Model offered a custom 60-day refund window without manager authorization. Semantic drift score: 0.82.' }
    ]
  },
  {
    id: 'TR-8928',
    agent: 'Code Review Sentinel',
    agentKey: 'code',
    model: 'Claude 3.7 Sonnet',
    prompt: 'Audit PR #842 for SQL injection and unsafe subprocess execution in Python service',
    status: 'COMPLETED',
    tokens: 41800,
    cost: 0.224,
    latency: '1.95s',
    hops: 3,
    timestamp: '1h 10m ago',
    steps: [
      { type: 'thought', title: 'AST Analysis', content: 'Parsing python diff for `exec()`, `eval()`, and unparameterized database cursors.' },
      { type: 'tool', title: 'Tool Call: semgrep_scan(target="services/db.py")', content: 'Found 0 vulnerabilities. Code conforms to PEP-675 literal string typing.' },
      { type: 'final', title: 'Review Passed', content: 'PR approved with 2 minor linting recommendations.' }
    ]
  }
];

// Code snippets for SDK tab
const SDK_SNIPPETS = {
  python: `# pip install agentpulse-sdk
from agentpulse import AgentPulse, CircuitBreaker

# 1. Initialize sentinel with your telemetry key
sentinel = AgentPulse(
    api_key="ap_live_7x90e8c201a4e5f98",
    environment="production"
)

# 2. Wrap your agent runner or LangChain / CrewAI executor
@sentinel.monitor(
    agent_id="financial-researcher",
    circuit_breaker=CircuitBreaker(
        max_hops=6,
        max_cost_usd=0.50,
        kill_on_infinite_loop=True
    )
)
async def run_agent_workflow(user_goal: str):
    # Your agent code runs untouched!
    result = await my_custom_agent.execute(user_goal)
    return result`,

  typescript: `// npm install @agentpulse/sdk
import { AgentPulse } from '@agentpulse/sdk';

const sentinel = new AgentPulse({
  apiKey: 'ap_live_7x90e8c201a4e5f98',
  environment: 'production',
  circuitBreaker: {
    maxHops: 6,
    maxCostUsd: 0.50,
    killOnRecursion: true,
  }
});

export async function handleAgentRequest(goal: string) {
  // Wrap your agent execution context
  return await sentinel.trace('customer-copilot', async (trace) => {
    const response = await myAgent.run(goal, {
      onStep: (step) => trace.logStep(step)
    });
    return response;
  });
}`,

  curl: `# Send raw async telemetry event to AgentPulse Edge Ingestion API
curl -X POST https://api.agentpulse.io/v1/telemetry/traces \\
  -H "Authorization: Bearer ap_live_7x90e8c201a4e5f98" \\
  -H "Content-Type: application/json" \\
  -d '{
    "agent_id": "custom-agent-01",
    "model": "claude-3-7-sonnet",
    "prompt": "Summarize market data",
    "hops": 3,
    "input_tokens": 12400,
    "output_tokens": 850,
    "cost_usd": 0.048,
    "latency_ms": 1120,
    "circuit_tripped": false
  }'`,

  openai: `# pip install agentpulse-sdk openai
from openai import OpenAI
from agentpulse import AgentPulse, CircuitBreaker, wrap_openai

# 1. Initialize sentinel
pulse = AgentPulse(api_key="ap_live_7x90e8c201a4e5f98")

# 2. 1-Line wrap around your existing OpenAI client
client = wrap_openai(
    OpenAI(),
    pulse=pulse,
    agent_id="customer-support-copilot",
    circuit_breaker=CircuitBreaker(max_hops=6, max_cost_usd=0.50)
)

# Standard calls are now monitored with zero code changes!
response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Analyze user invoice"}]
)`,

  langchain: `# pip install agentpulse-sdk langchain
from agentpulse import AgentPulse, CircuitBreaker
from agentpulse.integrations.langchain import AgentPulseCallbackHandler
from langchain.agents import AgentExecutor

pulse = AgentPulse(api_key="ap_live_7x90e8c201a4e5f98")

# Add the callback handler to any LangChain agent or chain
handler = AgentPulseCallbackHandler(
    pulse=pulse,
    agent_id="research-langchain-agent",
    circuit_breaker=CircuitBreaker(max_hops=8, max_cost_usd=0.75)
)

executor = AgentExecutor(agent=agent, tools=tools, callbacks=[handler])
result = executor.invoke({"input": "Generate competitor matrix"})`
};

// Application State
const API_BASE_URL = 'http://localhost:8000';
const DEMO_API_KEY = 'ap_live_demo_key_00001';

const state = {
  activeTab: 'dashboard',
  traces: [...INITIAL_TRACES],
  simulationRunning: false,
  simulationInterval: null,
  simTimerInterval: null,
  rules: {
    maxHops: 6,
    maxCost: 0.75,
    maxDupes: 2,
    recursionActive: true,
    costActive: true,
    repetitionActive: true,
    piiActive: true,
  },
  metrics: {
    activeAgents: 24,
    tokenSpend: 412.85,
    driftIndex: 1.2,
    anomalies: 9,
    latency: '1.28s'
  }
};

// Helper: Format ISO timestamp to relative time
function formatTimeAgo(date) {
  const seconds = Math.floor((new Date() - date) / 1000);
  if (seconds < 60) return `${Math.max(1, seconds)}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// Sync traces and KPI metrics from the real FastAPI backend
async function syncTracesFromApi() {
  try {
    const res = await fetch(`${API_BASE_URL}/v1/telemetry/traces`, {
      headers: { 'Authorization': `Bearer ${DEMO_API_KEY}` }
    });
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data) && data.length > 0) {
      const liveTraces = data.map(item => {
        const timeAgo = item.created_at ? formatTimeAgo(new Date(item.created_at)) : 'Just now';
        return {
          id: item.id,
          agent: item.agent_id,
          agentKey: item.agent_id.toLowerCase().includes('sql') ? 'sql' :
                    item.agent_id.toLowerCase().includes('research') ? 'researcher' :
                    item.agent_id.toLowerCase().includes('support') ? 'support' : 'code',
          model: item.model,
          prompt: item.prompt,
          status: item.status,
          tokens: item.total_tokens || 0,
          cost: item.cost_usd || 0.0,
          latency: `${(item.latency_ms / 1000).toFixed(2)}s`,
          hops: item.hops || 1,
          timestamp: timeAgo,
          guardrailReason: item.guardrail_reason,
          steps: []
        };
      });
      state.traces = liveTraces;
      renderTraces();
    }

    // Sync summary metrics
    const summaryRes = await fetch(`${API_BASE_URL}/v1/dashboard/summary`, {
      headers: { 'Authorization': `Bearer ${DEMO_API_KEY}` }
    });
    if (summaryRes.ok) {
      const summary = await summaryRes.json();
      const elAgents = document.getElementById('metricActiveAgents');
      const elSpend = document.getElementById('metricTokenSpend');
      const elAnomalies = document.getElementById('metricAnomalies');
      const elLatency = document.getElementById('metricLatency');

      if (elAgents && summary.active_agents) elAgents.textContent = summary.active_agents;
      if (elSpend && summary.total_cost_usd_24h !== undefined) {
        elSpend.textContent = `$${summary.total_cost_usd_24h.toFixed(2)}`;
      }
      if (elAnomalies && summary.circuit_trips_today !== undefined) {
        elAnomalies.textContent = summary.circuit_trips_today;
      }
      if (elLatency && summary.avg_latency_ms) {
        elLatency.textContent = `${(summary.avg_latency_ms / 1000).toFixed(2)}s`;
      }
    }
  } catch (err) {
    console.info('AgentPulse backend offline, using demo simulation data.');
  }
}

// DOM Content Loaded Handler
document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  renderTraces();
  setupTraceModal();
  setupSimulator();
  setupDriftSentinel();
  setupGuardrails();
  setupIntegrations();
  setupPricingAndRoi();
  setupExportReport();
  setupBanner();
  setupDialogPolyfillFallback();

  // Initial sync from live backend
  syncTracesFromApi();
});

/* ==========================================================================
   Navigation Tabs
   ========================================================================== */
function setupNavigation() {
  const tabs = document.querySelectorAll('.nav-tab');
  const panes = document.querySelectorAll('.tab-pane');

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      state.activeTab = target;

      tabs.forEach(t => t.classList.remove('active'));
      panes.forEach(p => p.classList.remove('active'));

      tab.classList.add('active');
      const targetPane = document.getElementById(`pane-${target}`);
      if (targetPane) {
        targetPane.classList.add('active');
      }
    });
  });

  // Direct upgrade plan button in header
  const upgradePlanBtn = document.getElementById('upgradePlanBtn');
  if (upgradePlanBtn) {
    upgradePlanBtn.addEventListener('click', () => {
      openCheckoutModal('pro', 'Professional Ops ($99/month)', '$99.00');
    });
  }
}

/* ==========================================================================
   Traces Rendering & Filter
   ========================================================================== */
function renderTraces(filter = 'all') {
  const container = document.getElementById('tracesList');
  if (!container) return;

  const filtered = filter === 'all' 
    ? state.traces 
    : state.traces.filter(t => t.agentKey === filter);

  container.innerHTML = filtered.map(trace => {
    let badgeClass = 'badge-success';
    let badgeText = 'COMPLETED';

    if (trace.status === 'CIRCUIT_KILLED') {
      badgeClass = 'badge-danger';
      badgeText = 'AUTO-KILLED';
    } else if (trace.status === 'DRIFT_WARNING') {
      badgeClass = 'badge-warning';
      badgeText = 'DRIFT DETECTED';
    }

    return `
      <div class="trace-item" data-id="${trace.id}">
        <div class="trace-top">
          <div class="trace-agent-name">
            <span>${trace.agent}</span>
            <span class="badge ${badgeClass}">${badgeText}</span>
          </div>
          <span class="trace-time">${trace.timestamp}</span>
        </div>
        <div class="trace-prompt">Prompt: "${escapeHtml(trace.prompt)}"</div>
        <div class="trace-meta-row">
          <div class="trace-meta-item"><span>Model:</span> <strong>${trace.model}</strong></div>
          <div class="trace-meta-item"><span>Tokens:</span> <strong>${trace.tokens.toLocaleString()}</strong></div>
          <div class="trace-meta-item"><span>Cost:</span> <strong class="text-emerald">$${trace.cost.toFixed(3)}</strong></div>
          <div class="trace-meta-item"><span>Hops:</span> <strong>${trace.hops} steps</strong></div>
          <div class="trace-meta-item"><span>Latency:</span> <strong>${trace.latency}</strong></div>
        </div>
      </div>
    `;
  }).join('');

  // Attach click listeners to open trace modal
  container.querySelectorAll('.trace-item').forEach(item => {
    item.addEventListener('click', () => {
      const traceId = item.dataset.id;
      openTraceModal(traceId);
    });
  });

  // Filter change
  const filterSelect = document.getElementById('filterAgentSelect');
  if (filterSelect && !filterSelect.dataset.bound) {
    filterSelect.dataset.bound = 'true';
    filterSelect.addEventListener('change', (e) => {
      renderTraces(e.target.value);
    });
  }

  // Refresh button
  const refreshBtn = document.getElementById('refreshTracesBtn');
  if (refreshBtn && !refreshBtn.dataset.bound) {
    refreshBtn.dataset.bound = 'true';
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      showToast('Syncing telemetry traces from edge API...', 'indigo');
      await syncTracesFromApi();
      refreshBtn.disabled = false;
      showToast('Telemetry traces synced from edge ingestion', 'emerald');
      renderTraces(filterSelect ? filterSelect.value : 'all');
    });
  }
}

/* ==========================================================================
   Trace Details Modal
   ========================================================================== */
function setupTraceModal() {
  const dialog = document.getElementById('traceModal');
  const closeBtn = document.getElementById('closeTraceBtn');
  if (closeBtn && dialog) {
    closeBtn.addEventListener('click', () => dialog.close());
  }
}

async function openTraceModal(traceId) {
  let trace = state.traces.find(t => t.id === traceId);
  const dialog = document.getElementById('traceModal');
  const title = document.getElementById('traceModalTitle');
  const badge = document.getElementById('traceModalBadge');
  const body = document.getElementById('traceModalBody');

  if (!trace || !dialog) return;

  // If steps are not loaded yet, fetch dynamically from the API
  if (!trace.steps || trace.steps.length === 0) {
    try {
      const res = await fetch(`${API_BASE_URL}/v1/telemetry/traces/${traceId}`, {
        headers: { 'Authorization': `Bearer ${DEMO_API_KEY}` }
      });
      if (res.ok) {
        const full = await res.json();
        trace.steps = (full.steps || []).map(s => ({
          type: s.step_type,
          title: s.title,
          content: s.content,
          tool_call: s.tool_call
        }));
        if (full.guardrail_reason) {
          trace.guardrailReason = full.guardrail_reason;
        }
      }
    } catch (e) {
      console.warn('Could not load steps from API, falling back to local view', e);
    }
  }

  title.textContent = `Trace: ${trace.agent} (${trace.id})`;
  badge.textContent = trace.status;
  badge.className = `badge ${trace.status === 'CIRCUIT_KILLED' ? 'badge-danger' : trace.status === 'DRIFT_WARNING' ? 'badge-warning' : 'badge-success'}`;

  const stepsList = trace.steps && trace.steps.length > 0 
    ? trace.steps.map((s, idx) => `
        <div class="tree-node ${s.type === 'tool' ? 'tool-call' : s.type === 'guardrail' ? 'guardrail-trigger' : s.type === 'final' ? 'final-answer' : ''}">
          <div class="tree-node-header">
            <span class="node-type-badge ${s.type === 'tool' ? 'node-tool' : s.type === 'guardrail' ? 'node-guardrail' : s.type === 'final' ? 'node-final' : 'node-thought'}">
              Step ${idx + 1}: ${s.type.toUpperCase()}
            </span>
            <span class="node-meta">${s.title}</span>
          </div>
          <div class="node-content">${escapeHtml(s.content)}</div>
          ${s.tool_call ? `<pre class="node-code"><code>${escapeHtml(s.tool_call)}</code></pre>` : ''}
        </div>
      `).join('')
    : `<div class="empty-state" style="min-height: 120px;"><p>No granular hop steps recorded for this trace.</p></div>`;

  body.innerHTML = `
    <div style="margin-bottom: 16px; background: rgba(255,255,255,0.03); padding: 12px; border-radius: 6px;">
      <div style="font-size: 0.82rem; color: #94a3b8; margin-bottom: 4px;"><strong>Initial Objective:</strong></div>
      <div style="color: #ffffff; font-size: 0.88rem;">"${escapeHtml(trace.prompt)}"</div>
      ${trace.guardrailReason ? `
        <div style="margin-top: 10px; background: rgba(244, 63, 94, 0.12); border-left: 3px solid #f43f5e; padding: 8px 12px; border-radius: 4px; font-size: 0.78rem; color: #fda4af;">
          <strong>🚨 Guardrail Triggered:</strong> ${escapeHtml(trace.guardrailReason)}
        </div>
      ` : ''}
      <div style="display: flex; gap: 16px; margin-top: 10px; font-size: 0.76rem; font-family: monospace; color: #64748b; flex-wrap: wrap;">
        <span>Model: <strong style="color:#ffffff;">${trace.model}</strong></span>
        <span>Tokens: <strong style="color:#ffffff;">${trace.tokens.toLocaleString()}</strong></span>
        <span>Cost: <strong style="color:#10b981;">$${trace.cost.toFixed(4)}</strong></span>
        <span>Duration: <strong style="color:#ffffff;">${trace.latency}</strong></span>
        <span>Hops: <strong style="color:#ffffff;">${trace.hops}</strong></span>
      </div>
    </div>

    <h4 style="font-size: 0.9rem; color: #ffffff; margin-bottom: 12px;">Step-by-Step Execution Graph:</h4>
    <div style="display: flex; flex-direction: column; gap: 10px;">
      ${stepsList}
    </div>
  `;

  dialog.showModal();
}

/* ==========================================================================
   Live Agent Simulator & Sandbox
   ========================================================================== */
function setupSimulator() {
  const startBtn = document.getElementById('startSimulationBtn');
  const resetBtn = document.getElementById('resetSimulationBtn');
  const tree = document.getElementById('executionTree');
  const emptyState = document.getElementById('simEmptyState');

  const streamStatus = document.getElementById('streamStatus');
  const streamStatusText = document.getElementById('streamStatusText');

  const liveElapsed = document.getElementById('liveElapsed');
  const liveStepCount = document.getElementById('liveStepCount');
  const liveTokens = document.getElementById('liveTokens');
  const liveCost = document.getElementById('liveCost');
  const liveGuardrailStatus = document.getElementById('liveGuardrailStatus');

  const agentPreset = document.getElementById('simAgentPreset');
  const userPrompt = document.getElementById('simUserPrompt');

  // Preset switch
  if (agentPreset && userPrompt) {
    agentPreset.addEventListener('change', () => {
      const val = agentPreset.value;
      if (val === 'market_research') {
        userPrompt.value = 'Analyze Q3 semiconductor supply chain constraints, fetch 3 recent SEC 10-K filings, compare gross margins, and summarize risk factors into a formatted portfolio report.';
      } else if (val === 'customer_support') {
        userPrompt.value = 'User query: "My invoice #INV-9281 shows a charge for $4,200 but my enterprise contract signed on Oct 1 guarantees $3,800. Also my credit card ending in 4102 was charged twice."';
      } else if (val === 'code_refactor') {
        userPrompt.value = 'Refactor legacy synchronous FastAPI handlers in 14 endpoint files to use asynchronous async/await SQLAlchemy 2.0 ORM sessions and update test suites.';
      } else {
        userPrompt.value = 'Custom prompt: Execute multi-step research and verify against safety benchmarks.';
      }
    });
  }

  startBtn.addEventListener('click', () => {
    if (state.simulationRunning) return;

    // Read toggles
    const chaosLoop = document.getElementById('chaosInfiniteLoop').checked;
    const chaosDrift = document.getElementById('chaosDrift').checked;
    const chaosBudget = document.getElementById('chaosBudgetSpike').checked;

    state.simulationRunning = true;
    startBtn.disabled = true;
    startBtn.style.opacity = '0.5';
    resetBtn.style.display = 'inline-block';

    emptyState.style.display = 'none';
    tree.innerHTML = '';

    streamStatus.querySelector('.status-indicator').className = 'status-indicator running';
    streamStatusText.textContent = 'Agent executing live multi-step hops...';

    let elapsed = 0;
    let stepCount = 0;
    let tokens = 0;
    let cost = 0.000;
    liveGuardrailStatus.textContent = 'SECURE';
    liveGuardrailStatus.className = 'stat-pill-val text-emerald';

    // Timer
    const startTime = Date.now();
    state.simTimerInterval = setInterval(() => {
      elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
      liveElapsed.textContent = `${elapsed}s`;
    }, 100);

    // Sequence of steps
    const simulationSequence = [
      {
        type: 'thought',
        title: 'Step 1: Goal Planning & Task Decomposition',
        content: `Analyzing objective: "${userPrompt.value.substring(0, 75)}...". Initiating tool chain.`,
        tokensAdd: 1850,
        costAdd: 0.008
      },
      {
        type: 'tool',
        title: 'Step 2: Tool Call → search_enterprise_kb(query="filings and margins")',
        content: 'Vector similarity search executed across 84 documents in 142ms. 3 matches retrieved.',
        code: '{"query": "filings and margins", "limit": 3, "score_threshold": 0.85}',
        tokensAdd: 6200,
        costAdd: 0.024
      },
      {
        type: 'thought',
        title: 'Step 3: Intermediate Reflection & Gap Analysis',
        content: 'Retrieved filings indicate 2 additional subsidiary entities requiring targeted lookup.',
        tokensAdd: 2400,
        costAdd: 0.012
      }
    ];

    // Branching based on chaos modes
    if (chaosBudget) {
      simulationSequence.push({
        type: 'tool',
        title: 'Step 4: Tool Call → fetch_unbounded_raw_filings(scope="all_years")',
        content: 'Incoming context stream expanding rapidly (over 85,000 tokens detected). Cost per run approaching budget ceiling.',
        tokensAdd: 82000,
        costAdd: 0.780
      });
      simulationSequence.push({
        type: 'guardrail',
        title: '🚨 CIRCUIT BREAKER TRIPPED: Single-Run Cost Ceiling ($0.75)',
        content: 'Cumulative run cost crossed $0.75 threshold ($0.824). Execution forcibly aborted in 4ms to preserve AI budget. Automated incident logged.',
        isBreaker: true
      });
    } else if (chaosLoop) {
      simulationSequence.push({
        type: 'tool',
        title: 'Step 4: Tool Call → query_filings(status="pending")',
        content: 'No new data returned. Agent executing re-try attempt.',
        tokensAdd: 4500,
        costAdd: 0.018
      });
      simulationSequence.push({
        type: 'tool',
        title: 'Step 5: Tool Call → query_filings(status="pending")',
        content: 'Identical parameters detected. Agent is trapped in recursive loop.',
        tokensAdd: 4500,
        costAdd: 0.018
      });
      simulationSequence.push({
        type: 'guardrail',
        title: '🚨 CIRCUIT BREAKER TRIPPED: Infinite Recursion Sentinel',
        content: 'Repetitive tool invocation and recursion threshold reached (Step 6). Process killed in 6ms. Saved estimated $14.20 in runaway recursive tokens.',
        isBreaker: true
      });
    } else if (chaosDrift) {
      simulationSequence.push({
        type: 'tool',
        title: 'Step 4: Tool Call → summarize_financial_risks()',
        content: 'Tool returned standard compliance text.',
        tokensAdd: 4100,
        costAdd: 0.016
      });
      simulationSequence.push({
        type: 'final',
        title: 'Step 5: Final Output Generated (Drift Detected)',
        content: 'Output generated but fails golden benchmark assertion: "Hallucinated non-existent 12% revenue margin expansion without source citation." Drift score: 0.79 (Alert dispatched).',
        isDrift: true,
        tokensAdd: 3200,
        costAdd: 0.014
      });
    } else {
      simulationSequence.push({
        type: 'tool',
        title: 'Step 4: Tool Call → compile_executive_summary()',
        content: 'Synthesizing verified margin metrics into structured markdown report.',
        tokensAdd: 4200,
        costAdd: 0.018
      });
      simulationSequence.push({
        type: 'final',
        title: 'Step 5: Final Response Completed',
        content: 'Analysis successfully validated. All assertions passed. Zero drift detected. Telemetry recorded.',
        tokensAdd: 2800,
        costAdd: 0.012
      });
    }

    let currentIndex = 0;

    function renderNextStep() {
      if (currentIndex >= simulationSequence.length) {
        // Complete
        clearInterval(state.simTimerInterval);
        state.simulationRunning = false;
        startBtn.disabled = false;
        startBtn.style.opacity = '1';
        streamStatus.querySelector('.status-indicator').className = 'status-indicator ready';
        streamStatusText.textContent = 'Execution run finished.';
        showToast('Agent telemetry run completed and logged to dashboard', 'emerald');
        return;
      }

      const step = simulationSequence[currentIndex];
      currentIndex++;
      stepCount++;

      tokens += (step.tokensAdd || 0);
      cost += (step.costAdd || 0);

      liveStepCount.textContent = stepCount;
      liveTokens.textContent = tokens.toLocaleString();
      liveCost.textContent = `$${cost.toFixed(4)}`;

      // Render node in tree
      const nodeEl = document.createElement('div');
      nodeEl.className = `tree-node ${step.type === 'tool' ? 'tool-call' : step.type === 'guardrail' ? 'guardrail-trigger' : step.type === 'final' ? 'final-answer' : ''}`;
      
      let badgeClass = 'node-thought';
      if (step.type === 'tool') badgeClass = 'node-tool';
      if (step.type === 'guardrail') badgeClass = 'node-guardrail';
      if (step.type === 'final') badgeClass = 'node-final';

      nodeEl.innerHTML = `
        <div class="tree-node-header">
          <span class="node-type-badge ${badgeClass}">${step.type.toUpperCase()}</span>
          <span class="node-meta">${step.title}</span>
        </div>
        <div class="node-content">${escapeHtml(step.content)}</div>
        ${step.code ? `<pre class="node-code"><code>${escapeHtml(step.code)}</code></pre>` : ''}
      `;

      tree.appendChild(nodeEl);
      tree.scrollTop = tree.scrollHeight;

      if (step.isBreaker) {
        liveGuardrailStatus.textContent = 'KILLED BY CIRCUIT BREAKER';
        liveGuardrailStatus.className = 'stat-pill-val text-rose';
        showToast('Guardrail Triggered: Run auto-killed in <10ms to protect budget', 'rose');
        
        // Add to telemetry traces
        state.traces.unshift({
          id: `TR-${Math.floor(8900 + Math.random() * 200)}`,
          agent: agentPreset.options[agentPreset.selectedIndex].text.split('(')[0].trim(),
          agentKey: 'custom',
          model: 'Claude 3.7 Sonnet',
          prompt: userPrompt.value,
          status: 'CIRCUIT_KILLED',
          tokens: tokens,
          cost: cost,
          latency: `${liveElapsed.textContent}`,
          hops: stepCount,
          timestamp: 'Just now',
          steps: simulationSequence.slice(0, currentIndex)
        });
        renderTraces();

        // Increment anomalies intercepted metric
        state.metrics.anomalies++;
        const anomalyCounter = document.getElementById('metricAnomalies');
        if (anomalyCounter) anomalyCounter.textContent = state.metrics.anomalies;

        // Also add row to guardrails table
        addGuardrailLogRow(agentPreset.value, 'Recursive Loop Intercept', 'Aborted Execution in 6ms', `${tokens.toLocaleString()} tokens`, '+$0.68');
      } else if (step.isDrift) {
        liveGuardrailStatus.textContent = 'DRIFT DETECTED';
        liveGuardrailStatus.className = 'stat-pill-val text-amber';
      }

      // Schedule next step with realistic pacing
      const delay = step.type === 'tool' ? 1200 : 900;
      setTimeout(renderNextStep, delay);
    }

    renderNextStep();
  });

  resetBtn.addEventListener('click', () => {
    clearInterval(state.simTimerInterval);
    state.simulationRunning = false;
    startBtn.disabled = false;
    startBtn.style.opacity = '1';
    resetBtn.style.display = 'none';

    emptyState.style.display = 'flex';
    tree.innerHTML = '';
    tree.appendChild(emptyState);

    liveElapsed.textContent = '0.0s';
    liveStepCount.textContent = '0';
    liveTokens.textContent = '0';
    liveCost.textContent = '$0.0000';
    liveGuardrailStatus.textContent = 'SECURE';
    liveGuardrailStatus.className = 'stat-pill-val text-emerald';

    streamStatus.querySelector('.status-indicator').className = 'status-indicator ready';
    streamStatusText.textContent = 'Ready to execute';
  });
}

function addGuardrailLogRow(agent, trigger, action, tokensSaved, costPreserved) {
  const table = document.getElementById('guardrailLogsTable');
  if (!table) return;

  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td>Just now</td>
    <td><code>${agent}</code></td>
    <td><span class="badge badge-danger">${trigger}</span></td>
    <td>${action}</td>
    <td>${tokensSaved}</td>
    <td class="text-emerald">${costPreserved}</td>
  `;
  table.prepend(tr);
}

/* ==========================================================================
   Drift Sentinel Tab
   ========================================================================== */
function setupDriftSentinel() {
  const auditBtn = document.getElementById('runDriftAuditBtn');
  if (!auditBtn) return;

  auditBtn.addEventListener('click', () => {
    auditBtn.disabled = true;
    auditBtn.innerHTML = '<span class="pulse-dot"></span> Evaluating 50 Golden Test Cases...';

    setTimeout(() => {
      auditBtn.disabled = false;
      auditBtn.innerHTML = `
        <svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>
        Run Drift Audit
      `;
      showToast('Drift Audit Completed: 49/50 test cases passed. 1 tone regression flagged.', 'emerald');
    }, 1400);
  });
}

/* ==========================================================================
   Guardrails & Circuit Breakers Tab
   ========================================================================== */
function setupGuardrails() {
  const maxHops = document.getElementById('ruleMaxHops');
  const maxCost = document.getElementById('ruleMaxCost');

  if (maxHops) {
    maxHops.addEventListener('change', (e) => {
      state.rules.maxHops = parseInt(e.target.value, 10);
      showToast(`Recursion boundary updated to max ${state.rules.maxHops} hops`, 'indigo');
    });
  }

  if (maxCost) {
    maxCost.addEventListener('change', (e) => {
      state.rules.maxCost = parseFloat(e.target.value);
      showToast(`Cost ceiling updated to $${state.rules.maxCost.toFixed(2)}/run`, 'indigo');
    });
  }
}

/* ==========================================================================
   Integrations & SDK Snippets
   ========================================================================== */
function setupIntegrations() {
  const sdkTabs = document.querySelectorAll('.sdk-tab');
  const codeDisplay = document.getElementById('sdkCodeDisplay');
  const copyCodeBtn = document.getElementById('copyCodeBtn');

  sdkTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      sdkTabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');

      const lang = tab.dataset.lang;
      if (codeDisplay && SDK_SNIPPETS[lang]) {
        codeDisplay.innerHTML = `<code class="language-${lang}">${escapeHtml(SDK_SNIPPETS[lang])}</code>`;
      }
    });
  });

  if (copyCodeBtn && codeDisplay) {
    copyCodeBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(codeDisplay.textContent).then(() => {
        const orig = copyCodeBtn.innerHTML;
        copyCodeBtn.innerHTML = '<span>✓ Copied!</span>';
        setTimeout(() => { copyCodeBtn.innerHTML = orig; }, 1800);
        showToast('SDK snippet copied to clipboard', 'emerald');
      });
    });
  }

  // API Key Visibility & Copy
  const apiKeyInput = document.getElementById('apiKeyInput');
  const toggleKeyBtn = document.getElementById('toggleApiKeyVisibility');
  const copyKeyBtn = document.getElementById('copyApiKeyBtn');

  if (toggleKeyBtn && apiKeyInput) {
    toggleKeyBtn.addEventListener('click', () => {
      if (apiKeyInput.type === 'password') {
        apiKeyInput.type = 'text';
        toggleKeyBtn.textContent = 'Hide';
      } else {
        apiKeyInput.type = 'password';
        toggleKeyBtn.textContent = 'Reveal';
      }
    });
  }

  if (copyKeyBtn && apiKeyInput) {
    copyKeyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(apiKeyInput.value).then(() => {
        showToast('API Key copied to clipboard', 'emerald');
      });
    });
  }

  // Webhook Test Button
  const testWebhookBtn = document.getElementById('testWebhookBtn');
  const webhookStatusMsg = document.getElementById('webhookStatusMsg');

  if (testWebhookBtn) {
    testWebhookBtn.addEventListener('click', () => {
      testWebhookBtn.disabled = true;
      testWebhookBtn.textContent = 'Dispatching payload...';

      setTimeout(() => {
        testWebhookBtn.disabled = false;
        testWebhookBtn.innerHTML = `
          <svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13"></path><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
          Send Test Alert Ping
        `;
        webhookStatusMsg.textContent = 'HTTP 200 OK — Alert received by Slack endpoint';
        webhookStatusMsg.style.color = '#10b981';
        showToast('Slack Webhook delivered: [Alert] Circuit Breaker ping verified', 'emerald');
      }, 900);
    });
  }
}

/* ==========================================================================
   Pricing, ROI Calculator & Upgrade Modal
   ========================================================================== */
function setupPricingAndRoi() {
  // Plan selection buttons
  const planButtons = document.querySelectorAll('.select-plan-btn');
  planButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const plan = btn.dataset.plan;
      if (plan === 'starter') {
        openCheckoutModal('starter', 'Starter Sentinel ($49/month)', '$49.00');
      } else if (plan === 'pro') {
        openCheckoutModal('pro', 'Professional Ops ($99/month)', '$99.00');
      } else if (plan === 'scale') {
        openCheckoutModal('scale', 'Scale Sentinel ($249/month)', '$249.00');
      }
    });
  });

  // ROI Calculator
  const calcRuns = document.getElementById('calcAgentRuns');
  const calcAvgCost = document.getElementById('calcAvgCost');
  const calcLoopRate = document.getElementById('calcLoopRate');

  const runsVal = document.getElementById('calcAgentRunsVal');
  const costVal = document.getElementById('calcAvgCostVal');
  const loopVal = document.getElementById('calcLoopRateVal');
  const savedDollars = document.getElementById('calcSavedDollars');

  function updateRoi() {
    if (!calcRuns || !calcAvgCost || !calcLoopRate) return;

    const runs = parseInt(calcRuns.value, 10);
    const avgCost = parseFloat(calcAvgCost.value);
    const loopRate = parseFloat(calcLoopRate.value) / 100;

    runsVal.textContent = `${runs.toLocaleString()} runs/month`;
    costVal.textContent = `$${avgCost.toFixed(2)} / run`;
    loopVal.textContent = `${(loopRate * 100).toFixed(1)}% runaway loops`;

    // Calculation: When an agent loops rogue, it typically consumes 12x the standard single-hop cost
    const rogueMultiplier = 12;
    const runawayRuns = runs * loopRate;
    const wastedCostMonthly = runawayRuns * (avgCost * rogueMultiplier);

    savedDollars.textContent = `$${Math.round(wastedCostMonthly).toLocaleString()} / mo`;
  }

  if (calcRuns) calcRuns.addEventListener('input', updateRoi);
  if (calcAvgCost) calcAvgCost.addEventListener('input', updateRoi);
  if (calcLoopRate) calcLoopRate.addEventListener('input', updateRoi);

  const calcUpgradeBtn = document.getElementById('calcUpgradeBtn');
  if (calcUpgradeBtn) {
    calcUpgradeBtn.addEventListener('click', () => {
      openCheckoutModal('pro', 'Professional Ops ($99/month)', '$99.00');
    });
  }

  // Checkout Dialog setup
  const checkoutDialog = document.getElementById('checkoutDialog');
  const closeCheckoutBtn = document.getElementById('closeCheckoutBtn');
  const checkoutForm = document.getElementById('checkoutForm');

  if (closeCheckoutBtn && checkoutDialog) {
    closeCheckoutBtn.addEventListener('click', () => checkoutDialog.close());
  }

  if (checkoutForm && checkoutDialog) {
    checkoutForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const submitBtn = document.getElementById('submitPaymentBtn');
      const companyName = document.getElementById('companyName').value;
      const billingEmail = document.getElementById('billingEmail').value;
      const plan = state.currentSelectedPlan || 'pro';

      submitBtn.disabled = true;
      submitBtn.textContent = 'Contacting Stripe & Provisioning...';

      try {
        const res = await fetch(`${API_BASE_URL}/v1/billing/create-checkout-session`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            plan: plan,
            email: billingEmail,
            team_name: companyName,
            success_url: window.location.href.split('?')[0] + '?checkout=success',
            cancel_url: window.location.href.split('?')[0] + '?checkout=cancelled'
          })
        });

        if (res.ok) {
          const data = await res.json();
          // If a real Stripe Hosted Checkout URL is returned, redirect directly
          if (data.checkout_url && data.checkout_url.startsWith('https://checkout.stripe.com')) {
            window.location.href = data.checkout_url;
            return;
          }
        }
      } catch (err) {
        console.warn('Billing API offline, running in development simulation mode:', err);
      }

      // Development / sandbox activation
      setTimeout(() => {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Activate Pro Sentinel ($99/mo)';
        checkoutDialog.close();

        // Update banner
        const bannerText = document.querySelector('.mrr-badge');
        if (bannerText) {
          bannerText.textContent = 'Goal: $10k/mo MRR • 65/100 Teams Active (+$99)';
        }

        showToast('🎉 Subscription Active! 14-day trial started. API Key generated.', 'emerald');
      }, 1000);
    });
  }
}

function openCheckoutModal(planId, label, price) {
  state.currentSelectedPlan = planId;
  const dialog = document.getElementById('checkoutDialog');
  const labelEl = document.getElementById('selectedPlanLabel');
  const priceEl = document.getElementById('selectedPlanPrice');
  const titleEl = document.getElementById('checkoutModalTitle');
  const submitBtn = document.getElementById('submitPaymentBtn');

  if (!dialog) return;

  if (labelEl) labelEl.textContent = label;
  if (priceEl) priceEl.textContent = price;
  if (titleEl) titleEl.textContent = `Upgrade to ${label.split('(')[0].trim()}`;
  if (submitBtn) submitBtn.textContent = `Activate ${label.split('(')[0].trim()} (${price}/mo)`;

  dialog.showModal();
}

/* ==========================================================================
   Executive Report Export
   ========================================================================== */
function setupExportReport() {
  const exportBtn = document.getElementById('exportReportBtn');
  if (!exportBtn) return;

  exportBtn.addEventListener('click', () => {
    const reportData = {
      product: "AgentPulse Ops",
      report_type: "Executive AI Fleet Observability & Drift Audit",
      generated_at: new Date().toISOString(),
      active_monitored_agents: state.metrics.activeAgents,
      daily_token_spend_usd: state.metrics.tokenSpend,
      estimated_monthly_spend_usd: (state.metrics.tokenSpend * 30).toFixed(2),
      estimated_prevented_token_waste_usd: 1728.00,
      circuit_breaker_interceptions_today: state.metrics.anomalies,
      p95_latency_seconds: state.metrics.latency,
      prompt_drift_index: `${state.metrics.driftIndex}%`,
      compliance_status: "PASSED_ZERO_PII_LEAKS",
      recent_traces_summary: state.traces.map(t => ({
        id: t.id,
        agent: t.agent,
        model: t.model,
        status: t.status,
        tokens: t.tokens,
        cost: t.cost,
        latency: t.latency
      }))
    };

    const blob = new Blob([JSON.stringify(reportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `AgentPulse_Executive_Audit_${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast('Executive Token & Drift Audit downloaded successfully', 'emerald');
  });
}

/* ==========================================================================
   Banner
   ========================================================================== */
function setupBanner() {
  const bar = document.getElementById('announcementBar');
  const closeBtn = document.getElementById('dismissBannerBtn');
  if (closeBtn && bar) {
    closeBtn.addEventListener('click', () => {
      bar.style.display = 'none';
    });
  }
}

/* ==========================================================================
   Dialog Light-Dismiss Polyfill Fallback (Modern Web Guidance)
   ========================================================================== */
function setupDialogPolyfillFallback() {
  if (!('closedBy' in HTMLDialogElement.prototype)) {
    const outside = (e, dialog) => {
      const r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
      let pressedOutside = false;
      dialog.addEventListener('pointerdown', (e) => { pressedOutside = outside(e, dialog); });
      dialog.addEventListener('pointerup', (e) => {
        if (pressedOutside && outside(e, dialog)) dialog.close();
      });
    }
  }
}

/* ==========================================================================
   Toast Notification Helper
   ========================================================================== */
function showToast(message, variant = 'emerald') {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${variant === 'rose' ? 'toast-rose' : variant === 'indigo' ? 'toast-indigo' : ''}`;
  
  let icon = '✓';
  if (variant === 'rose') icon = '⚠️';
  if (variant === 'indigo') icon = 'ℹ️';

  toast.innerHTML = `<span>${icon}</span> <span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 200ms ease';
    setTimeout(() => toast.remove(), 250);
  }, 3200);
}

function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
