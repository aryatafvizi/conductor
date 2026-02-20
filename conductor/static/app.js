/**
 * Conductor Dashboard — WebSocket client & UI controller
 */

const WS_URL = `ws://${window.location.host}/ws`;
const STAGE_ORDER = [
    'planning', 'coding', 'prechecks', 'pr_created',
    'ci_monitoring', 'ci_fixing', 'greptile_review',
    'addressing_comments', 'ready_for_review', 'needs_human', 'merged',
];
const STATUS_ICONS = {
    pending: '⏳', blocked: '🚫', ready: '🟢',
    running: '🏃', done: '✅', failed: '❌', cancelled: '⛔',
};
const TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled']);

let ws = null;
let state = { tasks: [], agents: [], workspaces: [], quota: {}, pr_lifecycles: [], logs: [], rules: [] };
let selectedWorkspace = null;
let modelConfig = { planning: '', coding: '', testing: '', available: [] };
let showAllTasks = true;
const agentActivity = {};  // { agentId: { action: '', detail: '', ts: Date } }

// ── Per-workspace chat history ─────────────────────────────────────────────
const chatHistories = {};   // { workspaceName: htmlString }
let chatWaiting = false;    // true while waiting for a response

function getConvId() {
    return selectedWorkspace ? `ws-${selectedWorkspace.name}` : 'default';
}

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
        case 'task_deleted':
            state.tasks = (state.tasks || []).filter(t => t.id !== msg.data.task_id);
            renderTasks();
            break;
        case 'prl_deleted':
            state.pr_lifecycles = (state.pr_lifecycles || []).filter(p => p.id !== msg.data.prl_id);
            renderPRs();
            break;
        case 'task':
            upsertItem(state.tasks, msg.data, 'id');
            renderTasks();
            echoTaskEvent(msg.data);
            break;
        case 'workspaces':
            state.workspaces = msg.data;
            renderWorkspaces();
            break;
        case 'agent_status':
            upsertItem(state.agents, msg.data, 'id');
            renderAgents();
            echoAgentEvent(msg.data);
            break;
        case 'agent_output':
            appendAgentOutput(msg.data.agent_id, msg.data.line);
            break;
        case 'agent_failure':
            handleAgentFailure(msg.data);
            break;
        case 'pr_lifecycle':
            upsertItem(state.pr_lifecycles, msg.data, 'id');
            renderPRs();
            echoPREvent(msg.data);
            break;
        case 'chat_response':
            removeChatThinking();
            addChatMessage('assistant', msg.data.response);
            chatWaiting = false;
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
        case 'models_updated':
            modelConfig = msg.data;
            populateModelDropdowns();
            break;
        case 'diff_stats':
            updateDiffStats(msg.data);
            break;
    }
}

function upsertItem(arr, item, key) {
    const idx = arr.findIndex(x => x[key] === item[key]);
    if (idx >= 0) arr[idx] = item;
    else arr.push(item);
}

// ── Render Functions ───────────────────────────────────────────────────────

let chatInitialized = false;

function renderAll() {
    renderQuota();
    renderTasks();
    renderAgents();
    renderWorkspaces();
    renderPRs();
    renderLogs();
    if (!chatInitialized) {
        chatInitialized = true;
        restoreChatHistory();
    }
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
    let tasks = [...(state.tasks || [])].reverse();
    count.textContent = tasks.length;

    if (!tasks.length) {
        el.innerHTML = '<div class="empty-state">No tasks yet</div>';
        return;
    }

    // Filter if showAllTasks is false → hide terminal tasks
    const visible = showAllTasks ? tasks : tasks.filter(t => !TERMINAL_STATUSES.has(t.status));

    if (!visible.length) {
        el.innerHTML = '<div class="empty-state">All tasks completed — <a href="#" onclick="toggleTaskFilter(); return false;">show all</a></div>';
        return;
    }

    el.innerHTML = visible.map(t => {
        const dimmed = TERMINAL_STATUSES.has(t.status) ? ' dimmed' : '';
        const deleteBtn = t.status !== 'running'
            ? `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); deleteTask(${t.id})" title="Remove">🗑️</button>`
            : '';
        const cancelBtn = t.status === 'running'
            ? `<button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); cancelTask(${t.id})">Cancel</button>`
            : '';
        const wsClick = t.workspace ? `onclick="navigateToWorkspace('${esc(t.workspace)}')"` : '';
        return `
        <div class="list-item${dimmed}" ${wsClick} style="${t.workspace ? 'cursor:pointer' : ''}">
            <div>
                <div class="item-title">${STATUS_ICONS[t.status] || '❓'} #${t.id} ${esc(t.title)}</div>
                <div class="item-meta">${t.priority} · ${t.workspace || '—'} · ${t.branch || '—'}</div>
            </div>
            <div class="item-actions">
                ${cancelBtn}${deleteBtn}
            </div>
        </div>
    `}).join('');
}

