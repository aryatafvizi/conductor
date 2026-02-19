/**
 * Conductor Dashboard — WebSocket client & UI controller
 */

const WS_URL = `ws://${window.location.host}/ws`;
const CONV_ID = 'default';
const STAGE_ORDER = [
    'planning', 'coding', 'prechecks', 'pr_created',
    'ci_monitoring', 'ci_fixing', 'greptile_review',
    'addressing_comments', 'ready_for_review', 'needs_human', 'merged',
];
const STATUS_ICONS = {
    pending: '⏳', blocked: '🚫', ready: '🟢',
    running: '🏃', done: '✅', failed: '❌', cancelled: '⛔',
};

let ws = null;
let state = { tasks: [], agents: [], workspaces: [], quota: {}, pr_lifecycles: [], logs: [], rules: [] };

// ── WebSocket ──────────────────────────────────────────────────────────────

function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        const el = document.getElementById('conn-status');
        el.textContent = 'Connected';
        el.className = 'connection-status connected';
    };

    ws.onclose = () => {
        const el = document.getElementById('conn-status');
        el.textContent = 'Disconnected — reconnecting…';
        el.className = 'connection-status error';
        setTimeout(connect, 3000);
    };

    ws.onerror = () => {
        ws.close();
    };

    ws.onmessage = (evt) => {
        const msg = JSON.parse(evt.data);
        handleMessage(msg);
    };
}

function handleMessage(msg) {
    switch (msg.type) {
        case 'init':
            state = msg.data;
            renderAll();
            break;
        case 'task_created':
        case 'task_updated':
            upsertItem(state.tasks, msg.data, 'id');
            renderTasks();
            break;
        case 'task_started':
            upsertItem(state.tasks, msg.data.task, 'id');
            upsertItem(state.agents, msg.data.agent, 'id');
            renderTasks();
            renderAgents();
            break;
        case 'agent_status':
            upsertItem(state.agents, msg.data, 'id');
            renderAgents();
            break;
        case 'agent_output':
            appendAgentOutput(msg.data.agent_id, msg.data.line);
            break;
        case 'pr_lifecycle':
            upsertItem(state.pr_lifecycles, msg.data, 'id');
            renderPRs();
            break;
        case 'chat_response':
            addChatMessage('assistant', msg.data.response);
            break;
        case 'plan_approved':
            addChatMessage('assistant', '✅ Plan approved! Task created & pipeline started.');
            renderTasks();
            renderPRs();
            break;
        case 'github_event':
            addLogEntry({ ts: new Date().toISOString(), level: 'INFO', component: 'github', event: `${msg.data.type} on PR #${msg.data.pr_number}` });
            break;
        case 'rule_triggered':
            addLogEntry({ ts: new Date().toISOString(), level: 'INFO', component: 'rules', event: `Rule "${msg.data.rule}" triggered` });
            break;
    }
}

function upsertItem(arr, item, key) {
    const idx = arr.findIndex(x => x[key] === item[key]);
    if (idx >= 0) arr[idx] = item;
    else arr.push(item);
}

// ── Render Functions ───────────────────────────────────────────────────────

function renderAll() {
    renderQuota();
    renderTasks();
    renderAgents();
    renderWorkspaces();
    renderPRs();
    renderLogs();
}

function renderQuota() {
    const q = state.quota;
    if (!q) return;

    setText('q-agents-text', `${q.agent_requests_used || 0}/${q.agent_requests_limit || 200}`);
    setText('q-prompts-text', `${q.prompts_used || 0}/${q.prompts_limit || 1500}`);
    setText('q-concurrent-text', `${q.concurrent_agents || 0}/${q.max_concurrent || 3}`);

    setBar('q-agents-bar', q.agent_pct || 0);
    setBar('q-prompts-bar', q.prompt_pct || 0);
    setBar('q-concurrent-bar', q.max_concurrent ? (q.concurrent_agents / q.max_concurrent * 100) : 0);

    const pausedEl = document.getElementById('q-paused');
    if (q.is_paused) {
        pausedEl.textContent = 'Paused 🔴';
        pausedEl.className = 'badge badge-error';
    } else {
        pausedEl.textContent = 'Active 🟢';
        pausedEl.className = 'badge badge-ok';
    }
    setText('q-reset', `Reset in: ${q.time_until_reset || '—'}`);
}

