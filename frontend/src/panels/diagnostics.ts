/**
 * DiagnosticsPanel — Phase 1 (Train + TrainController only).
 *
 * Floating, draggable, read-only debug panel that polls
 * GET /api/inspector/entity/train/<train_id> on a configurable cadence
 * and displays the 4 groups returned by the backend, with per-row diff
 * highlighting and tooltips for derived-property formulas.
 *
 * Phase 2+ will add Path / Block / Switch / Route inspectors. This file
 * is structured so that a future entity selector slots in without breaking
 * the train-only path.
 *
 * Window chrome (drag, resize, double-click-to-fit, DPI re-clamp, z-order) is
 * owned by FloatingWindow (lib/window-manager.ts) — this file only builds and
 * updates its own toolbar/body/footer content.
 */

import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { FloatingWindow, windowManager, type WindowGeometry } from '../lib/window-manager';
import {
    browserWindowPersistenceEnv,
    loadWindowState,
    saveWindowState,
    ensureRegistered,
    type WindowState,
} from '../lib/window-persistence';
import { logsPanelReplacesWindows } from '../next/logs/replaced-windows';
// Shapes of the inspector snapshot the backend returns from
// GET /api/inspector/entity/train/<id>. Loosely typed — the panel only reads
// these members and is defensive about missing ones.
interface InspectorItem {
    name: string;
    value: unknown;
    error?: string | null;
    source?: string;
    computed_from?: string[];
    formula_doc?: string;
    writer?: string;
    consumer?: string;
    type?: string;
}

interface InspectorGroup {
    name: string;
    items?: InspectorItem[];
}

interface InspectorSnapshot {
    id?: string;
    label?: string;
    fetched_at?: number;
    groups?: InspectorGroup[];
}