// Per-agent output buffers: { agentId: [line, line, ...] }
const agentOutputBuffers = {};

function formatElapsed(startedAt) {
    if (!startedAt) return '';
    const secs = Math.floor(Date.now() / 1000 - startedAt);
    if (secs < 0) return '0s';
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
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

    el.innerHTML = agents.map(a => {
        const elapsed = formatElapsed(a.started_at);
        const buf = agentOutputBuffers[a.id] || [];
        const lastLine = buf.length > 0 ? buf[buf.length - 1] : '';
        const shortId = a.id.slice(0, 12);
        const isExpanded = document.querySelector(`#agent-log-${CSS.escape(a.id)}[open]`) !== null;
        const activity = agentActivity[a.id];
        const activityStr = activity ? `${activity.action}${activity.detail ? ': ' + activity.detail : ''}` : 'Starting…';
        return `
        <div class="list-item agent-card" onclick="selectWorkspace('${esc(a.workspace)}')" style="cursor:pointer">
            <div style="flex:1;min-width:0">
                <div class="item-title">
                    <span class="status-icon status-running pulsing"></span>
                    ${esc(shortId)}…
                    <span class="agent-elapsed">⏱ ${elapsed}</span>
                </div>
                <div class="item-meta">Task #${a.task_id} · ${a.workspace} · ${a.request_count} reqs</div>
                <div class="agent-activity">⚡ ${esc(activityStr)}</div>
                ${lastLine ? `<div class="agent-last-output" title="${esc(lastLine)}">▸ ${esc(lastLine.slice(0, 120))}</div>` : ''}
                <details id="agent-log-${a.id}" class="agent-output-details" ${isExpanded ? 'open' : ''} onclick="event.stopPropagation()">
                    <summary>View output (${buf.length} lines)</summary>
                    <pre class="agent-output-log">${buf.slice(-50).map(l => esc(l)).join('\n')}</pre>
                </details>
            </div>
            <div class="item-actions">
                <button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); killAgent('${a.id}')">Kill</button>
            </div>
        </div>
    `}).join('');
}

function renderWorkspaces() {
    const el = document.getElementById('workspaces-list');
    const workspaces = state.workspaces || [];

    if (!workspaces.length) {
        el.innerHTML = '<div class="empty-state">No workspaces found</div>';
        return;
    }

    el.innerHTML = workspaces.map(w => {
        const sel = selectedWorkspace && selectedWorkspace.name === w.name ? ' selected' : '';
        return `
        <div class="list-item workspace-item${sel}" onclick="selectWorkspace('${esc(w.name)}')">
            <div>
                <div class="item-title">
                    <span class="status-icon status-${w.status}"></span>
                    ${esc(w.name)}
                </div>
                <div class="item-meta">${w.branch || '—'} · ${w.is_dirty ? '⚠️ dirty' : 'clean'}</div>
            </div>
            <div class="item-actions">
                ${w.snapshot_sha ? `<button class="btn btn-sm" onclick="event.stopPropagation(); rollback('${w.name}')">↩️ Rollback</button>` : ''}
                ${w.is_dirty || w.status === 'assigned' ? `<button class="btn btn-sm btn-danger" onclick="event.stopPropagation(); resetWorkspace('${esc(w.name)}')">🔄 Reset</button>` : ''}
            </div>
        </div>
    `}).join('');
}

function selectWorkspace(name) {
    const workspaces = state.workspaces || [];
    const w = workspaces.find(w => w.name === name);
    if (!w) return;

    // Save current chat history before switching
    saveChatHistory();

    if (selectedWorkspace && selectedWorkspace.name === name) {
        selectedWorkspace = null;
    } else {
        selectedWorkspace = w;
    }

    renderWorkspaces();
    renderDiffStats();
    updateWorkspaceCtx();
    restoreChatHistory();
}

