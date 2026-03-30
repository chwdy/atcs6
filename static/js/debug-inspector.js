/**
 * Debug Inspector - Tree-view object browser for the Python backend.
 *
 * Communicates with /api/debug/inspect to lazily traverse the object graph.
 * Standalone — no dependencies on the main app JS.
 */

const API_BASE = '';

// ── API helpers ───────────────────────────────────────────────────────────

async function fetchJSON(url) {
    const resp = await fetch(API_BASE + url);
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return resp.json();
}

async function fetchInspect(path, depth = 1) {
    const params = new URLSearchParams({ path, depth: String(depth) });
    return fetchJSON(`/api/debug/inspect?${params}`);
}

async function fetchSummary() {
    return fetchJSON('/api/debug/summary');
}

async function fetchBlockHistory(limit = 30) {
    return fetchJSON(`/api/debug/block-history?limit=${limit}`);
}

// ── State ─────────────────────────────────────────────────────────────────

let autoRefreshInterval = null;
const expandedPaths = new Set();

// ── Summary panel ─────────────────────────────────────────────────────────

async function refreshSummary() {
    const el = document.getElementById('summary-content');
    try {
        const data = await fetchSummary();
        el.innerHTML = Object.entries(data)
            .map(([k, v]) => {
                let cls = 'summary-val';
                if (k === 'simulation_running') {
                    cls += v ? ' running' : ' stopped';
                    v = v ? 'running' : 'stopped';
                }
                return `<div class="summary-item">
                    <span class="summary-key">${k}</span>
                    <span class="${cls}">${v}</span>
                </div>`;
            })
            .join('');
    } catch (e) {
        el.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
    }
}

// ── Block history panel ───────────────────────────────────────────────────

async function refreshHistory() {
    const container = document.getElementById('history-container');
    const badge = document.getElementById('history-count');
    try {
        const data = await fetchBlockHistory(30);
        badge.textContent = data.total;
        if (!data.history.length) {
            container.innerHTML = '<div class="empty-state">No block state changes recorded yet</div>';
            return;
        }
        container.innerHTML = data.history
            .reverse()
            .map(h => {
                const time = h.timestamp ? h.timestamp.split('T')[1]?.substring(0, 8) : '??';
                const fromCls = `state-${h.from_state}`;
                const toCls = `state-${h.to_state}`;
                return `<div class="history-entry">
                    <span class="history-time">${time}</span>
                    <span class="history-block">${h.block_id}</span>
                    <span class="history-transition">
                        <span class="${fromCls}">${h.from_state}</span> → <span class="${toCls}">${h.to_state}</span>
                    </span>
                    <span class="history-train">${h.train_id || ''}</span>
                </div>`;
            })
            .join('');
    } catch (e) {
        container.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
    }
}

// ── Tree rendering ────────────────────────────────────────────────────────

function valueDisplay(val) {
    if (val === null) return { text: 'null', cls: 'null' };
    if (val === undefined) return { text: 'undefined', cls: 'null' };
    if (typeof val === 'boolean') return { text: String(val), cls: 'boolean' };
    if (typeof val === 'number') return { text: String(val), cls: 'number' };
    if (typeof val === 'string') return { text: `"${val}"`, cls: 'string' };
    return null;
}

function isExpandable(val) {
    if (val === null || val === undefined) return false;
    if (typeof val !== 'object') return false;
    const t = val._type;
    return t === 'dict' || t === 'list' || t === 'tuple' || t === 'set' ||
           t === 'frozenset' || t === 'deque' || t === 'object';
}

function typeSuffix(val) {
    if (typeof val !== 'object' || val === null) return '';
    const t = val._type;
    if (!t) return '';
    if (t === 'dict') return `dict (${val.length})`;
    if (t === 'list') return `list (${val.length})`;
    if (t === 'tuple') return `tuple (${val.length})`;
    if (t === 'set') return `set (${val.length})`;
    if (t === 'frozenset') return `frozenset (${val.length})`;
    if (t === 'deque') return `deque (${val.length}, max=${val.maxlen ?? '?'})`;
    if (t === 'object') return val.class || 'object';
    if (t === 'enum') return val.class || 'enum';
    if (t === 'method') return `fn ${val.name}()`;
    if (t === 'circular') return 'circular ref';
    if (t === 'unknown') return val.repr || '?';
    return t;
}

