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
        { label: "1 Hz", hz: 1 },
        { label: "4 Hz", hz: 4 },
        { label: "On-demand", hz: 0 },
    ];

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
        }

        init() {
            this._buildDom();
            this._restoreState();
            this._setupDrag();
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
                    <button class="inspector-panel-refresh-btn" type="button" data-role="refresh">↻</button>
                </div>
                <div class="inspector-panel-body" data-role="body">
                    <div class="inspector-panel-empty">Select a train to begin.</div>
                </div>
                <div class="inspector-panel-footer" data-role="footer"></div>
            `;
            document.body.appendChild(panel);

            this._panelEl = panel;
            this._bodyEl = panel.querySelector('[data-role="body"]');
            this._trainSelectEl = panel.querySelector('[data-role="train-select"]');
            this._rateSelectEl = panel.querySelector('[data-role="rate-select"]');
            this._refreshBtnEl = panel.querySelector('[data-role="refresh"]');
            this._closeBtnEl = panel.querySelector('.inspector-panel-close');
            this._footerEl = panel.querySelector('[data-role="footer"]');

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
            this._refreshTrainList().then(() => {
                this._startPolling();
            });
            this._persistState();
        }

        hide() {
            this._visible = false;
            this._panelEl.classList.add("hidden");
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

            this._rateSelectEl.value = String(this._refreshHz);
            if (this._visible) {
                this._panelEl.classList.remove("hidden");
            }
        }

        _persistState() {
            saveState({
                visible: this._visible,
                trainId: this._currentTrainId,
                refreshHz: this._refreshHz,
                x: this._position ? this._position.x : undefined,
                y: this._position ? this._position.y : undefined,
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
            const items = group.items || [];
            const rows = collapsed
                ? ""
                : items.map(it => {
                    const prev = prevIndex.get(`${group.name}::${it.name}`);
                    return this._renderItem(it, prev);
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

        _renderItem(item, prev) {
            const errored = item.error !== undefined && item.error !== null;
            const valueChanged = prev !== undefined && !errored
                && JSON.stringify(prev.value) !== JSON.stringify(item.value);

            const valueText = errored
                ? escapeHtml(item.error)
                : escapeHtml(shortValue(item.value));

            // Build tooltip for property formulas / latch metadata.
            const titleParts = [];
            if (item.computed_from) {
                titleParts.push("computed_from: " + item.computed_from.join(", "));
            }
            if (item.formula_doc) {
                titleParts.push(item.formula_doc);
            }
            if (item.writer) titleParts.push("writer: " + item.writer);
            if (item.consumer) titleParts.push("consumer: " + item.consumer);
            if (item.type) titleParts.push("type: " + item.type);
            const title = titleParts.join("\n");

            const classes = ["inspector-row"];
            if (errored) classes.push("inspector-row-error");
            if (valueChanged) classes.push("inspector-row-flash");

            const fullValue = errored ? "" : escapeHtml(fmtValue(item.value));

            return `
                <div class="${classes.join(" ")}" data-source="${escapeHtml(item.source || "")}"
                     ${title ? `title="${escapeHtml(title)}"` : ""}>
                    <span class="inspector-row-name">${escapeHtml(item.name)}</span>
                    <span class="inspector-row-source-badge" data-source="${escapeHtml(item.source || "")}">${escapeHtml(item.source || "")}</span>
                    <span class="inspector-row-value" data-full-value="${fullValue}">${valueText}</span>
                </div>
            `;
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