function navigateToWorkspace(name) {
    if (!name) return;
    // If already selected, just scroll to chat
    if (selectedWorkspace && selectedWorkspace.name === name) {
        document.getElementById('chat-messages').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        return;
    }
    selectWorkspace(name);
    // Scroll chat panel into view
    setTimeout(() => {
        document.getElementById('chat-messages').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 100);
}

function navigateToWorkspaceByBranch(branch) {
    if (!branch) return;
    const workspaces = state.workspaces || [];
    // Find workspace by branch match, or by name if workspace has the branch checked out
    const match = workspaces.find(w => w.branch === branch);
    if (match) {
        navigateToWorkspace(match.name);
    }
}

function saveChatHistory() {
    const key = selectedWorkspace ? selectedWorkspace.name : '__default__';
    const el = document.getElementById('chat-messages');
    chatHistories[key] = el.innerHTML;
}

function restoreChatHistory() {
    const key = selectedWorkspace ? selectedWorkspace.name : '__default__';
    const el = document.getElementById('chat-messages');

    // If we have cached HTML, use it
    if (chatHistories[key]) {
        el.innerHTML = chatHistories[key];
        el.scrollTop = el.scrollHeight;
        const hasAssistant = el.querySelector('.chat-msg.assistant') !== null;
        document.getElementById('btn-chat-approve').disabled = !hasAssistant;
        return;
    }

    // Otherwise fetch from backend
    const convId = selectedWorkspace ? `ws-${selectedWorkspace.name}` : 'default';
    el.innerHTML = '';
    fetch(`/api/chat/${encodeURIComponent(convId)}`)
        .then(r => r.json())
        .then(data => {
            const messages = data.messages || [];
            if (!messages.length) {
                document.getElementById('btn-chat-approve').disabled = true;
                return;
            }
            messages.forEach(m => {
                const msg = document.createElement('div');
                msg.className = `chat-msg ${m.role === 'assistant' ? 'assistant' : 'user'}`;
                msg.innerHTML = renderChatMarkdown(m.content);
                el.appendChild(msg);
            });
            el.scrollTop = el.scrollHeight;
            // Cache for fast switching
            chatHistories[key] = el.innerHTML;
            const hasAssistant = el.querySelector('.chat-msg.assistant') !== null;
            document.getElementById('btn-chat-approve').disabled = !hasAssistant;
        })
        .catch(() => {
            document.getElementById('btn-chat-approve').disabled = true;
        });
}

function updateWorkspaceCtx() {
    const el = document.getElementById('chat-workspace-ctx');
    if (selectedWorkspace) {
        el.className = 'chat-workspace-ctx active';
        el.innerHTML = `<span>📂 <strong>${esc(selectedWorkspace.name)}</strong></span> <span style="color:var(--text-muted)">${esc(selectedWorkspace.path)} · ${selectedWorkspace.branch || '—'}</span>`;
    } else {
        el.className = 'chat-workspace-ctx';
        el.innerHTML = '<span class="ctx-label">No workspace selected — click a workspace above</span>';
    }
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
        const branch = prl.branch ? `<span class="pr-branch">${esc(prl.branch)}</span>` : '';
        const elapsed = prl.created_at ? formatElapsed(prl.created_at) : '';

        return `
            <div class="pr-item" onclick="navigateToWorkspaceByBranch('${esc(prl.branch)}')" style="cursor:pointer">
                <div class="pr-header">
                    <span class="pr-title">${prl.pr_number ? `#${prl.pr_number}` : '—'} ${esc(prl.title)}</span>
                    <div class="pr-header-actions">
                        ${stageBadge}
                        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); deletePRL(${prl.id})" title="Dismiss">✕</button>
                    </div>
                </div>
                <div class="pr-stages">${dots}</div>
                <div class="item-meta" style="margin-top:6px">
                    ${branch}
                    Iteration ${prl.iteration}/${prl.max_iterations} ·
                    Greptile: ${prl.greptile_comments_resolved}/${prl.greptile_comments_total} resolved
                    ${elapsed ? ` · ${elapsed}` : ''}
                </div>
            </div>
        `;
    }).join('');
}

// ── Diff Stats ─────────────────────────────────────────────────────────────

const latestDiffStats = {};  // { workspace: stats }

function updateDiffStats(stats) {
    latestDiffStats[stats.workspace] = stats;
    renderDiffStats();
}

function renderDiffStats() {
    const el = document.getElementById('diff-stats');
    const countEl = document.getElementById('diff-count');
    const headerEl = document.getElementById('diff-header-label');

    // Filter to selected workspace if one is active
    const wsName = selectedWorkspace ? selectedWorkspace.name : null;
    const relevantStats = wsName
        ? (latestDiffStats[wsName] ? [latestDiffStats[wsName]] : [])
        : Object.values(latestDiffStats);

    if (headerEl) {
        headerEl.textContent = wsName ? `CHANGED FILES (${wsName})` : 'CHANGED FILES';
    }

    const allFiles = [];
    let totalAdded = 0, totalRemoved = 0;

    for (const stats of relevantStats) {
        for (const f of (stats.files || [])) {
            allFiles.push({ ...f, workspace: stats.workspace });
            totalAdded += f.added;
            totalRemoved += f.removed;
        }
    }

    countEl.textContent = allFiles.length;

    if (!allFiles.length) {
        el.innerHTML = '<div class="empty-state">No file changes detected</div>';
        return;
    }

    // Sort by most changes first
    allFiles.sort((a, b) => (b.added + b.removed) - (a.added + a.removed));

    const rows = allFiles.map(f => {
        const statusIcon = f.status === 'new' ? '🆕' : '✏️';
        const addedStr = f.added > 0 ? `<span class="diff-added">+${f.added}</span>` : '';
        const removedStr = f.removed > 0 ? `<span class="diff-removed">-${f.removed}</span>` : '';
        const total = f.added + f.removed;
        // Mini bar: green portion = added, red portion = removed
        const pctGreen = total > 0 ? Math.round((f.added / total) * 100) : 0;
        const bar = `<div class="diff-bar"><div class="diff-bar-green" style="width:${pctGreen}%"></div><div class="diff-bar-red" style="width:${100 - pctGreen}%"></div></div>`;
        return `
            <div class="diff-file-row">
                <span class="diff-file-icon">${statusIcon}</span>
                <span class="diff-file-name" title="${esc(f.file)}">${esc(f.file.split('/').pop())}</span>
                <span class="diff-file-path" title="${esc(f.file)}">${esc(f.file)}</span>
                ${bar}
                <span class="diff-file-stats">${addedStr} ${removedStr}</span>
            </div>`;
    }).join('');

    const summary = `
        <div class="diff-summary">
            <span class="diff-added">+${totalAdded}</span>
            <span class="diff-removed">-${totalRemoved}</span>
            <span class="diff-total">${allFiles.length} file${allFiles.length !== 1 ? 's' : ''}</span>
        </div>`;

    el.innerHTML = summary + rows;
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
    msg.innerHTML = renderChatMarkdown(text);
    el.appendChild(msg);
    el.scrollTop = el.scrollHeight;

    // Save to history map
    saveChatHistory();

    // Enable approve button when assistant responds
    if (role === 'assistant') {
        document.getElementById('btn-chat-approve').disabled = false;
    }
}

function addChatThinking() {
    const el = document.getElementById('chat-messages');
    const msg = document.createElement('div');
    msg.className = 'chat-msg assistant chat-thinking';
    msg.innerHTML = '<span class="thinking-dots">Thinking<span>.</span><span>.</span><span>.</span></span>';
    el.appendChild(msg);
    el.scrollTop = el.scrollHeight;
}

function removeChatThinking() {
    const el = document.getElementById('chat-messages');
    const thinking = el.querySelector('.chat-thinking');
    if (thinking) thinking.remove();
}

/** Basic markdown rendering for chat messages */
function renderChatMarkdown(text) {
    if (!text) return '';
    let html = esc(text);
    // Code blocks: ```lang\ncode\n```
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // Inline code
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // Headers
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    // Horizontal rule
    html = html.replace(/^---$/gm, '<hr>');
    // Bold
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    // Bullet lists
    html = html.replace(/^[*\-] (.+)$/gm, '<li>$1</li>');
    // Numbered lists
    html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
    // Wrap consecutive <li> in <ul>
    html = html.replace(/((?:<li>.*?<\/li>\n?)+)/g, '<ul>$1</ul>');
    // Links: [text](url)
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    // Line breaks (but not inside pre/code blocks or after block elements)
    html = html.replace(/\n/g, '<br>');
    // Clean up <br> after block elements
    html = html.replace(/(<\/h[1-4]>|<\/ul>|<\/pre>|<hr>)<br>/g, '$1');
    html = html.replace(/<br>(<h[1-4]|<ul|<pre|<hr)/g, '$1');
    return html;
}

document.getElementById('btn-chat-send').addEventListener('click', () => {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text || chatWaiting) return;

    addChatMessage('user', text);
    input.value = '';
    input.style.height = 'auto';

    chatWaiting = true;
    addChatThinking();

    const payload = {
        action: 'chat',
        conversation_id: getConvId(),
        text: text,
        model: document.getElementById('model-planning').value || '',
    };
    if (selectedWorkspace) {
        payload.workspace = selectedWorkspace.name;
        payload.workspace_path = selectedWorkspace.path;
    }
    ws.send(JSON.stringify(payload));
});

document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        document.getElementById('btn-chat-send').click();
    }
});

document.getElementById('btn-chat-approve').addEventListener('click', () => {
    fetch('/api/chat/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: getConvId() }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addChatMessage('assistant', `⚠️ ${data.error}`);
                // Re-enable so user can retry
                document.getElementById('btn-chat-approve').disabled = false;
            } else {
                addChatMessage('assistant', `✅ Plan approved! Task #${data.task_id} created.`);
                document.getElementById('btn-chat-approve').disabled = true;
            }
        })
        .catch(() => {
            document.getElementById('btn-chat-approve').disabled = false;
        });
});

