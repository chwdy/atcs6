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
        this.waypointHighlightTimeout = null;
    }

    get api() { return this.editor.api; }
    get renderer() { return this.editor.renderer; }
    get cachedNetwork() { return this.editor.cachedNetwork; }

    rerenderRouteEditor() {
        this.normalizeWaypointFlexibility();
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
                <div class="routes-toolbar mb-16">
                    <button id="create-route-btn" class="btn btn-primary btn-sm flex-1">
                        + New Route
                    </button>
                    <button id="refresh-routes-btn" class="btn btn-secondary btn-sm btn-icon-only" title="Refresh routes" aria-label="Refresh routes">
                        🔄
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

        this.editor.showSecondarySidebar('🛤️ Routes', content);
        setTimeout(() => this.initRoutesPanelHandlers(), 0);
    }

    renderRouteListItem(route) {
        const isActive = route.is_active;
        const statusText = isActive ? 'ACTIVE' : 'INACTIVE';
        const trainCount = route.assigned_train_ids?.length || 0;
        const waypointCount = route.waypoints?.length || 0;
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
                        <div class="route-row-meta text-xs text-muted">
                            <span>🚂 ${trainCount} train${trainCount !== 1 ? 's' : ''}</span>
                            <span>📍 ${waypointCount} waypoint${waypointCount !== 1 ? 's' : ''}</span>
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
                    detail: 'This removes its waypoints and train assignments.',
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
                <div class="route-empty-dashed text-muted text-center text-sm">
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
            </div>
        `;
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

        const waypointCount = route.waypoints?.length || 0;
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
                    <div class="route-header-col narrow">
                         <label class="property-label">Statistics</label>
                         <div class="property-value route-stats-value">
                            <span class="text-xs text-muted">${waypointCount} stops · ${assignedCount} trains</span>
                         </div>
                    </div>
                </div>
            </div>
        `;
    }

    renderWaypointItem(waypoint, index) {
        const viability = this.viabilityResults.get(index);
        let viabilityHtml = '';
        
        if (index > 0) {
            if (viability === undefined) {
                viabilityHtml = '<span class="text-muted" title="Checking path...">⏳</span>';
            } else if (viability.viable) {
                viabilityHtml = `<span class="text-success" title="Path viable: ${viability.hop_count} blocks">✓ ${viability.hop_count} blk</span>`;
            } else {
                viabilityHtml = `<span class="text-danger" title="${viability.error || 'No path'}">✗ Error</span>`;
            }
        }

        const platformInfo = this.getPlatformInfo(waypoint.platform_id);
        const platformName = platformInfo.name || waypoint.platform_id || 'Unknown';
        const platformId = waypoint.platform_id || 'Unknown';
        
        const dwellTime = waypoint.dwell_time_sec || 30;
        const supportsFlexible = this.canUseFlexiblePlatform(waypoint.platform_id);
        const isFlexible = supportsFlexible && !!waypoint.flexible_platform;
        const requiredSide = waypoint.required_platform_side || 'either';
        const directionPolicy = waypoint.travel_direction_policy || 'either';
        
        const stationGroup = this.getStationGroupForPlatform(waypoint.platform_id);
        const stationGroupName = stationGroup?.name || stationGroup?.station_id || 'Ungrouped';
        const stationHint = stationGroup 
            ? `${stationGroupName}`
            : `ID: ${platformId}`;

        // Flexible controls in a dedicated, visually distinct area
        const flexibleOptionsHtml = isFlexible
            ? `
                <div class="waypoint-flexible-area">
                    <div class="flex gap-8">
                        <div class="flex-1">
                            <label class="property-label mb-4">Platform Side</label>
                            <select class="property-input input-compact waypoint-side-select" data-index="${index}">
                                <option value="either" ${requiredSide === 'either' ? 'selected' : ''}>Any Side</option>
                                <option value="left" ${requiredSide === 'left' ? 'selected' : ''}>Left</option>
                                <option value="right" ${requiredSide === 'right' ? 'selected' : ''}>Right</option>
                                <option value="both" ${requiredSide === 'both' ? 'selected' : ''}>Both</option>
                            </select>
                        </div>
                        <div class="flex-1">
                            <label class="property-label mb-4">Direction</label>
                            <select class="property-input input-compact waypoint-direction-select" data-index="${index}">
                                <option value="either" ${directionPolicy === 'either' ? 'selected' : ''}>Any Dir</option>
                                <option value="preserve" ${directionPolicy === 'preserve' ? 'selected' : ''}>Keep</option>
                            </select>
                        </div>
                    </div>
                </div>
            ` : '';

        // Standardized card layout
        return `
            <div class="waypoint-item waypoint-card mb-8 p-8 bg-input rounded-lg" data-index="${index}">
                <div class="waypoint-item-header">
                    <div class="flex flex-col">
                        <div class="flex items-center gap-8">
                            <span class="text-lg font-bold text-info">${index + 1}</span>
                            <span class="waypoint-title">${platformName}</span>
                        </div>
                        <div class="text-xs text-muted mt-2">${stationHint} · ${viabilityHtml}</div>
                    </div>
                    <div class="flex gap-4">
                        <button class="btn btn-sm btn-secondary btn-icon-only locate-waypoint-btn" data-index="${index}" title="Locate on Map">📍</button>
                        <button class="btn btn-sm btn-danger btn-icon-only delete-waypoint" data-index="${index}" title="Remove Stop">×</button>
                    </div>
                </div>
                
                <div class="waypoint-controls-row">
                    <div class="waypoint-control-group">
                        <label>Dwell (s)</label>
                        <input type="number" min="0" step="1" 
                               class="property-input input-compact input-tiny waypoint-dwell-input" 
                               data-index="${index}" value="${dwellTime}">
                    </div>

                    <div class="waypoint-control-group flex-1">
                         <!-- Spacer or future controls -->
                    </div>

                    <div class="waypoint-control-group">
                        <label>Order</label>
                        <div class="flex gap-2">
                             <button class="btn btn-sm btn-secondary btn-icon-only move-waypoint-up" data-index="${index}" ${index === 0 ? 'disabled' : ''}>↑</button>
                             <button class="btn btn-sm btn-secondary btn-icon-only move-waypoint-down" data-index="${index}" ${index >= this.currentRoute.waypoints.length - 1 ? 'disabled' : ''}>↓</button>
                        </div>
                    </div>

                    <div class="waypoint-control-group">
                        <label>Options</label>
                        <label class="btn btn-sm waypoint-flex-toggle ${isFlexible ? 'btn-success' : 'btn-secondary'} btn-icon-only flex items-center gap-4"
                               title="${supportsFlexible ? 'Enable flexible platform assignment' : 'Flexible assignment unavailable (single platform station)'}">
                            <input type="checkbox" class="waypoint-flexible-toggle" 
                                   data-index="${index}" ${isFlexible ? 'checked' : ''} 
                                   ${supportsFlexible ? '' : 'disabled'}>
                            <span class="waypoint-flex-label">Flex</span>
                        </label>
                    </div>
                </div>

                ${flexibleOptionsHtml}
            </div>
        `;
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

    canUseFlexiblePlatform(platformId) {
        const stationGroup = this.getStationGroupForPlatform(platformId);
        const platformCount = stationGroup?.platform_ids?.length || 0;
        return platformCount > 1;
    }

    getFlexiblePlatformContext(platformId) {
        const stationGroup = this.getStationGroupForPlatform(platformId);
        const platformCount = stationGroup?.platform_ids?.length || 0;
        const stationName = stationGroup?.name || stationGroup?.station_id || 'station';
        return `${platformCount} platforms in ${stationName}`;
    }

    normalizeWaypointFlexibility() {
        const waypoints = this.currentRoute?.waypoints || [];
        waypoints.forEach(waypoint => {
            if (!this.canUseFlexiblePlatform(waypoint.platform_id)) {
                waypoint.flexible_platform = false;
                waypoint.required_platform_side = 'either';
                waypoint.travel_direction_policy = 'either';
            }
        });
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

    clearWaypointLocationHighlight() {
        if (this.waypointHighlightTimeout) {
            clearTimeout(this.waypointHighlightTimeout);
            this.waypointHighlightTimeout = null;
        }
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
        }
    }

    highlightPlatformLocations(platformIds, autoClearMs = 0) {
        const positions = this.getPlatformPositionSet(platformIds);
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(positions);
        }
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
        }

        if (this.waypointHighlightTimeout) {
            clearTimeout(this.waypointHighlightTimeout);
            this.waypointHighlightTimeout = null;
        }

        if (autoClearMs > 0) {
            this.waypointHighlightTimeout = setTimeout(() => {
                this.clearWaypointLocationHighlight();
            }, autoClearMs);
        }
    }

    focusWaypointLocation(index) {
        const waypoint = this.currentRoute?.waypoints?.[index];
        if (!waypoint) return;

        const platformInfo = this.getPlatformInfo(waypoint.platform_id);
        if (!platformInfo.positions.length) {
            window.showToast(`No map geometry found for ${waypoint.platform_id}`, 'warning', 3000);
            return;
        }

        this.highlightPlatformLocations([waypoint.platform_id], 5000);
        const displayName = platformInfo.name || waypoint.platform_id;
        document.getElementById('status-state').textContent = `Route Editor: Highlighting waypoint ${index + 1} (${displayName})`;
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
                            <button class="btn btn-sm btn-danger unassign-train-btn" data-train-id="${trainId}">Unassign</button>
                        </div>
                        <div class="flex items-center gap-8 text-xs">
                            <span class="text-muted">${wpText}</span>
                            <input type="number" min="1" step="1" class="property-input route-input-compact route-number-input set-waypoint-input" value="${(trainStatus?.current_waypoint_index || 0) + 1}" data-train-id="${trainId}">
                            <button class="btn btn-sm btn-secondary set-waypoint-btn" data-train-id="${trainId}">Set WP</button>
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
            <label class="flex items-center gap-6 mt-8 text-xs text-muted route-option-hint">
                <input type="checkbox" id="assign-train-force" class="route-inline-checkbox"> Force reassign
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
                    detail: 'This removes all waypoints and train assignments for this route.',
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

        document.querySelectorAll('.locate-waypoint-btn').forEach(btn => {
            btn.onclick = (e) => {
                const index = parseInt(e.currentTarget.dataset.index, 10);
                this.focusWaypointLocation(index);
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
                const waypoint = this.currentRoute.waypoints[index];
                if (!waypoint) return;

                if (!this.canUseFlexiblePlatform(waypoint.platform_id)) {
                    waypoint.flexible_platform = false;
                    e.target.checked = false;
                    window.showToast('Flexible platform is only available for stations with multiple platforms.', 'warning', 3000);
                    return;
                }

                waypoint.flexible_platform = !!e.target.checked;
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

        const platformIds = this.getPlatforms().map(p => this.getPlatformId(p)).filter(Boolean);
        this.highlightPlatformLocations(platformIds);
    }

    /**
     * Exit waypoint click mode
     */
    exitWaypointClickMode() {
        this.waypointClickMode = false;
        document.getElementById('status-state').textContent = 'Ready';

        this.clearWaypointLocationHighlight();
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
        const track = this.cachedNetwork?.tracks?.find(t => t.x === x && t.y === y);
        const platformId = track?.station_id;
        if (!platformId) return null;

        return this.getPlatforms().find(p => this.getPlatformId(p) === platformId) || null;
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
        const confirmed = await showConfirmModal({
            title: 'Delete Waypoint',
            message: 'Delete this waypoint?',
            detail: 'Route order and viability will be recalculated.',
            confirmText: 'Delete',
            confirmVariant: 'danger'
        });
        if (!confirmed) return;

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