function renderTasks() {
    const el = document.getElementById('tasks-list');
    const count = document.getElementById('task-count');
    const tasks = state.tasks || [];
    count.textContent = tasks.length;

    if (!tasks.length) {
        el.innerHTML = '<div class="empty-state">No tasks yet</div>';
        return;
    }

    el.innerHTML = tasks.map(t => `
        <div class="list-item">
            <div>
                <div class="item-title">${STATUS_ICONS[t.status] || '❓'} #${t.id} ${esc(t.title)}</div>
                <div class="item-meta">${t.priority} · ${t.workspace || '—'} · ${t.branch || '—'}</div>
            </div>
            <div class="item-actions">
                ${t.status === 'running' ? `<button class="btn btn-danger btn-sm" onclick="cancelTask(${t.id})">Cancel</button>` : ''}
            </div>
        </div>
    `).join('');
}

function renderAgents() {
    const el = document.getElementById('agents-list');
    const count = document.getElementById('agent-count');
    const agents = (state.agents || []).filter(a => ['starting', 'running'].includes(a.status));
    count.textContent = agents.length;

    if (!agents.length) {
        el.innerHTML = '<div class="empty-state">No active agents</div>';
        return;
    }

    el.innerHTML = agents.map(a => `
        <div class="list-item">
            <div>
                <div class="item-title">
                    <span class="status-icon status-running pulsing"></span>
                    ${esc(a.id)}
                </div>
                <div class="item-meta">Task #${a.task_id} · ${a.workspace} · ${a.request_count} requests</div>
            </div>
            <div class="item-actions">
                <button class="btn btn-danger btn-sm" onclick="killAgent('${a.id}')">Kill</button>
            </div>
        </div>
    `).join('');
}

function renderWorkspaces() {
    const el = document.getElementById('workspaces-list');
    const workspaces = state.workspaces || [];

    if (!workspaces.length) {
        el.innerHTML = '<div class="empty-state">No workspaces found</div>';
        return;
    }

    el.innerHTML = workspaces.map(w => `
        <div class="list-item">
            <div>
                <div class="item-title">
                    <span class="status-icon status-${w.status}"></span>
                    ${esc(w.name)}
                </div>
                <div class="item-meta">${w.branch || '—'} · ${w.is_dirty ? '⚠️ dirty' : 'clean'}</div>
            </div>
            <div class="item-actions">
                ${w.snapshot_sha ? `<button class="btn btn-sm" onclick="rollback('${w.name}')">↩️ Rollback</button>` : ''}
            </div>
        </div>
    `).join('');
}

function renderPRs() {
    const el = document.getElementById('pr-list');
    const prs = state.pr_lifecycles || [];

    if (!prs.length) {
        el.innerHTML = '<div class="empty-state">No PR lifecycles</div>';
        return;
    }

    el.innerHTML = prs.map(prl => {
        const stageIdx = STAGE_ORDER.indexOf(prl.stage);
        const dots = STAGE_ORDER.slice(0, 8).map((s, i) => {
            let cls = 'pr-stage-dot';
            if (i < stageIdx) cls += ' done';
            else if (i === stageIdx) cls += ' active';
            return `<div class="${cls}" title="${s}"></div>`;
        }).join('');

        const stageBadge = getStageBadge(prl.stage);

        return `
            <div class="pr-item">
                <div class="pr-header">
                    <span class="pr-title">${prl.pr_number ? `#${prl.pr_number}` : '—'} ${esc(prl.title)}</span>
                    ${stageBadge}
                </div>
                <div class="pr-stages">${dots}</div>
                <div class="item-meta" style="margin-top:6px">
                    Iteration ${prl.iteration}/${prl.max_iterations} ·
                    Greptile: ${prl.greptile_comments_resolved}/${prl.greptile_comments_total} resolved
                </div>
            </div>
        `;
    }).join('');
}

function renderLogs() {
    const el = document.getElementById('log-entries');
    const logs = state.logs || [];
    el.innerHTML = logs.map(formatLogLine).join('');
    el.scrollTop = el.scrollHeight;
}

// ── Chat ───────────────────────────────────────────────────────────────────

function addChatMessage(role, text) {
    const el = document.getElementById('chat-messages');
    const msg = document.createElement('div');
    msg.className = `chat-msg ${role}`;
    msg.textContent = text;
    el.appendChild(msg);
    el.scrollTop = el.scrollHeight;

    // Enable approve button if assistant might have a plan
    if (role === 'assistant' && text.includes('{')) {
        document.getElementById('btn-chat-approve').disabled = false;
    }
}

document.getElementById('btn-chat-send').addEventListener('click', () => {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    addChatMessage('user', text);
    input.value = '';

    ws.send(JSON.stringify({
        action: 'chat',
        conversation_id: CONV_ID,
        text: text,
    }));
});

document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        document.getElementById('btn-chat-send').click();
    }
});

document.getElementById('btn-chat-approve').addEventListener('click', () => {
    fetch('/api/chat/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: CONV_ID }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addChatMessage('assistant', `⚠️ ${data.error}`);
            } else {
                addChatMessage('assistant', `✅ Plan approved! Task #${data.task_id} created.`);
                document.getElementById('btn-chat-approve').disabled = true;
            }
        });
});

