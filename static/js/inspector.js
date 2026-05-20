/**
 * InspectorPanel — Phase 1 (Train + TrainController only).
 *
 * Floating, draggable, read-only debug panel that polls
 * GET /api/inspector/entity/train/<train_id> on a configurable cadence
 * and displays the 4 groups returned by the backend, with per-row diff
 * highlighting and tooltips for derived-property formulas.
 *
 * Phase 2+ will add Path / Block / Switch / Route inspectors. This file
 * is structured so that a future entity selector slots in without breaking
 * the train-only path.
 */
(function () {
    "use strict";

    const STATE_KEY = "inspectorPanelStateV1";
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

    function clampWidth(w) {
        const max = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, window.innerWidth - 40));
        return Math.max(MIN_WIDTH, Math.min(max, w));
    }

    function loadState() {
        try {
            const raw = localStorage.getItem(STATE_KEY);
            if (!raw) return {};
            return JSON.parse(raw) || {};
        } catch (e) {
            return {};
        }
    }

    function saveState(state) {
        try {
            localStorage.setItem(STATE_KEY, JSON.stringify(state));
        } catch (e) {
            // ignore quota / disabled storage
        }
    }

    function fmtValue(value) {
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

    function shortValue(value) {
        const full = fmtValue(value);
        if (full.length <= 80) return full;
        return full.slice(0, 78) + "…";
    }

    function isContainer(v) {
        return v !== null && typeof v === "object";
    }

    function containerSummary(value) {
        if (Array.isArray(value)) {
            return `[${value.length}]`;
        }
        const keys = Object.keys(value);
        if (keys.length === 0) return "{}";
        const preview = keys.slice(0, 3).join(", ");
        const tail = keys.length > 3 ? ", …" : "";
        return `{${keys.length}} ${preview}${tail}`;
    }

    class InspectorPanel {
        constructor() {
            this._panelEl = null;
            this._bodyEl = null;
            this._trainSelectEl = null;
            this._rateSelectEl = null;
            this._refreshBtnEl = null;

            this._pollHandle = null;
            this._inflight = false;
            this._abortController = null;

            this._lastSnapshot = null; // last rendered snapshot, used for diff
            this._collapsedGroups = new Set();
            this._currentTrainId = null;
            this._refreshHz = 1;
            this._visible = false;
            this._dragOffset = null;
            this._position = null; // {x, y}

            this._knownTrainIds = []; // cached from getNetwork()

            this._jsonExpansion = new Map(); // pathKey -> bool (in-memory only)
            this._width = DEFAULT_WIDTH;
            this._nameTooltipEl = null;
            this._resizing = false;
            this._expandAllBtnEl = null;
            this._collapseAllBtnEl = null;
            this._resizeHandleEl = null;
            this._sortMode = "asc"; // null | "asc" | "desc"; default A→Z
            this._sortBtnEl = null;
        }

        init() {
            this._buildDom();
            this._restoreState();
            this._setupDrag();
            this._setupResize();
            this._setupNameInteractions();
            this._wireToggleButton();
            this._wireSelectors();

            // First train list fetch (best-effort; ignore errors)
            this._refreshTrainList().then(() => {
                if (this._visible) this._startPolling();
            });
        }

        // ----- DOM construction --------------------------------------------

        _buildDom() {
            const panel = document.createElement("div");
            panel.className = "inspector-panel hidden";
            panel.id = "inspector-panel";
            panel.innerHTML = `
                <div class="inspector-panel-header" data-role="drag-handle">
                    <span class="inspector-panel-title">Inspector</span>
                    <button class="inspector-panel-close" type="button" title="Close">×</button>
                </div>
                <div class="inspector-panel-toolbar">
                    <label class="inspector-panel-field">
                        Train
                        <select class="inspector-panel-train-select" data-role="train-select"></select>
                    </label>
                    <label class="inspector-panel-field">
                        Refresh
                        <select class="inspector-panel-rate-select" data-role="rate-select"></select>
                    </label>
                    <div class="inspector-panel-tools">
                        <button class="inspector-panel-refresh-btn" type="button" data-role="refresh" title="Refresh now">↻</button>
                        <button class="inspector-panel-tree-btn" type="button" data-role="expand-all" title="Expand all values">⊞</button>
                        <button class="inspector-panel-tree-btn" type="button" data-role="collapse-all" title="Collapse all values">⊟</button>
                        <button class="inspector-panel-sort-btn" type="button" data-role="sort">⇅</button>
                    </div>
                </div>
                <div class="inspector-panel-body" data-role="body">
                    <div class="inspector-panel-empty">Select a train to begin.</div>
                </div>
                <div class="inspector-panel-footer" data-role="footer"></div>
                <div class="inspector-resize-handle" data-role="resize-handle"></div>
            `;
            document.body.appendChild(panel);

            this._panelEl = panel;
            this._bodyEl = panel.querySelector('[data-role="body"]');
            this._trainSelectEl = panel.querySelector('[data-role="train-select"]');
            this._rateSelectEl = panel.querySelector('[data-role="rate-select"]');
            this._refreshBtnEl = panel.querySelector('[data-role="refresh"]');
            this._closeBtnEl = panel.querySelector('.inspector-panel-close');
            this._footerEl = panel.querySelector('[data-role="footer"]');
            this._expandAllBtnEl = panel.querySelector('[data-role="expand-all"]');
            this._collapseAllBtnEl = panel.querySelector('[data-role="collapse-all"]');
            this._sortBtnEl = panel.querySelector('[data-role="sort"]');
            this._resizeHandleEl = panel.querySelector('[data-role="resize-handle"]');

            const tooltip = document.createElement("div");
            tooltip.className = "inspector-name-tooltip";
            tooltip.style.display = "none";
            document.body.appendChild(tooltip);
            this._nameTooltipEl = tooltip;

            // Populate refresh-rate dropdown.
            this._rateSelectEl.innerHTML = REFRESH_OPTIONS
                .map(o => `<option value="${o.hz}">${o.label}</option>`)
                .join("");
        }

        _wireToggleButton() {
            const btn = document.getElementById("btn-inspector-toggle");
            if (btn) {
                btn.addEventListener("click", () => this.toggle());
            }
            this._closeBtnEl.addEventListener("click", () => this.hide());
        }

        _wireSelectors() {
            this._trainSelectEl.addEventListener("change", (e) => {
                this._onTrainChange(e.target.value || null);
            });
            this._rateSelectEl.addEventListener("change", (e) => {
                const hz = Number(e.target.value);
                this._onRateChange(Number.isFinite(hz) ? hz : 1);
            });
            this._refreshBtnEl.addEventListener("click", () => {
                this._pollOnce();
            });
            this._expandAllBtnEl.addEventListener("click", () => this._expandAll());
            this._collapseAllBtnEl.addEventListener("click", () => this._collapseAll());
            this._sortBtnEl.addEventListener("click", () => this._cycleSortMode());
        }

        // ----- Drag ---------------------------------------------------------

        _setupDrag() {
            const handle = this._panelEl.querySelector('[data-role="drag-handle"]');

            handle.addEventListener("pointerdown", (e) => {
                if (e.target.closest("button, select, input, textarea")) return;
                handle.setPointerCapture(e.pointerId);
                const rect = this._panelEl.getBoundingClientRect();
                this._dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
                this._panelEl.classList.add("inspector-panel-dragging");
            });

            handle.addEventListener("pointermove", (e) => {
                if (!this._dragOffset) return;
                const x = e.clientX - this._dragOffset.x;
                const y = e.clientY - this._dragOffset.y;
                const clampedX = Math.max(0, Math.min(window.innerWidth - 80, x));
                const clampedY = Math.max(0, Math.min(window.innerHeight - 40, y));
                this._panelEl.style.left = clampedX + "px";
                this._panelEl.style.top = clampedY + "px";
                this._panelEl.style.right = "auto";
                this._position = { x: clampedX, y: clampedY };
            });

            const endDrag = () => {
                if (!this._dragOffset) return;
                this._dragOffset = null;
                this._panelEl.classList.remove("inspector-panel-dragging");
                this._persistState();
            };
            handle.addEventListener("pointerup", endDrag);
            handle.addEventListener("pointercancel", endDrag);
        }

        // ----- Resize -------------------------------------------------------

        _setupResize() {
            const handle = this._resizeHandleEl;
            if (!handle) return;
            let startX = 0;
            let startWidth = 0;

            handle.addEventListener("pointerdown", (e) => {
                e.stopPropagation();
                e.preventDefault();
                handle.setPointerCapture(e.pointerId);
                this._resizing = true;
                startX = e.clientX;
                startWidth = this._panelEl.getBoundingClientRect().width;
                this._panelEl.classList.add("inspector-panel-resizing");
            });

            handle.addEventListener("pointermove", (e) => {
                if (!this._resizing) return;
                const newWidth = clampWidth(startWidth + (e.clientX - startX));
                this._panelEl.style.width = newWidth + "px";
                this._width = newWidth;
            });

            const endResize = () => {
                if (!this._resizing) return;
                this._resizing = false;
                this._panelEl.classList.remove("inspector-panel-resizing");
                this._persistState();
            };
            handle.addEventListener("pointerup", endResize);
            handle.addEventListener("pointercancel", endResize);
        }

        // ----- Viewport clamp ----------------------------------------------

        _clampPositionToViewport() {
            if (!this._position) return; // using default CSS positioning (right: 20px)
            const margin = 8;
            const rect = this._panelEl.getBoundingClientRect();
            const width = rect.width || this._width || DEFAULT_WIDTH;
            const height = rect.height;

            let x = this._position.x;
            let y = this._position.y;

            if (x + width > window.innerWidth - margin) {
                x = window.innerWidth - width - margin;
            }
            if (x < margin) x = margin;

            if (height > 0 && y + height > window.innerHeight - margin) {
                y = window.innerHeight - height - margin;
            }
            if (y < margin) y = margin;

            if (x !== this._position.x || y !== this._position.y) {
                this._position = { x, y };
                this._panelEl.style.left = x + "px";
                this._panelEl.style.top = y + "px";
                this._panelEl.style.right = "auto";
                this._persistState();
            }
        }

        // ----- Name interactions: click-to-copy + hover tooltip + JSON carets

        _setupNameInteractions() {
            // Click on a name → copy to clipboard + toast.
            this._bodyEl.addEventListener("click", (e) => {
                const expandBtn = e.target.closest(".inspector-row-expand-btn");
                if (expandBtn) {
                    const rowKey = expandBtn.dataset.rowKey;
                    if (rowKey) this._expandRow(rowKey);
                    e.stopPropagation();
                    return;
                }
                const collapseBtn = e.target.closest(".inspector-row-collapse-btn");
                if (collapseBtn) {
                    const rowKey = collapseBtn.dataset.rowKey;
                    if (rowKey) this._collapseRow(rowKey);
                    e.stopPropagation();
                    return;
                }
                const caret = e.target.closest(".inspector-json-caret");
                if (caret) {
                    const pathKey = caret.dataset.pathKey;
                    if (pathKey) {
                        this._jsonExpansion.set(pathKey, !this._jsonExpansion.get(pathKey));
                        if (this._lastSnapshot) this._render(this._lastSnapshot);
                    }
                    e.stopPropagation();
                    return;
                }
                const nameEl = e.target.closest(".inspector-row-name");
                if (!nameEl) return;
                const name = nameEl.dataset.name;
                if (!name) return;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(name).then(() => {
                        if (window.showToast) window.showToast("Copied: " + name, "success", 2000);
                    }).catch(() => {
                        if (window.showToast) window.showToast("Failed to copy name", "error", 2000);
                    });
                }
            });

            // Hover on a name → custom tooltip with full name + metadata.
            // Hover on a truncated scalar value → tooltip with the full value.
            this._bodyEl.addEventListener("pointerover", (e) => {
                const nameEl = e.target.closest(".inspector-row-name");
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
                const valueEl = e.target.closest(".inspector-row-value[data-full]");
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

            this._bodyEl.addEventListener("pointerout", (e) => {
                const target = e.target.closest(".inspector-row-name, .inspector-row-value[data-full]");
                if (!target) return;
                const related = e.relatedTarget;
                if (related && target.contains(related)) return;
                this._hideNameTooltip();
            });

            this._bodyEl.addEventListener("pointermove", (e) => {
                if (!this._nameTooltipEl || this._nameTooltipEl.style.display === "none") return;
                if (!e.target.closest(".inspector-row-name, .inspector-row-value[data-full]")) return;
                this._positionNameTooltip(e);
            });
        }

        _showNameTooltip(nameEl, evt) {
            const tt = this._nameTooltipEl;
            if (!tt) return;
            const name = nameEl.dataset.name || nameEl.textContent;
            const meta = nameEl.dataset.meta || "";
            let html = `<div class="inspector-name-tooltip-name">${escapeHtml(name)}</div>`;
            if (meta) {
                html += `<div class="inspector-name-tooltip-meta">${escapeHtml(meta)}</div>`;
            }
            tt.innerHTML = html;
            tt.style.display = "block";
            this._positionNameTooltip(evt);
        }

        _showValueTooltip(scalarEl, evt) {
            const tt = this._nameTooltipEl;
            if (!tt) return;
            const full = scalarEl.dataset.full || "";
            if (!full) return;
            tt.innerHTML = `<div class="inspector-name-tooltip-value">${escapeHtml(full)}</div>`;
            tt.style.display = "block";
            this._positionNameTooltip(evt);
        }

        _hideNameTooltip() {
            if (this._nameTooltipEl) this._nameTooltipEl.style.display = "none";
        }

        _positionNameTooltip(evt) {
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
            this._panelEl.classList.remove("hidden");
            this._clampPositionToViewport();
            this._refreshTrainList().then(() => {
                this._startPolling();
            });
            this._persistState();
        }

        hide() {
            this._visible = false;
            this._panelEl.classList.add("hidden");
            this._hideNameTooltip();
            this._stopPolling();
            this._persistState();
        }

        // ----- State persistence -------------------------------------------

        _restoreState() {
            const s = loadState();
            this._visible = !!s.visible;
            this._currentTrainId = s.trainId || null;
            this._refreshHz = (s.refreshHz === 0 || s.refreshHz)
                ? Number(s.refreshHz)
                : 1;
            this._collapsedGroups = new Set(Array.isArray(s.collapsedGroups) ? s.collapsedGroups : []);

            if (s.x !== undefined && s.y !== undefined) {
                this._position = { x: Number(s.x), y: Number(s.y) };
                this._panelEl.style.left = this._position.x + "px";
                this._panelEl.style.top = this._position.y + "px";
                this._panelEl.style.right = "auto";
            }

            if (typeof s.width === "number" && s.width > 0) {
                this._width = clampWidth(Number(s.width));
                this._panelEl.style.width = this._width + "px";
            }

            if (s.sortMode === "asc" || s.sortMode === "desc" || s.sortMode === null) {
                this._sortMode = s.sortMode;
            } else {
                this._sortMode = "asc";
            }
            this._updateSortBtn();

            this._rateSelectEl.value = String(this._refreshHz);
            if (this._visible) {
                this._panelEl.classList.remove("hidden");
                this._clampPositionToViewport();
            }
        }

        _persistState() {
            saveState({
                visible: this._visible,
                trainId: this._currentTrainId,
                refreshHz: this._refreshHz,
                x: this._position ? this._position.x : undefined,
                y: this._position ? this._position.y : undefined,
                width: this._width,
                sortMode: this._sortMode,
                collapsedGroups: Array.from(this._collapsedGroups),
            });
        }

        // ----- Train list ---------------------------------------------------

        async _refreshTrainList() {
            if (!window.api || typeof window.api.getNetwork !== "function") {
                return;
            }
            try {
                const network = await window.api.getNetwork();
                const trains = (network && Array.isArray(network.trains)) ? network.trains : [];
                this._knownTrainIds = trains.map(t => t.id || t.train_id).filter(Boolean);

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
                console.warn("[Inspector] failed to load train list:", e);
            }
        }

        // ----- Polling lifecycle -------------------------------------------

        _onTrainChange(newId) {
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

        _onRateChange(hz) {
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
            if (!window.api || typeof window.api.getInspectorEntityTrain !== "function") {
                this._renderError(new Error("API not available"));
                return;
            }

            this._inflight = true;
            this._abortController = new AbortController();
            try {
                const data = await window.api.getInspectorEntityTrain(this._currentTrainId);
                if (data === null) {
                    this._renderTrainGone();
                    this._stopPolling();
                    return;
                }
                this._render(data);
            } catch (e) {
                this._renderError(e);
            } finally {
                this._inflight = false;
            }
        }

        // ----- Rendering ----------------------------------------------------

        _renderEmpty(text) {
            this._bodyEl.innerHTML = `<div class="inspector-panel-empty">${text}</div>`;
            this._footerEl.textContent = "";
        }

        _renderTrainGone() {
            this._bodyEl.innerHTML =
                '<div class="inspector-panel-empty inspector-panel-warn">Train no longer exists.</div>';
            this._footerEl.textContent = "";
        }

        _renderError(err) {
            const msg = (err && err.message) ? err.message : String(err);
            this._bodyEl.innerHTML =
                `<div class="inspector-panel-empty inspector-panel-error">Error: ${msg}</div>`;
        }

        _render(snapshot) {
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
            this._bodyEl.querySelectorAll(".inspector-group-header").forEach((hdr) => {
                hdr.addEventListener("click", () => {
                    const groupName = hdr.dataset.groupName;
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

        _indexSnapshot(snapshot) {
            const out = new Map();
            if (!snapshot || !Array.isArray(snapshot.groups)) return out;
            for (const g of snapshot.groups) {
                for (const it of (g.items || [])) {
                    out.set(`${g.name}::${it.name}`, it);
                }
            }
            return out;
        }

        _renderGroup(group, prevIndex) {
            const collapsed = this._collapsedGroups.has(group.name);
            const items = this._applySortOrder(group.items || []);
            const rows = collapsed
                ? ""
                : items.map(it => {
                    const prev = prevIndex.get(`${group.name}::${it.name}`);
                    return this._renderItem(it, group, prev);
                }).join("");
            return `
                <div class="inspector-group ${collapsed ? "collapsed" : ""}">
                    <div class="inspector-group-header" data-group-name="${escapeHtml(group.name)}">
                        <span class="inspector-group-caret">${collapsed ? "▸" : "▾"}</span>
                        <span class="inspector-group-name">${escapeHtml(group.name)}</span>
                        <span class="inspector-group-count">${items.length}</span>
                    </div>
                    <div class="inspector-group-items">${rows}</div>
                </div>
            `;
        }

        _renderItem(item, group, prev) {
            const errored = item.error !== undefined && item.error !== null;
            const valueChanged = prev !== undefined && !errored
                && JSON.stringify(prev.value) !== JSON.stringify(item.value);

            const structured = !errored && isContainer(item.value);
            const rowKey = `${group.name}::${item.name}`;
            const treeBtns = structured
                ? `<button class="inspector-row-expand-btn" type="button" data-row-key="${escapeHtml(rowKey)}" title="Expand all in this field">⊞</button>` +
                  `<button class="inspector-row-collapse-btn" type="button" data-row-key="${escapeHtml(rowKey)}" title="Collapse all in this field">⊟</button>`
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
                valueHtml = `<span class="inspector-json-scalar">${escapeHtml(short)}</span>`;
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

            const classes = ["inspector-row"];
            if (errored) classes.push("inspector-row-error");
            if (valueChanged) classes.push("inspector-row-flash");
            if (structured) classes.push("inspector-row-structured");

            return `
                <div class="${classes.join(" ")}" data-source="${escapeHtml(item.source || "")}">
                    <span class="inspector-row-name"
                          data-name="${escapeHtml(item.name)}"
                          ${meta ? `data-meta="${escapeHtml(meta)}"` : ""}>${escapeHtml(item.name)}</span>
                    <span class="inspector-row-source-badge" data-source="${escapeHtml(item.source || "")}">${escapeHtml(item.source || "")}</span>
                    <span class="inspector-row-value"${rowValueAttrs}>${valueHtml}</span>
                </div>
            `;
        }

        // ----- JSON tree rendering -----------------------------------------

        _renderJsonValue(value, rowKey, path) {
            if (value === null) {
                return `<span class="inspector-json-null">null</span>`;
            }
            if (value === undefined) {
                return `<span class="inspector-json-null">undefined</span>`;
            }
            const t = typeof value;
            if (t === "string") {
                return `<span class="inspector-json-string">"${escapeHtml(value)}"</span>`;
            }
            if (t === "number") {
                return `<span class="inspector-json-number">${escapeHtml(String(value))}</span>`;
            }
            if (t === "boolean") {
                return `<span class="inspector-json-bool">${value}</span>`;
            }
            if (Array.isArray(value)) {
                return this._renderJsonContainer(value, rowKey, path, true);
            }
            if (t === "object") {
                return this._renderJsonContainer(value, rowKey, path, false);
            }
            return `<span class="inspector-json-scalar">${escapeHtml(String(value))}</span>`;
        }

        _renderJsonContainer(value, rowKey, path, isArray) {
            const entries = isArray
                ? value.map((v, i) => [i, v])
                : Object.entries(value);
            const len = entries.length;
            if (len === 0) {
                return `<span class="inspector-json-empty">${isArray ? "[]" : "{}"}</span>`;
            }

            const pathKey = `${rowKey}::${path}`;
            const expanded = !!this._jsonExpansion.get(pathKey);
            const caret = `<span class="inspector-json-caret" data-path-key="${escapeHtml(pathKey)}">${expanded ? "▾" : "▸"}</span>`;

            if (!expanded) {
                const summary = containerSummary(value);
                return `<span class="inspector-json-node">${caret}<span class="inspector-json-summary">${escapeHtml(summary)}</span></span>`;
            }

            const open = isArray ? "[" : "{";
            const close = isArray ? "]" : "}";
            const childPath = (k) => isArray ? `${path}[${k}]` : `${path}.${k}`;
            const rows = entries.map(([k, v]) => {
                const keyHtml = isArray
                    ? `<span class="inspector-json-index">${k}:</span>`
                    : `<span class="inspector-json-key">"${escapeHtml(String(k))}":</span>`;
                return `<div class="inspector-json-child">${keyHtml} ${this._renderJsonValue(v, rowKey, childPath(k))}</div>`;
            }).join("");

            return `<span class="inspector-json-node inspector-json-node-expanded">${caret}<span class="inspector-json-bracket">${open}</span><div class="inspector-json-children">${rows}</div><span class="inspector-json-bracket">${close}</span></span>`;
        }

        _walkContainerPaths(value, rowKey, path, callback) {
            if (!isContainer(value)) return;
            const pathKey = `${rowKey}::${path}`;
            callback(pathKey);
            if (Array.isArray(value)) {
                for (let i = 0; i < value.length; i++) {
                    this._walkContainerPaths(value[i], rowKey, `${path}[${i}]`, callback);
                }
            } else {
                for (const k of Object.keys(value)) {
                    this._walkContainerPaths(value[k], rowKey, `${path}.${k}`, callback);
                }
            }
        }

        _expandAll() {
            if (!this._lastSnapshot) return;
            for (const group of (this._lastSnapshot.groups || [])) {
                for (const item of (group.items || [])) {
                    if (!isContainer(item.value)) continue;
                    const rowKey = `${group.name}::${item.name}`;
                    this._walkContainerPaths(item.value, rowKey, "root", (pk) => {
                        this._jsonExpansion.set(pk, true);
                    });
                }
            }
            this._render(this._lastSnapshot);
        }

        _expandRow(rowKey) {
            if (!this._lastSnapshot) return;
            for (const group of (this._lastSnapshot.groups || [])) {
                for (const item of (group.items || [])) {
                    if (`${group.name}::${item.name}` !== rowKey) continue;
                    if (!isContainer(item.value)) return;
                    this._walkContainerPaths(item.value, rowKey, "root", (pk) => {
                        this._jsonExpansion.set(pk, true);
                    });
                    this._render(this._lastSnapshot);
                    return;
                }
            }
        }

        _collapseRow(rowKey) {
            if (!this._lastSnapshot) return;
            for (const group of (this._lastSnapshot.groups || [])) {
                for (const item of (group.items || [])) {
                    if (`${group.name}::${item.name}` !== rowKey) continue;
                    if (!isContainer(item.value)) return;
                    this._walkContainerPaths(item.value, rowKey, "root", (pk) => {
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

        _applySortOrder(items) {
            if (!this._sortMode) return items;
            const sorted = items.slice();
            sorted.sort((a, b) => {
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

    function escapeHtml(s) {
        if (s === null || s === undefined) return "";
        return String(s)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function bootstrap() {
        if (window.inspectorPanel) return; // idempotent
        window.inspectorPanel = new InspectorPanel();
        window.inspectorPanel.init();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
        bootstrap();
    }
})();
