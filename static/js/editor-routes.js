/**
 * Route Editor Handler
 *
 * Manages train routes — ordered lists of RouteTasks defining where trains
 * should go. This is distinct from the Path Editor which handles block-level
 * reservations.
 *
 * Edits the backend `tasks` model directly (per design_new.md "Route And
 * RouteExecutor"). Each task is a RouteTask:
 *   { task_kind: "waypoint"|"stop"|"dwell",
 *     task_location: one of {block_id}/{platform_id}/{station_id},
 *     task_direction: "forward"|"reverse"|"either",
 *     task_time?: float, target_time?: float }
 *
 * Features:
 * - Create/edit routes as ordered tasks (kind / target / direction / timing)
 * - Click a tile on the map to add a task at that platform or block
 * - Real-time viability checking between consecutive movement tasks
 * - Train assignment with multi-train support
 * - Route activation/deactivation
 */

class RouteEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this.routes = [];
        this.currentRoute = null;
        this.currentRouteStatus = null;
        this.viabilityResults = new Map(); // row index -> viability result
        this.locationHighlightTimeout = null;
        // Map-driven waypoint add: feasible tiles for the next waypoint as a
        // Set<"x,y">, or null when any resource tile is allowed (first waypoint).
        this._waypointReachablePositions = null;
        this._resourcePicker = null;
        this._resourcePickerDismiss = null;
        // Map-driven train assign: force flag (mirrors the checkbox) and the
        // tile->trainId map of currently-eligible trains.
        this._assignForce = false;
        this._assignTileTrainMap = null;
        // Collapsible run-history section. Default to expanded — the audit
        // trail answers the "what just happened?" question the user asks
        // after every state-change, so hiding it behind a Show button has
        // the wrong polarity. Compactness comes from capping rows per train
        // (see `_historyMaxRunsPerTrain`) instead of hiding the whole panel.
        this._historyExpanded = true;
        this._historyData = null;
        this._historyMaxRunsPerTrain = 10;
        // Per-task expansion state. Tracked by task object reference (not
        // index) so add/move/delete doesn't shift which cards are open.
        this._taskExpanded = new WeakMap();
        // Server-sync poller. When the route editor is visible we
        // periodically re-fetch the route's server-controlled fields
        // (is_active, started_at, assigned_train_ids, currentRouteStatus)
        // and re-render the panel iff those changed. This catches
        // Phase-1 driven auto-deactivation, per-train route teardown
        // from Phase-3 latch consumption, etc. — events the optimistic
        // single-shot updates after a lifecycle API call would miss.
        // The poller intentionally does NOT touch UI-edit fields
        // (name, mode, tasks, priority, acquisition_mode, ...) — those
        // remain owned by the editor until the user clicks Save.
        this._routeSyncInterval = null;
        this._routeSyncIntervalMs = 500;
        // Unsaved-changes tracking. Tasks are owned by the editor until the
        // user clicks Save; the panel can therefore show tasks the server
        // has never seen. When `_savedTasksSerialized` differs from the
        // current `currentRoute.tasks` serialized, the editor is "dirty"
        // and any action that depends on server-side tasks (activate,
        // assign, route-status fetch) must save first or abort.
        this._savedTasksSerialized = null;
    }

    get api() { return this.editor.api; }
    get renderer() { return this.editor.renderer; }
    get cachedNetwork() { return this.editor.cachedNetwork; }

    rerenderRouteEditor() {
        const content = this.buildRouteEditorHTML();
        this.editor.showSecondarySidebar('Route Editor', content);
        document.getElementById('secondary-sidebar')?.classList.add('route-editor-wide');
        setTimeout(() => this.initRouteEditorHandlers(), 0);
    }

    /**
     * Start the route-editor's server-sync poller. Re-fetches the route
     * and its per-train status every `_routeSyncIntervalMs` and, when
     * server-controlled fields changed, calls `rerenderRouteEditor()`.
     * Caller invariant: only call after `this.currentRoute` is populated.
     */
    _startRouteSyncPoller() {
        this._stopRouteSyncPoller();
        if (!this.currentRoute?.route_id) return;
        this._routeSyncInterval = setInterval(
            () => this._syncRouteFromServer().catch(err => {
                console.warn('[RouteEditor] route sync failed:', err);
            }),
            this._routeSyncIntervalMs,
        );
    }

    _stopRouteSyncPoller() {
        if (this._routeSyncInterval) {
            clearInterval(this._routeSyncInterval);
            this._routeSyncInterval = null;
        }
    }

    /**
     * Pull server-truth for the currently-open route. Returns silently
     * (and stops the poller) when the route was deleted or the panel
     * has navigated away. Only the server-controlled fields are written
     * back — UI-edit fields (name, mode, tasks, priority, etc.) remain
     * untouched until the user clicks Save.
     *
     * Re-renders the panel only when one of the server-controlled fields
     * actually changed, so the user isn't blinked at every 500ms tick.
     */
    async _syncRouteFromServer() {
        const routeId = this.currentRoute?.route_id;
        if (!routeId) return;
        if (this.editor._currentEditorType !== 'route-editor'
            || this.editor._currentEditorId !== routeId) {
            // Panel changed under us — stop polling and bail.
            this._stopRouteSyncPoller();
            return;
        }
        let route;
        let status;
        try {
            [route, status] = await Promise.all([
                this.api.getRoute(routeId),
                this.api.getRouteStatus(routeId),
            ]);
        } catch (err) {
            if (err && err.status === 404) {
                // Route was deleted server-side; stop polling. Don't tear
                // down the panel here — the user can navigate manually.
                this._stopRouteSyncPoller();
            }
            throw err;
        }
        const changed = (
            this.currentRoute.is_active !== route.is_active
            || this.currentRoute.started_at !== route.started_at
            || !this._arraysEqual(
                this.currentRoute.assigned_train_ids || [],
                route.assigned_train_ids || [],
            )
            || JSON.stringify(this.currentRouteStatus?.trains)
                !== JSON.stringify(status?.trains)
        );
        // Sync only server-controlled fields. Leave name/mode/tasks/etc
        // alone — those are owned by the editor's in-flight edit state.
        this.currentRoute.is_active = route.is_active;
        this.currentRoute.started_at = route.started_at;
        this.currentRoute.assigned_train_ids = route.assigned_train_ids;
        this.currentRouteStatus = status;
        if (changed) {
            this.rerenderRouteEditor();
        }
    }

    _arraysEqual(a, b) {
        if (a === b) return true;
        if (!Array.isArray(a) || !Array.isArray(b)) return false;
        if (a.length !== b.length) return false;
        for (let i = 0; i < a.length; i++) {
            if (a[i] !== b[i]) return false;
        }
        return true;
    }

    /**
     * HTML-escape a string for safe embedding inside element bodies and
     * `attr="…"` values. Mirrors editor-route-runs.js#_esc so the two
     * panels share an identical escape contract.
     */
    _esc(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    /**
     * Snapshot the currently-loaded tasks as the "saved baseline" used by
     * `_hasUnsavedTaskChanges()`. Called after loading the route from the
     * server (showRouteEditor) and after a successful saveRoute().
     */
    _refreshSavedTasksBaseline() {
        const tasks = (this.currentRoute && this.currentRoute.tasks) || [];
        this._savedTasksSerialized = JSON.stringify(tasks);
    }

    /**
     * Return true when the editor's local task list differs from the last
     * server-confirmed version (initial load or latest successful save).
     * Used to decide whether activate/assign needs a save-first detour.
     */
    _hasUnsavedTaskChanges() {
        if (!this.currentRoute) return false;
        if (this._savedTasksSerialized === null) {
            // Baseline was never taken (e.g. a brand-new route whose
            // initial fetch hasn't completed) — treat as dirty so we
            // don't proceed past a state we can't verify.
            return (this.currentRoute.tasks || []).length > 0;
        }
        const current = JSON.stringify(this.currentRoute.tasks || []);
        return current !== this._savedTasksSerialized;
    }

    /**
     * Save-and-continue gate for actions that depend on server-side task
     * state (activate, assign). When the editor has unsaved task changes,
     * prompt the user to save first; on confirm, save and return whether
     * the save succeeded. When clean, return true immediately so the
     * caller proceeds.
     *
     * Returns:
     *   true  — caller may proceed.
     *   false — caller must abort (user cancelled, or save failed; in the
     *           save-failed case the saveRoute() call already toasted the
     *           specific reason, e.g. "Task 2 has no target selected").
     */
    async _promptSaveIfDirty(actionLabel) {
        if (!this._hasUnsavedTaskChanges()) return true;
        const localCount = (this.currentRoute.tasks || []).length;
        const confirmed = await showConfirmModal({
            title: `${actionLabel}: unsaved changes`,
            message: 'Save your task edits before continuing?',
            detail: (
                `You have unsaved task changes in this panel (${localCount} `
                + `task${localCount === 1 ? '' : 's'} locally). The server `
                + `still has the previous version, so ${actionLabel.toLowerCase()} `
                + `would otherwise act on stale data. "Save and continue" `
                + `pushes your edits then runs ${actionLabel.toLowerCase()}.`
            ),
            confirmText: 'Save and continue',
            confirmVariant: 'primary',
        });
        if (!confirmed) return false;
        const ok = await this.saveRoute();
        return !!ok;
    }

    /**
     * Show the routes list panel
     */
    async showRoutesPanel() {
        // Leaving the per-route editor — stop the server-sync poller so
        // it doesn't keep ticking against a route we're no longer viewing.
        this._stopRouteSyncPoller();

        this.editor._currentEditorType = 'routes-list';
        this.editor._currentEditorId = null;

        try {
            const result = await this.api.getRoutes();
            // Handle both array response and {routes: [...]} response
            this.routes = Array.isArray(result) ? result : (result?.routes || []);
        } catch (err) {
            console.error('[RouteEditor] Failed to load routes:', err);
            window.showToast('Failed to load routes: ' + err.message, 'error', 5000);
            this.routes = [];
        }

        let content = `
            <div class="routes-panel">
                <div class="routes-toolbar mb-16">
                    <button id="create-route-btn" class="btn btn-primary btn-sm flex-1">
                        + New Route
                    </button>
                    <button id="refresh-routes-btn" class="btn btn-secondary btn-sm btn-icon-only" title="Refresh routes" aria-label="Refresh routes">
                        <i class="ti ti-refresh"></i>
                    </button>
                </div>
        `;

        if (this.routes.length === 0) {
            content += `
                <div class="routes-empty-state text-muted text-center">
                    <div class="mb-8">No routes defined</div>
                    <div class="text-sm">Click "New Route" to create a train route</div>
                </div>
            `;
        } else {
            content += '<div class="routes-list">';
            for (const route of this.routes) {
                content += this.renderRouteListItem(route);
            }
            content += '</div>';
        }

        content += '</div>';

        this.editor.showSecondarySidebar('Routes', content);
        setTimeout(() => this.initRoutesPanelHandlers(), 0);
    }

    renderRouteListItem(route) {
        const isActive = route.is_active;
        const statusText = isActive ? 'ACTIVE' : 'INACTIVE';
        const trainIds = Array.isArray(route.assigned_train_ids) ? route.assigned_train_ids : [];
        const trainCount = trainIds.length;
        const taskCount = route.tasks?.length || 0;
        const statusClass = isActive ? 'is-active' : 'is-inactive';
        const modeText = (route.mode || 'one_off').replaceAll('_', ' ');
        const routeName = route.name || route.route_id;

        const trainsLabel = trainCount === 0
            ? 'No trains'
            : (trainCount <= 3
                ? trainIds.join(', ')
                : `${trainIds.slice(0, 2).join(', ')} +${trainCount - 2}`);

        return `
            <div class="route-row" data-route-id="${route.route_id}">
                <div class="route-row-main">
                    <div class="route-row-status ${statusClass}">
                        <span class="route-status-dot"></span>
                        <span class="route-status-label">${statusText}</span>
                    </div>
                    <div class="route-row-content">
                        <div class="route-row-name font-mono">${routeName}</div>
                        <div class="route-row-meta text-xs text-muted info-meta">
                            <span title="${trainIds.join(', ') || 'no trains assigned'}" class="font-mono">${trainsLabel}</span>
                            <span>${taskCount} task${taskCount !== 1 ? 's' : ''}</span>
                            <span class="chip route-mode-pill uppercase">${modeText}</span>
                        </div>
                    </div>
                </div>
                <div class="route-row-actions">
                    <button class="edit-btn btn-sm edit-route-btn" data-route-id="${route.route_id}">Edit</button>
                    <button class="btn btn-sm btn-danger delete-route-btn" data-route-id="${route.route_id}">Delete</button>
                </div>
            </div>
        `;
    }

    initRoutesPanelHandlers() {
        const createBtn = document.getElementById('create-route-btn');
        if (createBtn) {
            createBtn.onclick = () => this.createNewRoute();
        }

        const refreshBtn = document.getElementById('refresh-routes-btn');
        if (refreshBtn) {
            refreshBtn.onclick = () => this.showRoutesPanel();
        }

        const editBtns = document.querySelectorAll('.edit-route-btn');
        editBtns.forEach(btn => {
            btn.onclick = () => {
                const routeId = btn.dataset.routeId;
                this.showRouteEditor(routeId);
            };
        });

        const deleteBtns = document.querySelectorAll('.delete-route-btn');
        deleteBtns.forEach(btn => {
            btn.onclick = async () => {
                const routeId = btn.dataset.routeId;
                const route = this.routes.find(r => r.route_id === routeId);
                const confirmed = await showConfirmModal({
                    title: 'Delete Route',
                    message: `Delete route "${route?.name || routeId}"?`,
                    detail: 'This removes its tasks and train assignments.',
                    confirmText: 'Delete',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;

                try {
                    await this.api.deleteRoute(routeId);
                    window.showToast('Route deleted', 'success', 3000);
                    await this.showRoutesPanel();
                } catch (err) {
                    console.error('[RouteEditor] Failed to delete route:', err);
                    window.showToast('Failed to delete route: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    /**
     * Create a new route (inactive, empty) and open the editor.
     */
    async createNewRoute() {
        const routeId = `ROUTE_${Date.now()}`;
        const newRoute = {
            route_id: routeId,
            name: `Route ${(this.routes?.length || 0) + 1}`,
            mode: 'one_off',
            tasks: [],
            assigned_train_ids: [],
            is_active: false
        };

        try {
            await this.api.createRoute(newRoute);
            window.showToast('Route created', 'success', 3000);
            await this.showRouteEditor(routeId);
        } catch (err) {
            console.error('[RouteEditor] Failed to create route:', err);
            window.showToast('Failed to create route: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Show the route editor for a specific route
     */
    async showRouteEditor(routeId) {
        this.editor._currentEditorType = 'route-editor';
        this.editor._currentEditorId = routeId;

        try {
            this.currentRoute = await this.api.getRoute(routeId);
            this.currentRouteStatus = await this.api.getRouteStatus(routeId);
        } catch (err) {
            if (err && err.status === 404) {
                // Stale tab — the route was removed (e.g. after a rename or
                // delete) while a persisted inspector tab still referenced
                // it. Close that tab silently so the 404 stops repeating on
                // every reload.
                console.warn(`[RouteEditor] Route ${routeId} no longer exists; closing stale tab.`);
                this.editor._currentEditorType = null;
                this.editor._currentEditorId = null;
                const ui = this.editor.uiHandler;
                if (ui) {
                    const staleTab = (ui.secondaryTabs || []).find(
                        tab => tab.editorType === 'route-editor' && tab.editorId === routeId
                    );
                    if (staleTab) {
                        await ui.closeSecondaryTab(staleTab.tabId);
                    } else {
                        await ui.hideSecondarySidebar();
                    }
                }
                return;
            }
            console.error('[RouteEditor] Failed to load route:', err);
            window.showToast('Failed to load route', 'error', 5000);
            return;
        }

        if (!Array.isArray(this.currentRoute.tasks)) {
            this.currentRoute.tasks = [];
        }

        // Take the saved-tasks baseline so subsequent edits register as
        // dirty until the user saves them.
        this._refreshSavedTasksBaseline();

        // Check viability for all consecutive movement-task pairs
        await this.checkAllViability();

        this.rerenderRouteEditor();
        // Start polling server-truth for is_active / assigned_train_ids /
        // per-train status so the panel stays in sync with auto-deactivation
        // from Phase 1 terminal-walk, route-run teardown from Phase 3 latch
        // consumption, etc.
        this._startRouteSyncPoller();
    }

    /**
     * Build the route editor HTML
     */
    buildRouteEditorHTML() {
        const route = this.currentRoute;
        const isActive = route.is_active;
        const tasks = route.tasks || [];

        let tasksHtml = '';
        if (tasks.length > 0) {
            tasksHtml = '<div class="waypoints-list">';
            tasks.forEach((task, index) => {
                tasksHtml += this.renderTaskRow(task, index);
            });
            tasksHtml += '</div>';
        } else {
            tasksHtml = `
                <div class="route-empty-dashed text-muted text-center text-sm">
                    Click "+ Add Task" to add a step, or "Add on map" then click tiles on the layout.
                </div>
            `;
        }

        const trainAssignmentHtml = this.renderTrainAssignmentSection();

        const isDirty = this._hasUnsavedTaskChanges();
        return `
            <div class="route-editor" data-route-id="${route.route_id}">
                <!-- Top toolbar: back / refresh / save. Sticky so Save is
                     always reachable no matter which section the user is
                     editing — fixes the "Save is two scrolls away from
                     Tasks" UX bug. -->
                <div class="re-toolbar">
                    <button id="back-to-routes-btn"
                            class="btn btn-secondary btn-sm re-toolbar-back"
                            title="Return to the routes list">
                        ← Routes
                    </button>
                    <div class="re-toolbar-spacer">
                        ${isDirty
                            ? `<span class="re-toolbar-dirty" title="You have unsaved task edits in this panel.">● unsaved</span>`
                            : ''}
                    </div>
                    <button id="refresh-route-btn"
                            class="btn btn-secondary btn-sm btn-icon-only"
                            title="Pull the latest server state for this route"
                            aria-label="Refresh route">
                        <i class="ti ti-refresh"></i>
                    </button>
                    <button id="save-route-btn"
                            class="btn btn-primary btn-sm ${isDirty ? 're-toolbar-save-dirty' : ''}"
                            title="Save name, mode, tasks, and policy edits">
                        Save${isDirty ? ' *' : ''}
                    </button>
                </div>

                <!-- Route Header -->
                <div class="mb-16">
                    ${this.renderRouteHeader(route)}
                </div>

                <!-- Tasks Section -->
                <div class="control-section mb-16">
                    <div class="flex justify-between items-center mb-12">
                        <h4 class="text-sm text-muted">Tasks</h4>
                        <div class="flex gap-4">
                            <button id="add-task-btn" class="btn btn-sm btn-primary">+ Add Task</button>
                            <button id="add-task-map-btn" class="btn btn-sm btn-secondary" title="Click tiles on the map to add waypoints; reachable tiles light up. Esc to finish.">Add on map</button>
                        </div>
                    </div>
                    ${tasksHtml}
                </div>

                <!-- Train Assignment Section -->
                <div class="control-section mb-16">
                    <h4 class="text-sm text-muted mb-12">Assigned Trains</h4>
                    ${trainAssignmentHtml}
                </div>

                <!-- Controls Section: lifecycle only (Save moved to toolbar) -->
                <div class="control-section">
                    <h4 class="text-sm text-muted mb-12">Controls</h4>
                    ${this.renderRouteStatusPill()}
                    <div class="flex gap-8 mt-12">
                        ${isActive
                            ? `<button id="deactivate-route-btn" class="btn btn-danger btn-sm flex-1">Deactivate</button>`
                            : `<button id="activate-route-btn" class="btn btn-success btn-sm flex-1">Activate</button>`
                        }
                        <button id="delete-route-btn" class="btn btn-danger btn-sm flex-1">Delete Route</button>
                    </div>
                </div>

                <!-- History Section -->
                ${this.renderHistorySection()}
            </div>
        `;
    }

    renderHistorySection() {
        // History defaults to expanded. The toggle still exists so the user
        // can hide it on tiny screens, but the polarity is correct now:
        // "what just happened?" is the most common follow-up question after
        // a state-change, so the audit trail is visible by default.
        return `
            <div class="control-section mt-16">
                <div class="flex justify-between items-center mb-12">
                    <h4 class="text-sm text-muted">History</h4>
                    <button id="toggle-history-btn" class="btn btn-sm btn-secondary">${this._historyExpanded ? 'Hide' : 'Show'}</button>
                </div>
                <div id="route-history-body" class="route-history">${this._historyExpanded ? this.renderHistoryBody() : ''}</div>
            </div>
        `;
    }

    /**
     * Background-fetch the history payload if the section is open and we
     * don't have data yet. Called from `initRouteEditorHandlers` so the
     * default-open state actually renders something instead of "Loading…".
     */
    async _maybeAutoLoadHistory() {
        if (!this._historyExpanded) return;
        if (this._historyData != null) return;
        const body = document.getElementById('route-history-body');
        if (!body) return;
        try {
            this._historyData = await this.api.getRouteHistory(this.currentRoute.route_id);
        } catch (err) {
            console.error('[RouteEditor] history auto-load failed:', err);
            body.innerHTML = '<div class="route-history-empty">Failed to load history</div>';
            return;
        }
        body.innerHTML = this.renderHistoryBody();
    }

    /**
     * Toggle the history section; fetch on expand. Updates the container in
     * place so it doesn't disrupt the rest of the panel.
     */
    async toggleHistory() {
        this._historyExpanded = !this._historyExpanded;
        const btn = document.getElementById('toggle-history-btn');
        const body = document.getElementById('route-history-body');
        if (btn) btn.textContent = this._historyExpanded ? 'Hide' : 'Show';
        if (!body) return;
        if (!this._historyExpanded) {
            body.innerHTML = '';
            return;
        }
        body.innerHTML = '<div class="route-history-empty">Loading…</div>';
        try {
            this._historyData = await this.api.getRouteHistory(this.currentRoute.route_id);
        } catch (err) {
            console.error('[RouteEditor] history load failed:', err);
            body.innerHTML = '<div class="route-history-empty">Failed to load history</div>';
            return;
        }
        body.innerHTML = this.renderHistoryBody();
    }

    renderHistoryBody() {
        const data = this._historyData;
        if (!data) return '<div class="route-history-empty">Loading…</div>';

        // Route-level metadata: created, last edited, last activated. Per
        // design_new.md "Route data" + "Route edit timestamps", these are the
        // only route-level timestamps in v1 — per-stop progress is per-run
        // and rendered inside each run block below.
        const metaRows = [];
        if (data.created_at != null) {
            metaRows.push(`<div>Created ${this._formatTime(data.created_at)}</div>`);
        }
        if (data.last_edited_at != null) {
            metaRows.push(`<div>Last edited ${this._formatTime(data.last_edited_at)}</div>`);
        }
        metaRows.push(`<div>${data.activated_at != null
            ? `Last activated ${this._formatTime(data.activated_at)}`
            : 'Never activated'}</div>`);

        // Cap at top `_historyMaxRunsPerTrain` rows per assigned train so
        // the panel stays compact even after many circular-route cycles.
        // Bucket newest-first per train, then merge by overall recency so
        // the most recent activity across all trains still leads.
        const allRuns = (data.runs || []).slice().reverse(); // newest first
        const perTrainCap = this._historyMaxRunsPerTrain;
        const seenPerTrain = new Map();
        const capped = [];
        let truncatedCount = 0;
        for (const run of allRuns) {
            const tid = run.train_id || '<unknown>';
            const used = seenPerTrain.get(tid) || 0;
            if (used >= perTrainCap) {
                truncatedCount += 1;
                continue;
            }
            seenPerTrain.set(tid, used + 1);
            capped.push(run);
        }
        const runs = capped;
        const truncatedHtml = truncatedCount > 0
            ? `<div class="route-history-empty">+ ${truncatedCount} older run${truncatedCount === 1 ? '' : 's'} hidden (top ${perTrainCap} shown per train)</div>`
            : '';
        const runsHtml = runs.length === 0
            ? '<div class="route-history-empty">No runs yet</div>'
            : runs.map(run => this.renderHistoryRun(run)).join('');
        return `
            <div class="route-history-activated">${metaRows.join('')}</div>
            ${runsHtml}
            ${truncatedHtml}
        `;
    }

    renderHistoryRun(run) {
        const statusClass = run.status === 'FINISHED' ? 'is-finished'
            : run.status === 'DEACTIVATED' ? 'is-deactivated' : 'is-running';
        const tasksHtml = (run.tasks || []).map(t => {
            const task = this.currentRoute?.tasks?.[t.task_index];
            let targetName = `Task ${t.task_index + 1}`;
            if (task) {
                const loc = task.task_location || {};
                const type = ('block_id' in loc) ? 'block'
                    : ('station_id' in loc) ? 'station' : 'platform';
                const id = loc.block_id || loc.station_id || loc.platform_id || '';
                targetName = this.getTargetDisplayName(type, id) || id || targetName;
            }
            const finished = t.finished_at != null;
            const timeText = finished ? this._formatTime(t.finished_at) : '—';
            const delayChip = (finished && t.delay_class)
                ? `<span class="chip chip-state ${({ early: 'chip-info', on_time: 'chip-success', late: 'chip-warn' })[t.delay_class] || 'chip-muted'}">${t.delay_class.replace('_', '-')}</span>`
                : '';
            return `
                <div class="route-history-task ${finished ? '' : 'is-pending'}">
                    <span class="route-history-task-name">${t.task_index + 1}. ${targetName}</span>
                    <span class="route-history-task-meta">${delayChip}<span class="route-history-time">${timeText}</span></span>
                </div>`;
        }).join('');
        return `
            <div class="route-history-run">
                <div class="route-history-run-head">
                    <span class="route-history-status ${statusClass}">${run.status}</span>
                    <span class="font-mono text-xs">${run.train_id}</span>
                    <span class="text-xs text-muted">${this._formatTime(run.started_at)}</span>
                </div>
                <div class="route-history-tasks">${tasksHtml}</div>
            </div>
        `;
    }

    /**
     * Format a timestamp. Run/task times are recorded from the simulation
     * clock; render real Unix timestamps as a time-of-day and sim-relative
     * values as elapsed seconds.
     */
    _formatTime(t) {
        if (t == null) return '—';
        if (t > 1e9) {
            try { return new Date(t * 1000).toLocaleTimeString(); } catch (e) { /* fall through */ }
        }
        return `t=${Number(t).toFixed(1)}s`;
    }

    renderRouteHeader(route) {
        const modeOptions = [
            { value: 'one_off',    label: 'One-off',    glyph: '→' },
            { value: 'circular',   label: 'Circular',   glyph: '↻' },
            { value: 'round_trip', label: 'Round-trip', glyph: '⇋' }
        ];

        // Segmented control per DESIGN_LANGUAGE.md §3.
        const modeSeg = modeOptions.map(opt => `
            <button type="button"
                    class="switch-btn ${route.mode === opt.value ? 'active' : ''}"
                    data-mode="${opt.value}" title="${opt.label}">
                <span class="route-seg-glyph">${opt.glyph}</span>
                <span class="route-seg-label">${opt.label}</span>
            </button>
        `).join('');

        const taskCount = route.tasks?.length || 0;
        const assignedCount = route.assigned_train_ids?.length || 0;

        return `
            <div class="route-editor-header">
                <div class="route-header-row">
                    <div class="route-header-col">
                        <label class="property-label">Route Name</label>
                        <input type="text" id="route-name-input" class="property-input"
                               value="${route.name || ''}" placeholder="Enter name">
                    </div>
                </div>

                <div class="route-header-row">
                    <div class="route-header-col">
                        <label class="property-label">Mode</label>
                        <div id="route-mode-seg" class="switch-group compact route-seg">
                            ${modeSeg}
                        </div>
                    </div>
                    <div class="route-header-col narrow info-display-small">
                         <label class="property-label info-label">Statistics</label>
                         <div class="route-stats-value info-meta">
                            ${taskCount} task${taskCount !== 1 ? 's' : ''} · ${assignedCount} train${assignedCount !== 1 ? 's' : ''}
                         </div>
                    </div>
                </div>

                ${this.renderRoutePolicyRow(route)}
            </div>
        `;
    }

    /**
     * Per design_new.md "Action on waypoint unreachable" + "RouteRun
     * overrides": the route-level allowlisted policy fields plus per-field
     * push toggles (force vs soft). Each picker reads route.<field> on
     * render; the save flow packs {value, push} when push is non-default.
     */
    renderRoutePolicyRow(route) {
        const action = (route.action_on_waypoint_unreachable || 'WAIT');
        const priority = Number.isInteger(route.priority) ? route.priority : 5;
        const acq = (route.acquisition_mode || 'best_effort');
        const seg = (id, options, current) => {
            return `
                <div id="${id}" class="switch-group compact route-seg" role="radiogroup">
                    ${options.map(o => `
                        <button type="button" class="switch-btn ${o.value === current ? 'active' : ''}"
                                data-value="${o.value}" data-seg="${id}">${o.label}</button>
                    `).join('')}
                </div>
            `;
        };
        const pushSeg = (id, current) => seg(id, [
            { value: 'soft', label: 'Soft' },
            { value: 'force', label: 'Force' },
        ], current || 'soft');
        return `
            <div class="route-header-row route-policy-grid" data-route-policy-row>
                <div class="route-header-col">
                    <label class="property-label">Acquisition</label>
                    ${seg('route-acq-seg', [
                        { value: 'best_effort', label: 'Best effort' },
                        { value: 'all_or_nothing', label: 'All or nothing' },
                    ], acq)}
                    <div class="text-xxs text-muted mt-4">Push to runs:</div>
                    ${pushSeg('route-acq-push-seg', 'soft')}
                </div>
                <div class="route-header-col">
                    <label class="property-label">On waypoint unreachable</label>
                    ${seg('route-action-seg', [
                        { value: 'WAIT', label: 'Wait' },
                        { value: 'SKIP', label: 'Skip' },
                    ], action)}
                    <div class="text-xxs text-muted mt-4">Push to runs:</div>
                    ${pushSeg('route-action-push-seg', 'soft')}
                </div>
                <div class="route-header-col">
                    <label class="property-label">Priority (0-9)</label>
                    <input type="number" id="route-priority-input" class="property-input"
                           min="0" max="9" step="1" value="${priority}">
                    <div class="text-xxs text-muted mt-4">Push to runs:</div>
                    ${pushSeg('route-priority-push-seg', 'soft')}
                </div>
            </div>
        `;
    }

    /**
     * Compact active/inactive chip for the Controls section. Per-train
     * progress is shown in the Assigned Trains rows above — keeping it out
     * of here avoids two parallel renderings of the same data.
     */
    renderRouteStatusPill() {
        if (!this.currentRoute?.is_active) {
            return `<div class="route-status-box is-inactive"><span class="route-status-dot"></span>Inactive</div>`;
        }
        const trainCount = (this.currentRouteStatus?.trains || []).length;
        if (trainCount === 0) {
            return `<div class="route-status-box is-info"><span class="route-status-dot"></span>Active · no trains</div>`;
        }
        return `<div class="route-status-box is-active"><span class="route-status-dot"></span>Active · ${trainCount} train${trainCount !== 1 ? 's' : ''}</div>`;
    }

    /**
     * Render one task row (1:1 with a RouteTask).
     *
     * Two states:
     *   • Collapsed — single summary row (ordinal · kind · target · direction · time)
     *   • Expanded — summary row plus two-tier edit body (identity controls,
     *     then a quieter row of direction + travel/dwell + depart-at) and
     *     an action strip.
     * Expansion is tracked per-task in `_taskExpanded` (WeakMap keyed by
     * task object, so reorders don't shift open cards).
     */
    renderTaskRow(task, index) {
        const kind = task.task_kind || 'waypoint';
        const loc = task.task_location || {};
        const targetType = ('block_id' in loc) ? 'block'
            : ('station_id' in loc) ? 'station' : 'platform';
        const targetId = loc.block_id || loc.platform_id || loc.station_id || '';
        const direction = task.task_direction || 'either';
        const isDwell = kind === 'dwell';
        const isExpanded = this._taskExpanded.has(task);
        const lastIndex = (this.currentRoute.tasks?.length || 1) - 1;

        // Icons match the tool panel (Tabler — see icon-selections.json and
        // commit 3957a09). DESIGN_LANGUAGE.md still describes the older
        // Unicode-glyph convention; we follow the in-code precedent for
        // consistency with the rest of the editor.
        const KIND_ICON  = { waypoint: 'arrow-narrow-right', stop: 'map-pin', dwell: 'clock' };
        const TYPE_ICON  = { block: 'rectangle', platform: 'train', station: 'home' };
        const DIR_ICON   = { forward: 'arrow-narrow-right', reverse: 'arrow-narrow-left', either: 'arrows-horizontal' };
        const KIND_LABEL = { waypoint: 'Waypoint', stop: 'Stop', dwell: 'Dwell' };
        const TYPE_LABEL = { block: 'Block', platform: 'Platform', station: 'Station' };
        const DIR_LABEL  = { forward: 'Forward', reverse: 'Reverse', either: 'Either' };

        const ti = (name) => `<i class="ti ti-${name}"></i>`;

        const renderSeg = (segClass, choices, current, dataKey, iconMap, labelMap) => {
            const items = choices.map(v => {
                const icon = iconMap[v] ? ti(iconMap[v]) : '';
                const label = labelMap ? `<span class="route-seg-label">${labelMap[v]}</span>` : '';
                return `<button type="button" class="switch-btn ${current === v ? 'active' : ''}"
                                 data-index="${index}" data-${dataKey}="${v}" title="${labelMap?.[v] || v}">
                            ${icon}${label}
                        </button>`;
            }).join('');
            return `<div class="switch-group compact route-seg ${segClass}" data-index="${index}">${items}</div>`;
        };

        const kindSeg = renderSeg('task-kind-seg', ['waypoint', 'stop', 'dwell'],
            kind, 'kind', KIND_ICON, KIND_LABEL);
        const typeSeg = renderSeg('task-target-type-seg', ['block', 'platform', 'station'],
            targetType, 'type', TYPE_ICON, TYPE_LABEL);
        const dirSeg  = renderSeg('task-direction-seg', ['forward', 'reverse', 'either'],
            direction, 'direction', DIR_ICON, DIR_LABEL);

        // Viability badge — surfaced on the summary line for movement tasks.
        let viabilityHtml = '';
        if (!isDwell) {
            const viability = this.viabilityResults.get(index);
            if (viability !== undefined) {
                if (viability.viable) {
                    viabilityHtml = `<span class="task-summary-viability text-success" title="Path viable: ${viability.hop_count} blocks">✓ ${viability.hop_count}</span>`;
                } else {
                    viabilityHtml = `<span class="task-summary-viability text-danger" title="${viability.error || 'No path'}">✗</span>`;
                }
            }
        }

        // Summary line — calm resting state. The kind chip is tinted by
        // status accent (info/accent/warning per the token table) so
        // Stop / Waypoint / Dwell read at a glance.
        const targetName = isDwell
            ? 'Dwell'
            : (this.getTargetDisplayName(targetType, targetId) || targetId || 'No target');
        const hasTime = (task.task_time !== null && task.task_time !== undefined && task.task_time !== '');
        const timeText = hasTime ? `${task.task_time}s ${isDwell ? 'dwell' : 'travel'}` : '';

        const summaryHtml = `
            <button type="button" class="task-summary" data-index="${index}" aria-expanded="${isExpanded}">
                <span class="waypoint-ordinal">${index + 1}</span>
                <span class="chip chip-state ${({ waypoint: 'chip-info', stop: 'chip-accent', dwell: 'chip-warn' })[kind] || 'chip-muted'} task-summary-chip">
                    ${ti(KIND_ICON[kind])}
                    <span>${KIND_LABEL[kind]}</span>
                </span>
                ${isDwell ? '' : `
                    <span class="chip task-summary-chip task-summary-target" title="${targetName}">
                        ${ti(TYPE_ICON[targetType])}
                        <span class="task-summary-target-name">${targetName}</span>
                    </span>
                    <span class="chip task-summary-chip task-summary-direction">
                        ${ti(DIR_ICON[direction])}
                        <span>${DIR_LABEL[direction]}</span>
                    </span>
                `}
                ${timeText ? `<span class="task-summary-time">${timeText}</span>` : ''}
                ${viabilityHtml}
                <span class="task-summary-toggle">${ti(isExpanded ? 'chevron-down' : 'chevron-right')}</span>
            </button>
        `;

        if (!isExpanded) {
            return `<div class="waypoint-item waypoint-card" data-index="${index}">${summaryHtml}</div>`;
        }

        // Expanded body — Tier 1 (identity) + Tier 2 (quieter) + actions.
        const targetRow = isDwell
            ? `<div class="task-dwell-target-note">Holds at previous task's target</div>`
            : `
                <div class="task-field-row">
                    <label>Type</label>
                    ${typeSeg}
                </div>
                <div class="task-field-row">
                    <label>Target</label>
                    <select class="property-input input-compact task-target-id-select" data-index="${index}">${this.renderTargetOptions(targetType, targetId)}</select>
                    <button class="btn btn-sm btn-secondary btn-icon-only locate-task-btn" data-index="${index}" title="Locate on map" aria-label="Locate on map">${ti('target')}</button>
                    <button class="btn btn-sm btn-secondary btn-icon-only task-target-pick-btn" data-index="${index}" title="Pick element from map" aria-label="Pick element from map">${ti('pointer')}</button>
                </div>`;

        const timeLabel = isDwell ? 'Dwell' : 'Travel';

        return `
            <div class="waypoint-item waypoint-card" data-index="${index}">
                ${summaryHtml}
                <div class="task-body">
                    <div class="task-tier task-tier-primary">
                        <div class="task-field-row">
                            <label>Kind</label>
                            ${kindSeg}
                        </div>
                        ${targetRow}
                    </div>

                    <div class="task-tier task-tier-secondary">
                        <div class="task-field-row">
                            <label>Direction</label>
                            ${dirSeg}
                        </div>
                        <div class="task-field-row task-field-row-times">
                            <label>${timeLabel}</label>
                            <input type="number" min="0" step="1"
                                   class="property-input input-compact task-time-input"
                                   data-index="${index}" value="${task.task_time ?? ''}"
                                   placeholder="seconds">
                            <label class="task-sublabel">Depart</label>
                            <input type="number" min="0" step="1"
                                   class="property-input input-compact task-target-time-input"
                                   data-index="${index}" value="${task.target_time ?? ''}"
                                   placeholder="sim time">
                        </div>
                    </div>

                    <div class="task-actions-row">
                        <button class="btn btn-sm btn-secondary btn-icon-only move-task-up" data-index="${index}" ${index === 0 ? 'disabled' : ''} title="Move up" aria-label="Move up">${ti('arrow-up')}</button>
                        <button class="btn btn-sm btn-secondary btn-icon-only move-task-down" data-index="${index}" ${index >= lastIndex ? 'disabled' : ''} title="Move down" aria-label="Move down">${ti('arrow-down')}</button>
                        <button class="btn btn-sm btn-danger delete-task" data-index="${index}" title="Remove task">${ti('trash')} Delete</button>
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Build <option> markup for the target dropdown of a given target type,
     * preselecting `selectedId` (and preserving an unknown id rather than
     * silently dropping it).
     */
    renderTargetOptions(targetType, selectedId) {
        let items = [];
        if (targetType === 'block') {
            items = this.getBlocks().map(b => ({ id: b.block_id, name: b.name || b.block_id }));
        } else if (targetType === 'station') {
            items = this.getStationGroups().map(s => ({ id: s.station_id, name: s.name || s.station_id }));
        } else {
            items = this.getPlatforms().map(p => ({ id: this.getPlatformId(p), name: p.name || this.getPlatformId(p) }));
        }

        let html = '<option value="">Select…</option>';
        let found = false;
        items.forEach(it => {
            if (!it.id) return;
            const sel = it.id === selectedId ? 'selected' : '';
            if (sel) found = true;
            html += `<option value="${it.id}" ${sel}>${it.name}</option>`;
        });
        if (selectedId && !found) {
            html += `<option value="${selectedId}" selected>${selectedId} (?)</option>`;
        }
        return html;
    }

    getBlocks() {
        const blocks = this.cachedNetwork?.blocks;
        if (Array.isArray(blocks)) return blocks;
        if (blocks && typeof blocks === 'object') return Object.values(blocks);
        return [];
    }

    getPlatforms() {
        if (Array.isArray(this.cachedNetwork?.platforms)) {
            return this.cachedNetwork.platforms;
        }
        if (this.cachedNetwork?.platforms && typeof this.cachedNetwork.platforms === 'object') {
            return Object.values(this.cachedNetwork.platforms);
        }
        return [];
    }

    getStationGroups() {
        if (Array.isArray(this.cachedNetwork?.station_groups)) {
            return this.cachedNetwork.station_groups;
        }
        if (this.cachedNetwork?.station_groups && typeof this.cachedNetwork.station_groups === 'object') {
            return Object.values(this.cachedNetwork.station_groups);
        }
        return [];
    }

    getPlatformId(platform) {
        return platform?.platform_id || platform?.station_id || '';
    }

    getPlatformInfo(platformId) {
        const platform = this.getPlatforms().find(p => this.getPlatformId(p) === platformId);
        return {
            id: platformId || 'Unknown',
            name: platform?.name || '',
            positions: Array.isArray(platform?.positions) ? platform.positions : []
        };
    }

    getStationGroupForPlatform(platformId) {
        return this.getStationGroups().find(group =>
            Array.isArray(group?.platform_ids) && group.platform_ids.includes(platformId)
        ) || null;
    }

    getTargetDisplayName(targetType, targetId) {
        if (!targetId) return '';
        if (targetType === 'block') {
            const b = this.getBlocks().find(b => b.block_id === targetId);
            return b?.name || targetId;
        }
        if (targetType === 'station') {
            const s = this.getStationGroups().find(s => s.station_id === targetId);
            return s?.name || targetId;
        }
        return this.getPlatformInfo(targetId).name || targetId;
    }

    getPlatformPositionSet(platformIds) {
        const ids = new Set(platformIds);
        const positions = new Set();
        this.getPlatforms().forEach(platform => {
            const platformId = this.getPlatformId(platform);
            if (!ids.has(platformId)) return;
            (platform.positions || []).forEach(pos => {
                if (Array.isArray(pos) && pos.length >= 2) {
                    positions.add(`${pos[0]},${pos[1]}`);
                }
            });
        });
        return positions;
    }

    getBlockPositionSet(blockId) {
        const positions = new Set();
        (this.cachedNetwork?.tracks || []).forEach(t => {
            if (t.block_section_id === blockId) {
                positions.add(`${t.x},${t.y}`);
            }
        });
        return positions;
    }

    _applyHighlight(positions, autoClearMs = 0) {
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(positions);
        }
        if (this.cachedNetwork) {
            this.renderer.requestRedraw();
        }
        if (this.locationHighlightTimeout) {
            clearTimeout(this.locationHighlightTimeout);
            this.locationHighlightTimeout = null;
        }
        if (autoClearMs > 0) {
            this.locationHighlightTimeout = setTimeout(() => {
                this.clearLocationHighlight();
            }, autoClearMs);
        }
    }

    clearLocationHighlight() {
        if (this.locationHighlightTimeout) {
            clearTimeout(this.locationHighlightTimeout);
            this.locationHighlightTimeout = null;
        }
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.cachedNetwork) {
            this.renderer.requestRedraw();
        }
    }

    highlightPlatformLocations(platformIds, autoClearMs = 0) {
        this._applyHighlight(this.getPlatformPositionSet(platformIds), autoClearMs);
    }

    /**
     * Highlight (and briefly flash) the map geometry for a task's target.
     */
    focusTaskLocation(index) {
        const task = this.currentRoute?.tasks?.[index];
        if (!task) return;

        const loc = task.task_location || {};
        let positions = new Set();
        let label = '';

        if ('block_id' in loc && loc.block_id) {
            label = this.getTargetDisplayName('block', loc.block_id);
            positions = this.getBlockPositionSet(loc.block_id);
        } else if ('station_id' in loc && loc.station_id) {
            label = this.getTargetDisplayName('station', loc.station_id);
            const group = this.getStationGroups().find(g => g.station_id === loc.station_id);
            positions = this.getPlatformPositionSet(group?.platform_ids || []);
        } else if (loc.platform_id) {
            label = this.getTargetDisplayName('platform', loc.platform_id);
            positions = this.getPlatformPositionSet([loc.platform_id]);
        }

        if (!positions || positions.size === 0) {
            window.showToast(`No map geometry found for task ${index + 1}`, 'warning', 3000);
            return;
        }

        this._applyHighlight(positions, 5000);
        document.getElementById('status-state').textContent =
            `Route Editor: Highlighting task ${index + 1} (${label})`;
    }

    /**
     * Render one assigned-train row using the §12 row-list pattern (status
     * dot · train id · verb · target noun · modifier · action). Replaces
     * the earlier card-shape that violated §12 principle 2 (homogeneous
     * comparable records belong in rows, not cards).
     *
     * Columns:
     *   ● status dot     hue channel — warn (dwell), info (running), danger (fault)
     *     T1             train identity (mono, bright)
     *     [DWELL]        verb chip — categorical filter the eye scans first
     *     Station 1      target noun — what the train is at / heading to
     *     fwd · 7s       modifier — direction + derived dwell time
     *     [Unassign]     ghost action; not danger-colored (reversible)
     */
    renderAssignedTrainRow(trainId, trainStatus) {
        const tasks = this.currentRoute?.tasks || [];
        const finishedIdx = trainStatus?.current_waypoint_index;
        const nextIdx = trainStatus
            ? ((finishedIdx ?? -1) + 1)
            : null;

        const cap = (s) => (typeof s === 'string' && s.length)
            ? s.charAt(0).toUpperCase() + s.slice(1)
            : '';

        // Resolve target name from the live task definition.
        let targetName = '';
        if (trainStatus?.current_task_location) {
            const loc = trainStatus.current_task_location;
            if (loc.block_id) targetName = this.getTargetDisplayName('block', loc.block_id);
            else if (loc.station_id) targetName = this.getTargetDisplayName('station', loc.station_id);
            else if (loc.platform_id) targetName = this.getTargetDisplayName('platform', loc.platform_id);
        }

        // Verb chip — kind of task the train is currently working on. Map to
        // the same color tokens the route-runs editor uses for task verbs so
        // the eye trains across both panels (info=stop, warn=dwell).
        const kind = trainStatus?.current_task_kind || null;
        const verb = kind ? kind.toUpperCase() : 'IDLE';
        const verbAccent =
            kind === 'dwell' ? 'warn'
            : kind === 'stop' ? 'info'
            : kind === 'waypoint' ? 'info'
            : 'muted';

        // Hue dot — warn while dwelling (the train is paused), danger when
        // the status payload is missing (server hasn't returned per-train
        // info), transparent otherwise. The dot is "is something noteworthy
        // happening?" not "is this row important." See §12 color channels.
        let dotVariant = '';
        if (!trainStatus) dotVariant = 'danger';
        else if (kind === 'dwell') dotVariant = 'warn';

        // Modifier line — direction + derived dwell remaining.
        //
        // dwell_remaining_sec leaks a Unix epoch in some demo scenes (the
        // backend's get_remaining_dwell_time mixes wall-clock and sim-clock
        // when the demo back-dates task_runs to make dwells elapse — see
        // demo_4_scene.py). Cap defensively: if the value is
        // bigger than any plausible dwell (1 hour), suppress it instead of
        // rendering "dwell 1779949962s" which is what motivated this fix.
        const modParts = [];
        if (trainStatus?.direction) modParts.push(cap(trainStatus.direction));
        if (trainStatus?.at_platform) modParts.push('at platform');
        const dwellRaw = trainStatus?.dwell_remaining_sec;
        if (dwellRaw != null && Number.isFinite(dwellRaw) && dwellRaw >= 0 && dwellRaw <= 3600) {
            modParts.push(`${Math.round(dwellRaw)}s left`);
        }
        const modifier = modParts.join(' · ');

        // Status text when targetName is missing (between runs, queued, etc.)
        let nounText;
        if (!trainStatus) {
            nounText = '—';
        } else if (nextIdx === null || tasks.length === 0) {
            nounText = 'queued';
        } else {
            nounText = targetName || `task ${(nextIdx % Math.max(tasks.length, 1)) + 1}`;
        }

        // Tooltip carries the long form for power users; principle 12
        // (density serves readability — keep the compact form in title).
        let tooltip = `${trainId}`;
        if (kind) tooltip += ` · ${verb}`;
        if (targetName) tooltip += ` → ${targetName}`;
        if (modifier) tooltip += ` · ${modifier}`;
        const escTooltip = this._esc(tooltip);

        return `
            <div class="atr-row" data-train-id="${trainId}" title="${escTooltip}">
                <span class="atr-status atr-status-${dotVariant || 'ok'}"></span>
                <span class="atr-id font-mono">${this._esc(trainId)}</span>
                <span class="atr-verb atr-verb-${verbAccent}">${this._esc(verb)}</span>
                <span class="atr-noun">${this._esc(nounText)}</span>
                <span class="atr-mod">${this._esc(modifier)}</span>
                <button class="btn btn-sm btn-secondary unassign-train-btn atr-action"
                        data-train-id="${trainId}"
                        title="Unassign ${trainId} from this route">Unassign</button>
            </div>
        `;
    }

    renderTrainAssignmentSection() {
        const route = this.currentRoute;
        const assignedTrains = route.assigned_train_ids || [];
        const statusByTrainId = new Map();
        (this.currentRouteStatus?.trains || []).forEach(ts => {
            statusByTrainId.set(ts.train_id, ts);
        });

        let assignedHtml = '';
        if (assignedTrains.length > 0) {
            assignedHtml = '<div class="assigned-trains-list mb-12">';
            assignedTrains.forEach(trainId => {
                assignedHtml += this.renderAssignedTrainRow(trainId, statusByTrainId.get(trainId));
            });
            assignedHtml += '</div>';
        } else {
            assignedHtml = '<div class="text-muted text-sm mb-12">No trains assigned</div>';
        }

        // Get available trains (not assigned to this route)
        const allTrainsRaw = Array.isArray(this.cachedNetwork?.trains)
            ? this.cachedNetwork.trains
            : Object.values(this.cachedNetwork?.trains || {});
        const availableTrains = allTrainsRaw
            .map(t => ({
                train_id: t?.train_id || t?.id || '',
                model: t?.model || ''
            }))
            .filter(t => t.train_id && !assignedTrains.includes(t.train_id));

        let trainSelectHtml = '<option value="">Select train...</option>';
        availableTrains.forEach(train => {
            const label = train.model ? `${train.train_id} (${train.model})` : train.train_id;
            trainSelectHtml += `<option value="${train.train_id}">${label}</option>`;
        });

        // Force reassign lives in the same flex row as Assign — the
        // checkbox modifies the button's behavior, so per §12 principle 1
        // (proximity) they belong in one visual cluster. The previous
        // layout put Force two rows below the button which trained users
        // to ignore it.
        return `
            ${assignedHtml}
            <div class="atr-assign-row">
                <select id="assign-train-select" class="property-input route-input-compact atr-assign-select">
                    ${trainSelectHtml}
                </select>
                <label class="checkbox-wrapper atr-force-label" title="Reassign the train even if it is already on another route">
                    <input type="checkbox" id="assign-train-force" class="checkbox-input"
                           ${this._assignForce ? 'checked' : ''}>
                    <span class="checkbox-custom"></span>
                    <span class="checkbox-label">Force</span>
                </label>
                <button id="assign-train-btn" class="btn btn-sm btn-primary">Assign</button>
            </div>
            <div class="flex gap-8 mt-8">
                <button id="assign-train-map-btn" class="btn btn-sm btn-secondary flex-1" title="Click trains on the layout to assign them; eligible trains light up. Esc to finish.">Assign on map</button>
            </div>
        `;
    }

    initRouteEditorHandlers() {
        // Back-to-routes button (top toolbar).
        const backBtn = document.getElementById('back-to-routes-btn');
        if (backBtn) {
            backBtn.onclick = () => this.showRoutesPanel();
        }

        // Save button (top toolbar).
        const saveBtn = document.getElementById('save-route-btn');
        if (saveBtn) {
            saveBtn.onclick = () => this.saveRoute();
        }

        // Refresh button — explicitly re-fetch route + status. The poller
        // already syncs every 500ms but users wanted an explicit affordance
        // for confidence ("I just clicked something; did anything change?").
        const refreshBtn = document.getElementById('refresh-route-btn');
        if (refreshBtn) {
            refreshBtn.onclick = async () => {
                const routeId = this.currentRoute?.route_id;
                if (!routeId) return;
                refreshBtn.disabled = true;
                try {
                    // Use the same code path as showRouteEditor for a full
                    // refresh — keeps the dirty-baseline snapshot in sync.
                    await this.showRouteEditor(routeId);
                } finally {
                    // showRouteEditor() rebuilds the panel, so this button
                    // reference is stale; nothing to re-enable.
                }
            };
        }

        const deleteRouteBtn = document.getElementById('delete-route-btn');
        if (deleteRouteBtn) {
            deleteRouteBtn.onclick = async () => {
                const confirmed = await showConfirmModal({
                    title: 'Delete Route',
                    message: `Delete route "${this.currentRoute?.name || this.currentRoute?.route_id}"?`,
                    detail: 'This removes all tasks and train assignments for this route.',
                    confirmText: 'Delete',
                    confirmVariant: 'danger'
                });
                if (!confirmed) {
                    return;
                }
                try {
                    await this.api.deleteRoute(this.currentRoute.route_id);
                    window.showToast('Route deleted', 'success', 3000);
                    await this.showRoutesPanel();
                } catch (err) {
                    console.error('[RouteEditor] Failed to delete route:', err);
                    window.showToast('Failed to delete route: ' + err.message, 'error', 5000);
                }
            };
        }

        // Add task buttons
        const addTaskBtn = document.getElementById('add-task-btn');
        if (addTaskBtn) {
            addTaskBtn.onclick = () => this.addTask();
        }
        const addTaskMapBtn = document.getElementById('add-task-map-btn');
        if (addTaskMapBtn) {
            addTaskMapBtn.onclick = () =>
                this.editor.enterMode(this.buildWaypointAddMode());
        }

        // Activate/Deactivate buttons
        const activateBtn = document.getElementById('activate-route-btn');
        if (activateBtn) {
            activateBtn.onclick = () => this.activateRoute();
        }

        const deactivateBtn = document.getElementById('deactivate-route-btn');
        if (deactivateBtn) {
            deactivateBtn.onclick = () => this.deactivateRoute();
        }

        const toggleHistoryBtn = document.getElementById('toggle-history-btn');
        if (toggleHistoryBtn) {
            toggleHistoryBtn.onclick = () => this.toggleHistory();
        }

        // History defaults to expanded but the data isn't always loaded
        // yet (especially right after entering the editor). Kick off a
        // background fetch so the section populates without the user
        // having to click Hide/Show to re-trigger it.
        this._maybeAutoLoadHistory().catch(err => {
            console.warn('[RouteEditor] history auto-load swallowed:', err);
        });

        // Route-mode segmented control — click toggles the active button.
        // The value is read back at save() time from the .active button.
        document.querySelectorAll('#route-mode-seg .switch-btn').forEach(btn => {
            btn.onclick = (e) => {
                const seg = e.currentTarget.parentElement;
                seg.querySelectorAll('.switch-btn').forEach(b =>
                    b.classList.toggle('active', b === e.currentTarget));
            };
        });

        // Route-policy + per-field push segmented controls. Toggle .active
        // on click; the value is read back at save() time. The push pickers
        // tag each request payload with `{value, push}` per design_new.md
        // "RouteRun overrides" canonical home.
        [
            'route-acq-seg',
            'route-acq-push-seg',
            'route-action-seg',
            'route-action-push-seg',
            'route-priority-push-seg',
        ].forEach(segId => {
            document.querySelectorAll(`#${segId} .switch-btn`).forEach(btn => {
                btn.onclick = (e) => {
                    const seg = e.currentTarget.parentElement;
                    seg.querySelectorAll('.switch-btn').forEach(b =>
                        b.classList.toggle('active', b === e.currentTarget));
                };
            });
        });

        // Task reorder/delete/locate buttons
        document.querySelectorAll('.move-task-up').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.currentTarget.dataset.index, 10);
                this.moveTask(index, -1);
            };
        });

        document.querySelectorAll('.move-task-down').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.currentTarget.dataset.index, 10);
                this.moveTask(index, 1);
            };
        });

        document.querySelectorAll('.delete-task').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.currentTarget.dataset.index, 10);
                this.deleteTask(index);
            };
        });

        document.querySelectorAll('.locate-task-btn').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.currentTarget.dataset.index, 10);
                this.focusTaskLocation(index);
            };
        });

        // Tap the summary row to toggle expansion. Tracked on the task
        // object via _taskExpanded so reorders don't shuffle which cards
        // are open.
        document.querySelectorAll('.task-summary').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.currentTarget.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                if (this._taskExpanded.has(task)) {
                    this._taskExpanded.delete(task);
                } else {
                    this._taskExpanded.set(task, true);
                }
                this.rerenderRouteEditor();
            };
        });

        // Task field handlers — kind / direction / target-type are
        // segmented controls (per DESIGN_LANGUAGE.md §3); each .switch-btn
        // click selects the value carried on its data-* attribute.
        document.querySelectorAll('.task-kind-seg .switch-btn').forEach(btn => {
            btn.onclick = (e) => {
                const seg = e.currentTarget.closest('.task-kind-seg');
                const index = parseInt(seg.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                task.task_kind = e.currentTarget.dataset.kind;
                // kind change toggles target/timing controls — re-render.
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.task-target-type-seg .switch-btn').forEach(btn => {
            btn.onclick = (e) => {
                const seg = e.currentTarget.closest('.task-target-type-seg');
                const index = parseInt(seg.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                const type = e.currentTarget.dataset.type;
                const key = type === 'block' ? 'block_id'
                    : type === 'station' ? 'station_id' : 'platform_id';
                task.task_location = { [key]: '' };
                // target dropdown must repopulate — re-render.
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.task-target-id-select').forEach(select => {
            select.onchange = async (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                const loc = task.task_location || {};
                const key = ('block_id' in loc) ? 'block_id'
                    : ('station_id' in loc) ? 'station_id' : 'platform_id';
                task.task_location = { [key]: e.target.value };
                await this.checkAllViability();
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.task-direction-seg .switch-btn').forEach(btn => {
            btn.onclick = (e) => {
                const seg = e.currentTarget.closest('.task-direction-seg');
                const index = parseInt(seg.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                task.task_direction = e.currentTarget.dataset.direction;
                // Re-render so the summary line picks up the new direction.
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.task-time-input').forEach(input => {
            input.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                task.task_time = this._parseOptionalNumber(e.target.value);
                // Re-render so the summary line reflects the new duration.
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.task-target-time-input').forEach(input => {
            input.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (task) task.target_time = this._parseOptionalNumber(e.target.value);
            };
        });

        // Train assignment buttons
        const assignTrainBtn = document.getElementById('assign-train-btn');
        if (assignTrainBtn) {
            assignTrainBtn.onclick = () => this.assignTrain();
        }

        const assignTrainMapBtn = document.getElementById('assign-train-map-btn');
        if (assignTrainMapBtn) {
            assignTrainMapBtn.onclick = () =>
                this.editor.enterMode(this.buildTrainAssignMode());
        }

        const forceCheckbox = document.getElementById('assign-train-force');
        if (forceCheckbox) {
            forceCheckbox.onchange = (e) => {
                this._assignForce = !!e.target.checked;
                // Live-update the eligible-train highlight when toggled mid-mode.
                if (this.editor.activeMode?.name === 'route-train-assign') {
                    this.refreshAssignCandidates();
                }
            };
        }

        document.querySelectorAll('.unassign-train-btn').forEach(btn => {
            btn.onclick = (e) => {
                const trainId = e.currentTarget.dataset.trainId;
                this.unassignTrain(trainId);
            };
        });
    }

    _parseOptionalNumber(value) {
        if (value === '' || value === null || value === undefined) return null;
        const f = parseFloat(value);
        return Number.isFinite(f) ? f : null;
    }

    /**
     * Append a blank stop task (default platform target) and re-render.
     * Default kind is `stop` so a fresh route halts between targets; without a
     * stop the live path stays extended across waypoints and a destination
     * that requires a turnaround fails with "extend would reverse at
     * authority end". Operators can flip the kind to waypoint per-row.
     */
    addTask() {
        if (!Array.isArray(this.currentRoute.tasks)) this.currentRoute.tasks = [];
        const newTask = {
            task_kind: 'stop',
            task_location: { platform_id: '' },
            task_direction: 'either',
            task_time: null,
            target_time: null
        };
        this.currentRoute.tasks.push(newTask);
        // Newly-added tasks open in the editing state so the user can fill
        // them out immediately; everything else stays at rest.
        this._taskExpanded.set(newTask, true);
        this.rerenderRouteEditor();
    }

    // ===================================================================
    // Map-driven waypoint add (mirrors the path tool's feasibility UX)
    // ===================================================================

    /**
     * Build the EditorMode for adding waypoints by clicking the layout.
     * Activated via `editor.enterMode(...)`.
     */
    buildWaypointAddMode() {
        return {
            name: 'route-waypoint-add',
            cancelMessage: 'Cancelled',
            onEnter: () => {
                this._setStatus('Route: click a tile to add a waypoint — reachable tiles are highlighted. Esc to finish.');
                this.refreshWaypointFeasibility();
            },
            onClick: ({ gridPos, screenX, screenY }) =>
                this.handleWaypointAddClick(gridPos, screenX, screenY),
            onExit: () => {
                this._dismissResourcePicker();
                this._waypointReachablePositions = null;
                this.clearLocationHighlight();
                this._setStatus('Ready');
            }
        };
    }

    _setStatus(text) {
        const el = document.getElementById('status-state');
        if (el) el.textContent = text;
    }

    /**
     * Return the last movement task's target as {type, id}, or null when no
     * movement task has a target yet (the first waypoint).
     */
    _lastMovementTaskLocation() {
        const tasks = this.currentRoute?.tasks || [];
        for (let i = tasks.length - 1; i >= 0; i--) {
            const t = tasks[i];
            if (t.task_kind === 'dwell') continue;
            const loc = t.task_location || {};
            if (loc.block_id) return { type: 'block', id: loc.block_id };
            if (loc.station_id) return { type: 'station', id: loc.station_id };
            if (loc.platform_id) return { type: 'platform', id: loc.platform_id };
        }
        return null;
    }

    /**
     * Map a list of reachable endpoint ids (blocks + station groups) to a
     * Set<"x,y"> of tile positions for highlighting.
     */
    _endpointsToPositionSet(endpoints) {
        const positions = new Set();
        const stationGroups = this.getStationGroups();
        const stationIds = new Set(stationGroups.map(g => g.station_id));
        (endpoints || []).forEach(ep => {
            if (stationIds.has(ep)) {
                const group = stationGroups.find(g => g.station_id === ep);
                this.getPlatformPositionSet(group?.platform_ids || []).forEach(p => positions.add(p));
            } else {
                this.getBlockPositionSet(ep).forEach(p => positions.add(p));
            }
        });
        return positions;
    }

    /**
     * Recompute and highlight the feasible tiles for the next waypoint. With
     * no prior movement task, anything goes (no highlight); otherwise we ask
     * the backend which blocks/stations are reachable from the last waypoint
     * and light those tiles up.
     */
    async refreshWaypointFeasibility() {
        const prev = this._lastMovementTaskLocation();
        if (!prev) {
            this._waypointReachablePositions = null;
            this.clearLocationHighlight();
            return;
        }
        try {
            const result = await this.api.getReachableFrom(prev.type, prev.id);
            const positions = this._endpointsToPositionSet(result?.reachable_endpoints || []);
            this._waypointReachablePositions = positions;
            this._applyHighlight(positions);
        } catch (err) {
            console.error('[RouteEditor] reachable-from failed:', err);
            // Fall back to permissive (no gate) so the tool stays usable.
            this._waypointReachablePositions = null;
            this.clearLocationHighlight();
        }
    }

    /**
     * Which targetable resources sit on a tile: platform (and its station
     * group) and/or block. Empty when the tile carries none.
     */
    _resourceOptionsAtTile(gridPos, track) {
        const options = [];
        const platform = this.findPlatformAtPosition(gridPos.x, gridPos.y);
        if (platform) {
            const platformId = this.getPlatformId(platform);
            options.push({ type: 'platform', id: platformId, label: `Platform ${platform.name || platformId}` });
            const group = this.getStationGroupForPlatform(platformId);
            if (group) {
                options.push({ type: 'station', id: group.station_id, label: `Station ${group.name || group.station_id}` });
            }
        }
        if (track?.block_section_id) {
            options.push({ type: 'block', id: track.block_section_id, label: `Block ${track.block_section_id}` });
        }
        return options;
    }

    /**
     * Handle a layout click while in waypoint-add mode. Returns true to
     * consume the click (always, while the mode is active).
     */
    async handleWaypointAddClick(gridPos, screenX, screenY) {
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);
        const options = this._resourceOptionsAtTile(gridPos, track);

        // Empty tile: stay in mode, do nothing.
        if (options.length === 0) return true;

        // Feasibility gate (skipped for the first waypoint, where reachable is null).
        if (this._waypointReachablePositions) {
            const key = `${gridPos.x},${gridPos.y}`;
            if (!this._waypointReachablePositions.has(key)) {
                window.showToast('Not reachable from the previous waypoint', 'warning', 2500);
                return true;
            }
        }

        if (options.length === 1) {
            await this._addWaypointFromOption(options[0]);
        } else {
            this._showResourcePicker(screenX, screenY, options, (opt) => this._addWaypointFromOption(opt));
        }
        return true;
    }

    /**
     * Append a waypoint for a chosen {type, id} resource, re-render the panel
     * (without leaving the mode), and recompute feasibility for the next click.
     */
    async _addWaypointFromOption(opt) {
        if (!Array.isArray(this.currentRoute.tasks)) this.currentRoute.tasks = [];
        const key = opt.type === 'block' ? 'block_id'
            : opt.type === 'station' ? 'station_id' : 'platform_id';
        this.currentRoute.tasks.push({
            task_kind: 'stop',
            task_location: { [key]: opt.id },
            task_direction: 'either',
            task_time: null,
            target_time: null
        });
        await this.checkAllViability();
        this.rerenderRouteEditor();
        await this.refreshWaypointFeasibility();
        window.showToast(`Added stop: ${opt.label}`, 'success', 2000);
    }

    /**
     * Floating menu to pick which resource on a tile to add. Positioned at the
     * click point; dismisses on pick / outside-click / Esc (Esc routes through
     * the interaction's onExit).
     */
    _showResourcePicker(screenX, screenY, options, onPick) {
        this._dismissResourcePicker();
        const menu = document.createElement('div');
        menu.className = 'route-resource-picker';
        menu.style.left = `${screenX}px`;
        menu.style.top = `${screenY}px`;

        options.forEach(opt => {
            const btn = document.createElement('button');
            btn.className = 'route-resource-option';
            btn.textContent = opt.label;
            btn.onclick = (e) => {
                e.stopPropagation();
                this._dismissResourcePicker();
                onPick(opt);
            };
            menu.appendChild(btn);
        });

        document.body.appendChild(menu);
        this._resourcePicker = menu;

        this._resourcePickerDismiss = (e) => {
            if (this._resourcePicker && !this._resourcePicker.contains(e.target)) {
                this._dismissResourcePicker();
            }
        };
        // Defer so the click that opened the menu doesn't immediately close it.
        setTimeout(() => document.addEventListener('mousedown', this._resourcePickerDismiss), 0);
    }

    _dismissResourcePicker() {
        if (this._resourcePicker) {
            this._resourcePicker.remove();
            this._resourcePicker = null;
        }
        if (this._resourcePickerDismiss) {
            document.removeEventListener('mousedown', this._resourcePickerDismiss);
            this._resourcePickerDismiss = null;
        }
    }

    // ===================================================================
    // Map-driven train assignment
    // ===================================================================

    buildTrainAssignMode() {
        return {
            name: 'route-train-assign',
            cancelMessage: 'Cancelled',
            onEnter: () => {
                this.refreshAssignCandidates();
            },
            onClick: ({ gridPos }) => this.handleTrainAssignClick(gridPos),
            onExit: () => {
                this._assignTileTrainMap = null;
                this.clearLocationHighlight();
                this._setStatus('Ready');
            }
        };
    }

    /**
     * Return the id of the block currently occupied by `trainId`, or null.
     */
    _blockOccupiedByTrain(trainId) {
        const blocks = this.cachedNetwork?.blocks || {};
        if (Array.isArray(blocks)) {
            const found = blocks.find(b => b?.occupied_by === trainId);
            return found ? (found.block_id || null) : null;
        }
        for (const [blockId, b] of Object.entries(blocks)) {
            if (b?.occupied_by === trainId) return b.block_id || blockId;
        }
        return null;
    }

    /**
     * Highlight the layout tiles of trains eligible for assignment to this
     * route. By default only trains with no route assigned are eligible; with
     * the force flag set, every train is (excluding ones already on this
     * route). Builds a tile->trainId map for click resolution.
     */
    async refreshAssignCandidates() {
        let assignedByTrain = {};
        try {
            assignedByTrain = await this.api.getAssignedRoutesByTrain();
        } catch (err) {
            console.error('[RouteEditor] getAssignedRoutesByTrain failed:', err);
        }

        const force = this._assignForce;
        const alreadyOnThisRoute = new Set(this.currentRoute.assigned_train_ids || []);
        const allTrains = Array.isArray(this.cachedNetwork?.trains)
            ? this.cachedNetwork.trains
            : Object.values(this.cachedNetwork?.trains || {});

        const positions = new Set();
        const tileTrain = new Map();
        allTrains.forEach(t => {
            const trainId = t?.train_id || t?.id;
            if (!trainId || alreadyOnThisRoute.has(trainId)) return;
            const hasRoute = !!assignedByTrain[trainId];
            if (!force && hasRoute) return;
            const blockId = this._blockOccupiedByTrain(trainId);
            if (!blockId) return;
            this.getBlockPositionSet(blockId).forEach(p => {
                positions.add(p);
                tileTrain.set(p, trainId);
            });
        });

        this._assignTileTrainMap = tileTrain;
        this._applyHighlight(positions);

        const eligible = new Set(tileTrain.values()).size;
        this._setStatus(force
            ? `Route: click any train to assign (force). ${eligible} eligible. Esc to finish.`
            : `Route: click an unassigned train to assign. ${eligible} eligible. Esc to finish.`);
    }

    /**
     * Handle a layout click while in train-assign mode. Assigns the eligible
     * train at the clicked tile, then recomputes candidates so the just-
     * assigned train drops out (unless forcing). Always consumes the click.
     */
    async handleTrainAssignClick(gridPos) {
        const trainId = this._assignTileTrainMap?.get(`${gridPos.x},${gridPos.y}`);
        if (!trainId) return true; // no eligible train here; stay in mode

        // Save unsaved task edits first — same reason as the dropdown
        // assign: a panel showing 4 tasks but a server holding 0 would
        // make the assign-route handler reject the request.
        const dirtyOk = await this._promptSaveIfDirty('Assign train');
        if (!dirtyOk) return true;

        // Same pre-flight check as the dropdown-driven assignTrain(): warn
        // when sim is paused or this specific train is OFF, so map clicks
        // don't silently no-op either.
        const ok = await this._confirmRuntimePreconditions('Assign train', trainId);
        if (!ok) return true;

        try {
            await this.api.assignRoute(trainId, this.currentRoute.route_id, {
                force: this._assignForce
            });
            // Don't mutate assigned_train_ids locally — let the server
            // tell us what stuck. See assignTrain() for context.
            await this._refetchRouteAfterLifecycleOp();
            window.showToast(`Train ${trainId} assigned`, 'success', 2500);
            this.rerenderRouteEditor();
            await this.refreshAssignCandidates();
        } catch (err) {
            console.error('[RouteEditor] assign (map) failed:', err);
            window.showToast('Failed to assign train: ' + err.message, 'error', 5000);
        }
        return true;
    }

    /**
     * Find platform at a grid position
     */
    findPlatformAtPosition(x, y) {
        const track = this.cachedNetwork?.tracks?.find(t => t.x === x && t.y === y);
        const platformId = track?.station_id;
        if (!platformId) return null;

        return this.getPlatforms().find(p => this.getPlatformId(p) === platformId) || null;
    }

    /**
     * Move a task up or down in the list
     */
    async moveTask(index, direction) {
        const tasks = this.currentRoute.tasks;
        const newIndex = index + direction;
        if (newIndex < 0 || newIndex >= tasks.length) return;

        [tasks[index], tasks[newIndex]] = [tasks[newIndex], tasks[index]];

        await this.checkAllViability();
        this.rerenderRouteEditor();
    }

    /**
     * Delete a task
     */
    async deleteTask(index) {
        const confirmed = await showConfirmModal({
            title: 'Delete Task',
            message: 'Delete this task?',
            detail: 'Route order and viability will be recalculated.',
            confirmText: 'Delete',
            confirmVariant: 'danger'
        });
        if (!confirmed) return;

        this.currentRoute.tasks.splice(index, 1);

        await this.checkAllViability();
        this.rerenderRouteEditor();
    }

    /**
     * Return the canonical {key: id} location of a task, or null when its
     * target is unset.
     */
    _taskLocationOrNull(task) {
        const loc = task?.task_location || {};
        const key = ('block_id' in loc) ? 'block_id'
            : ('station_id' in loc) ? 'station_id' : 'platform_id';
        return loc[key] ? { [key]: loc[key] } : null;
    }

    /**
     * Check viability for all consecutive movement-task pairs (waypoint/stop;
     * dwell rows are skipped because they don't change location).
     */
    async checkAllViability() {
        this.viabilityResults.clear();
        const tasks = this.currentRoute.tasks || [];

        const movement = [];
        tasks.forEach((task, idx) => {
            if (task.task_kind === 'dwell') return;
            const loc = this._taskLocationOrNull(task);
            if (loc) movement.push({ idx, loc, direction: task.task_direction || 'either' });
        });

        if (movement.length < 2) return;

        const pairs = [];
        for (let i = 1; i < movement.length; i++) {
            pairs.push({
                from: movement[i - 1].loc,
                to: movement[i].loc,
                direction: movement[i].direction,
            });
        }

        try {
            const results = await this.api.checkViability(pairs);
            (results.results || []).forEach((result, k) => {
                const rowIndex = movement[k + 1].idx;
                this.viabilityResults.set(rowIndex, result);
            });
        } catch (err) {
            console.error('[RouteEditor] Viability check failed:', err);
        }
    }

    /**
     * Save route changes (serializes rows into the canonical `tasks` payload).
     *
     * Returns:
     *   true  — the route was successfully persisted.
     *   false — a validation error (toast already shown) or a network error
     *           (toast already shown). Callers that chain another action
     *           after save (e.g. activate, assign) should abort on false.
     */
    async saveRoute() {
        const nameInput = document.getElementById('route-name-input');
        const modeSeg = document.getElementById('route-mode-seg');

        if (nameInput) {
            this.currentRoute.name = nameInput.value;
        }
        if (modeSeg) {
            const active = modeSeg.querySelector('.switch-btn.active');
            if (active) this.currentRoute.mode = active.dataset.mode;
        }

        const src = this.currentRoute.tasks || [];
        const tasks = [];
        for (let i = 0; i < src.length; i++) {
            const t = src[i];
            const kind = t.task_kind || 'waypoint';
            let location;

            if (kind === 'dwell') {
                // Runtime ignores a dwell's location, but the backend requires
                // a truthy task_location on every task — inherit the nearest
                // preceding task that has a target.
                let inherited = null;
                for (let j = i - 1; j >= 0; j--) {
                    const prevLoc = this._taskLocationOrNull(src[j]);
                    if (prevLoc) { inherited = prevLoc; break; }
                }
                if (!inherited) {
                    window.showToast(
                        `Dwell task ${i + 1} must follow a stop or waypoint with a target.`,
                        'error', 5000);
                    return false;
                }
                location = inherited;
            } else {
                location = this._taskLocationOrNull(t);
                if (!location) {
                    window.showToast(`Task ${i + 1} has no target selected.`, 'error', 5000);
                    return false;
                }
            }

            tasks.push({
                task_kind: kind,
                task_location: location,
                task_direction: t.task_direction || 'either',
                task_time: (t.task_time ?? null),
                target_time: (kind === 'stop' ? (t.target_time ?? null) : null)
            });
        }

        // Per design_new.md "RouteRun overrides": the three allowlisted
        // policy fields each carry their own push policy. Pack as
        // `{value, push}` so the server's per-field push logic picks it up.
        const segValue = (segId) => {
            const seg = document.getElementById(segId);
            if (!seg) return null;
            const active = seg.querySelector('.switch-btn.active');
            return active ? active.dataset.value : null;
        };

        const acqValue = segValue('route-acq-seg');
        const acqPush = segValue('route-acq-push-seg') || 'soft';
        const actionValue = segValue('route-action-seg');
        const actionPush = segValue('route-action-push-seg') || 'soft';
        const priorityRaw = document.getElementById('route-priority-input')?.value;
        const priorityPush = segValue('route-priority-push-seg') || 'soft';

        const payload = {
            name: this.currentRoute.name,
            mode: this.currentRoute.mode,
            tasks: tasks,
        };
        if (acqValue) {
            payload.acquisition_mode = { value: acqValue, push: acqPush };
        }
        if (actionValue) {
            payload.action_on_waypoint_unreachable = { value: actionValue, push: actionPush };
        }
        // Priority via the shared validator (helpers.js#validatePriorityInput)
        // so invalid user input surfaces a toast instead of silently dropping
        // the field per design_new.md "RouteRun overrides" (priority in 0..9).
        const priorityCheck = validatePriorityInput(priorityRaw);
        if (priorityCheck.kind === 'error') {
            window.showToast(priorityCheck.message, 'error', 4000);
            return false;
        }
        if (priorityCheck.kind === 'ok') {
            payload.priority = { value: priorityCheck.value, push: priorityPush };
        }

        // Warn if the route has no Stop tasks. A pure-waypoint route keeps the
        // live path extended across every task, so any task that would require
        // a turnaround fails with "extend would reverse at authority end" at
        // run time. Operators authoring forward-only loops can dismiss; anyone
        // else needs at least one stop to allow a direction change.
        if (tasks.length > 0 && !tasks.some(t => t.task_kind === 'stop')) {
            const ok = await showConfirmModal({
                title: 'Route has no Stop tasks',
                message: 'Save without any Stop task?',
                detail: (
                    'Waypoint and dwell tasks keep the live path extended — '
                    + "the train won't halt or reset its travel direction "
                    + 'between them. If any task ahead requires a turnaround, '
                    + 'the run will fail with "extend would reverse at '
                    + 'authority end". Add at least one Stop where the train '
                    + 'should be able to change direction.'
                ),
                confirmText: 'Save anyway',
                confirmVariant: 'primary',
            });
            if (!ok) return false;
        }

        try {
            await this.api.updateRoute(this.currentRoute.route_id, payload);
            window.showToast('Route saved', 'success', 3000);
            // Refresh the unsaved-changes baseline so subsequent dirty checks
            // see the saved state as clean.
            this._refreshSavedTasksBaseline();
            return true;
        } catch (err) {
            console.error('[RouteEditor] Failed to save route:', err);
            window.showToast('Failed to save route: ' + err.message, 'error', 5000);
            return false;
        }
    }

    /**
     * Activate the route
     */
    async activateRoute() {
        // The user's local task edits may not have been pushed yet — the
        // server-side activate handler rejects 0-task routes (see
        // train_route_service.activate_route ValidationError), so a user
        // who added 4 tasks visibly in this panel but didn't click Save
        // would see a confusing "no tasks" error from the server. Save
        // first if dirty, abort on save failure.
        const dirtyOk = await this._promptSaveIfDirty('Activate route');
        if (!dirtyOk) return;

        // Per design_new.md step `9c4f`: Phase 1 only picks up a route when
        // is_active=True AND the train is in the assigned list AND the train's
        // per-tick phase loop actually runs. With sim paused, "Activate" flips
        // is_active synchronously but nothing else happens until the user
        // resumes. Warn so the success toast can't mislead the user.
        const ok = await this._confirmRuntimePreconditions('Activate route');
        if (!ok) return;

        try {
            const resp = await this.api.activateRoute(this.currentRoute.route_id);
            // Use server-returned route as truth instead of optimistically
            // writing `is_active = true`; the server may have rejected an
            // invalid activation (e.g. 0-task route) — although we now 400
            // those before they reach here.
            this._mergeServerRoute(resp?.route);
            window.showToast('Route activated', 'success', 3000);
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to activate route:', err);
            window.showToast('Failed to activate route: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Deactivate the route
     */
    async deactivateRoute() {
        try {
            const resp = await this.api.deactivateRoute(this.currentRoute.route_id);
            this._mergeServerRoute(resp?.route);
            window.showToast('Route deactivated', 'success', 3000);
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to deactivate route:', err);
            window.showToast('Failed to deactivate route: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Pre-flight check before a route mutation that needs the per-train phase
     * loop to commit it. Both `PUT /api/trains/<id>/assign-route` and
     * `POST /api/train-routes/<id>/activate` return success synchronously, but
     * the runtime effect (RouteRun teardown / fresh RouteRun allocation,
     * path creation) only materializes when Phase 1 and Phase 3 actually run.
     * Per simulation.py:179/218, both phases are skipped while the controller
     * is OFF, and the sim thread itself is paused when
     * `app.isSimulationRunning === false`. In either state the user would see
     * a green "success" toast with no visible change. Warn first so the user
     * can choose to proceed deliberately.
     *
     * `trainId` is optional; pass it when the action targets a single train
     * (assign / unassign) so the warning can also catch power-off. For
     * route-level actions (activate / deactivate) leave it null and only the
     * sim-paused branch fires.
     *
     * Returns true when the action should proceed, false to abort.
     */
    async _confirmRuntimePreconditions(actionLabel, trainId = null) {
        const reasons = [];
        const app = window.app;
        const simPaused = !(app && app.isSimulationRunning);
        if (simPaused) {
            reasons.push('Simulation is paused.');
        }

        let trainOff = false;
        if (trainId) {
            try {
                const payload = await this.api.getTrain(trainId);
                const controller = payload?.controller;
                // controller.state is the TrainState enum lowercased on the
                // wire ("off" / "ready" / "running_moving" / ...). Treat both
                // an explicit "off" and a false power_on flag as off.
                trainOff = (
                    controller?.state === 'off'
                    || controller?.power_on === false
                );
            } catch (err) {
                console.warn(
                    '[RouteEditor] getTrain pre-flight check failed; '
                    + 'proceeding without train-state check:', err
                );
            }
        }
        if (trainOff) {
            reasons.push(`Train ${trainId} is powered off.`);
        }

        if (reasons.length === 0) {
            return true;
        }

        const detail = reasons.join(' ')
            + ` ${actionLabel} will only take effect once the simulation`
            + ` is running and the train is powered on. The change is`
            + ` recorded immediately but the train will not react until then.`;

        return await showConfirmModal({
            title: `${actionLabel}: precondition not met`,
            message: 'Continue anyway?',
            detail,
            confirmText: 'Proceed',
            confirmVariant: 'primary',
        });
    }

    /**
     * Assign a train to this route
     */
    async assignTrain() {
        const select = document.getElementById('assign-train-select');
        if (!select || !select.value) return;

        const trainId = select.value;
        const forceAssign = !!document.getElementById('assign-train-force')?.checked;

        // Save unsaved task edits first — see activateRoute() for context.
        // Without this, the server-side assign-route handler rejects 0-task
        // routes (see routes/trains.py) and the user sees a confusing
        // "no tasks" error despite seeing tasks in the panel.
        const dirtyOk = await this._promptSaveIfDirty('Assign train');
        if (!dirtyOk) return;

        // Per design_new.md "Route assignment writers": assign-route writes the
        // route_abort latch + updates Route.assigned_train_ids synchronously,
        // but the live RouteRun flip (and new RouteRun allocation against the
        // newly-assigned route) waits for the next per-train phase loop. If
        // sim is paused or T1 is OFF, that loop never fires and the user sees
        // a success toast with no visible effect. Warn explicitly.
        const ok = await this._confirmRuntimePreconditions('Assign train', trainId);
        if (!ok) return;

        try {
            await this.api.assignRoute(trainId, this.currentRoute.route_id, {
                force: forceAssign
            });

            // Don't mutate the assigned_train_ids array locally — the server
            // is the source of truth (the assign may auto-deactivate the
            // route if it had 0 tasks, the abort latch may not consume on
            // a paused sim, etc.). Re-fetch the route definition and per-
            // train status so the panel reflects server reality.
            await this._refetchRouteAfterLifecycleOp();
            window.showToast(`Train ${trainId} assigned`, 'success', 3000);

            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to assign train:', err);
            window.showToast('Failed to assign train: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Unassign a train from this route
     */
    async unassignTrain(trainId) {
        try {
            await this.api.assignRoute(trainId, null);

            await this._refetchRouteAfterLifecycleOp();
            window.showToast(`Train ${trainId} unassigned`, 'success', 3000);

            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to unassign train:', err);
            window.showToast('Failed to unassign train: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Pull the server's current view of `currentRoute` after a lifecycle
     * op (assign/unassign/activate/deactivate). Updates only
     * server-controlled fields and the per-train status payload — UI
     * edit fields (name/mode/tasks/priority/...) are untouched.
     */
    async _refetchRouteAfterLifecycleOp() {
        const routeId = this.currentRoute?.route_id;
        if (!routeId) return;
        try {
            const [route, status] = await Promise.all([
                this.api.getRoute(routeId),
                this.api.getRouteStatus(routeId),
            ]);
            this._mergeServerRoute(route);
            this.currentRouteStatus = status;
        } catch (err) {
            console.warn('[RouteEditor] post-op refetch failed:', err);
        }
    }

    /**
     * Copy server-controlled fields from a freshly-fetched `route` payload
     * onto `this.currentRoute`. Never overwrites edit-state fields. Used
     * by both the polling sync and post-lifecycle refetch.
     */
    _mergeServerRoute(route) {
        if (!route || !this.currentRoute) return;
        this.currentRoute.is_active = route.is_active;
        this.currentRoute.started_at = route.started_at;
        this.currentRoute.assigned_train_ids = route.assigned_train_ids;
        // last_edited_at also reflects server state; sync it so any other
        // panel reading it (history view, audit) sees the same timestamp.
        if ('last_edited_at' in route) {
            this.currentRoute.last_edited_at = route.last_edited_at;
        }
    }

}