function inlineValue(val) {
    // Try to show a compact inline value
    const prim = valueDisplay(val);
    if (prim) return prim;
    if (typeof val === 'object' && val !== null) {
        if (val._type === 'enum') return { text: val.value, cls: 'enum' };
    }
    return null;
}

function createTreeNode(key, val, parentPath, indentLevel) {
    const fullPath = parentPath ? `${parentPath}.${key}` : key;
    const node = document.createElement('div');
    node.className = 'tree-node';
    node.dataset.path = fullPath;

    const row = document.createElement('div');
    row.className = 'tree-row';

    // Indent
    for (let i = 0; i < indentLevel; i++) {
        const indent = document.createElement('span');
        indent.className = 'tree-indent';
        row.appendChild(indent);
    }

    const expandable = isExpandable(val);

    // Toggle arrow
    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle' + (expandable ? '' : ' leaf');
    toggle.textContent = expandable ? '\u25B6' : '\u25CF';
    row.appendChild(toggle);

    // Key name
    if (key !== undefined && key !== '') {
        const keySpan = document.createElement('span');
        keySpan.className = 'tree-key';
        keySpan.textContent = key;
        row.appendChild(keySpan);
    }

    // Inline value or type info
    const inline = inlineValue(val);
    if (inline) {
        const valSpan = document.createElement('span');
        valSpan.className = 'tree-value ' + inline.cls;
        valSpan.textContent = inline.text;
        row.appendChild(valSpan);
    }

    // Type suffix
    const suffix = typeSuffix(val);
    if (suffix) {
        const typeSpan = document.createElement('span');
        typeSpan.className = 'tree-type';
        typeSpan.textContent = suffix;
        row.appendChild(typeSpan);
    }

    node.appendChild(row);

    // Children container
    const children = document.createElement('div');
    children.className = 'tree-children';
    children.style.display = 'none';
    node.appendChild(children);

    // Expand/collapse
    if (expandable) {
        row.classList.add('tree-clickable');

        const doToggle = async () => {
            if (children.style.display === 'none') {
                toggle.textContent = '\u25BC';
                children.style.display = 'block';
                expandedPaths.add(fullPath);
                await loadChildren(fullPath, children, indentLevel + 1);
            } else {
                toggle.textContent = '\u25B6';
                children.style.display = 'none';
                expandedPaths.delete(fullPath);
                children.innerHTML = '';
            }
        };

        toggle.addEventListener('click', doToggle);
        row.addEventListener('dblclick', doToggle);

        // Also allow clicking the key
        const keyEl = row.querySelector('.tree-key');
        if (keyEl) keyEl.addEventListener('click', doToggle);
    }

    return node;
}

async function loadChildren(path, container, indentLevel) {
    container.innerHTML = '<div class="tree-loading">Loading...</div>';
    try {
        const resp = await fetchInspect(path, 1);
        const data = resp.data;
        container.innerHTML = '';

        if (data === null || typeof data !== 'object') {
            // Primitive resolved — show inline
            const prim = valueDisplay(data);
            if (prim) {
                container.innerHTML = `<div class="tree-row">
                    ${'<span class="tree-indent"></span>'.repeat(indentLevel)}
                    <span class="tree-toggle leaf">\u25CF</span>
                    <span class="tree-value ${prim.cls}">${prim.text}</span>
                </div>`;
            }
            return;
        }

        const entries = getChildEntries(data);
        if (entries.length === 0) {
            container.innerHTML = '<div class="tree-loading">empty</div>';
            return;
        }

        for (const [childKey, childVal] of entries) {
            container.appendChild(createTreeNode(childKey, childVal, path, indentLevel));
        }
    } catch (e) {
        container.innerHTML = `<div class="tree-loading">Error: ${e.message}</div>`;
    }
}

