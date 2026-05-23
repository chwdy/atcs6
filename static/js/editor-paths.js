class PathEditorHandler {
    constructor(editor) {
        this.editor = editor;
    }

    get api() { return this.editor.api; }
    get renderer() { return this.editor.renderer; }
    get cachedNetwork() { return this.editor.cachedNetwork; }
    get cachedPaths() { return this.editor.cachedPaths; }
    set cachedPaths(value) { this.editor.cachedPaths = value; }
    get pathToolState() { return this.editor.pathToolState; }
    set pathToolState(value) { this.editor.pathToolState = value; }
    get extendPathState() { return this.editor.extendPathState; }
    set extendPathState(value) { this.editor.extendPathState = value; }

    enterRouteToolMode() {
        this.pathToolState = {
            active: false,
            startBlockId: null,
            startPosition: null,
            reachableEndpoints: [],
            reachablePositions: new Set(),
            requestedDirection: 'forward',
            acquisitionMode: 'all_or_nothing',
            trainId: null
        };
        document.getElementById('status-state').textContent = 'Path: Click train (start) block';
    }

    async exitRouteToolMode() {
        await this.resetRouteToolState();
        document.getElementById('status-state').textContent = 'Ready';
    }

    // EditorMode factory for the route-creation tool.
    buildRouteToolMode() {
        return {
            name: 'Route creation',
            toolName: 'path',
            cancelMessage: 'Route creation cancelled',
            onEnter: () => this.enterRouteToolMode(),
            onClick: async ({ gridPos }) => {
                await this.handleRouteToolClick(gridPos);
                return true;
            },
            onExit: () => this.exitRouteToolMode()
        };
    }

    async resetRouteToolState() {
        this.pathToolState = {
            active: false,
            startBlockId: null,
            startPosition: null,
            reachableEndpoints: [],
            reachablePositions: new Set(),
            requestedDirection: 'forward',
            acquisitionMode: 'all_or_nothing',
            trainId: null
        };
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.renderer.clearRoutePreview) {
            this.renderer.clearRoutePreview();
        }
        if (this.editor._currentEditorType === 'path-setup') {
            await this.editor.goBackSecondaryPanel();
            // If history was empty (no panel before path tool), goBack does nothing.
            // In that case _currentEditorType is still 'path-setup', so close the sidebar.
            if (this.editor._currentEditorType === 'path-setup') {
                this.editor.hideSecondarySidebar();
            }
        }
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
        }
        document.getElementById('status-state').textContent = 'Path: Click start block';
    }

    async handleRouteToolClick(gridPos) {
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);

        if (!this.pathToolState.active) {
            await this.handleRouteFirstClick(gridPos, track);
        } else {
            await this.handleRouteSecondClick(gridPos, track);
        }
    }

    async handleRouteFirstClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const blockId = track.block_section_id;
        if (!blockId) {
            window.showToast('Must start path from a block', 'warning', 3000);
            return;
        }

        this.pathToolState.startBlockId = blockId;
        this.pathToolState.startPosition = { x: gridPos.x, y: gridPos.y };
        this.pathToolState.active = true;

        const block = this.cachedNetwork?.blocks?.[blockId];
        if (block?.occupied_by) {
            this.pathToolState.trainId = block.occupied_by;
        } else {
            window.showToast('No train at start block; pick a block occupied by a train', 'warning', 3000);
            await this.resetRouteToolState();
            return;
        }

        try {
            document.getElementById('status-state').textContent = 'Path: Loading destinations...';
            const result = await this.api.getReachableEndpoints(blockId);
            this.pathToolState.reachableEndpoints = result.reachable_endpoints || [];

            this.buildReachablePositionsSet();

            const count = this.pathToolState.reachableEndpoints.length;
            document.getElementById('status-state').textContent = `Path: Click destination (${count} reachable)`;

            if (this.renderer.setReachableHighlights) {
                this.renderer.setReachableHighlights(this.pathToolState.reachablePositions);
            }

            this.showRoutePrioritySelector();

            if (this.cachedNetwork) {
                this.renderer.drawNetwork(this.cachedNetwork);
            }
        } catch (err) {
            console.error('[Editor] Failed to get reachable destinations:', err);
            window.showToast('Failed to get reachable destinations: ' + err.message, 'error', 5000);
            await this.resetRouteToolState();
        }
    }

    buildReachablePositionsSet() {
        const positions = new Set();
        const tracks = this.cachedNetwork?.tracks || [];

        for (const endpoint of this.pathToolState.reachableEndpoints) {
            for (const track of tracks) {
                if (track.block_section_id === endpoint) {
                    positions.add(`${track.x},${track.y}`);
                }
            }
        }

        this.pathToolState.reachablePositions = positions;
    }

    async handleRouteSecondClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const endBlockId = track.block_section_id;
        if (!endBlockId) {
            window.showToast('Must end path on a block', 'warning', 3000);
            return;
        }

        if (!this.pathToolState.reachableEndpoints.includes(endBlockId)) {
            window.showToast('Cannot reach this block from start', 'warning', 3000);
            return;
        }

        try {
            document.getElementById('status-state').textContent = 'Path: Creating...';

            const options = {
                train_id: this.pathToolState.trainId,
                requested_destination_direction: this.pathToolState.requestedDirection,
                acquisition_mode: this.pathToolState.acquisitionMode
            };

            const result = await this.createPathWithConflictResolution(endBlockId, options);

            if (result.success) {
                window.showToast(`Path ${result.path_id} created`, 'success', 5000);
                await window.app.refreshNetwork();
                await this.resetRouteToolState();
            } else {
                const errorMsg = result.error || result.failure_kind || 'Unknown error';
                window.showToast(`Path failed: ${errorMsg}`, 'error', 5000);

                if (result.conflict_tiles && this.renderer.flashConflictTiles) {
                    this.renderer.flashConflictTiles(result.conflict_tiles, 2000);
                }
            }
        } catch (err) {
            console.error('[Editor] Failed to create path:', err);
            window.showToast('Failed to create path: ' + err.message, 'error', 5000);
        }
    }

    async createPathWithConflictResolution(endBlockId, options) {
        const buildArgs = () => ({
            train_id: options.train_id,
            requested_destination_block_id: endBlockId,
            requested_destination_direction: options.requested_destination_direction || 'forward',
            acquisition_mode: options.acquisition_mode || 'all_or_nothing'
        });

        try {
            return await this.api.createPath(buildArgs());
        } catch (err) {
            if (err.status !== 409 || err.conflict !== 'train_has_active_path') {
                throw err;
            }

            const existingPathId = err.existing_path_id;
            const trainId = err.train_id || options.train_id;
            const existingPath = err.existing_path || {};
            const detail =
                `Existing path: ${existingPathId}` +
                (existingPath.authority_end_block_id
                    ? `\nDestination: ${existingPath.authority_end_block_id}`
                    : '');

            const confirmed = await showConfirmModal({
                title: 'Train Already Has Active Path',
                message: `Train ${trainId} already has an active path. Cancel it and assign the new path?`,
                detail,
                confirmText: 'Cancel Existing Path',
                confirmVariant: 'danger'
            });
            if (!confirmed) {
                return { success: false, error: 'Path assignment cancelled' };
            }

            const cancelResult = await this.api.cancelPath(existingPathId);
            if (!cancelResult.success) {
                return {
                    success: false,
                    error: cancelResult.error || cancelResult.message || 'Failed to cancel existing path'
                };
            }

            return await this.api.createPath(buildArgs());
        }
    }

    showRoutePrioritySelector() {
        this.editor._currentEditorType = 'path-setup';
        this.editor._currentEditorId = null;

        const trainInfo = this.pathToolState.trainId
            ? `
                <div class="info-display-row info-display-small">
                    <span class="info-label">Train</span>
                    <span class="info-value text-info">🚂 ${this.pathToolState.trainId}</span>
                </div>
            `
            : '<div class="text-muted text-sm py-8">No train at start block</div>';

        const direction = this.pathToolState.requestedDirection || 'forward';
        const mode = this.pathToolState.acquisitionMode || 'all_or_nothing';

        const content = `
            <div class="path-setup-panel">
                ${renderHeaderId(this.pathToolState.startBlockId, '#00d4ff')}

                <div class="property-item">
                    <label class="property-label">Direction</label>
                    <div class="switch-group">
                        <button id="path-direction-forward-btn"
                                class="switch-btn ${direction === 'forward' ? 'active' : ''}"
                                data-direction="forward">▶ Forward</button>
                        <button id="path-direction-reverse-btn"
                                class="switch-btn ${direction === 'reverse' ? 'active' : ''}"
                                data-direction="reverse">◀ Reverse</button>
                    </div>
                    <div class="text-xxs text-muted mt-4">
                        Direction the train should travel away from its current position.
                    </div>
                </div>

                <div class="property-item">
                    <label class="property-label">Acquisition Mode</label>
                    <div class="switch-group">
                        <button id="path-mode-all-btn"
                                class="switch-btn ${mode === 'all_or_nothing' ? 'active' : ''}"
                                data-mode="all_or_nothing">All-or-nothing</button>
                        <button id="path-mode-best-btn"
                                class="switch-btn ${mode === 'best_effort' ? 'active' : ''}"
                                data-mode="best_effort">Best-effort</button>
                    </div>
                    <div class="text-xxs text-muted mt-4">
                        All-or-nothing: fail if any resource cannot be acquired.
                        Best-effort: take what is currently free.
                    </div>
                </div>

                ${trainInfo}

                <div class="info-box text-sm mt-16">
                    Click on an <span class="text-warning">orange</span> destination block to create the path.
                    <br><br>
                    Right-click or press ESC to cancel.
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('🛤️ Create Path', content);

        const sidebar = document.getElementById('secondary-sidebar');
        if (sidebar) {
            sidebar.classList.add('compact');
        }

        setTimeout(() => {
            const dirButtons = document.querySelectorAll('[data-direction]');
            dirButtons.forEach(btn => {
                btn.addEventListener('click', () => {
                    this.pathToolState.requestedDirection = btn.dataset.direction;
                    dirButtons.forEach(b => b.classList.toggle('active', b === btn));
                });
            });

            const modeButtons = document.querySelectorAll('[data-mode]');
            modeButtons.forEach(btn => {
                btn.addEventListener('click', () => {
                    this.pathToolState.acquisitionMode = btn.dataset.mode;
                    modeButtons.forEach(b => b.classList.toggle('active', b === btn));
                });
            });
        }, 0);
    }

    async showPathsPanel() {
        this.editor._currentEditorType = 'paths-list';
        this.editor._currentEditorId = null;

        try {
            this.cachedPaths = await this.api.getPaths();
        } catch (err) {
            console.error('[Editor] Failed to load paths:', err);
            window.showToast('Failed to load paths: ' + err.message, 'error', 5000);
            return;
        }

        let content = `
            <div class="paths-panel">
                <div class="flex gap-8 mb-16">
                    <button id="refresh-paths-btn" class="btn btn-secondary btn-sm flex-1">
                        🔄 Refresh
                    </button>
                </div>
        `;

        if (this.cachedPaths.length === 0) {
            content += '<div class="text-muted text-center routes-empty-state">No active paths</div>';
        } else {
            content += '<div class="paths-list">';
            for (const path of this.cachedPaths) {
                content += this.renderPathListItem(path);
            }
            content += '</div>';
        }

        content += '</div>';

        this.editor.showSecondarySidebar('📋 Active Paths', content);
        
        setTimeout(() => this.initRoutesPanelHandlers(), 0);
    }

    renderPathListItem(path) {
        const statusClass = this.getPathStatusClass(path.status);
        const resources = path.resource_sequence || [];
        const totalSteps = resources.length;
        const currentIndex = path.current_resource_index ?? 0;
        const progress = totalSteps > 0
            ? Math.round((currentIndex / totalSteps) * 100)
            : 0;
        const statusText = path.status.toUpperCase();
        const direction = path.requested_destination_direction || 'forward';
        const directionLabel = direction === 'reverse' ? '◀' : '▶';
        const destination = path.authority_end_block_id
            || path.requested_destination_block_id
            || '—';

        return `
            <div class="path-row ${statusClass} mb-8">
                <div class="path-row-main">
                    <div class="path-row-status">
                        <div class="train-status-dot path-status-dot"></div>
                        <div class="text-xxs font-bold uppercase tracking-wider path-status-label">
                            ${statusText}
                        </div>
                        <div class="text-xxs font-bold text-muted path-priority-tag" title="Direction">${directionLabel}</div>
                    </div>
                    <div class="train-info flex-1 path-row-content">
                        <div class="flex flex-col gap-1 mb-6">
                            <div class="flex justify-between items-start gap-2">
                                <span class="train-id font-mono font-bold text-white path-id-text">${path.path_id}</span>
                            </div>
                        </div>
                        <div class="flex justify-between items-end gap-2">
                            <div class="text-xs text-muted flex-1 path-meta-col">
                                <div class="path-endpoints-stack">
                                    <div class="font-mono text-white opacity-90">→ ${destination}</div>
                                </div>
                                <div class="mb-4">${path.train_id ? `🚂 ${path.train_id}` : 'No Train'}</div>
                                ${path.status === 'active' ? `
                                    <div class="progress-container mt-1 path-progress-compact">
                                        <progress class="path-progress-meter" value="${progress}" max="100"></progress>
                                    </div>
                                ` : ''}
                            </div>
                            <div class="flex gap-4 flex-shrink-0">
                                <button class="edit-btn btn-sm view-path-btn" data-path-id="${path.path_id}">View</button>
                                <button class="btn btn-sm btn-danger btn-danger-solid cancel-path-btn" data-path-id="${path.path_id}">Cancel</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    getPathStatusColor(status) {
        const colors = {
            'pending': '#888',
            'setting': '#ffd93d',
            'set': '#04d361',
            'active': '#00ff88',
            'clearing': '#ff9500',
            'completed': '#666',
            'cancelled': '#e83f5b'
        };
        return colors[status] || '#888';
    }

    getPathStatusClass(status) {
        const map = {
            pending: 'path-status-pending',
            setting: 'path-status-setting',
            set: 'path-status-set',
            active: 'path-status-active',
            clearing: 'path-status-clearing',
            completed: 'path-status-completed',
            cancelled: 'path-status-cancelled'
        };
        return map[status] || 'path-status-pending';
    }

    initRoutesPanelHandlers() {
        const refreshBtn = document.getElementById('refresh-paths-btn');
        if (refreshBtn) {
            refreshBtn.onclick = async () => {
                await this.showPathsPanel();
            };
        }

        const viewBtns = document.querySelectorAll('.view-path-btn');
        viewBtns.forEach(btn => {
            btn.onclick = async () => {
                const pathId = btn.dataset.pathId;
                await this.showPathEditor(pathId);
            };
        });

        const cancelBtns = document.querySelectorAll('.cancel-path-btn');
        cancelBtns.forEach(btn => {
            btn.onclick = async () => {
                const pathId = btn.dataset.pathId;
                const confirmed = await showConfirmModal({
                    title: 'Cancel Path',
                    message: `Cancel path "${pathId}"?`,
                    detail: 'Reserved blocks for this path will be released.',
                    confirmText: 'Cancel Path',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;
                
                try {
                    const result = await this.api.cancelPath(pathId);
                    if (result.success) {
                        const queued = result.status === 'queued';
                        const message = queued
                            ? `Path ${pathId} cancellation queued — train is braking`
                            : `Path ${pathId} cancelled`;
                        window.showToast(message, 'success', 5000);
                        await window.app.refreshNetwork();
                        await this.showPathsPanel();
                        // For latched cancels, the path is not yet removed.
                        // Reconcile after one tick (~200 ms) so the UI catches
                        // up with the Phase-3 commit in the simulation.
                        if (queued) {
                            setTimeout(async () => {
                                try {
                                    await window.app.refreshNetwork();
                                    await this.showPathsPanel();
                                } catch (err) {
                                    console.warn('[Editor] Deferred path refresh failed:', err);
                                }
                            }, 500);
                        }
                    } else {
                        window.showToast(`Failed to cancel: ${result.reason || result.error || 'unknown error'}`, 'error', 5000);
                    }
                } catch (err) {
                    console.error('[Editor] Failed to cancel path:', err);
                    window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    async showPathEditor(pathId) {
        this.editor._currentEditorType = 'path';
        this.editor._currentEditorId = pathId;

        let path;
        try {
            path = await this.api.getPath(pathId);
            if (path.error) throw new Error(path.error);
        } catch (err) {
            window.showToast('Path not found', 'error', 5000);
            return;
        }

        const statusClass = this.getPathStatusClass(path.status);
        const statusColor = this.getPathStatusColor(path.status);
        const resources = path.resource_sequence || [];
        const totalSteps = resources.length;
        const currentIndex = path.current_resource_index ?? 0;
        const progress = totalSteps > 0
            ? Math.round((currentIndex / totalSteps) * 100)
            : 0;
        const direction = path.requested_destination_direction || 'forward';
        const destination = path.authority_end_block_id
            || path.requested_destination_block_id
            || '—';

        const resourceListHtml = resources.map((resource, i) => {
            const isCurrent = i === currentIndex;
            const isCleared = i < currentIndex;
            let cssClass = 'text-muted';
            if (isCurrent) cssClass = 'text-success font-bold';
            else if (isCleared) cssClass = 'text-muted';

            const label = resource.resource_id || `${resource.resource_type || '?'} #${resource.sequence_index ?? i}`;
            const kind = resource.resource_type || 'resource';

            return `<div class="block-item text-xs ${cssClass}">
                ${isCurrent ? '▶ ' : '  '}${label}<span class="text-muted ml-2">[${kind}]</span>${isCleared ? ' ✓' : ''}
            </div>`;
        }).join('');

        const content = `
            <div class="path-editor-panel" data-path-id="${pathId}">
                ${renderHeaderId(path.path_id, statusColor)}

                <!-- INFO SECTION (Read-only, styled like Train Control) -->
                <div class="info-box-track mb-12 info-display-large">
                    <div class="flex justify-between items-start mb-4">
                        <span class="info-label text-muted">Status</span>
                        <span class="info-meta">${direction === 'reverse' ? '◀ Reverse' : '▶ Forward'}</span>
                    </div>

                    <div class="text-2xl font-bold mb-6 font-mono path-status-title ${statusClass}">
                        ${path.status.toUpperCase()}
                    </div>

                    <div class="grid grid-cols-1 gap-y-4 text-sm font-mono">
                        <div>
                            <span class="info-label text-muted block mb-2">Destination</span>
                            <div class="path-route-stack">
                                <div class="info-value text-white opacity-90">→ ${destination}</div>
                            </div>
                        </div>
                        <div>
                            <span class="info-label text-muted block">Train</span>
                            <span class="info-value ${path.train_id ? 'text-info' : 'text-muted'}">${path.train_id ? `🚂 ${path.train_id}` : 'None assigned'}</span>
                        </div>
                        ${path.status === 'active' ? `
                        <div>
                            <span class="info-label text-muted block mb-1">Progress</span>
                            <div class="progress-container path-progress-full">
                                <progress class="path-progress-meter ${statusClass}" value="${progress}" max="100"></progress>
                            </div>
                            <span class="info-meta mt-1">${progress}% (${currentIndex}/${totalSteps})</span>
                        </div>
                        ` : ''}
                    </div>
                </div>

                <!-- CONTROL BUTTONS -->
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Actions</h4>
                    <div class="flex gap-8">
                        ${(path.status === 'set' || path.status === 'active') ? `
                            <button id="extend-path-btn" class="btn btn-primary btn-sm flex-1">
                                ➕ Extend
                            </button>
                        ` : ''}
                        <button id="cancel-path-btn" class="btn btn-danger btn-sm flex-1 btn-danger-solid">
                            ❌ Cancel
                        </button>
                    </div>
                </div>

                <!-- RESOURCE LIST (Scrollable) -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <h4 class="text-sm text-muted mb-8">Resources (${totalSteps})</h4>
                    <div class="bg-input rounded-lg path-block-list-wrap">
                        ${resourceListHtml || '<div class="text-muted text-xs">No resources</div>'}
                    </div>
                </div>

                <button id="back-to-paths-btn" class="btn btn-secondary btn-full btn-sm mt-12">
                    ← Back to Paths List
                </button>
            </div>
        `;

        this.editor.showSecondarySidebar('🛤️ Path Details', content);

        setTimeout(() => this.initPathEditorHandlers(pathId, path), 0);

        if (this.renderer.highlightPath) {
            this.renderer.highlightPath(path);
        }
    }

    initPathEditorHandlers(pathId, path) {
        const backBtn = document.getElementById('back-to-paths-btn');
        if (backBtn) {
            backBtn.onclick = async () => {
                if (this.renderer.clearPathHighlight) {
                    this.renderer.clearPathHighlight();
                }
                await this.showPathsPanel();
            };
        }

        const cancelBtn = document.getElementById('cancel-path-btn');
        if (cancelBtn) {
            cancelBtn.onclick = async () => {
                const detail = 'Reserved blocks and switches for this path will be released.';

                const confirmed = await showConfirmModal({
                    title: 'Cancel Path',
                    message: `Cancel path "${pathId}"?`,
                    detail,
                    confirmText: 'Cancel Path',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;

                try {
                    const result = await this.api.cancelPath(pathId);
                    if (result.success) {
                        const queued = result.status === 'queued';
                        let message;
                        if (queued) {
                            message = `Path ${pathId} cancellation queued — train is braking`;
                        } else {
                            const releasedCount =
                                (result.released_block_ids?.length || 0)
                                + (result.released_switch_ids?.length || 0);
                            const suffix = releasedCount > 0
                                ? ` (${releasedCount} resource${releasedCount === 1 ? '' : 's'} released)`
                                : '';
                            message = `Path ${pathId} cancelled${suffix}`;
                        }
                        window.showToast(message, 'success', 5000);
                        await window.app.refreshNetwork();
                        if (this.renderer.clearPathHighlight) {
                            this.renderer.clearPathHighlight();
                        }
                        await this.showPathsPanel();
                        if (queued) {
                            setTimeout(async () => {
                                try {
                                    await window.app.refreshNetwork();
                                    await this.showPathsPanel();
                                } catch (err) {
                                    console.warn('[Editor] Deferred path refresh failed:', err);
                                }
                            }, 500);
                        }
                    } else {
                        window.showToast(`Failed to cancel: ${result.reason || result.message || result.error}`, 'error', 5000);
                    }
                } catch (err) {
                    console.error('[Editor] Failed to cancel path:', err);
                    window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
                }
            };
        }

        const extendBtn = document.getElementById('extend-path-btn');
        if (extendBtn) {
            extendBtn.onclick = async () => {
                window.showToast('Path extension not yet implemented', 'info', 3000);
            };
        }
    }

    enterExtendRouteMode() {
        this.extendPathState = {
            active: false,
            selectedPathId: null,
            selectedPath: null,
            reachableEndpoints: [],
            reachablePositions: new Set()
        };
        document.getElementById('status-state').textContent = 'Extend Path: Click on a path to extend';
    }

    async exitExtendRouteMode() {
        await this.resetExtendRouteState();
        document.getElementById('status-state').textContent = 'Ready';
    }

    // EditorMode factory for the extend-route tool.
    buildExtendRouteMode() {
        return {
            name: 'Extend route',
            toolName: 'extend-path',
            cancelMessage: 'Extend route cancelled',
            onEnter: () => this.enterExtendRouteMode(),
            onClick: async ({ gridPos }) => {
                await this.handleExtendRouteToolClick(gridPos);
                return true;
            },
            onExit: () => this.exitExtendRouteMode()
        };
    }

    async resetExtendRouteState() {
        this.extendPathState = {
            active: false,
            selectedPathId: null,
            selectedPath: null,
            reachableEndpoints: [],
            reachablePositions: new Set()
        };
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.renderer.clearPathHighlight) {
            this.renderer.clearPathHighlight();
        }
        if (this.editor._currentEditorType === 'extend-path-setup') {
            await this.editor.goBackSecondaryPanel();
            // If history was empty, goBack does nothing - close sidebar as fallback.
            if (this.editor._currentEditorType === 'extend-path-setup') {
                this.editor.hideSecondarySidebar();
            }
        }
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
        }
    }

    async handleExtendRouteToolClick(gridPos) {
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);

        if (!this.extendPathState?.active) {
            await this.handleExtendRouteFirstClick(gridPos, track);
        } else {
            await this.handleExtendRouteSecondClick(gridPos, track);
        }
    }

    async handleExtendRouteFirstClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const blockId = track.block_section_id;
        if (!blockId) {
            window.showToast('No block at this position', 'warning', 3000);
            return;
        }

        try {
            const paths = await this.api.getPaths();

            const pathContainsBlock = (p) => {
                const resources = p.resource_sequence || [];
                return resources.some(r =>
                    r.resource_type === 'block' && r.resource_id === blockId
                );
            };

            const matchingPaths = paths.filter(p =>
                p.authority_end_block_id === blockId &&
                ['pending', 'setting', 'set', 'active'].includes(p.status)
            );

            if (matchingPaths.length === 0) {
                const partOfPath = paths.find(p =>
                    pathContainsBlock(p) &&
                    ['pending', 'setting', 'set', 'active'].includes(p.status)
                );

                if (partOfPath) {
                    window.showToast('Can only extend from the END of a path. Click on the end block.', 'warning', 4000);
                } else {
                    window.showToast('No active path ends at this block', 'warning', 3000);
                }
                return;
            }

            const selectedPath = matchingPaths[0];
            
            this.extendPathState.selectedPathId = selectedPath.path_id;
            this.extendPathState.selectedPath = selectedPath;
            this.extendPathState.active = true;

            if (this.renderer.highlightPath) {
                this.renderer.highlightPath(selectedPath);
            }

            document.getElementById('status-state').textContent = 'Extend Path: Loading destinations...';
            const endpointsResult = await this.api.getPathReachableExtensions(selectedPath.path_id);
            
            if (endpointsResult.error) {
                window.showToast('Cannot extend path: ' + endpointsResult.error, 'error', 5000);
                await this.resetExtendRouteState();
                return;
            }
            
            this.extendPathState.reachableEndpoints = endpointsResult.reachable_endpoints || [];
            this.extendPathState.exitDirection = endpointsResult.exit_direction;

            this.buildExtendRoutePositionsSet();

            const count = this.extendPathState.reachableEndpoints.length;
            document.getElementById('status-state').textContent = `Extend Path: Click new destination (${count} reachable)`;

            if (this.renderer.setReachableHighlights) {
                this.renderer.setReachableHighlights(this.extendPathState.reachablePositions);
            }

            this.showExtendRoutePanel();

            if (this.cachedNetwork) {
                this.renderer.drawNetwork(this.cachedNetwork);
            }
        } catch (err) {
            console.error('[Editor] Failed to get paths for extend:', err);
            window.showToast('Failed to load paths: ' + err.message, 'error', 5000);
            await this.resetExtendRouteState();
        }
    }

    buildExtendRoutePositionsSet() {
        const positions = new Set();
        const tracks = this.cachedNetwork?.tracks || [];

        for (const endpoint of this.extendPathState.reachableEndpoints) {
            for (const track of tracks) {
                if (track.block_section_id === endpoint) {
                    positions.add(`${track.x},${track.y}`);
                }
            }
        }

        this.extendPathState.reachablePositions = positions;
    }

    async handleExtendRouteSecondClick(gridPos, track) {
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const newEndBlockId = track.block_section_id;
        if (!newEndBlockId) {
            window.showToast('Must select a block as destination', 'warning', 3000);
            return;
        }

        if (!this.extendPathState.reachableEndpoints.includes(newEndBlockId)) {
            window.showToast('Cannot extend to this block', 'warning', 3000);
            return;
        }

        try {
            document.getElementById('status-state').textContent = 'Extend Path: Extending...';

            const direction = this.extendPathState.selectedPath?.requested_destination_direction || 'forward';
            const result = await this.api.extendPath(
                this.extendPathState.selectedPathId,
                {
                    requested_destination_block_id: newEndBlockId,
                    requested_destination_direction: direction,
                    acquisition_mode: 'all_or_nothing'
                }
            );

            if (result.success) {
                window.showToast(`Path extended to ${newEndBlockId}`, 'success', 5000);
                await window.app.refreshNetwork();
                await this.resetExtendRouteState();
                document.getElementById('status-state').textContent = 'Extend Path: Click on a path to extend';
            } else {
                const errorMsg = result.error || result.failure_kind || result.message || 'unknown error';
                window.showToast(`Extend failed: ${errorMsg}`, 'error', 5000);
            }
        } catch (err) {
            console.error('[Editor] Failed to extend route:', err);
            window.showToast('Failed to extend route: ' + err.message, 'error', 5000);
        }
    }

    showExtendRoutePanel() {
        this.editor._currentEditorType = 'extend-path-setup';
        this.editor._currentEditorId = this.extendPathState.selectedPathId;

        const route = this.extendPathState.selectedPath;
        const exitDir = this.extendPathState.exitDirection;
        const exitDirText = exitDir ? this.getDirectionName(exitDir) : 'Unknown';
        const reachableCount = this.extendPathState.reachableEndpoints.length;

        const currentEnd = route.authority_end_block_id
            || route.requested_destination_block_id
            || '—';

        const content = `
                <div class="extend-route-panel">
                ${renderHeaderId(route.path_id, this.getPathStatusColor(route.status))}

                ${renderReadOnlyValue('Current End', currentEnd, 'value-display-mono text-sm')}
                ${renderReadOnlyValue('Direction', `${exitDirText} (${exitDir || '?'})`, 'text-info font-bold')}
                ${renderReadOnlyValue('Status', route.status.toUpperCase(), `font-bold`)}
                ${route.train_id
                    ? renderReadOnlyValue('Train', `${route.train_id}`, 'text-info font-bold')
                    : ''
                }
                
                <div class="info-box text-sm mt-16">
                    ${reachableCount > 0 
                        ? `Click on an <span class="text-warning">orange</span> destination block to extend the route ${exitDirText.toLowerCase()}.`
                        : `<span class="text-danger">No forward destinations available.</span> The route may be at a dead end or all forward blocks are occupied.`
                    }
                    <br><br>
                    Right-click or press ESC to cancel.
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('+ Extend Path', content);
        
        const sidebar = document.getElementById('secondary-sidebar');
        if (sidebar) {
            sidebar.classList.add('compact');
        }
    }

    getDirectionName(dir) {
        const names = { 'N': 'North', 'S': 'South', 'E': 'East', 'W': 'West' };
        return names[dir] || dir || 'Unknown';
    }

    enterCancelRouteMode() {
        document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
    }

    exitCancelRouteMode() {
        document.getElementById('status-state').textContent = 'Ready';
    }

    // EditorMode factory for the cancel-route tool.
    buildCancelRouteMode() {
        return {
            name: 'Cancel route',
            toolName: 'cancel-path',
            cancelMessage: 'Cancel-route mode exited',
            onEnter: () => this.enterCancelRouteMode(),
            onClick: async ({ gridPos }) => {
                await this.handleCancelRouteToolClick(gridPos);
                return true;
            },
            onExit: () => this.exitCancelRouteMode()
        };
    }

    async handleCancelRouteToolClick(gridPos) {
        const track = this.cachedNetwork?.tracks?.find(t => t.x === gridPos.x && t.y === gridPos.y);
        
        if (!track) {
            window.showToast('No track at this position', 'warning', 3000);
            return;
        }

        const blockId = track.block_section_id;
        if (!blockId) {
            window.showToast('No block at this position', 'warning', 3000);
            return;
        }

        try {
            const paths = await this.api.getPaths();

            const pathContainsBlock = (p) => {
                const resources = p.resource_sequence || [];
                return resources.some(r =>
                    r.resource_type === 'block' && r.resource_id === blockId
                );
            };

            const matchingPaths = paths.filter(p =>
                pathContainsBlock(p) &&
                ['pending', 'setting', 'set', 'active'].includes(p.status)
            );

            if (matchingPaths.length === 0) {
                window.showToast('No active path found at this block', 'warning', 3000);
                return;
            }

            const pathToCancel = matchingPaths[0];
            const destination = pathToCancel.authority_end_block_id
                || pathToCancel.requested_destination_block_id
                || '—';
            const detail =
                `Destination: ${destination}` +
                (pathToCancel.train_id ? `\nTrain: ${pathToCancel.train_id}` : '');

            const confirmed = await showConfirmModal({
                title: 'Cancel Path',
                message: `Cancel path "${pathToCancel.path_id}"?`,
                detail,
                confirmText: 'Cancel Path',
                confirmVariant: 'danger'
            });
            if (!confirmed) {
                return;
            }

            document.getElementById('status-state').textContent = 'Cancel Path: Cancelling...';

            const cancelResult = await this.api.cancelPath(pathToCancel.path_id);

            if (cancelResult.success) {
                const queued = cancelResult.status === 'queued';
                const message = queued
                    ? `Path ${pathToCancel.path_id} cancellation queued — train is braking`
                    : `Path ${pathToCancel.path_id} cancelled`;
                window.showToast(message, 'success', 5000);
                await window.app.refreshNetwork();
                if (queued) {
                    setTimeout(async () => {
                        try {
                            await window.app.refreshNetwork();
                        } catch (err) {
                            console.warn('[Editor] Deferred path refresh failed:', err);
                        }
                    }, 500);
                }
            } else {
                window.showToast(`Failed to cancel: ${cancelResult.reason || cancelResult.error || 'unknown error'}`, 'error', 5000);
            }

            document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
        } catch (err) {
            console.error('[Editor] Failed to cancel path:', err);
            window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
            document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
        }
    }
}