// ── Controls ───────────────────────────────────────────────────────────────

document.getElementById('btn-kill-all').addEventListener('click', () => {
    if (!confirm('🔴 STOP ALL — Kill every running agent?')) return;
    ws.send(JSON.stringify({ action: 'kill_all' }));
});

function killAgent(agentId) {
    ws.send(JSON.stringify({ action: 'kill_agent', agent_id: agentId }));
}

function rollback(workspace) {
    if (!confirm(`↩️ Rollback ${workspace} to pre-task snapshot?`)) return;
    ws.send(JSON.stringify({ action: 'rollback', workspace: workspace }));
}

function cancelTask(taskId) {
    fetch(`/api/tasks/${taskId}/cancel`, { method: 'POST' });
}

// ── Log Filtering ──────────────────────────────────────────────────────────

document.getElementById('log-level-filter').addEventListener('change', fetchFilteredLogs);
document.getElementById('log-search').addEventListener('input', debounce(fetchFilteredLogs, 300));

function fetchFilteredLogs() {
    const level = document.getElementById('log-level-filter').value;
    const search = document.getElementById('log-search').value;
    const params = new URLSearchParams();
    if (level) params.set('level', level);
    if (search) params.set('search', search);

    fetch(`/api/logs?${params}`)
        .then(r => r.json())
        .then(logs => {
            state.logs = logs;
            renderLogs();
        });
}

// ── Agent Output ───────────────────────────────────────────────────────────

function appendAgentOutput(agentId, line) {
    // Also add to logs
    addLogEntry({
        ts: new Date().toISOString(),
        level: 'DEBUG',
        component: `agent:${agentId.slice(0, 12)}`,
        event: line.slice(0, 200),
    });
}

function addLogEntry(entry) {
    state.logs = state.logs || [];
    state.logs.push(entry);
    if (state.logs.length > 200) state.logs = state.logs.slice(-150);

    const el = document.getElementById('log-entries');
    el.innerHTML += formatLogLine(entry);
    el.scrollTop = el.scrollHeight;
}

// ── Helpers ────────────────────────────────────────────────────────────────

function formatLogLine(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const ts = (entry.ts || '').slice(11, 19);
    const level = entry.level || 'INFO';
    const comp = entry.component || '';
    const event = entry.event || '';
    return `<div class="log-line">
        <span class="log-ts">${ts}</span>
        <span class="log-level-${level}">[${level}]</span>
        <span class="log-component">${comp}</span>
        <span class="log-event">${esc(event)}</span>
    </div>`;
}

function getStageBadge(stage) {
    const map = {
        planning: ['badge-info', '📝 Planning'],
        coding: ['badge-info', '💻 Coding'],
        prechecks: ['badge-info', '🔍 Prechecks'],
        pr_created: ['badge-purple', '📦 PR Created'],
        ci_monitoring: ['badge-info', '👀 CI Monitoring'],
        ci_fixing: ['badge-warn', '🔧 CI Fixing'],
        greptile_review: ['badge-purple', '🔍 Greptile Review'],
        addressing_comments: ['badge-warn', '💬 Addressing Comments'],
        ready_for_review: ['badge-ok', '✅ Ready'],
        needs_human: ['badge-error', '⚠️ Needs Human'],
        merged: ['badge-ok', '🎉 Merged'],
    };
    const [cls, label] = map[stage] || ['', stage];
    return `<span class="badge ${cls}">${label}</span>`;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function setBar(id, pct) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.width = `${Math.min(100, pct)}%`;
    if (pct >= 80) el.classList.add('warn');
    else el.classList.remove('warn');
}

function esc(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function debounce(fn, ms) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}

// ── Auto-refresh ───────────────────────────────────────────────────────────

setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'refresh' }));
    }
}, 15000);

// ── Init ───────────────────────────────────────────────────────────────────

connect();