(function () {
    "use strict";

    // Registered under the shared window-persistence scheme (lib/window-
    // persistence.ts) as windowId 'diagnostics', windowKind 'diagnostics' —
    // the one non-tab-group entry alongside 'home' and every spawned
    // tab-group window. loadState/saveState keep their original shape
    // (x/y/width/height/visible/trainId/refreshHz/sortMode/collapsedGroups)
    // so nothing downstream of them (_applyPersistedFields,
    // _buildFloatingWindow, _persistState) needed to change for this move —
    // only where the state actually lives did.
    const WINDOW_ID = "diagnostics";
    const persistenceEnv = browserWindowPersistenceEnv();
    const FLASH_MS = 1000;
    const REFRESH_OPTIONS = [
        { label: "4 Hz", hz: 4 },
        { label: "1 Hz", hz: 1 },
        { label: "Every 2s", hz: 0.5 },
        { label: "Every 5s", hz: 0.2 },
        { label: "On-demand", hz: 0 },
    ];
    const DEFAULT_WIDTH = 420;
    const MIN_WIDTH = 320;
    const MAX_WIDTH = 900;

    function loadState(): Record<string, unknown> {
        const state = loadWindowState(persistenceEnv, WINDOW_ID);
        if (!state) return {};
        return {
            visible: !state.hidden,
            trainId: state.diagnostics?.trainId ?? null,
            refreshHz: state.diagnostics?.refreshHz,
            x: state.geometry?.left ?? undefined,
            y: state.geometry?.top ?? undefined,
            width: state.geometry?.width ?? undefined,
            height: state.geometry?.height ?? undefined,
            sortMode: state.diagnostics?.sortMode,
            collapsedGroups: state.diagnostics?.collapsedGroups ?? [],
        };
    }

    function saveState(s: Record<string, unknown>): void {
        const state: WindowState = {
            geometry: {
                left: typeof s.x === "number" ? s.x : null,
                top: typeof s.y === "number" ? s.y : null,
                width: typeof s.width === "number" ? s.width : null,
                height: typeof s.height === "number" ? s.height : null,
            },
            floating: true,
            hidden: !s.visible,
            diagnostics: {
                trainId: (s.trainId as string) || null,
                refreshHz: typeof s.refreshHz === "number" ? s.refreshHz : 1,
                sortMode: (s.sortMode as "asc" | "desc" | null) ?? null,
                collapsedGroups: Array.isArray(s.collapsedGroups) ? (s.collapsedGroups as string[]) : [],
            },
        };
        saveWindowState(persistenceEnv, WINDOW_ID, state);
        ensureRegistered(persistenceEnv, WINDOW_ID, "diagnostics");
    }

    function fmtValue(value: unknown): string {
        if (value === null || value === undefined) return String(value);
        if (typeof value === "string") return value;
        if (typeof value === "number" || typeof value === "boolean") {
            return String(value);
        }
        try {
            return JSON.stringify(value, null, 2);
        } catch (e) {
            return String(value);
        }
    }

    function shortValue(value: unknown): string {
        const full = fmtValue(value);
        if (full.length <= 80) return full;
        return full.slice(0, 78) + "…";
    }

    function isContainer(v: unknown): v is object {
        return v !== null && typeof v === "object";
    }

    function containerSummary(value: object): string {
        if (Array.isArray(value)) {
            return `[${value.length}]`;
        }
        const keys = Object.keys(value);
        if (keys.length === 0) return "{}";
        const preview = keys.slice(0, 3).join(", ");
        const tail = keys.length > 3 ? ", …" : "";
        return `{${keys.length}} ${preview}${tail}`;
    }

    class DiagnosticsPanel {
        // DOM handles — all assigned by _buildContentDom(), which FloatingWindow
        // calls synchronously (via its `content.mount` hook) from within
        // _buildFloatingWindow(), which init() calls before any other method
        // runs. Declared definite (!) per the renderer.ts migration idiom.
        _floatingWindow!: FloatingWindow;
        _bodyEl!: HTMLElement;
        _trainSelectEl!: HTMLSelectElement;
        _rateSelectEl!: HTMLSelectElement;
        _refreshBtnEl!: HTMLElement;
        _footerEl!: HTMLElement;
        _expandAllBtnEl!: HTMLElement;
        _collapseAllBtnEl!: HTMLElement;
        _sortBtnEl!: HTMLElement;
        _nameTooltipEl!: HTMLElement;

        _pollHandle: ReturnType<typeof setInterval> | null;
        _inflight: boolean;
        _abortController: AbortController | null;

        _lastSnapshot: InspectorSnapshot | null;
        _collapsedGroups: Set<string>;
        _currentTrainId: string | null;
        _refreshHz: number;
        _visible: boolean;
        _knownTrainIds: string[];
        _jsonExpansion: Map<string, boolean>;
        _sortMode: "asc" | "desc" | null;

        constructor() {
            // DOM handles (_bodyEl, _trainSelectEl, _rateSelectEl, _refreshBtnEl,
            // _nameTooltipEl, _expandAllBtnEl, _collapseAllBtnEl, _sortBtnEl,
            // _footerEl) are assigned by _buildContentDom(); declared definite
            // above. _floatingWindow is assigned by _buildFloatingWindow().
            this._pollHandle = null;
            this._inflight = false;
            this._abortController = null;

            this._lastSnapshot = null; // last rendered snapshot, used for diff
            this._collapsedGroups = new Set();
            this._currentTrainId = null;
            this._refreshHz = 1;
            this._visible = false;

            this._knownTrainIds = []; // cached from getNetwork()

            this._jsonExpansion = new Map(); // pathKey -> bool (in-memory only)
            this._sortMode = "asc"; // null | "asc" | "desc"; default A→Z
        }

        init() {
            const s = loadState();
            this._applyPersistedFields(s);
            this._buildFloatingWindow(s);
            this._setupNameInteractions();
            this._wireToggleButton();
            this._wireSelectors();

            this._rateSelectEl.value = String(this._refreshHz);
            this._updateSortBtn();

            // First train list fetch (best-effort; ignore errors)
            this._refreshTrainList().then(() => {
                if (this._visible) this._startPolling();
            });
        }

        // ----- Persisted fields (no DOM) ------------------------------------

        _applyPersistedFields(s: Record<string, unknown>) {
            this._visible = !!s.visible;
            this._currentTrainId = (s.trainId as string) || null;
            this._refreshHz = (s.refreshHz === 0 || s.refreshHz)
                ? Number(s.refreshHz)
                : 1;
            this._collapsedGroups = new Set(
                Array.isArray(s.collapsedGroups) ? (s.collapsedGroups as string[]) : []
            );
            this._sortMode =
                s.sortMode === "asc" || s.sortMode === "desc" || s.sortMode === null
                    ? (s.sortMode as "asc" | "desc" | null)
                    : "asc";
        }

        // ----- Window chrome + DOM construction ------------------------------

        /** Build the FloatingWindow this panel lives in. Diagnostics is a
         *  permanent singleton toggled visible/hidden (never destroyed), so its
         *  header close (×) button just hides it — see onCloseRequested. */
        _buildFloatingWindow(s: Record<string, unknown>) {
            const initialGeometry: Partial<WindowGeometry> = {};
            if (typeof s.x === "number" && typeof s.y === "number") {
                initialGeometry.left = Number(s.x);
                initialGeometry.top = Number(s.y);
            } else {
                // Mirrors the old CSS default position (top: 80px; right: 20px)
                // now that FloatingWindow always positions via an explicit left/top.
                initialGeometry.left = Math.max(20, window.innerWidth - DEFAULT_WIDTH - 20);
                initialGeometry.top = 80;
            }
            initialGeometry.width =
                typeof s.width === "number" && s.width > 0 ? Number(s.width) : DEFAULT_WIDTH;
            if (typeof s.height === "number" && s.height > 0) {
                initialGeometry.height = Number(s.height);
            }

            this._floatingWindow = new FloatingWindow({
                id: "diagnostics",
                kind: "diagnostics",
                manager: windowManager,
                title: "Diagnostics",
                minW: MIN_WIDTH,
                maxW: MAX_WIDTH,
                initialGeometry,
                initialHidden: !this._visible,
                content: { mount: (el) => this._buildContentDom(el) },
                onCloseRequested: () => this.hide(),
                onGeometryChange: () => this._persistState(),
            });
        }

        _buildContentDom(contentEl: HTMLElement) {
            contentEl.innerHTML = `
                <div class="diagnostics-panel-toolbar">
                    <label class="diagnostics-panel-field">
                        Train
                        <select class="diagnostics-panel-train-select" data-role="train-select"></select>
                    </label>
                    <label class="diagnostics-panel-field">
                        Refresh
                        <select class="diagnostics-panel-rate-select" data-role="rate-select"></select>
                    </label>
                    <div class="diagnostics-panel-tools">
                        <button class="diagnostics-panel-refresh-btn" type="button" data-role="refresh" title="Refresh now">↻</button>
                        <button class="diagnostics-panel-tree-btn" type="button" data-role="expand-all" title="Expand all values">⊞</button>
                        <button class="diagnostics-panel-tree-btn" type="button" data-role="collapse-all" title="Collapse all values">⊟</button>
                        <button class="diagnostics-panel-sort-btn" type="button" data-role="sort">⇅</button>
                    </div>
                </div>
                <div class="diagnostics-panel-body" data-role="body">
                    <div class="empty-state">Select a train to begin.</div>
                </div>
                <div class="diagnostics-panel-footer" data-role="footer"></div>
            `;

            this._bodyEl = contentEl.querySelector('[data-role="body"]') as HTMLElement;
            this._trainSelectEl = contentEl.querySelector('[data-role="train-select"]') as HTMLSelectElement;
            this._rateSelectEl = contentEl.querySelector('[data-role="rate-select"]') as HTMLSelectElement;
            this._refreshBtnEl = contentEl.querySelector('[data-role="refresh"]') as HTMLElement;
            this._footerEl = contentEl.querySelector('[data-role="footer"]') as HTMLElement;
            this._expandAllBtnEl = contentEl.querySelector('[data-role="expand-all"]') as HTMLElement;
            this._collapseAllBtnEl = contentEl.querySelector('[data-role="collapse-all"]') as HTMLElement;
            this._sortBtnEl = contentEl.querySelector('[data-role="sort"]') as HTMLElement;

            const tooltip = document.createElement("div");
            tooltip.className = "diagnostics-name-tooltip";
            tooltip.style.display = "none";
            document.body.appendChild(tooltip);
            this._nameTooltipEl = tooltip;

            // Populate refresh-rate dropdown.
            this._rateSelectEl.innerHTML = REFRESH_OPTIONS
                .map(o => `<option value="${o.hz}">${o.label}</option>`)
                .join("");
        }

        _wireToggleButton() {
            const btn = document.getElementById("btn-diagnostics-toggle");
            if (btn) {
                btn.addEventListener("click", () => this.toggle());
            }
        }

        _wireSelectors() {
            this._trainSelectEl.addEventListener("change", (e) => {
                this._onTrainChange((e.target as HTMLSelectElement).value || null);
            });
            this._rateSelectEl.addEventListener("change", (e) => {
                const hz = Number((e.target as HTMLSelectElement).value);
                this._onRateChange(Number.isFinite(hz) ? hz : 1);
            });
            this._refreshBtnEl.addEventListener("click", () => {
                this._pollOnce();
            });
            this._expandAllBtnEl.addEventListener("click", () => this._expandAll());
            this._collapseAllBtnEl.addEventListener("click", () => this._collapseAll());
            this._sortBtnEl.addEventListener("click", () => this._cycleSortMode());
        }

        // ----- Name interactions: click-to-copy + hover tooltip + JSON carets

        _setupNameInteractions() {
            // Click on a name → copy to clipboard + toast.
            this._bodyEl.addEventListener("click", (e) => {
                const t = e.target as HTMLElement;
                const expandBtn = t.closest(".diagnostics-row-expand-btn") as HTMLElement | null;
                if (expandBtn) {
                    const rowKey = expandBtn.dataset.rowKey;
                    if (rowKey) this._expandRow(rowKey);
                    e.stopPropagation();
                    return;
                }
                const collapseBtn = t.closest(".diagnostics-row-collapse-btn") as HTMLElement | null;
                if (collapseBtn) {
                    const rowKey = collapseBtn.dataset.rowKey;
                    if (rowKey) this._collapseRow(rowKey);
                    e.stopPropagation();
                    return;
                }
                const caret = t.closest(".diagnostics-json-caret") as HTMLElement | null;
                if (caret) {
                    const pathKey = caret.dataset.pathKey;
                    if (pathKey) {
                        this._jsonExpansion.set(pathKey, !this._jsonExpansion.get(pathKey));
                        if (this._lastSnapshot) this._render(this._lastSnapshot);
                    }
                    e.stopPropagation();
                    return;
                }
                const nameEl = t.closest(".diagnostics-row-name") as HTMLElement | null;
                if (!nameEl) return;
                const name = nameEl.dataset.name;
                if (!name) return;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(name).then(() => {
                        if (showToast) showToast("Copied: " + name, "success", 2000);
                    }).catch(() => {
                        if (showToast) showToast("Failed to copy name", "error", 2000);
                    });
                }
            });

            // Hover on a name → custom tooltip with full name + metadata.
            // Hover on a truncated scalar value → tooltip with the full value.
            this._bodyEl.addEventListener("pointerover", (e: PointerEvent) => {
                const t = e.target as HTMLElement;
                const nameEl = t.closest(".diagnostics-row-name") as HTMLElement | null;
                if (nameEl) {
                    const truncated = nameEl.scrollWidth > nameEl.clientWidth;
                    const meta = nameEl.dataset.meta || "";
                    if (!truncated && !meta) {
                        this._hideNameTooltip();
                        return;
                    }
                    this._showNameTooltip(nameEl, e);
                    return;
                }
                const valueEl = t.closest(".diagnostics-row-value[data-full]") as HTMLElement | null;
                if (valueEl) {
                    const full = valueEl.dataset.full || "";
                    const cssTrunc = valueEl.scrollWidth > valueEl.clientWidth;
                    const strTrunc = full && full !== valueEl.textContent;
                    if (cssTrunc || strTrunc) {
                        this._showValueTooltip(valueEl, e);
                        return;
                    }
                }
                this._hideNameTooltip();
            });

            this._bodyEl.addEventListener("pointerout", (e: PointerEvent) => {
                const t = e.target as HTMLElement;
                const target = t.closest(".diagnostics-row-name, .diagnostics-row-value[data-full]");
                if (!target) return;
                const related = e.relatedTarget as Node | null;
                if (related && target.contains(related)) return;
                this._hideNameTooltip();
            });

            this._bodyEl.addEventListener("pointermove", (e: PointerEvent) => {
                if (!this._nameTooltipEl || this._nameTooltipEl.style.display === "none") return;
                if (!(e.target as HTMLElement).closest(".diagnostics-row-name, .diagnostics-row-value[data-full]")) return;
                this._positionNameTooltip(e);
            });
        }

        _showNameTooltip(nameEl: HTMLElement, evt: MouseEvent) {
            const tt = this._nameTooltipEl;
            if (!tt) return;
            const name = nameEl.dataset.name || nameEl.textContent;
            const meta = nameEl.dataset.meta || "";
            let html = `<div class="diagnostics-name-tooltip-name">${escapeHtml(name)}</div>`;
            if (meta) {
                html += `<div class="diagnostics-name-tooltip-meta">${escapeHtml(meta)}</div>`;
            }
            tt.innerHTML = html;
            // Always render just above this panel's current stacking position —
            // the panel's z-index shifts as WindowManager re-ranks focused windows.
            tt.style.zIndex = String(this._floatingWindow.getZIndex() + 1);
            tt.style.display = "block";
            this._positionNameTooltip(evt);
        }

        _showValueTooltip(scalarEl: HTMLElement, evt: MouseEvent) {
            const tt = this._nameTooltipEl;
            if (!tt) return;
            const full = scalarEl.dataset.full || "";
            if (!full) return;
            tt.innerHTML = `<div class="diagnostics-name-tooltip-value">${escapeHtml(full)}</div>`;
            tt.style.zIndex = String(this._floatingWindow.getZIndex() + 1);
            tt.style.display = "block";
            this._positionNameTooltip(evt);
        }

        _hideNameTooltip() {
            if (this._nameTooltipEl) this._nameTooltipEl.style.display = "none";
        }

        _positionNameTooltip(evt: MouseEvent) {
            const tt = this._nameTooltipEl;
            if (!tt) return;
            const offset = 14;
            const rect = tt.getBoundingClientRect();
            const vw = window.innerWidth;
            const vh = window.innerHeight;
            let x = evt.clientX + offset;
            let y = evt.clientY + offset;
            if (x + rect.width > vw - 8) x = evt.clientX - rect.width - offset;
            if (y + rect.height > vh - 8) y = vh - rect.height - 8;
            if (x < 4) x = 4;
            if (y < 4) y = 4;
            tt.style.left = x + "px";
            tt.style.top = y + "px";
        }

        // ----- Visibility ---------------------------------------------------

        toggle() {
            if (this._visible) {
                this.hide();
            } else {
                this.show();
            }
        }

        show() {
            this._visible = true;
            this._floatingWindow.setHidden(false);
            this._refreshTrainList().then(() => {
                this._startPolling();
            });
            this._persistState();
        }

        hide() {
            this._visible = false;
            this._floatingWindow.setHidden(true);
            this._hideNameTooltip();
            this._stopPolling();
            this._persistState();
        }

        // ----- State persistence -------------------------------------------

        _persistState() {
            const geo = this._floatingWindow.getGeometry();
            saveState({
                visible: this._visible,
                trainId: this._currentTrainId,
                refreshHz: this._refreshHz,
                x: geo.left ?? undefined,
                y: geo.top ?? undefined,
                width: geo.width ?? undefined,
                height: geo.height ?? undefined,
                sortMode: this._sortMode,
                collapsedGroups: Array.from(this._collapsedGroups),
            });
        }

        // ----- Train list ---------------------------------------------------

        async _refreshTrainList() {
            if (!api || typeof api.getNetwork !== "function") {
                return;
            }
            try {
                const network = await api.getNetwork();
                const trains: any[] = (network && Array.isArray(network.trains)) ? network.trains : [];
                this._knownTrainIds = trains
                    .map((t) => t.id || t.train_id)
                    .filter((id): id is string => !!id);

                const previous = this._currentTrainId;
                this._trainSelectEl.innerHTML = '<option value="">(select a train)</option>' +
                    this._knownTrainIds.map(id => `<option value="${id}">${id}</option>`).join("");

                if (previous && this._knownTrainIds.includes(previous)) {
                    this._trainSelectEl.value = previous;
                } else if (this._knownTrainIds.length > 0 && !previous) {
                    // Don't auto-select; let the user pick.
                    this._trainSelectEl.value = "";
                }
            } catch (e) {
                console.warn("[Diagnostics] failed to load train list:", e);
            }
        }

        // ----- Polling lifecycle -------------------------------------------

        _onTrainChange(newId: string | null) {
            this._stopPolling();
            this._currentTrainId = newId || null;
            this._lastSnapshot = null;
            this._persistState();
            if (this._currentTrainId && this._visible) {
                this._pollOnce();
                this._startPolling();
            } else {
                this._renderEmpty("Select a train to begin.");
            }
        }

        _onRateChange(hz: number) {
            this._refreshHz = hz;
            this._persistState();
            this._stopPolling();
            if (this._visible && this._currentTrainId) {
                this._startPolling();
            }
        }

        _startPolling() {
            this._stopPolling();
            if (!this._visible || !this._currentTrainId) return;
            // 0 Hz means on-demand only — caller uses the manual refresh button.
            if (this._refreshHz <= 0) {
                this._pollOnce();
                return;
            }
            const intervalMs = Math.max(50, Math.round(1000 / this._refreshHz));
            this._pollOnce();
            this._pollHandle = setInterval(() => this._pollOnce(), intervalMs);
        }

        _stopPolling() {
            if (this._pollHandle) {
                clearInterval(this._pollHandle);
                this._pollHandle = null;
            }
            if (this._abortController) {
                try { this._abortController.abort(); } catch (e) { /* noop */ }
                this._abortController = null;
            }
            this._inflight = false;
        }

        async _pollOnce() {
            if (!this._currentTrainId) {
                this._renderEmpty("Select a train to begin.");
                return;
            }
            if (this._inflight) return; // skip while previous is in flight
            if (!api || typeof api.getInspectorEntityTrain !== "function") {
                this._renderError(new Error("API not available"));
                return;
            }

            this._inflight = true;
            this._abortController = new AbortController();
            try {
                const data = await api.getInspectorEntityTrain(this._currentTrainId);
                if (data === null) {
                    this._renderTrainGone();
                    this._stopPolling();
                    return;
                }
                this._render(data as InspectorSnapshot);
            } catch (e) {
                this._renderError(e);
            } finally {
                this._inflight = false;
            }
        }

        // ----- Rendering ----------------------------------------------------

        _renderEmpty(text: string) {
            this._bodyEl.innerHTML = `<div class="empty-state">${text}</div>`;
            this._footerEl.textContent = "";
        }

        _renderTrainGone() {
            this._bodyEl.innerHTML =
                '<div class="empty-state empty-state-warn">Train no longer exists.</div>';
            this._footerEl.textContent = "";
        }

        _renderError(err: unknown) {
            const msg = (err as { message?: string })?.message || String(err);
            this._bodyEl.innerHTML =
                `<div class="empty-state empty-state-error">Error: ${msg}</div>`;
        }

        _render(snapshot: InspectorSnapshot) {
            // The tooltip references DOM nodes that are about to be replaced.
            this._hideNameTooltip();

            // Build per-row index from previous snapshot for diff purposes.
            const prevIndex = this._indexSnapshot(this._lastSnapshot);

            const fragments = [];
            for (const group of (snapshot.groups || [])) {
                fragments.push(this._renderGroup(group, prevIndex));
            }
            this._bodyEl.innerHTML = fragments.join("");

            // Wire group collapse/expand.
            this._bodyEl.querySelectorAll(".diagnostics-group-header").forEach((hdr) => {
                hdr.addEventListener("click", () => {
                    const groupName = (hdr as HTMLElement).dataset.groupName!;
                    if (this._collapsedGroups.has(groupName)) {
                        this._collapsedGroups.delete(groupName);
                    } else {
                        this._collapsedGroups.add(groupName);
                    }
                    this._persistState();
                    this._render(snapshot); // re-render to apply new collapse state
                });
            });

            // Footer with timestamp + train label.
            const ts = snapshot.fetched_at
                ? new Date(snapshot.fetched_at * 1000).toLocaleTimeString()
                : "";
            this._footerEl.textContent =
                `${snapshot.label || snapshot.id || "?"} · fetched ${ts}`;

            this._lastSnapshot = snapshot;
        }

        _indexSnapshot(snapshot: InspectorSnapshot | null) {
            const out = new Map();
            if (!snapshot || !Array.isArray(snapshot.groups)) return out;
            for (const g of snapshot.groups) {
                for (const it of (g.items || [])) {
                    out.set(`${g.name}::${it.name}`, it);
                }
            }
            return out;
        }

        _renderGroup(group: InspectorGroup, prevIndex: Map<string, InspectorItem>) {
            const collapsed = this._collapsedGroups.has(group.name);
            const items = this._applySortOrder(group.items || []);
            const rows = collapsed
                ? ""
                : items.map((it: InspectorItem) => {
                    const prev = prevIndex.get(`${group.name}::${it.name}`);
                    return this._renderItem(it, group, prev);
                }).join("");
            return `
                <div class="diagnostics-group ${collapsed ? "collapsed" : ""}">
                    <div class="diagnostics-group-header" data-group-name="${escapeHtml(group.name)}">
                        <span class="diagnostics-group-caret">${collapsed ? "▸" : "▾"}</span>
                        <span class="diagnostics-group-name">${escapeHtml(group.name)}</span>
                        <span class="chip">${items.length}</span>
                    </div>
                    <div class="diagnostics-group-items">${rows}</div>
                </div>
            `;
        }

        _renderItem(item: InspectorItem, group: InspectorGroup, prev: InspectorItem | undefined) {
            const errored = item.error !== undefined && item.error !== null;
            const valueChanged = prev !== undefined && !errored
                && JSON.stringify(prev.value) !== JSON.stringify(item.value);

            const structured = !errored && isContainer(item.value);
            const rowKey = `${group.name}::${item.name}`;
            const treeBtns = structured
                ? `<button class="diagnostics-row-expand-btn" type="button" data-row-key="${escapeHtml(rowKey)}" title="Expand all in this field">⊞</button>` +
                  `<button class="diagnostics-row-collapse-btn" type="button" data-row-key="${escapeHtml(rowKey)}" title="Collapse all in this field">⊟</button>`
                : "";

            let valueHtml;
            let rowValueAttrs = "";
            if (errored) {
                valueHtml = escapeHtml(item.error);
            } else if (structured) {
                valueHtml = `${treeBtns}${this._renderJsonValue(item.value, rowKey, "root")}`;
            } else {
                const full = fmtValue(item.value);
                const short = shortValue(item.value);
                valueHtml = `<span class="diagnostics-json-scalar">${escapeHtml(short)}</span>`;
                rowValueAttrs = ` data-full="${escapeHtml(full)}"`;
            }

            // Metadata shown inside the custom name tooltip.
            const metaParts = [];
            if (item.computed_from) {
                metaParts.push("computed_from: " + item.computed_from.join(", "));
            }
            if (item.formula_doc) metaParts.push(item.formula_doc);
            if (item.writer) metaParts.push("writer: " + item.writer);
            if (item.consumer) metaParts.push("consumer: " + item.consumer);
            if (item.type) metaParts.push("type: " + item.type);
            const meta = metaParts.join("\n");

            const classes = ["diagnostics-row"];
            if (errored) classes.push("diagnostics-row-error");
            if (valueChanged) classes.push("diagnostics-row-flash");
            if (structured) classes.push("diagnostics-row-structured");

            return `
                <div class="${classes.join(" ")}" data-source="${escapeHtml(item.source || "")}">
                    <span class="diagnostics-row-name"
                          data-name="${escapeHtml(item.name)}"
                          ${meta ? `data-meta="${escapeHtml(meta)}"` : ""}>${escapeHtml(item.name)}</span>
                    <span class="diagnostics-row-source-badge" data-source="${escapeHtml(item.source || "")}">${escapeHtml(item.source || "")}</span>
                    <span class="diagnostics-row-value"${rowValueAttrs}>${valueHtml}</span>
                </div>
            `;
        }

        // ----- JSON tree rendering -----------------------------------------

        _renderJsonValue(value: unknown, rowKey: string, path: string): string {
            if (value === null) {
                return `<span class="diagnostics-json-null">null</span>`;
            }
            if (value === undefined) {
                return `<span class="diagnostics-json-null">undefined</span>`;
            }
            const t = typeof value;
            if (t === "string") {
                return `<span class="diagnostics-json-string">"${escapeHtml(value)}"</span>`;
            }
            if (t === "number") {
                return `<span class="diagnostics-json-number">${escapeHtml(String(value))}</span>`;
            }
            if (t === "boolean") {
                return `<span class="diagnostics-json-bool">${value}</span>`;
            }
            if (Array.isArray(value)) {
                return this._renderJsonContainer(value, rowKey, path, true);
            }
            if (t === "object") {
                return this._renderJsonContainer(value, rowKey, path, false);
            }
            return `<span class="diagnostics-json-scalar">${escapeHtml(String(value))}</span>`;
        }

        _renderJsonContainer(value: object, rowKey: string, path: string, isArray: boolean): string {
            const entries = isArray
                ? (value as unknown[]).map((v: unknown, i: number) => [i, v] as [number, unknown])
                : Object.entries(value);
            const len = entries.length;
            if (len === 0) {
                return `<span class="diagnostics-json-empty">${isArray ? "[]" : "{}"}</span>`;
            }

            const pathKey = `${rowKey}::${path}`;
            const expanded = !!this._jsonExpansion.get(pathKey);
            const caret = `<span class="diagnostics-json-caret" data-path-key="${escapeHtml(pathKey)}">${expanded ? "▾" : "▸"}</span>`;

            if (!expanded) {
                const summary = containerSummary(value);
                return `<span class="diagnostics-json-node">${caret}<span class="diagnostics-json-summary">${escapeHtml(summary)}</span></span>`;
            }

            const open = isArray ? "[" : "{";
            const close = isArray ? "]" : "}";
            const childPath = (k: string | number) => isArray ? `${path}[${k}]` : `${path}.${k}`;
            const rows = entries.map(([k, v]: [string | number, unknown]) => {
                const keyHtml = isArray
                    ? `<span class="diagnostics-json-index">${k}:</span>`
                    : `<span class="diagnostics-json-key">"${escapeHtml(String(k))}":</span>`;
                return `<div class="diagnostics-json-child">${keyHtml} ${this._renderJsonValue(v, rowKey, childPath(k))}</div>`;
            }).join("");

            return `<span class="diagnostics-json-node diagnostics-json-node-expanded">${caret}<span class="diagnostics-json-bracket">${open}</span><div class="diagnostics-json-children">${rows}</div><span class="diagnostics-json-bracket">${close}</span></span>`;
        }

        _walkContainerPaths(value: unknown, rowKey: string, path: string, callback: (pathKey: string) => void) {
            if (!isContainer(value)) return;
            const pathKey = `${rowKey}::${path}`;
            callback(pathKey);
            if (Array.isArray(value)) {
                for (let i = 0; i < value.length; i++) {
                    this._walkContainerPaths(value[i], rowKey, `${path}[${i}]`, callback);
                }
            } else {
                const obj = value as Record<string, unknown>;
                for (const k of Object.keys(obj)) {
                    this._walkContainerPaths(obj[k], rowKey, `${path}.${k}`, callback);
                }
            }
        }

        _expandAll() {
            if (!this._lastSnapshot) return;
            for (const group of (this._lastSnapshot.groups || [])) {
                for (const item of (group.items || [])) {
                    if (!isContainer(item.value)) continue;
                    const rowKey = `${group.name}::${item.name}`;
                    this._walkContainerPaths(item.value, rowKey, "root", (pk: string) => {
                        this._jsonExpansion.set(pk, true);
                    });
                }
            }
            this._render(this._lastSnapshot);
        }

        _expandRow(rowKey: string) {
            if (!this._lastSnapshot) return;
            for (const group of (this._lastSnapshot.groups || [])) {
                for (const item of (group.items || [])) {
                    if (`${group.name}::${item.name}` !== rowKey) continue;
                    if (!isContainer(item.value)) return;
                    this._walkContainerPaths(item.value, rowKey, "root", (pk: string) => {
                        this._jsonExpansion.set(pk, true);
                    });
                    this._render(this._lastSnapshot);
                    return;
                }
            }
        }

        _collapseRow(rowKey: string) {
            if (!this._lastSnapshot) return;
            for (const group of (this._lastSnapshot.groups || [])) {
                for (const item of (group.items || [])) {
                    if (`${group.name}::${item.name}` !== rowKey) continue;
                    if (!isContainer(item.value)) return;
                    this._walkContainerPaths(item.value, rowKey, "root", (pk: string) => {
                        this._jsonExpansion.delete(pk);
                    });
                    this._render(this._lastSnapshot);
                    return;
                }
            }
        }

        _collapseAll() {
            if (this._jsonExpansion.size === 0) return;
            this._jsonExpansion.clear();
            if (this._lastSnapshot) this._render(this._lastSnapshot);
        }

        // ----- Sort ---------------------------------------------------------

        _applySortOrder(items: InspectorItem[]): InspectorItem[] {
            if (!this._sortMode) return items;
            const sorted = items.slice();
            sorted.sort((a: InspectorItem, b: InspectorItem) => {
                const an = String(a.name || "").toLowerCase();
                const bn = String(b.name || "").toLowerCase();
                if (an < bn) return -1;
                if (an > bn) return 1;
                return 0;
            });
            if (this._sortMode === "desc") sorted.reverse();
            return sorted;
        }

        _cycleSortMode() {
            const next = this._sortMode === null
                ? "asc"
                : (this._sortMode === "asc" ? "desc" : null);
            this._sortMode = next;
            this._updateSortBtn();
            this._persistState();
            if (this._lastSnapshot) this._render(this._lastSnapshot);
        }

        _updateSortBtn() {
            if (!this._sortBtnEl) return;
            const m = this._sortMode;
            if (m === "asc") {
                this._sortBtnEl.textContent = "A↓";
                this._sortBtnEl.title = "Items sorted A→Z. Click for Z→A.";
                this._sortBtnEl.classList.add("active");
            } else if (m === "desc") {
                this._sortBtnEl.textContent = "Z↓";
                this._sortBtnEl.title = "Items sorted Z→A. Click to restore default order.";
                this._sortBtnEl.classList.add("active");
            } else {
                this._sortBtnEl.textContent = "⇅";
                this._sortBtnEl.title = "Items in default (curated) order. Click to sort A→Z.";
                this._sortBtnEl.classList.remove("active");
            }
        }
    }

    function escapeHtml(s: unknown): string {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function bootstrap() {
        if (window.diagnosticsPanel) return; // idempotent
        if (logsPanelReplacesWindows()) return; // ?ui=next: the Logs panel's Diagnostics tab
        window.diagnosticsPanel = new DiagnosticsPanel();
        window.diagnosticsPanel.init();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();

// Marks this file as an ES module so Vite can `import` it for the bootstrap
// side effect above (the IIFE self-installs window.diagnosticsPanel).
export {};
