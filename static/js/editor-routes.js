class RouteEditorHandler {
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
            priority: 5,
            trainId: null
        };
        document.getElementById('status-state').textContent = 'Path: Click start block';
    }

    exitRouteToolMode() {
        this.resetRouteToolState();
        document.getElementById('status-state').textContent = 'Ready';
    }

    resetRouteToolState() {
        this.pathToolState = {
            active: false,
            startBlockId: null,
            startPosition: null,
            reachableEndpoints: [],
            reachablePositions: new Set(),
            priority: 5,
            trainId: null
        };
        if (this.renderer.setReachableHighlights) {
            this.renderer.setReachableHighlights(new Set());
        }
        if (this.renderer.clearRoutePreview) {
            this.renderer.clearRoutePreview();
        }
        if (this.editor._currentEditorType === 'path-setup') {
            this.editor.hideSecondarySidebar();
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

            const trains = this.cachedNetwork?.trains || [];
            const train = trains.find(t => t.train_id === block.occupied_by);
            if (train?.priority) {
                this.pathToolState.priority = train.priority;
            }
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
            this.resetRouteToolState();
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
                priority: this.pathToolState.priority
            };
            
            if (this.pathToolState.trainId) {
                options.train_id = this.pathToolState.trainId;
            }

            const result = await this.api.createPath(
                this.pathToolState.startBlockId,
                endBlockId,
                options
            );

            if (result.success) {
                window.showToast(`Path ${result.path.path_id} created`, 'success', 5000);
                await window.app.refreshNetwork();
                this.resetRouteToolState();
            } else {
                window.showToast(`Path failed: ${result.error}`, 'error', 5000);

                if (result.conflict_tiles && this.renderer.flashConflictTiles) {
                    this.renderer.flashConflictTiles(result.conflict_tiles, 2000);
                }
            }
        } catch (err) {
            console.error('[Editor] Failed to create path:', err);
            window.showToast('Failed to create path: ' + err.message, 'error', 5000);
        }
    }

    showRoutePrioritySelector() {
        this.editor._currentEditorType = 'path-setup';
        this.editor._currentEditorId = null;

        const trainInfo = this.pathToolState.trainId
            ? renderReadOnlyValue('Train', `🚂 ${this.pathToolState.trainId}`, 'text-info font-bold')
            : '<div class="text-muted text-sm" style="padding: 8px 0;">No train at start block</div>';

        const content = `
            <div class="path-setup-panel">
                ${renderHeaderId(this.pathToolState.startBlockId, '#00d4ff')}
                
                <div class="property-item">
                    <label class="property-label">Priority (1-10)</label>
                    <div class="flex items-center gap-12">
                        <input type="range" id="path-priority-slider" 
                               min="1" max="10" value="${this.pathToolState.priority}"
                               class="property-input" style="flex: 1;">
                        <span id="path-priority-value" class="text-lg font-bold text-track" style="min-width: 30px; text-align: center;">
                            ${this.pathToolState.priority}
                        </span>
                    </div>
                    <div class="text-xxs text-muted" style="margin-top: 4px;">
                        5 = Normal, 10 = Critical
                    </div>
                </div>
                
                ${trainInfo}
                
                <div class="info-box text-sm" style="margin-top: 16px;">
                    Click on an <span style="color: #ff9500;">orange</span> destination block to create path.
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
            const slider = document.getElementById('path-priority-slider');
            const valueDisplay = document.getElementById('path-priority-value');
            if (slider && valueDisplay) {
                slider.addEventListener('input', (e) => {
                    this.pathToolState.priority = parseInt(e.target.value);
                    valueDisplay.textContent = e.target.value;
                });
            }
        }, 0);
    }

    async showPathsPanel() {
        this.editor._currentEditorType = 'paths-list';
        this.editor._currentEditorId = null;

        try {
            const result = await this.api.getPaths();
            this.cachedPaths = result.paths || [];
        } catch (err) {
            console.error('[Editor] Failed to load paths:', err);
            window.showToast('Failed to load paths: ' + err.message, 'error', 5000);
            return;
        }

        let content = `
            <div class="paths-panel">
                <button id="refresh-paths-btn" class="btn btn-secondary btn-full text-sm mb-8">
                    🔄 Refresh Paths
                </button>
        `;

        if (this.cachedPaths.length === 0) {
            content += '<div class="text-muted text-center" style="padding: 32px 16px;">No active paths</div>';
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
        const statusColor = this.getPathStatusColor(path.status);
        const progress = path.block_sequence?.length > 0
            ? Math.round((path.current_block_index / path.block_sequence.length) * 100)
            : 0;

        return `
            <div class="path-item" data-path-id="${path.path_id}">
                <div class="path-header">
                    <span class="path-status" style="color: ${statusColor}">
                        ● ${path.status.toUpperCase()}
                    </span>
                    <span class="path-priority">P${path.priority}</span>
                </div>
                <div class="path-id text-sm value-display-mono">${path.path_id}</div>
                <div class="path-endpoints text-xs text-muted">
                    ${path.start_block_id} → ${path.end_block_id}
                </div>
                ${path.train_id ? `<div class="path-train text-xs">🚂 ${path.train_id}</div>` : ''}
                ${path.status === 'active' ? `
                    <div class="progress-container">
                        <div class="progress-bar" style="width: ${progress}%"></div>
                        <span class="progress-text">${progress}%</span>
                    </div>
                ` : ''}
                <div class="path-actions">
                    <button class="btn btn-sm btn-info view-path-btn" data-path-id="${path.path_id}">View</button>
                    <button class="btn btn-sm btn-danger cancel-path-btn" data-path-id="${path.path_id}">Cancel</button>
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
                if (!confirm(`Cancel path "${pathId}"?`)) return;
                
                try {
                    const result = await this.api.cancelPath(pathId);
                    if (result.success) {
                        window.showToast(`Path ${pathId} cancelled`, 'success', 5000);
                        await window.app.refreshNetwork();
                        await this.showPathsPanel();
                    } else {
                        window.showToast(`Failed to cancel: ${result.message || result.error}`, 'error', 5000);
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

        const statusColor = this.getPathStatusColor(path.status);
        const progress = path.block_sequence?.length > 0
            ? Math.round((path.current_block_index / path.block_sequence.length) * 100)
            : 0;

        const blocksListHtml = (path.block_sequence || []).map((blockId, i) => {
            const isCurrent = i === path.current_block_index;
            const isCleared = path.blocks_cleared?.includes(blockId);
            let cssClass = 'text-muted';
            if (isCurrent) cssClass = 'text-success font-bold';
            else if (isCleared) cssClass = 'text-muted';
            
            return `<div class="block-item text-xs ${cssClass}">
                ${isCurrent ? '▶ ' : '  '}${blockId}${isCleared ? ' ✓' : ''}
            </div>`;
        }).join('');

        const content = `
            <div class="path-editor-panel" data-path-id="${pathId}">
                ${renderHeaderId(path.path_id, statusColor)}
                
                ${renderPropertyItem('Status', `
                    <div class="value-display flex items-center gap-8">
                        <span class="status-dot" style="background: ${statusColor}; width: 12px; height: 12px; border-radius: 50%; display: inline-block;"></span>
                        <span class="font-bold" style="color: ${statusColor}">${path.status.toUpperCase()}</span>
                    </div>
                `)}
                
                ${renderReadOnlyValue('Priority', `${path.priority} / 10`, 'font-bold')}
                
                ${path.train_id
                    ? renderReadOnlyValue('Train', `🚂 ${path.train_id}`, 'text-info font-bold')
                    : renderReadOnlyValue('Train', 'None assigned', 'text-muted')
                }
                
                ${renderReadOnlyValue('Start Block', path.start_block_id, 'value-display-mono text-sm')}
                ${renderReadOnlyValue('End Block', path.end_block_id, 'value-display-mono text-sm')}
                
                ${path.status === 'active' ? `
                    <div class="property-item">
                        <label class="property-label">Progress</label>
                        <div class="progress-container">
                            <div class="progress-bar" style="width: ${progress}%"></div>
                            <span class="progress-text">${progress}%</span>
                        </div>
                    </div>
                ` : ''}
                
                <div class="property-item">
                    <label class="property-label">Blocks (${path.block_sequence?.length || 0})</label>
                    <div class="bg-input rounded-lg" style="max-height: 150px; overflow-y: auto; padding: 8px; border: 1px solid #3a3a4e;">
                        ${blocksListHtml || '<div class="text-muted text-xs">No blocks</div>'}
                    </div>
                </div>
                
                ${path.approach_locked_blocks?.length > 0 ? `
                    <div class="info-box-warning text-xs text-warning">
                        ⚠️ ${path.approach_locked_blocks.length} blocks approach-locked
                    </div>
                ` : ''}
                
                <div class="property-item-row" style="margin-top: 16px;">
                    ${(path.status === 'set' || path.status === 'active') ? `
                        <button id="extend-path-btn" class="btn btn-primary text-sm" style="flex: 1;">
                            Extend Path
                        </button>
                    ` : ''}
                    <button id="cancel-path-btn" class="btn btn-danger text-sm" style="flex: 1;">
                        Cancel Path
                    </button>
                </div>
                
                <button id="back-to-paths-btn" class="btn btn-secondary btn-full text-sm" style="margin-top: 8px;">
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
                const hasApproachLocked = path.approach_locked_blocks?.length > 0;
                const msg = hasApproachLocked
                    ? `Cancel path "${pathId}"?\n\nNote: ${path.approach_locked_blocks.length} blocks are approach-locked and will be released when train stops.`
                    : `Cancel path "${pathId}"?`;
                
                if (!confirm(msg)) return;

                try {
                    const result = await this.api.cancelPath(pathId);
                    if (result.success) {
                        window.showToast(`Path ${pathId} cancelled`, 'success', 5000);
                        await window.app.refreshNetwork();
                        if (this.renderer.clearPathHighlight) {
                            this.renderer.clearPathHighlight();
                        }
                        await this.showPathsPanel();
                    } else {
                        window.showToast(`Failed to cancel: ${result.message || result.error}`, 'error', 5000);
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

    exitExtendRouteMode() {
        this.resetExtendRouteState();
        document.getElementById('status-state').textContent = 'Ready';
    }

    resetExtendRouteState() {
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
            this.editor.hideSecondarySidebar();
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
            const result = await this.api.getPaths();
            const paths = result.paths || [];
            
            const matchingPaths = paths.filter(p => 
                p.end_block_id === blockId && 
                ['pending', 'setting', 'set', 'active'].includes(p.status)
            );

            if (matchingPaths.length === 0) {
                const partOfPath = paths.find(p => 
                    p.block_sequence?.includes(blockId) && 
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
                this.resetExtendRouteState();
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
            this.resetExtendRouteState();
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
            
            const result = await this.api.extendPath(
                this.extendPathState.selectedPathId,
                newEndBlockId
            );

            if (result.success) {
                window.showToast(`Path extended to ${newEndBlockId}`, 'success', 5000);
                await window.app.refreshNetwork();
                this.resetExtendRouteState();
                document.getElementById('status-state').textContent = 'Extend Path: Click on a path to extend';
            } else {
                window.showToast(`Extend failed: ${result.error || result.message}`, 'error', 5000);
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
        const statusColor = this.getPathStatusColor(path.status);
        const exitDir = this.extendPathState.exitDirection;
        const exitDirText = exitDir ? this.getDirectionName(exitDir) : 'Unknown';
        const reachableCount = this.extendPathState.reachableEndpoints.length;

        const content = `
            <div class="extend-route-panel">
                ${renderHeaderId(path.path_id, statusColor)}
                
                ${renderReadOnlyValue('Current End', path.end_block_id, 'value-display-mono text-sm')}
                ${renderReadOnlyValue('Direction', `${exitDirText} (${exitDir || '?'})`, 'text-info font-bold')}
                ${renderReadOnlyValue('Status', path.status.toUpperCase(), `font-bold`)}
                ${path.train_id
                    ? renderReadOnlyValue('Train', `${path.train_id}`, 'text-info font-bold')
                    : ''
                }
                
                <div class="info-box text-sm" style="margin-top: 16px;">
                    ${reachableCount > 0 
                        ? `Click on an <span style="color: #ff9500;">orange</span> destination block to extend the route ${exitDirText.toLowerCase()}.`
                        : `<span style="color: #ff6b6b;">No forward destinations available.</span> The route may be at a dead end or all forward blocks are occupied.`
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
            const result = await this.api.getPaths();
            const paths = result.paths || [];
            
            const matchingPaths = paths.filter(p => 
                p.block_sequence?.includes(blockId) && 
                ['pending', 'setting', 'set', 'active'].includes(p.status)
            );

            if (matchingPaths.length === 0) {
                window.showToast('No active path found at this block', 'warning', 3000);
                return;
            }

            const pathToCancel = matchingPaths[0];
            
            const msg = `Cancel path "${pathToCancel.path_id}"?\n\n` +
                `From: ${pathToCancel.start_block_id}\n` +
                `To: ${pathToCancel.end_block_id}` +
                (pathToCancel.train_id ? `\nTrain: ${pathToCancel.train_id}` : '');
            
            if (!confirm(msg)) {
                return;
            }

            document.getElementById('status-state').textContent = 'Cancel Path: Cancelling...';
            
            const cancelResult = await this.api.cancelPath(pathToCancel.path_id);
            
            if (cancelResult.success) {
                window.showToast(`Path ${pathToCancel.path_id} cancelled`, 'success', 5000);
                await window.app.refreshNetwork();
            } else {
                window.showToast(`Failed to cancel: ${cancelResult.message || cancelResult.error}`, 'error', 5000);
            }
            
            document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
        } catch (err) {
            console.error('[Editor] Failed to cancel path:', err);
            window.showToast('Failed to cancel path: ' + err.message, 'error', 5000);
            document.getElementById('status-state').textContent = 'Cancel Path: Click on a path to cancel';
        }
    }
}