function getChildEntries(data) {
    if (!data || typeof data !== 'object') return [];
    const t = data._type;

    if (t === 'dict' && data.items) {
        return Object.entries(data.items);
    }
    if (t === 'dict' && data.keys) {
        // depth=0 fallback: show keys as unexpanded
        return data.keys.map(k => [k, { _type: 'unknown', repr: '...' }]);
    }
    if ((t === 'list' || t === 'tuple' || t === 'set' || t === 'frozenset' || t === 'deque') && data.items) {
        return data.items.map((v, i) => [String(i), v]);
    }
    if (t === 'object' && data.fields) {
        if (Array.isArray(data.fields)) {
            // depth=0: field names only
            return data.fields.map(f => [f, { _type: 'unknown', repr: '...' }]);
        }
        return Object.entries(data.fields);
    }
    return [];
}

// ── Tree: root load ───────────────────────────────────────────────────────

async function loadTree(path) {
    const container = document.getElementById('tree-container');
    container.innerHTML = '<div class="tree-loading">Loading...</div>';
    expandedPaths.clear();

    try {
        const resp = await fetchInspect(path, 1);
        const data = resp.data;
        container.innerHTML = '';

        // If we navigated to a primitive
        const prim = valueDisplay(data);
        if (prim) {
            container.innerHTML = `<div class="tree-row">
                <span class="tree-toggle leaf">\u25CF</span>
                <span class="tree-value ${prim.cls}">${prim.text}</span>
            </div>`;
            return;
        }

        if (typeof data === 'object' && data !== null) {
            const entries = getChildEntries(data);
            if (entries.length === 0 && data._type) {
                // Single non-expandable (enum, method, etc)
                const node = createTreeNode('', data, path, 0);
                container.appendChild(node);
                return;
            }
            for (const [k, v] of entries) {
                container.appendChild(createTreeNode(k, v, path, 0));
            }
        }
    } catch (e) {
        container.innerHTML = `<div class="empty-state">Error: ${e.message}</div>`;
    }
}

// ── Navigation ────────────────────────────────────────────────────────────

function navigateTo(path) {
    document.getElementById('path-input').value = path;
    loadTree(path);
}

function goUp() {
    const input = document.getElementById('path-input');
    const path = input.value.trim();
    if (!path) return;
    const parts = path.split('.');
    parts.pop();
    navigateTo(parts.join('.'));
}

// ── Auto-refresh ──────────────────────────────────────────────────────────

function startAutoRefresh() {
    stopAutoRefresh();
    autoRefreshInterval = setInterval(() => {
        refreshSummary();
        refreshHistory();
        // Re-fetch expanded tree nodes
        refreshExpandedNodes();
    }, 2000);
    document.getElementById('status-indicator').className = 'status-dot status-on';
}

function stopAutoRefresh() {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
    document.getElementById('status-indicator').className = 'status-dot status-off';
}

async function refreshExpandedNodes() {
    // Refresh the root tree at whatever path is currently displayed
    const path = document.getElementById('path-input').value.trim();
    // Only refresh top-level, don't disturb expanded children
    // (a full re-render would lose expand state; for now just refresh summary/history)
}

// ── Init ──────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Path navigation
    const pathInput = document.getElementById('path-input');
    document.getElementById('path-go').addEventListener('click', () => {
        navigateTo(pathInput.value.trim());
    });
    pathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') navigateTo(pathInput.value.trim());
    });
    document.getElementById('path-up').addEventListener('click', goUp);

    // Auto-refresh toggle
    document.getElementById('auto-refresh').addEventListener('change', (e) => {
        if (e.target.checked) startAutoRefresh();
        else stopAutoRefresh();
    });

    // Initial load
    refreshSummary();
    refreshHistory();
    loadTree('');
});
