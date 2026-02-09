/**
 * Route Editor Handler
 * 
 * Manages train routes - sequences of waypoints defining where trains should go.
 * This is distinct from the Path Editor which handles block-level reservations.
 * 
 * Features:
 * - Create/edit routes with waypoints
 * - Click on platforms to add waypoints
 * - Real-time viability checking between consecutive waypoints
 * - Train assignment with multi-train support
 * - Route activation/deactivation
 */

class RouteEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this.routes = [];
        this.currentRoute = null;
        this.currentRouteStatus = null;
        this.waypointClickMode = false;
        this.viabilityResults = new Map(); // Cache viability results
    }

    get api() { return this.editor.api; }
    get renderer() { return this.editor.renderer; }
    get cachedNetwork() { return this.editor.cachedNetwork; }

    rerenderRouteEditor() {
        const content = this.buildRouteEditorHTML();
        this.editor.showSecondarySidebar('🛤️ Route Editor', content);
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
                <div class="flex gap-8 mb-16">
                    <button id="create-route-btn" class="btn btn-primary btn-sm flex-1">
                        + New Route
                    </button>
                    <button id="refresh-routes-btn" class="btn btn-secondary btn-sm">
                        🔄
                    </button>
                </div>
        `;

        if (this.routes.length === 0) {
            content += `
                <div class="text-muted text-center" style="padding: 32px 16px;">
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

        this.editor.showSecondarySidebar('🛤️ Routes', content);
        setTimeout(() => this.initRoutesPanelHandlers(), 0);
    }

    renderRouteListItem(route) {
        const isActive = route.is_active;
        const statusColor = isActive ? '#00ff88' : '#888';
        const statusText = isActive ? 'ACTIVE' : 'INACTIVE';
        const trainCount = route.assigned_train_ids?.length || 0;
        const waypointCount = route.waypoints?.length || 0;

        return `
            <div class="train-summary-row mb-8" data-route-id="${route.route_id}">
                <div class="flex items-center gap-12 w-full p-2">
                    <div class="flex flex-col items-center justify-center gap-2" style="min-width: 40px; padding-top: 4px;">
                        <div class="train-status-dot" style="background-color: ${statusColor}"></div>
                        <div class="text-xxs font-bold uppercase tracking-wider" style="color: ${statusColor}; font-size: 0.65rem; line-height: 1; padding: 4px 0; margin-top: 2px;">
                            ${statusText}
                        </div>
                    </div>
                    <div class="train-info flex-1" style="min-width: 0;">
                        <div class="flex flex-col gap-1 mb-6">
                            <div class="flex justify-between items-start gap-2">
                                <span class="train-id font-mono font-bold text-white" style="word-break: break-all; line-height: 1.2;">${route.name || route.route_id}</span>
                            </div>
                        </div>
                        <div class="flex justify-between items-end gap-2">
                            <div class="text-xs text-muted flex-1" style="min-width: 0;">
                                <div class="mb-4">🚂 ${trainCount} train${trainCount !== 1 ? 's' : ''} assigned</div>
                                <div>📍 ${waypointCount} waypoint${waypointCount !== 1 ? 's' : ''}</div>
                                <div class="text-xxs mt-2 uppercase">${route.mode || 'one_off'}</div>
                            </div>
                            <div class="flex gap-4 flex-shrink-0">
                                <button class="edit-btn btn-sm edit-route-btn" data-route-id="${route.route_id}">Edit</button>
                                <button class="btn btn-sm btn-danger text-white delete-route-btn" style="background-color: var(--danger-color);" data-route-id="${route.route_id}">Delete</button>
                            </div>
                        </div>
                    </div>
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
                if (!confirm(`Delete route "${route?.name || routeId}"?`)) return;

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
     * Create a new route
     */
    async createNewRoute() {
        const routeId = `ROUTE_${Date.now()}`;
        const newRoute = {
            route_id: routeId,
            name: `Route ${this.routes.length + 1}`,
            mode: 'one_off',
            waypoints: [],
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
            console.error('[RouteEditor] Failed to load route:', err);
            window.showToast('Failed to load route', 'error', 5000);
            return;
        }

        // Check viability for all consecutive waypoint pairs
        await this.checkAllViability();

        this.rerenderRouteEditor();
    }

    /**
     * Build the route editor HTML
     */
    buildRouteEditorHTML() {
        const route = this.currentRoute;
        const isActive = route.is_active;

        let waypointsHtml = '';
        if (route.waypoints?.length > 0) {
            waypointsHtml = '<div class="waypoints-list">';
            route.waypoints.forEach((wp, index) => {
                waypointsHtml += this.renderWaypointItem(wp, index);
            });
            waypointsHtml += '</div>';
        } else {
            waypointsHtml = `
                <div class="text-muted text-center text-sm" style="padding: 24px 16px; border: 2px dashed #3a3a4e; border-radius: 8px;">
                    Click "Add Waypoint" then click on a platform to add it to the route
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

                <!-- Waypoints Section -->
                <div class="control-section mb-16">
                    <div class="flex justify-between items-center mb-12">
                        <h4 class="text-sm text-muted">Waypoints</h4>
                        <button id="add-waypoint-btn" class="btn btn-sm btn-primary">
                            + Add Waypoint
                        </button>
                    </div>
                    ${waypointsHtml}
                </div>

                <!-- Train Assignment Section -->
                <div class="control-section mb-16">
                    <h4 class="text-sm text-muted mb-12">Assigned Trains</h4>
                    ${trainAssignmentHtml}
                </div>

                <!-- Controls Section -->
                <div class="control-section">
                    <h4 class="text-sm text-muted mb-12">Controls</h4>
                    <div class="flex gap-8 mb-12">
                        ${isActive 
                            ? `<button id="deactivate-route-btn" class="btn btn-danger btn-sm flex-1 text-white" style="background-color: var(--danger-color);">Deactivate</button>`
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
            </div>
        `;
    }

    renderRouteHeader(route) {
        const modeOptions = [
            { value: 'one_off', label: 'One-off' },
            { value: 'circular', label: 'Circular' },
            { value: 'round_trip', label: 'Round-trip' }
        ];

        const modeSelect = modeOptions.map(opt => 
            `<option value="${opt.value}" ${route.mode === opt.value ? 'selected' : ''}>${opt.label}</option>`
        ).join('');

        return `
            <div class="route-header">
                <div class="property-item mb-12">
                    <label class="property-label">Route Name</label>
                    <input type="text" id="route-name-input" class="property-input" 
                           value="${route.name || ''}" placeholder="Enter route name">
                </div>
                
                <div class="property-item">
                    <label class="property-label">Mode</label>
                    <select id="route-mode-select" class="property-input">
                        ${modeSelect}
                    </select>
                </div>
                
                <div class="text-xs text-muted mt-8" style="line-height: 1.4;">
                    ${route.mode === 'one_off' ? 'Train runs once through all waypoints then stops' : ''}
                    ${route.mode === 'circular' ? 'Train continuously cycles through waypoints' : ''}
                    ${route.mode === 'round_trip' ? 'Train bounces back and forth between endpoints' : ''}
                </div>
            </div>
        `;
    }

    renderWaypointItem(waypoint, index) {
        const viability = this.viabilityResults.get(index);
        let viabilityHtml = '';
        
        if (index > 0) {
            if (viability === undefined) {
                viabilityHtml = '<span class="text-muted">Checking...</span>';
            } else if (viability.viable) {
                viabilityHtml = `<span class="text-success">✓ ${viability.hop_count} blocks</span>`;
            } else {
                viabilityHtml = `<span class="text-danger">✗ ${viability.error || 'No path'}</span>`;
            }
        } else {
            viabilityHtml = '<span class="text-muted">Start</span>';
        }

        const platformName = waypoint.platform_id || 'Unknown';
        const dwellTime = waypoint.dwell_time_sec || 30;
        const isFlexible = waypoint.flexible_platform || false;
        const requiredSide = waypoint.required_platform_side || 'either';
        const directionPolicy = waypoint.travel_direction_policy || 'either';

        return `
            <div class="waypoint-item mb-8 p-8 bg-input rounded-lg" data-index="${index}">
                <div class="flex justify-between items-start mb-8">
                    <div class="flex items-center gap-8">
                        <span class="text-lg font-bold text-info">${index + 1}</span>
                        <span class="font-mono text-white">${platformName}</span>
                    </div>
                    <div class="flex gap-4">
                        ${index > 0 ? `<button class="btn btn-xs btn-secondary move-waypoint-up" data-index="${index}">↑</button>` : ''}
                        ${index < this.currentRoute.waypoints.length - 1 ? `<button class="btn btn-xs btn-secondary move-waypoint-down" data-index="${index}">↓</button>` : ''}
                        <button class="btn btn-xs btn-danger delete-waypoint" data-index="${index}">🗑</button>
                    </div>
                </div>
                
                <div class="text-xs mb-8 ${isFlexible ? 'text-success' : 'text-muted'}">
                    ${isFlexible ? '🔀 Flexible Platform' : 'Fixed Platform'}
                </div>

                <div class="mb-8">
                    <label class="property-label">Dwell (sec)</label>
                    <input type="number" min="0" step="1" class="property-input waypoint-dwell-input" data-index="${index}" value="${dwellTime}">
                </div>

                <label class="flex items-center gap-6 mb-8 text-xs text-muted">
                    <input type="checkbox" class="waypoint-flexible-toggle" data-index="${index}" ${isFlexible ? 'checked' : ''}>
                    Flexible platform
                </label>

                <div class="mb-8">
                    <label class="property-label">Required Side</label>
                    <select class="property-input waypoint-side-select" data-index="${index}" ${isFlexible ? '' : 'disabled'}>
                        <option value="either" ${requiredSide === 'either' ? 'selected' : ''}>Either</option>
                        <option value="left" ${requiredSide === 'left' ? 'selected' : ''}>Left</option>
                        <option value="right" ${requiredSide === 'right' ? 'selected' : ''}>Right</option>
                        <option value="both" ${requiredSide === 'both' ? 'selected' : ''}>Both</option>
                    </select>
                </div>

                <div class="mb-8">
                    <label class="property-label">Direction Policy</label>
                    <select class="property-input waypoint-direction-select" data-index="${index}" ${isFlexible ? '' : 'disabled'}>
                        <option value="either" ${directionPolicy === 'either' ? 'selected' : ''}>Either</option>
                        <option value="preserve" ${directionPolicy === 'preserve' ? 'selected' : ''}>Preserve</option>
                    </select>
                </div>
                
                <div class="flex justify-between items-center text-xs">
                    <span class="text-muted">⏱ ${dwellTime}s dwell</span>
                    <span>${viabilityHtml}</span>
                </div>
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
                const trainStatus = statusByTrainId.get(trainId);
                const wpText = trainStatus
                    ? `WP ${trainStatus.current_waypoint_index + 1}`
                    : 'WP ?';
                assignedHtml += `
                    <div class="p-8 bg-input rounded-lg mb-4">
                        <div class="flex justify-between items-center mb-6">
                            <span class="font-mono text-white">🚂 ${trainId}</span>
                            <button class="btn btn-xs btn-danger unassign-train-btn" data-train-id="${trainId}">Unassign</button>
                        </div>
                        <div class="flex items-center gap-8 text-xs">
                            <span class="text-muted">${wpText}</span>
                            <input type="number" min="1" step="1" class="property-input set-waypoint-input" style="max-width: 80px;" value="${(trainStatus?.current_waypoint_index || 0) + 1}" data-train-id="${trainId}">
                            <button class="btn btn-xs btn-secondary set-waypoint-btn" data-train-id="${trainId}">Set WP</button>
                        </div>
                    </div>
                `;
            });
            assignedHtml += '</div>';
        } else {
            assignedHtml = '<div class="text-muted text-sm mb-12">No trains assigned</div>';
        }

        // Get available trains (not assigned to this route)
        const allTrains = this.cachedNetwork?.trains || [];
        const availableTrains = allTrains.filter(t => !assignedTrains.includes(t.train_id));

        let trainSelectHtml = '<option value="">Select train...</option>';
        availableTrains.forEach(train => {
            trainSelectHtml += `<option value="${train.train_id}">${train.train_id}</option>`;
        });

        return `
            ${assignedHtml}
            <div class="flex gap-8">
                <select id="assign-train-select" class="property-input flex-1">
                    ${trainSelectHtml}
                </select>
                <button id="assign-train-btn" class="btn btn-sm btn-primary">Assign</button>
            </div>
            <label class="flex items-center gap-6 mt-8 text-xs text-muted">
                <input type="checkbox" id="assign-train-force"> Force reassign
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
                if (!confirm(`Delete route "${this.currentRoute?.name || this.currentRoute?.route_id}"?`)) {
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

        // Add waypoint button
        const addWaypointBtn = document.getElementById('add-waypoint-btn');
        if (addWaypointBtn) {
            addWaypointBtn.onclick = () => this.enterWaypointClickMode();
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

        // Waypoint reorder/delete buttons
        document.querySelectorAll('.move-waypoint-up').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.target.dataset.index);
                this.moveWaypoint(index, -1);
            };
        });

        document.querySelectorAll('.move-waypoint-down').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.target.dataset.index);
                this.moveWaypoint(index, 1);
            };
        });

        document.querySelectorAll('.delete-waypoint').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.target.dataset.index);
                this.deleteWaypoint(index);
            };
        });

        document.querySelectorAll('.waypoint-dwell-input').forEach(input => {
            input.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                const value = parseFloat(e.target.value);
                if (!Number.isFinite(value) || value < 0) {
                    e.target.value = this.currentRoute.waypoints[index]?.dwell_time_sec ?? 30;
                    return;
                }
                this.currentRoute.waypoints[index].dwell_time_sec = value;
            };
        });

        document.querySelectorAll('.waypoint-flexible-toggle').forEach(toggle => {
            toggle.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                this.currentRoute.waypoints[index].flexible_platform = !!e.target.checked;
                this.rerenderRouteEditor();
            };
        });

        document.querySelectorAll('.waypoint-side-select').forEach(select => {
            select.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                this.currentRoute.waypoints[index].required_platform_side = e.target.value;
            };
        });

        document.querySelectorAll('.waypoint-direction-select').forEach(select => {
            select.onchange = (e) => {
                const index = parseInt(e.target.dataset.index, 10);
                this.currentRoute.waypoints[index].travel_direction_policy = e.target.value;
            };
        });

        // Train assignment buttons
        const assignTrainBtn = document.getElementById('assign-train-btn');
        if (assignTrainBtn) {
            assignTrainBtn.onclick = () => this.assignTrain();
        }

        document.querySelectorAll('.unassign-train-btn').forEach(btn => {
            btn.onclick = (e) => {
                const trainId = e.target.dataset.trainId;
                this.unassignTrain(trainId);
            };
        });

        document.querySelectorAll('.set-waypoint-btn').forEach(btn => {
            btn.onclick = (e) => {
                const trainId = e.target.dataset.trainId;
                const input = document.querySelector(`.set-waypoint-input[data-train-id="${trainId}"]`);
                const waypointIndex = (parseInt(input?.value || '1', 10) || 1) - 1;
                this.setTrainWaypoint(trainId, waypointIndex);
            };
        });
    }

    /**
     * Enter mode to click on platforms to add waypoints
     */
    enterWaypointClickMode() {
        this.waypointClickMode = true;
        document.getElementById('status-state').textContent = 'Route Editor: Click on a platform to add waypoint';
        
        // Highlight all platforms
        if (this.renderer.highlightPlatforms) {
            const platforms = this.cachedNetwork?.platforms || this.cachedNetwork?.stations || [];
            this.renderer.highlightPlatforms(platforms.map(p => p.platform_id || p.station_id));
        }
    }

    /**
     * Exit waypoint click mode
     */
    exitWaypointClickMode() {
        this.waypointClickMode = false;
        document.getElementById('status-state').textContent = 'Ready';
        
        if (this.renderer.clearPlatformHighlights) {
            this.renderer.clearPlatformHighlights();
        }
    }

    /**
     * Handle click when in waypoint click mode
     */
    async handleWaypointClick(gridPos) {
        if (!this.waypointClickMode) return false;

        // Find platform at this position
        const platform = this.findPlatformAtPosition(gridPos.x, gridPos.y);
        if (!platform) {
            window.showToast('No platform at this position. Click on a platform tile.', 'warning', 3000);
            return true; // Consumed the click
        }

        // Add waypoint
        const platformId = platform.platform_id || platform.station_id;
        const newWaypoint = {
            platform_id: platformId,
            dwell_time_sec: 30,
            flexible_platform: false,
            required_platform_side: 'either',
            travel_direction_policy: 'either',
            timing_mode: 'none'
        };

        this.currentRoute.waypoints.push(newWaypoint);
        this.exitWaypointClickMode();
        
        // Refresh editor
        await this.checkAllViability();
        this.rerenderRouteEditor();

        window.showToast(`Added waypoint: ${platform.name || platformId}`, 'success', 3000);
        return true; // Consumed the click
    }

    /**
     * Find platform at a grid position
     */
    findPlatformAtPosition(x, y) {
        const platforms = this.cachedNetwork?.platforms || this.cachedNetwork?.stations || [];
        return platforms.find(p => 
            p.positions?.some(pos => pos[0] === x && pos[1] === y)
        );
    }

    /**
     * Move waypoint up or down in the list
     */
    async moveWaypoint(index, direction) {
        const waypoints = this.currentRoute.waypoints;
        const newIndex = index + direction;

        if (newIndex < 0 || newIndex >= waypoints.length) return;

        // Swap
        [waypoints[index], waypoints[newIndex]] = [waypoints[newIndex], waypoints[index]];

        // Refresh
        await this.checkAllViability();
        this.rerenderRouteEditor();
    }

    /**
     * Delete a waypoint
     */
    async deleteWaypoint(index) {
        if (!confirm('Delete this waypoint?')) return;

        this.currentRoute.waypoints.splice(index, 1);

        // Refresh
        await this.checkAllViability();
        this.rerenderRouteEditor();
    }

    /**
     * Check viability for all consecutive waypoint pairs
     */
    async checkAllViability() {
        const waypoints = this.currentRoute.waypoints || [];
        if (waypoints.length < 2) {
            this.viabilityResults.clear();
            return;
        }

        const pairs = [];
        for (let i = 1; i < waypoints.length; i++) {
            pairs.push([waypoints[i-1].platform_id, waypoints[i].platform_id]);
        }

        try {
            const results = await this.api.checkViability(pairs);
            this.viabilityResults.clear();
            results.results?.forEach((result, idx) => {
                this.viabilityResults.set(idx + 1, result);
            });
        } catch (err) {
            console.error('[RouteEditor] Viability check failed:', err);
        }
    }

    /**
     * Save route changes
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

        try {
            await this.api.updateRoute(this.currentRoute.route_id, {
                name: this.currentRoute.name,
                mode: this.currentRoute.mode,
                waypoints: this.currentRoute.waypoints
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
                force: forceAssign,
                auto_park: true
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
            await this.api.assignRoute(trainId, null, {
                auto_park: true
            });
            
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

    async setTrainWaypoint(trainId, waypointIndex) {
        try {
            await this.api.setTrainWaypoint(trainId, waypointIndex);
            this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
            window.showToast(`Train ${trainId} waypoint set to ${waypointIndex + 1}`, 'success', 3000);

            this.rerenderRouteEditor();
        } catch (err) {
            console.error('[RouteEditor] Failed to set waypoint:', err);
            window.showToast('Failed to set waypoint: ' + err.message, 'error', 5000);
        }
    }
}