document.getElementById('btn-chat-clear').addEventListener('click', () => {
    const key = selectedWorkspace ? selectedWorkspace.name : '__default__';
    document.getElementById('chat-messages').innerHTML = '';
    chatHistories[key] = '';
    document.getElementById('btn-chat-approve').disabled = true;
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

function deleteTask(taskId) {
    fetch(`/api/tasks/${taskId}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addLogEntry({ ts: new Date().toISOString(), level: 'WARN', component: 'ui', event: data.error });
            }
        });
}

function deletePRL(prlId) {
    fetch(`/api/pr-lifecycles/${prlId}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addLogEntry({ ts: new Date().toISOString(), level: 'WARN', component: 'ui', event: data.error });
            }
        });
}

function toggleTaskFilter() {
    showAllTasks = !showAllTasks;
    renderTasks();
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
    // Buffer output per agent
    if (!agentOutputBuffers[agentId]) agentOutputBuffers[agentId] = [];
    agentOutputBuffers[agentId].push(line);
    if (agentOutputBuffers[agentId].length > 200) {
        agentOutputBuffers[agentId] = agentOutputBuffers[agentId].slice(-150);
    }

    // Parse agent activity from stream-json
    parseAgentActivity(agentId, line);

    // Re-render agents to show latest output
    renderAgents();

    // Also add to logs
    addLogEntry({
        ts: new Date().toISOString(),
        level: 'DEBUG',
        component: `agent:${agentId.slice(0, 12)}`,
        event: line.slice(0, 200),
    });
}

