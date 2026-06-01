/**
 * Debug Inspector - Tree-view object browser for the Python backend.
 *
 * Communicates with /api/debug/inspect to lazily traverse the object graph.
 * Standalone — no dependencies on the main app JS.
 *
 * Migrated from static/js/debug-inspector.js to a Vite entry (dist/debug.js)
 * powering the /debug page (longterm_todo.md item #7). The page's host markup
 * is static/debug.html, served by debug_inspector.py.
 */

const API_BASE = '';

// A serialized value from debug_inspector.py's serialize_object: either a JSON
// primitive, or a `{_type, ...}` wrapper describing a Python container/object.
// Loosely typed — the renderers probe fields defensively.
type DebugValue =
    | null
    | boolean
    | number
    | string
    | DebugWrapper;

interface DebugWrapper {
    _type?: string;
    length?: number;
    maxlen?: number | null;
    class?: string;
    name?: string;
    value?: string;
    repr?: string;
    items?: Record<string, DebugValue> | DebugValue[];
    keys?: string[];
    fields?: string[] | Record<string, DebugValue>;
    [key: string]: unknown;
}

interface InspectResponse {
    path: string;
    depth: number;
    data: DebugValue;
}

/** Narrow a DebugValue to the object-wrapper form. */
function asWrapper(val: DebugValue): DebugWrapper | null {
    return val !== null && typeof val === 'object' ? val : null;
}

/** getElementById that asserts presence — the /debug page always has these. */
function $(id: string): HTMLElement {
    return document.getElementById(id)!;
}

function errMsg(e: unknown): string {
    return (e as { message?: string })?.message ?? String(e);
}

// ── API helpers ───────────────────────────────────────────────────────────

async function fetchJSON(url: string): Promise<any> {
    const resp = await fetch(API_BASE + url);
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return resp.json();
}

async function fetchInspect(path: string, depth = 1): Promise<InspectResponse> {
    const params = new URLSearchParams({ path, depth: String(depth) });
    return fetchJSON(`/api/debug/inspect?${params}`);
}

async function fetchSummary(): Promise<Record<string, unknown>> {
    return fetchJSON('/api/debug/summary');
}

async function fetchBlockHistory(limit = 30): Promise<any> {
    return fetchJSON(`/api/debug/block-history?limit=${limit}`);
}

// ── State ─────────────────────────────────────────────────────────────────

let autoRefreshInterval: ReturnType<typeof setInterval> | null = null;
const expandedPaths = new Set<string>();

// ── Summary panel ─────────────────────────────────────────────────────────

async function refreshSummary() {
    const el = $('summary-content');
    try {
        const data = await fetchSummary();
        el.innerHTML = Object.entries(data)
            .map(([k, v]) => {
                let cls = 'summary-val';
                let display: unknown = v;
                if (k === 'simulation_running') {
                    cls += v ? ' running' : ' stopped';
                    display = v ? 'running' : 'stopped';
                }
                return `<div class="summary-item">
                    <span class="summary-key">${k}</span>
                    <span class="${cls}">${display}</span>
                </div>`;
            })
            .join('');
    } catch (e) {
        el.innerHTML = `<div class="empty-state">Error: ${errMsg(e)}</div>`;
    }
}

// ── Block history panel ───────────────────────────────────────────────────

