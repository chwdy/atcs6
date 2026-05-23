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
        // Collapsible run-history section.
        this._historyExpanded = false;
        this._historyData = null;
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
     * Show the routes list panel
     */
    async showRoutesPanel() {
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
                        ⟳
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
        const trainCount = route.assigned_train_ids?.length || 0;
        const taskCount = route.tasks?.length || 0;
        const statusClass = isActive ? 'is-active' : 'is-inactive';
        const modeText = (route.mode || 'one_off').replaceAll('_', ' ');
        const routeName = route.name || route.route_id;

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
                            <span>${trainCount} train${trainCount !== 1 ? 's' : ''}</span>
                            <span>${taskCount} task${taskCount !== 1 ? 's' : ''}</span>
                            <span class="route-mode-pill uppercase">${modeText}</span>
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

        // Check viability for all consecutive movement-task pairs
        await this.checkAllViability();

        this.rerenderRouteEditor();
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

        return `
            <div class="route-editor" data-route-id="${route.route_id}">
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

                <!-- Controls Section -->
                <div class="control-section">
                    <h4 class="text-sm text-muted mb-12">Controls</h4>
                    ${this.renderRouteStatusPill()}
                    <div class="flex gap-8 mb-12 mt-12">
                        ${isActive
                            ? `<button id="deactivate-route-btn" class="btn btn-danger btn-sm flex-1">Deactivate</button>`
                            : `<button id="activate-route-btn" class="btn btn-success btn-sm flex-1">Activate</button>`
                        }
                    </div>
                    <div class="flex gap-8">
                        <button id="save-route-btn" class="btn btn-primary btn-sm flex-1">Save</button>
                        <button id="back-to-routes-btn" class="btn btn-secondary btn-sm flex-1">Back</button>
                    </div>
                    <div class="flex gap-8 mt-8">
                        <button id="delete-route-btn" class="btn btn-danger btn-sm flex-1">Delete Route</button>
                    </div>
                </div>

                <!-- History Section -->
                ${this.renderHistorySection()}
            </div>
        `;
    }

    renderHistorySection() {
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
        const activatedText = data.activated_at != null
            ? `Activated ${this._formatTime(data.activated_at)}`
            : 'Never activated';
        const runs = (data.runs || []).slice().reverse(); // newest first
        const runsHtml = runs.length === 0
            ? '<div class="route-history-empty">No runs yet</div>'
            : runs.map(run => this.renderHistoryRun(run)).join('');
        return `
            <div class="route-history-activated">${activatedText}</div>
            ${runsHtml}
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
                ? `<span class="route-delay-chip ${t.delay_class}">${t.delay_class.replace('_', '-')}</span>`
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
            { value: 'one_off', label: 'One-off Run' },
            { value: 'circular', label: 'Circular Loop' },
            { value: 'round_trip', label: 'Round-trip Shuttle' }
        ];

        const modeSelect = modeOptions.map(opt =>
            `<option value="${opt.value}" ${route.mode === opt.value ? 'selected' : ''}>${opt.label}</option>`
        ).join('');

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
                        <select id="route-mode-select" class="property-input">
                            ${modeSelect}
                        </select>
                    </div>
                    <div class="route-header-col narrow info-display-small">
                         <label class="property-label info-label">Statistics</label>
                         <div class="route-stats-value info-meta">
                            ${taskCount} task${taskCount !== 1 ? 's' : ''} · ${assignedCount} train${assignedCount !== 1 ? 's' : ''}
                         </div>
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * A compact live-status readout for the Controls section: the route's
     * active state plus a per-train progress line when running. Reads the
     * status already fetched into `currentRouteStatus`.
     */
    renderRouteStatusPill() {
        if (!this.currentRoute?.is_active) {
            return `<div class="route-status-pill is-inactive"><span class="route-status-dot"></span>Inactive</div>`;
        }
        const trains = this.currentRouteStatus?.trains || [];
        if (trains.length === 0) {
            return `<div class="route-status-pill is-info"><span class="route-status-dot"></span>Active · no trains</div>`;
        }
        const lines = trains.map(ts => {
            const idx = ts.current_waypoint_index;
            const taskLabel = (idx === null || idx === undefined) ? 'queued' : `task ${idx + 1}`;
            const extras = [];
            if (ts.at_platform) extras.push('at platform');
            if (ts.dwell_remaining_sec != null) extras.push(`dwell ${Math.round(ts.dwell_remaining_sec)}s`);
            const extraText = extras.length ? ` · ${extras.join(' · ')}` : '';
            return `
                <div class="route-status-train">
                    <span class="font-mono">${ts.train_id}</span>
                    <span class="text-muted">${taskLabel}${extraText}</span>
                </div>`;
        }).join('');
        return `
            <div class="route-status-pill is-active"><span class="route-status-dot"></span>Active</div>
            <div class="route-status-trains">${lines}</div>
        `;
    }

    /**
     * Render one task row (1:1 with a RouteTask).
     */
    renderTaskRow(task, index) {
        const kind = task.task_kind || 'waypoint';
        const loc = task.task_location || {};
        const targetType = ('block_id' in loc) ? 'block'
            : ('station_id' in loc) ? 'station' : 'platform';
        const targetId = loc.block_id || loc.platform_id || loc.station_id || '';
        const direction = task.task_direction || 'either';
        const isDwell = kind === 'dwell';

        // Viability badge (only for movement tasks that are the 2nd+ movement).
        let viabilityHtml = '';
        if (!isDwell) {
            const viability = this.viabilityResults.get(index);
            if (viability !== undefined) {
                if (viability.viable) {
                    viabilityHtml = `<span class="text-success" title="Path viable: ${viability.hop_count} blocks">✓ ${viability.hop_count} blk</span>`;
                } else {
                    viabilityHtml = `<span class="text-danger" title="${viability.error || 'No path'}">✗ ${viability.error || 'No path'}</span>`;
                }
            }
        }

        const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
        const kindOptions = ['waypoint', 'stop', 'dwell'].map(k =>
            `<option value="${k}" ${kind === k ? 'selected' : ''}>${cap(k)}</option>`).join('');
        const typeOptions = ['block', 'platform', 'station'].map(t =>
            `<option value="${t}" ${targetType === t ? 'selected' : ''}>${cap(t)}</option>`).join('');
        const dirOptions = ['forward', 'reverse', 'either'].map(d =>
            `<option value="${d}" ${direction === d ? 'selected' : ''}>${cap(d)}</option>`).join('');

        const targetHtml = isDwell
            ? `
                <div class="waypoint-field waypoint-field-wide">
                    <label>Target</label>
                    <div class="text-xs text-muted">Holds at previous task's target</div>
                </div>`
            : `
                <div class="waypoint-field waypoint-field-wide">
                    <label>Target</label>
                    <div class="waypoint-target-row">
                        <select class="property-input input-compact task-target-type-select" data-index="${index}">${typeOptions}</select>
                        <select class="property-input input-compact task-target-id-select" data-index="${index}">${this.renderTargetOptions(targetType, targetId)}</select>
                    </div>
                </div>`;

        let timingHtml = '';
        if (isDwell) {
            timingHtml = `
                <div class="waypoint-field">
                    <label>Hold (s)</label>
                    <input type="number" min="0" step="1"
                           class="property-input input-compact task-time-input"
                           data-index="${index}" value="${task.task_time ?? ''}">
                </div>`;
        } else if (kind === 'stop') {
            timingHtml = `
                <div class="waypoint-field">
                    <label>Travel (s)</label>
                    <input type="number" min="0" step="1"
                           class="property-input input-compact task-time-input"
                           data-index="${index}" value="${task.task_time ?? ''}">
                </div>
                <div class="waypoint-field">
                    <label>Depart at (epoch s)</label>
                    <input type="number" min="0" step="1"
                           class="property-input input-compact task-target-time-input"
                           data-index="${index}" value="${task.target_time ?? ''}">
                </div>`;
        } else {
            timingHtml = `
                <div class="waypoint-field">
                    <label>Travel (s)</label>
                    <input type="number" min="0" step="1"
                           class="property-input input-compact task-time-input"
                           data-index="${index}" value="${task.task_time ?? ''}">
                </div>`;
        }

        const title = isDwell ? 'Dwell' : (this.getTargetDisplayName(targetType, targetId) || targetId || 'No target');
        const lastIndex = (this.currentRoute.tasks?.length || 1) - 1;
        const metaText = viabilityHtml ? `${cap(kind)} · ${viabilityHtml}` : cap(kind);

        return `
            <div class="waypoint-item waypoint-card mb-8 p-8 bg-input rounded-lg" data-index="${index}">
                <div class="waypoint-item-header">
                    <div class="flex flex-col">
                        <div class="flex items-center gap-8">
                            <span class="waypoint-ordinal">${index + 1}</span>
                            <span class="waypoint-title">${title}</span>
                        </div>
                        <div class="text-xs text-muted mt-2">${metaText}</div>
                    </div>
                    <div class="waypoint-actions flex gap-4">
                        <button class="btn btn-sm btn-secondary btn-icon-only locate-task-btn" data-index="${index}" title="Locate on map" aria-label="Locate on map">◎</button>
                        <button class="btn btn-sm btn-secondary btn-icon-only move-task-up" data-index="${index}" ${index === 0 ? 'disabled' : ''} title="Move up" aria-label="Move up">↑</button>
                        <button class="btn btn-sm btn-secondary btn-icon-only move-task-down" data-index="${index}" ${index >= lastIndex ? 'disabled' : ''} title="Move down" aria-label="Move down">↓</button>
                        <button class="btn btn-sm btn-danger btn-icon-only delete-task" data-index="${index}" title="Remove task" aria-label="Remove task">×</button>
                    </div>
                </div>

                <div class="waypoint-fields">
                    <div class="waypoint-field">
                        <label>Kind</label>
                        <select class="property-input input-compact task-kind-select" data-index="${index}">${kindOptions}</select>
                    </div>

                    <div class="waypoint-field">
                        <label>Direction</label>
                        <select class="property-input input-compact task-direction-select" data-index="${index}">${dirOptions}</select>
                    </div>

                    ${targetHtml}

                    ${timingHtml}
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
            this.renderer.drawNetwork(this.cachedNetwork);
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
            this.renderer.drawNetwork(this.cachedNetwork);
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
                const trainStatus = statusByTrainId.get(trainId);
                const taskText = trainStatus
                    ? `Task ${(trainStatus.current_waypoint_index ?? -1) + 1}`
                    : 'Task ?';
                assignedHtml += `
                    <div class="p-8 bg-input rounded-lg mb-4">
                        <div class="flex justify-between items-center mb-6">
                            <span class="font-mono text-white">${trainId}</span>
                            <button class="btn btn-sm btn-danger unassign-train-btn" data-train-id="${trainId}">Unassign</button>
                        </div>
                        <div class="flex items-center gap-8 text-xs">
                            <span class="text-muted">${taskText}</span>
                        </div>
                    </div>
                `;
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

        return `
            ${assignedHtml}
            <div class="flex gap-8">
                <select id="assign-train-select" class="property-input route-input-compact flex-1">
                    ${trainSelectHtml}
                </select>
                <button id="assign-train-btn" class="btn btn-sm btn-primary">Assign</button>
            </div>
            <div class="flex gap-8 mt-8">
                <button id="assign-train-map-btn" class="btn btn-sm btn-secondary flex-1" title="Click trains on the layout to assign them; eligible trains light up. Esc to finish.">Assign on map</button>
            </div>
            <label class="flex items-center gap-6 mt-8 text-xs text-muted route-option-hint">
                <input type="checkbox" id="assign-train-force" class="route-inline-checkbox" ${this._assignForce ? 'checked' : ''}> Force reassign
            </label>
        `;
    }

    initRouteEditorHandlers() {
        // Back button
        const backBtn = document.getElementById('back-to-routes-btn');
        if (backBtn) {
            backBtn.onclick = () => this.showRoutesPanel();
        }

        // Save button
        const saveBtn = document.getElementById('save-route-btn');
        if (saveBtn) {
            saveBtn.onclick = () => this.saveRoute();
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

        // Task field handlers
        document.querySelectorAll('.task-kind-select').forEach(select => {
            select.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                task.task_kind = e.target.value;
                // kind change toggles target/timing controls — re-render.
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.task-target-type-select').forEach(select => {
            select.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (!task) return;
                const type = e.target.value;
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

        document.querySelectorAll('.task-direction-select').forEach(select => {
            select.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (task) task.task_direction = e.target.value;
            };
        });

        document.querySelectorAll('.task-time-input').forEach(input => {
            input.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const task = this.currentRoute.tasks[index];
                if (task) task.task_time = this._parseOptionalNumber(e.target.value);
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
     * Append a blank waypoint task (default platform target) and re-render.
     */
    addTask() {
        if (!Array.isArray(this.currentRoute.tasks)) this.currentRoute.tasks = [];
        this.currentRoute.tasks.push({
            task_kind: 'waypoint',
            task_location: { platform_id: '' },
            task_direction: 'either',
            task_time: null,
            target_time: null
        });
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
            task_kind: 'waypoint',
            task_location: { [key]: opt.id },
            task_direction: 'either',
            task_time: null,
            target_time: null
        });
        await this.checkAllViability();
        this.rerenderRouteEditor();
        await this.refreshWaypointFeasibility();
        window.showToast(`Added waypoint: ${opt.label}`, 'success', 2000);
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

        try {
            await this.api.assignRoute(trainId, this.currentRoute.route_id, {
                force: this._assignForce
            });
            if (!this.currentRoute.assigned_train_ids.includes(trainId)) {
                this.currentRoute.assigned_train_ids.push(trainId);
            }
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
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
            if (loc) movement.push({ idx, loc });
        });

        if (movement.length < 2) return;

        const pairs = [];
        for (let i = 1; i < movement.length; i++) {
            pairs.push([movement[i - 1].loc, movement[i].loc]);
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
     */
    async saveRoute() {
        const nameInput = document.getElementById('route-name-input');
        const modeSelect = document.getElementById('route-mode-select');

        if (nameInput) {
            this.currentRoute.name = nameInput.value;
        }
        if (modeSelect) {
            this.currentRoute.mode = modeSelect.value;
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
                    return;
                }
                location = inherited;
            } else {
                location = this._taskLocationOrNull(t);
                if (!location) {
                    window.showToast(`Task ${i + 1} has no target selected.`, 'error', 5000);
                    return;
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

        try {
            await this.api.updateRoute(this.currentRoute.route_id, {
                name: this.currentRoute.name,
                mode: this.currentRoute.mode,
                tasks: tasks
            });
            window.showToast('Route saved', 'success', 3000);
        } catch (err) {
            console.error('[RouteEditor] Failed to save route:', err);
            window.showToast('Failed to save route: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Activate the route
     */
    async activateRoute() {
        try {
            await this.api.activateRoute(this.currentRoute.route_id);
            window.showToast('Route activated', 'success', 3000);
            this.currentRoute.is_active = true;
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
            await this.api.deactivateRoute(this.currentRoute.route_id);
            window.showToast('Route deactivated', 'success', 3000);
            this.currentRoute.is_active = false;
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to deactivate route:', err);
            window.showToast('Failed to deactivate route: ' + err.message, 'error', 5000);
        }
    }

    /**
     * Assign a train to this route
     */
    async assignTrain() {
        const select = document.getElementById('assign-train-select');
        if (!select || !select.value) return;

        const trainId = select.value;
        const forceAssign = !!document.getElementById('assign-train-force')?.checked;

        try {
            await this.api.assignRoute(trainId, this.currentRoute.route_id, {
                force: forceAssign
            });

            this.currentRoute.assigned_train_ids.push(trainId);
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
            window.showToast(`Train ${trainId} assigned`, 'success', 3000);

            // Refresh
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

            this.currentRoute.assigned_train_ids = this.currentRoute.assigned_train_ids.filter(
                id => id !== trainId
            );
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
            window.showToast(`Train ${trainId} unassigned`, 'success', 3000);

            // Refresh
            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to unassign train:', err);
            window.showToast('Failed to unassign train: ' + err.message, 'error', 5000);
        }
    }

}