function parseAgentActivity(agentId, line) {
    try {
        const data = JSON.parse(line);
        // Gemini stream-json emits various event types
        // Look for tool_use / function_call events
        if (data.type === 'tool_use' || data.type === 'function_call') {
            const name = data.name || data.tool || data.function || 'unknown';
            const detail = data.input?.path || data.input?.file_path || data.input?.command || data.input?.query || '';
            agentActivity[agentId] = { action: name, detail: detail.split('/').pop() || detail, ts: new Date() };
        } else if (data.type === 'tool_result' || data.type === 'function_response') {
            // Keep the current action, just note it finished
        } else if (data.type === 'text' || data.type === 'content') {
            const text = data.text || data.content || '';
            if (text.length > 10) {
                agentActivity[agentId] = { action: 'Thinking', detail: text.slice(0, 60), ts: new Date() };
            }
        } else if (data.turnComplete || data.type === 'turnComplete') {
            agentActivity[agentId] = { action: 'Turn complete', detail: '', ts: new Date() };
        } else if (data.action) {
            // Generic action field
            agentActivity[agentId] = { action: data.action, detail: data.path || data.file || '', ts: new Date() };
        }
    } catch (_) {
        // Non-JSON line — try simple text patterns
        if (line.includes('Reading file') || line.includes('read_file')) {
            agentActivity[agentId] = { action: 'Reading', detail: line.split(' ').pop(), ts: new Date() };
        } else if (line.includes('Writing') || line.includes('write_file') || line.includes('edit_file')) {
            agentActivity[agentId] = { action: 'Editing', detail: line.split(' ').pop(), ts: new Date() };
        } else if (line.includes('Running') || line.includes('execute')) {
            agentActivity[agentId] = { action: 'Running command', detail: '', ts: new Date() };
        } else if (line.includes('Searching') || line.includes('grep') || line.includes('search')) {
            agentActivity[agentId] = { action: 'Searching', detail: '', ts: new Date() };
        }
    }
}