async function refreshHistory() {
    const container = $('history-container');
    const badge = $('history-count');
    try {
        const data = await fetchBlockHistory(30);
        badge.textContent = data.total;
        if (!data.history.length) {
            container.innerHTML = '<div class="empty-state">No block state changes recorded yet</div>';
            return;
        }
        container.innerHTML = data.history
            .slice().reverse()
            .map((h: any) => {
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
        container.innerHTML = `<div class="empty-state">Error: ${errMsg(e)}</div>`;
    }
}

// ── Tree rendering ────────────────────────────────────────────────────────

function valueDisplay(val: DebugValue): { text: string; cls: string } | null {
    if (val === null) return { text: 'null', cls: 'null' };
    if (val === undefined) return { text: 'undefined', cls: 'null' };
    if (typeof val === 'boolean') return { text: String(val), cls: 'boolean' };
    if (typeof val === 'number') return { text: String(val), cls: 'number' };
    if (typeof val === 'string') return { text: `"${val}"`, cls: 'string' };
    return null;
}

function isExpandable(val: DebugValue): boolean {
    const w = asWrapper(val);
    if (!w) return false;
    const t = w._type;
    return t === 'dict' || t === 'list' || t === 'tuple' || t === 'set' ||
           t === 'frozenset' || t === 'deque' || t === 'object';
}

function typeSuffix(val: DebugValue): string {
    const w = asWrapper(val);
    if (!w) return '';
    const t = w._type;
    if (!t) return '';
    if (t === 'dict') return `dict (${w.length})`;
    if (t === 'list') return `list (${w.length})`;
    if (t === 'tuple') return `tuple (${w.length})`;
    if (t === 'set') return `set (${w.length})`;
    if (t === 'frozenset') return `frozenset (${w.length})`;
    if (t === 'deque') return `deque (${w.length}, max=${w.maxlen ?? '?'})`;
    if (t === 'object') return w.class || 'object';
    if (t === 'enum') return w.class || 'enum';
    if (t === 'method') return `fn ${w.name}()`;
    if (t === 'circular') return 'circular ref';
    if (t === 'unknown') return w.repr || '?';
    return t;
}

function inlineValue(val: DebugValue): { text: string; cls: string } | null {
    // Try to show a compact inline value
    const prim = valueDisplay(val);
    if (prim) return prim;
    const w = asWrapper(val);
    if (w && w._type === 'enum') return { text: String(w.value), cls: 'enum' };
    return null;
}

function createTreeNode(key: string, val: DebugValue, parentPath: string, indentLevel: number): HTMLElement {
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

async function loadChildren(path: string, container: HTMLElement, indentLevel: number) {
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
        container.innerHTML = `<div class="tree-loading">Error: ${errMsg(e)}</div>`;
    }
}

function getChildEntries(data: DebugValue): Array<[string, DebugValue]> {
    const w = asWrapper(data);
    if (!w) return [];
    const t = w._type;

    if (t === 'dict' && w.items && !Array.isArray(w.items)) {
        return Object.entries(w.items);
    }
    if (t === 'dict' && w.keys) {
        // depth=0 fallback: show keys as unexpanded
        return w.keys.map((k): [string, DebugValue] => [k, { _type: 'unknown', repr: '...' }]);
    }
    if ((t === 'list' || t === 'tuple' || t === 'set' || t === 'frozenset' || t === 'deque') && Array.isArray(w.items)) {
        return w.items.map((v, i): [string, DebugValue] => [String(i), v]);
    }
    if (t === 'object' && w.fields) {
        if (Array.isArray(w.fields)) {
            // depth=0: field names only
            return w.fields.map((f): [string, DebugValue] => [f, { _type: 'unknown', repr: '...' }]);
        }
        return Object.entries(w.fields);
    }
    return [];
}

// ── Tree: root load ───────────────────────────────────────────────────────

async function loadTree(path: string) {
    const container = $('tree-container');
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

        const w = asWrapper(data);
        if (w) {
            const entries = getChildEntries(data);
            if (entries.length === 0 && w._type) {
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
        container.innerHTML = `<div class="empty-state">Error: ${errMsg(e)}</div>`;
    }
}

// ── Navigation ────────────────────────────────────────────────────────────

function navigateTo(path: string) {
    ($('path-input') as HTMLInputElement).value = path;
    loadTree(path);
}

function goUp() {
    const input = $('path-input') as HTMLInputElement;
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
    $('status-indicator').className = 'status-dot status-on';
}

function stopAutoRefresh() {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
    $('status-indicator').className = 'status-dot status-off';
}

function refreshExpandedNodes() {
    // Refresh the root tree at whatever path is currently displayed.
    // Only refresh top-level, don't disturb expanded children
    // (a full re-render would lose expand state; for now just refresh summary/history)
}

// ── Init ──────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Path navigation
    const pathInput = $('path-input') as HTMLInputElement;
    $('path-go').addEventListener('click', () => {
        navigateTo(pathInput.value.trim());
    });
    pathInput.addEventListener('keydown', (e: KeyboardEvent) => {
        if (e.key === 'Enter') navigateTo(pathInput.value.trim());
    });
    $('path-up').addEventListener('click', goUp);

    // Auto-refresh toggle
    $('auto-refresh').addEventListener('change', (e) => {
        if ((e.target as HTMLInputElement).checked) startAutoRefresh();
        else stopAutoRefresh();
    });

    // Initial load
    refreshSummary();
    refreshHistory();
    loadTree('');
});

// Marks this file as an ES module so Vite can use it as the /debug build entry.
export {};