function resetWorkspace(name) {
    if (!confirm(`Reset workspace "${name}"?\n\nThis will:\n• Kill any running agents\n• Rollback all changes\n• Checkout main branch\n• Clean untracked files`)) {
        return;
    }
    fetch(`/api/workspaces/${name}/reset`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                addSystemChatMessage(`🔄 Workspace **${name}** has been reset to clean state.`);
            } else {
                alert(`Reset failed: ${data.error || 'Unknown error'}`);
            }
        })
        .catch(err => alert(`Reset error: ${err.message}`));
}

// ── Event echoing to chat ──────────────────────────────────────────────────

function addSystemChatMessage(text) {
    // Add a system message to the current chat
    const el = document.getElementById('chat-messages');
    const msg = document.createElement('div');
    msg.className = 'chat-msg system';
    msg.innerHTML = renderChatMarkdown(text);
    el.appendChild(msg);
    el.scrollTop = el.scrollHeight;
    saveChatHistory();

    // Also persist to the backend so the planner LLM can see events
    const convId = getConvId();
    fetch('/api/chat/system-event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: convId, text: text }),
    }).catch(() => { /* best effort */ });
}

function echoAgentEvent(agent) {
    const statusEmoji = { starting: '🚀', running: '🏃', completed: '✅', failed: '❌', killed: '💀' };
    const emoji = statusEmoji[agent.status] || '🤖';
    addSystemChatMessage(`${emoji} Agent \`${agent.id.slice(0, 12)}\` → **${agent.status}** (workspace: ${agent.workspace}, task: #${agent.task_id})`);
}

function echoTaskEvent(task) {
    const statusEmoji = { pending: '📋', ready: '🟢', running: '🏃', done: '✅', failed: '❌', blocked: '🚫', cancelled: '🗑️' };
    const emoji = statusEmoji[task.status] || '📋';
    addSystemChatMessage(`${emoji} Task #${task.id} "${task.title}" → **${task.status}**`);
}

function echoPREvent(prl) {
    const stageEmoji = { coding: '💻', prechecks: '🔍', pr_created: '🔗', ci_monitoring: '⏳', ci_fixing: '🔧', greptile_review: '🤖', addressing_comments: '💬', ready_for_review: '✅', needs_human: '🙋', planning: '📝' };
    const emoji = stageEmoji[prl.stage] || '📦';
    addSystemChatMessage(`${emoji} PR "${prl.title}" → **${prl.stage}** (iteration ${prl.iteration}/${prl.max_iterations})`);
}

function handleAgentFailure(data) {
    if (data.action === 'quota_wait') {
        // Quota exhaustion — waiting for reset
        addSystemChatMessage(
            `⏳ **API quota exhausted** — waiting 60s for reset ` +
            `(attempt ${data.retry_number}/${data.max_retries})\n\n` +
            `Agent will auto-retry after quota resets.`
        );
    } else if (data.is_flake && data.action === 'retrying') {
        // Flake with auto-retry
        const tailLines = (data.output_tail || []).slice(-3).map(l => esc(l)).join('\n');
        addSystemChatMessage(
            `⚠️ **Agent failed** (flake detected: ${esc(data.reason)})\n\n` +
            `🔄 Auto-retrying... (attempt ${data.retry_number}/${data.max_retries})` +
            (tailLines ? `\n\n\`\`\`\n${tailLines}\n\`\`\`` : '')
        );
    } else {
        // Real failure or retries exhausted
        const tailLines = (data.output_tail || []).slice(-5).map(l => esc(l)).join('\n');
        const retriesMsg = data.retries_exhausted
            ? '⚠️ Flake retries exhausted.'
            : '';

        const el = document.getElementById('chat-messages');
        const msg = document.createElement('div');
        msg.className = 'chat-msg system agent-failure-card';
        msg.innerHTML = `
            <div class="failure-header">❌ Agent Failed — Task #${data.task_id}</div>
            <div class="failure-reason">${retriesMsg} ${esc(data.reason)}</div>
            ${tailLines ? `<pre class="failure-output">${tailLines}</pre>` : ''}
            <div class="failure-info">PR lifecycle returned to <strong>Planning</strong>.</div>
            <div class="failure-info">How would you like to proceed?</div>
            <div class="failure-actions">
                <button class="btn btn-sm" onclick="retryFailedTask(${data.task_id}, '${esc(data.workspace)}')">🔄 Retry Agent</button>
                <button class="btn btn-sm" onclick="addSystemChatMessage('ℹ️ You can modify the plan in the chat and re-approve when ready.')">📝 Modify Plan</button>
            </div>
        `;
        el.appendChild(msg);
        el.scrollTop = el.scrollHeight;
        saveChatHistory();

        // Re-enable the approve button so user can re-approve the same plan
        document.getElementById('btn-chat-approve').disabled = false;
    }
}

function retryFailedTask(taskId, workspace) {
    addSystemChatMessage(`🔄 Retrying task #${taskId} on workspace ${workspace}...`);
    const convId = `ws-${workspace}`;
    fetch('/api/chat/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: convId, workspace: workspace }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                addSystemChatMessage(`❌ Retry failed: ${data.error}`);
            } else {
                addSystemChatMessage(`✅ Retry started! Task #${data.task_id} created.`);
            }
        })
        .catch(err => addSystemChatMessage(`❌ Retry error: ${err.message}`));
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

function formatElapsed(timestamp) {
    const seconds = Math.floor((Date.now() / 1000) - timestamp);
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m ago`;
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

// ── Keyboard Shortcuts ────────────────────────────────────────────────────

document.addEventListener('keydown', (e) => {
    // Escape to deselect workspace
    if (e.key === 'Escape' && selectedWorkspace) {
        saveChatHistory();
        selectedWorkspace = null;
        renderWorkspaces();
        updateWorkspaceCtx();
        restoreChatHistory();
    }
});

// ── Auto-refresh ───────────────────────────────────────────────────────────

setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'refresh' }));
    }
}, 15000);

// Update elapsed times every second (lightweight — just updates text, no re-render)
setInterval(() => {
    document.querySelectorAll('.agent-elapsed').forEach(el => {
        const card = el.closest('.agent-card');
        if (!card) return;
        const agents = (state.agents || []).filter(a => ['starting', 'running'].includes(a.status));
        if (agents.length) renderAgents();
    });
}, 1000);

// ── Init ───────────────────────────────────────────────────────────

connect();
fetchModelConfig();

// ── Model Config ───────────────────────────────────────────────────────

function fetchModelConfig() {
    fetch('/api/models')
        .then(r => r.json())
        .then(data => {
            modelConfig = data;
            populateModelDropdowns();
        })
        .catch(() => { });
}

function populateModelDropdowns() {
    const ids = ['model-planning', 'model-coding', 'model-testing'];
    const roles = ['planning', 'coding', 'testing'];

    ids.forEach((id, i) => {
        const select = document.getElementById(id);
        if (!select) return;
        const current = modelConfig[roles[i]] || '';
        select.innerHTML = (modelConfig.available || []).map(m =>
            `<option value="${m}"${m === current ? ' selected' : ''}>${m}</option>`
        ).join('');
    });
}

// Settings panel toggle
document.getElementById('btn-settings').addEventListener('click', () => {
    document.getElementById('settings-panel').classList.toggle('open');
});

// Persist model changes for coding and testing
['model-coding', 'model-testing'].forEach(id => {
    document.getElementById(id).addEventListener('change', (e) => {
        const role = id.replace('model-', '');
        const body = {};
        body[role] = e.target.value;
        fetch('/api/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    });
});

// Persist planning model change
document.getElementById('model-planning').addEventListener('change', (e) => {
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ planning: e.target.value }),
    });
});
