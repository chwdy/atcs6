class App {
    constructor() {
        this.renderer = new Renderer('rail-canvas');
        this.editor = new Editor(this.renderer, window.api);
        this.trainMonitor = new TrainMonitor();

        this.simulationInterval = null;
        this.currentNetwork = null;
        this.hasInitiallyCentered = false;
        this.hasInitiallySelected = false;
        this.isSimulationRunning = false;
        this.selectedTrainId = null;
        this.lastLookaheadFetch = 0;
        this.lookaheadThrottleMs = 100;
        this.lastLookaheadData = null;  // Store last lookahead for panel rebuilds
        this.lastLookaheadPayload = null;
        this.trainMonitorUnsub = null;

        this.setupControls();

        this.setupDebugPanel();
        this.setupSettingsPanel();
        this.refreshNetwork();
    }

    setupControls() {
        const playPauseBtn = document.getElementById('btn-play-pause');
        playPauseBtn.addEventListener('click', () => {
            if (this.isSimulationRunning) {
                // Pause
                window.api.controlSimulation('stop').catch(e => console.error('[App] Pause error:', e));
                this.stopSimulationPolling();
                this.updatePlaybackUI(false);
            } else {
                // Play
                window.api.controlSimulation('start').catch(e => console.error('[App] Play error:', e));
                this.startSimulationPolling();
                this.updatePlaybackUI(true);
            }
        });

        document.getElementById('btn-restart-server').addEventListener('click', async () => {
            const confirmed = await showConfirmModal({
                title: 'Restart Server?',
                message: 'The server process will exit and relaunch.',
                detail: 'Unsaved layout edits will be flushed to autosave first. The page will reload automatically when the server is back (~3–5s).',
                confirmText: 'Restart',
                cancelText: 'Cancel',
                confirmVariant: 'danger'
            });
            if (!confirmed) return;

            try {
                await window.api.restartServer();
            } catch (e) {
                console.error('[App] Restart request failed:', e);
                window.showToast?.('Could not reach server to restart.', 'error', 4000);
                return;
            }

            this.stopSimulationPolling();
            this.updatePlaybackUI(false);
            this.startRestartReconnectFlow();
        });

        document.querySelectorAll('.speed-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const speed = parseFloat(e.currentTarget.dataset.speed);
                if (!Number.isFinite(speed)) return;
                try {
                    await window.api.controlSimulation('set_speed', speed);
                } catch (err) {
                    console.error('[App] set_speed error:', err);
                    window.showToast(`Failed to set speed: ${err.message}`, 'error', 2000);
                    return;
                }
                this.syncSpeedPresetUI(speed);
                window.showToast(`Speed set to ${speed}x`, 'info', 1000);
            });
        });

        document.getElementById('mode-edit').addEventListener('click', () => {
            document.getElementById('mode-edit').classList.add('active');
            document.getElementById('mode-sim').classList.remove('active');
            // Enable editor tools
        });

        document.getElementById('mode-sim').addEventListener('click', () => {
            document.getElementById('mode-sim').classList.add('active');
            document.getElementById('mode-edit').classList.remove('active');
            // Disable editor tools
        });

        // Layout controls
        this.setupLayoutControls();
    }


    updatePlaybackUI(isPlaying) {
        const btn = document.getElementById('btn-play-pause');
        if (!btn) return;

        if (isPlaying) {
            btn.innerHTML = '<span class="icon">⏸</span> Pause';
            btn.classList.remove('btn-primary');
            btn.classList.add('btn-secondary');
        } else {
            btn.innerHTML = '<span class="icon">▶</span> Play';
            btn.classList.remove('btn-secondary');
            btn.classList.add('btn-primary');
        }
    }

    syncSpeedPresetUI(serverSpeed) {
        const buttons = document.querySelectorAll('.speed-btn');
        if (!buttons.length) return;
        let match = null;
        for (const btn of buttons) {
            if (Math.abs(parseFloat(btn.dataset.speed) - serverSpeed) < 1e-6) {
                match = btn;
                break;
            }
        }
        if (!match) return;
        if (match.classList.contains('active')) return;
        buttons.forEach(b => b.classList.remove('active'));
        match.classList.add('active');
    }

    setupLayoutControls() {
        const newBtn = document.getElementById('btn-new-layout');
        const saveBtn = document.getElementById('btn-save-layout');
        const loadBtn = document.getElementById('btn-load-layout');
        const saveModal = document.getElementById('save-layout-modal');
        const loadModal = document.getElementById('load-layout-modal');

        // New layout button
        if (newBtn) {
            newBtn.addEventListener('click', async () => {
                await this.newLayout();
            });
        }

        // Open save modal
        if (saveBtn) {
            saveBtn.addEventListener('click', async () => {
                this.openModal(saveModal);
                document.getElementById('layout-name').value = '';
                document.getElementById('layout-description').value = '';
                document.getElementById('layout-name').focus();
                // Load existing layouts for overwrite selection
                await this.loadSaveLayoutsList();
            });
        }

        // Open load modal
        if (loadBtn) {
            loadBtn.addEventListener('click', async () => {
                this.openModal(loadModal);
                await this.loadLayoutsList();
            });
        }

        // Save layout confirm
        const confirmSaveBtn = document.getElementById('btn-confirm-save');
        if (confirmSaveBtn) {
            confirmSaveBtn.addEventListener('click', async () => {
                await this.saveCurrentLayout();
            });
        }

        // Allow Enter key to save
        const layoutNameInput = document.getElementById('layout-name');
        if (layoutNameInput) {
            layoutNameInput.addEventListener('keydown', async (e) => {
                if (e.key === 'Enter') {
                    await this.saveCurrentLayout();
                }
            });
        }

        // Close modal buttons
        document.querySelectorAll('[data-close-modal]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const modal = e.target.closest('.modal');
                if (modal) this.closeModal(modal);
            });
        });

        // Close modal on backdrop click
        document.querySelectorAll('.modal').forEach(modal => {
            modal.addEventListener('click', (e) => {
                if (e.target === modal) {
                    this.closeModal(modal);
                }
            });
        });

        // Close modal on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                document.querySelectorAll('.modal.active').forEach(modal => {
                    this.closeModal(modal);
                });
            }
        });
    }

    openModal(modal) {
        if (modal) modal.classList.add('active');
    }

    closeModal(modal) {
        if (modal) modal.classList.remove('active');
    }

    async saveCurrentLayout() {
        const nameInput = document.getElementById('layout-name');
        const descInput = document.getElementById('layout-description');
        const name = nameInput.value.trim();
        const description = descInput.value.trim();

        if (!name) {
            window.showToast('Please enter a layout name', 'error');
            nameInput.focus();
            return;
        }

        try {
            const result = await window.api.saveLayout(name, description);

            // Show success message with step count
            let message = `Layout "${name}" saved`;
            if (result.steps_count > 0) {
                message += ` (${result.steps_count} steps)`;
            }
            window.showToast(message, 'success');

            // Show warning if steps failed
            if (result.warning) {
                window.showToast(`Warning: ${result.warning}`, 'warning');
            }

            this.closeModal(document.getElementById('save-layout-modal'));
        } catch (e) {
            window.showToast(`Failed to save layout: ${e.message}`, 'error');
        }
    }

    async loadSaveLayoutsList() {
        const listEl = document.getElementById('save-layouts-list');
        listEl.innerHTML = '<p class="loading-text">Loading...</p>';

        try {
            const layouts = await window.api.getLayouts();
            // Filter out old _steps suffix files (now merged into main file)
            const filteredLayouts = layouts.filter(l => !l.name.endsWith('_steps'));

            if (filteredLayouts.length === 0) {
                listEl.innerHTML = '<p class="empty-text">No existing layouts</p>';
                return;
            }

            listEl.innerHTML = filteredLayouts.map(layout => `
                <div class="layout-item" data-name="${layout.name}">
                    <div class="layout-item-info">
                        <span class="layout-item-name">${this.escapeHtml(layout.name)}</span>
                    </div>
                </div>
            `).join('');

            // Click to select and populate name field
            listEl.querySelectorAll('.layout-item').forEach(item => {
                item.addEventListener('click', () => {
                    // Clear previous selection
                    listEl.querySelectorAll('.layout-item').forEach(el => el.classList.remove('selected'));
                    // Select this one
                    item.classList.add('selected');
                    // Populate name field
                    document.getElementById('layout-name').value = item.dataset.name;
                });
            });
        } catch (e) {
            listEl.innerHTML = '<p class="empty-text">Failed to load layouts</p>';
        }
    }

    async loadLayoutsList() {
        const listEl = document.getElementById('layouts-list');
        listEl.innerHTML = '<p class="loading-text">Loading layouts...</p>';

        try {
            const layouts = await window.api.getLayouts();
            // Filter out _steps suffix files (old format, now merged)
            const filteredLayouts = layouts.filter(l => !l.name.endsWith('_steps'));

            if (filteredLayouts.length === 0) {
                listEl.innerHTML = '<p class="empty-text">No saved layouts yet</p>';
                return;
            }

            listEl.innerHTML = filteredLayouts.map(layout => {
                const isAutosave = layout.filename === '_autosave';
                const displayName = isAutosave ? 'Auto-save (Recovery)' : layout.name;

                // Build independent badges for snapshot and steps
                let badges = '';
                if (layout.has_snapshot) {
                    badges += '<span class="layout-format-badge snapshot">snapshot</span>';
                }
                if (layout.has_steps) {
                    badges += '<span class="layout-format-badge steps">steps</span>';
                }

                const itemClass = isAutosave ? 'layout-item autosave' : 'layout-item';

                return `
                <div class="${itemClass}" data-filename="${layout.filename}">
                    <div class="layout-item-info">
                        <div class="layout-item-name">${this.escapeHtml(displayName)}${badges}</div>
                        <div class="layout-item-meta">
                            ${layout.description ? this.escapeHtml(layout.description) + ' · ' : ''}
                            ${this.formatDate(layout.modified)}
                        </div>
                    </div>
                    <div class="layout-item-actions">
                        <button class="btn btn-primary btn-load-item">Load</button>
                        <button class="btn btn-danger btn-delete-item">Delete</button>
                    </div>
                </div>
            `}).join('');

            // Add event listeners
            listEl.querySelectorAll('.btn-load-item').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const item = e.target.closest('.layout-item');
                    const filename = item.dataset.filename;
                    await this.loadSelectedLayout(filename);
                });
            });

            listEl.querySelectorAll('.btn-delete-item').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const item = e.target.closest('.layout-item');
                    const filename = item.dataset.filename;
                    const name = item.querySelector('.layout-item-name').textContent;
                    await this.deleteSelectedLayout(filename, name);
                });
            });

        } catch (e) {
            listEl.innerHTML = `<p class="empty-text">Error loading layouts: ${e.message}</p>`;
        }
    }

    async loadSelectedLayout(filename) {
        try {
            await window.api.loadLayout(filename);
            window.showToast('Layout loaded successfully', 'success');
            this.closeModal(document.getElementById('load-layout-modal'));
            await this.refreshNetwork();
        } catch (e) {
            window.showToast(`Failed to load layout: ${e.message}`, 'error');
        }
    }

    async deleteSelectedLayout(filename, name) {
        const confirmed = await showConfirmModal({
            title: 'Delete Layout',
            message: `Delete layout "${name}"?`,
            detail: 'This action cannot be undone.',
            confirmText: 'Delete',
            confirmVariant: 'danger'
        });
        if (!confirmed) {
            return;
        }

        try {
            await window.api.deleteLayout(filename);
            window.showToast(`Layout "${name}" deleted`, 'success');
            await this.loadLayoutsList();
        } catch (e) {
            window.showToast(`Failed to delete layout: ${e.message}`, 'error');
        }
    }

    async newLayout() {
        try {
            // Check if there's anything on the layout
            const stats = await window.api.getNetworkStats();

            if (!stats.is_empty) {
                // Build a summary of what will be cleared
                const items = [];
                if (stats.tracks > 0) items.push(`${stats.tracks} track(s)`);
                if (stats.blocks > 0) items.push(`${stats.blocks} block(s)`);
                if (stats.signals > 0) items.push(`${stats.signals} signal(s)`);
                if (stats.stations > 0) items.push(`${stats.stations} station(s)`);
                if (stats.trains > 0) items.push(`${stats.trains} train(s)`);

                const summary = items.join(', ');
                const confirmed = await showConfirmModal({
                    title: 'Create New Layout',
                    message: 'Create a new blank layout?',
                    detail: `This will clear the current layout:\n${summary}`,
                    confirmText: 'Clear Layout',
                    confirmVariant: 'danger'
                });

                if (!confirmed) {
                    return;
                }
            }

            // Clear the network
            await window.api.clearNetwork();
            window.showToast('New layout created', 'success');
            await this.refreshNetwork();

        } catch (e) {
            window.showToast(`Failed to create new layout: ${e.message}`, 'error');
        }
    }

    escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    formatDate(isoString) {
        const date = new Date(isoString);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;

        return date.toLocaleDateString();
    }

    async refreshNetwork() {
        // Skip refresh if editor is actively using a tool that needs hover preview
        // This prevents flickering when moving the mouse with signal/station tools
        if (this.editor && this.editor.isToolActive()) {
            // Still update the cache and trains panel, but don't redraw directly
            // The setCachedNetwork call will trigger re-validation of hover state
            try {
                const network = await window.api.getNetwork();
                this.currentNetwork = network;
                
                // NEW: Update StateManager cache
                window.stateManager.updateFromNetwork(network);
                
                this.editor.setCachedNetwork(network);
                // Validate that the current editor still exists (close if deleted)
                this.editor.validateCurrentEditor();
                // Don't call drawWithHoverPreview here - setCachedNetwork triggers updateHoverPreview
                // which will call drawWithHoverPreview after re-validating the signal state
            } catch (e) {
                console.error('[App] Failed to refresh network:', e);
            }
            return;
        }

        try {
            const network = await window.api.getNetwork();

            // Pull station groups alongside so the renderer can draw station
            // labels at the centroid of each station's platforms. Failure here
            // shouldn't block the rest of the render.
            try {
                const stationResp = await window.api.listStationGroups();
                network.stations = stationResp?.stations || [];
            } catch (_) {
                network.stations = [];
            }

            this.currentNetwork = network;

            // NEW: Update StateManager cache
            window.stateManager.updateFromNetwork(network);

            this.editor.setCachedNetwork(network);
            if (!this.hasInitiallyCentered && network.tracks && network.tracks.length > 0) {
                this.renderer.centerOnContent(network.tracks);
                this.hasInitiallyCentered = true;
            }
            this.renderer.drawNetwork(network);
            // Validate that the current editor still exists (close if deleted)
            this.editor.validateCurrentEditor();

            // On first successful load, pre-select tile (0,0) so the Track Bits
            // sidebar section has a track to show by default. selectElement
            // handles the missing-tile case by creating a virtual empty tile.
            if (!this.hasInitiallySelected && network.tracks) {
                this.hasInitiallySelected = true;
                this.editor.selectElement({ x: 0, y: 0 }).catch(e =>
                    console.warn('[App] Initial select(0,0) failed:', e)
                );
            }

            // Sync play/pause button with server state
            if (typeof network.simulation_running === 'boolean') {
                this.isSimulationRunning = network.simulation_running;
                this.updatePlaybackUI(this.isSimulationRunning);
            }

            // Sync speed-preset active state with server state
            if (typeof network.simulation_speed === 'number') {
                this.syncSpeedPresetUI(network.simulation_speed);
            }
        } catch (e) {
            console.error('[App] Failed to refresh network:', e);
        }
    }

    selectTrain(trainId) {
        if (!this.currentNetwork || !this.currentNetwork.trains) {
            return;
        }
        
        const train = this.currentNetwork.trains.find(t => t.id === trainId);
        if (train) {
            this.selectedTrainId = trainId;
            this.lastLookaheadData = null;
            this.lastLookaheadPayload = null;
            // Pan to train position using controller span
            const controller = window.stateManager.getController(trainId);
            if (controller?.span?.end_a) {
                const headEnd = controller.span.leading_end === 'a' ? controller.span.end_a : controller.span.end_b;
                this.renderer.panX = -headEnd.tile[0] * this.renderer.gridSize * this.renderer.scale;
                this.renderer.panY = headEnd.tile[1] * this.renderer.gridSize * this.renderer.scale;
                this.renderer.draw();
            }
            this.editor.showTrainControlPanel(trainId);
            this.trainMonitor.show(trainId, train.name || trainId);
            this._subscribeTrainMonitor(trainId);
            this.fetchTrainLookahead(trainId, train);
            this.updateTrainDebugPanel(train);
        }
    }
    
    deselectTrain() {
        this.selectedTrainId = null;
        this.lastLookaheadData = null;  // Clear stored lookahead
        this.lastLookaheadPayload = null;
        if (this.trainMonitorUnsub) {
            this.trainMonitorUnsub();
            this.trainMonitorUnsub = null;
        }
        this.trainMonitor.hide();
        this.hideTrainDebugPanel();
    }

    _subscribeTrainMonitor(trainId) {
        if (!window.stateManager) return;
        if (this.trainMonitorUnsub) {
            this.trainMonitorUnsub();
            this.trainMonitorUnsub = null;
        }

        this.trainMonitorUnsub = window.stateManager.subscribe(
            'trains',
            trainId,
            (newTrain) => {
                if (!newTrain) {
                    this.deselectTrain();
                    return;
                }
                // Only fetch fresh lookahead data -- do NOT render with
                // stale lastLookaheadPayload here.  The async fetch will
                // call updateFromController with the fresh payload once it
                // arrives, preventing the flicker caused by rendering
                // stale-then-fresh on every poll cycle.
                this.fetchTrainLookahead(trainId, newTrain);
            },
            { immediate: true }
        );
    }
    
    updateTrainDebugPanel(train) {
        const checkbox = document.getElementById('debug-show-train-position');
        const debugGeometry = document.getElementById('train-debug-geometry');

        // If debug is disabled, hide both debug sub-blocks
        if (checkbox && !checkbox.checked) {
            const controlDebug = document.getElementById('train-control-debug-section');
            if (controlDebug) controlDebug.classList.add('hidden');
            if (debugGeometry) debugGeometry.classList.add('hidden');
            return;
        }

        if (!train) return;

        // Target the Train Control panel debug section
        const controlPanelDebug = document.getElementById('train-control-debug-section');
        if (!controlPanelDebug) return;

        // Reveal the geometry + read-only properties block (Status fields moved here).
        if (debugGeometry) debugGeometry.classList.remove('hidden');

        const controller = window.stateManager.getController(train.id);
        const dispatcher = window.stateManager.getDispatcherProgress(train.id);
        const span = controller?.span || null;

        // Build the skeleton once. Subsequent calls reuse the same DOM nodes so
        // the Resource Sequence list's scroll position is preserved across the
        // 200ms sim poll + every controller/dispatcher subscription tick.
        let panel = controlPanelDebug.querySelector('.train-debug-panel');
        if (!panel) {
            controlPanelDebug.innerHTML = `
                <div class="train-debug-panel info-display-compact" style="background: transparent; border: none; padding: 0;">
                    <div class="train-debug-row"><span class="train-debug-label">Train ID:</span><span class="train-debug-value" data-debug-field="train-id"></span></div>
                    <div class="train-debug-row"><span class="train-debug-label">State:</span><span class="train-debug-value" data-debug-field="state"></span></div>
                    <div data-debug-block="resource-sequence"></div>
                    <div data-debug-block="occupied-tiles"></div>
                    <div data-debug-block="occupied-blocks"></div>
                </div>
            `;
            panel = controlPanelDebug.querySelector('.train-debug-panel');
        }

        panel.querySelector('[data-debug-field="train-id"]').textContent = train.id;
        panel.querySelector('[data-debug-field="state"]').textContent = controller?.state || 'unknown';

        // Resource sequence: rebuild children only when the sequence identity
        // changes (path reassigned / extended). Otherwise just toggle the
        // `train-debug-path-current` class so scroll position survives.
        const seqBlock = panel.querySelector('[data-debug-block="resource-sequence"]');
        const seq = dispatcher?.resource_sequence;
        if (seq && seq.length > 0) {
            const first = seq[0];
            const last = seq[seq.length - 1];
            const sig = `${dispatcher.path_id || ''}|${seq.length}|${first.resource_id}|${last.resource_id}`;
            let pathContainer = seqBlock.querySelector('.train-debug-path');
            if (!pathContainer || seqBlock.dataset.sig !== sig) {
                let html = `<div class="train-debug-subsection">Resource Sequence (${seq.length})</div>`;
                html += `<div class="train-debug-path">`;
                seq.forEach((entry) => {
                    html += `<span class="train-debug-path-step">${entry.resource_type}: ${entry.resource_id} (${entry.entry_edge || '?'}→${entry.exit_edge || '?'})</span>`;
                });
                html += `</div>`;
                seqBlock.innerHTML = html;
                seqBlock.dataset.sig = sig;
                pathContainer = seqBlock.querySelector('.train-debug-path');
            }
            const steps = pathContainer.children;
            const currentIdx = dispatcher.current_resource_index;
            for (let i = 0; i < steps.length; i++) {
                steps[i].classList.toggle('train-debug-path-current', i === currentIdx);
            }
        } else if (seqBlock.dataset.sig) {
            seqBlock.innerHTML = '';
            delete seqBlock.dataset.sig;
        }

        const tilesBlock = panel.querySelector('[data-debug-block="occupied-tiles"]');
        const tiles = span?.occupied_tiles;
        if (tiles && tiles.length > 0) {
            const sig = `${tiles.length}|${tiles.map(t => `${t[0]},${t[1]}`).join('|')}`;
            if (tilesBlock.dataset.sig !== sig) {
                let html = `<div class="train-debug-subsection">`;
                html += `<div class="train-debug-subsection-title">Occupied Tiles (${tiles.length})</div>`;
                html += `<div class="train-debug-tiles">`;
                tiles.forEach(t => {
                    html += `<span class="train-debug-tile">(${t[0]}, ${t[1]})</span>`;
                });
                html += `</div></div>`;
                tilesBlock.innerHTML = html;
                tilesBlock.dataset.sig = sig;
            }
        } else if (tilesBlock.dataset.sig) {
            tilesBlock.innerHTML = '';
            delete tilesBlock.dataset.sig;
        }

        const blocksBlock = panel.querySelector('[data-debug-block="occupied-blocks"]');
        const blocks = span?.occupied_blocks;
        if (blocks && blocks.length > 0) {
            const sig = `${blocks.length}|${blocks.join('|')}`;
            if (blocksBlock.dataset.sig !== sig) {
                let html = `<div class="train-debug-subsection">`;
                html += `<div class="train-debug-subsection-title">Occupied Blocks (${blocks.length})</div>`;
                html += `<div class="train-debug-tiles">`;
                blocks.forEach(b => {
                    html += `<span class="train-debug-tile" style="color: var(--accent-purple);">${b}</span>`;
                });
                html += `</div></div>`;
                blocksBlock.innerHTML = html;
                blocksBlock.dataset.sig = sig;
            }
        } else if (blocksBlock.dataset.sig) {
            blocksBlock.innerHTML = '';
            delete blocksBlock.dataset.sig;
        }

        controlPanelDebug.classList.remove('hidden');
    }

    hideTrainDebugPanel() {
        const controlDebug = document.getElementById('train-control-debug-section');
        if (controlDebug) controlDebug.classList.add('hidden');
        const debugGeometry = document.getElementById('train-debug-geometry');
        if (debugGeometry) debugGeometry.classList.add('hidden');
    }

    async fetchTrainLookahead(trainId, train) {
        if (!trainId) {
            return;
        }
        // Check if debug is enabled, otherwise we don't need to fetch
        const debugCheckbox = document.getElementById('debug-show-train-position');
        const monitorVisible = this.trainMonitor.isVisible();
        if (debugCheckbox && !debugCheckbox.checked && !monitorVisible) {
            return;
        }
        
        const now = Date.now();
        if (now - this.lastLookaheadFetch < this.lookaheadThrottleMs) {
            return;
        }
        this.lastLookaheadFetch = now;
        
        try {
            if (this.selectedTrainId === trainId) {
                const trainSnapshot = train || this.currentNetwork?.trains?.find(t => t.id === trainId);
                if (!trainSnapshot) {
                    return;
                }
                const lookaheadPayload = await window.api.getTrainLookahead(trainId);
                this.lastLookaheadPayload = lookaheadPayload;
                if (this.trainMonitor.isVisible()) {
                    this.trainMonitor.updateFromController(trainSnapshot, lookaheadPayload);
                }
                // Lookahead detail lives in the Train Monitor (bottom panel),
                // not in this debug strip. Cache the data only — the monitor
                // is the canonical surface for it.
                const lookaheadData = this.trainMonitor.lookaheadData;
                if (lookaheadData) {
                    this.lastLookaheadData = lookaheadData;
                    lookaheadData.speed_limit_kmh = trainSnapshot.max_speed || 0;
                }
            }
        } catch (e) {
            console.error('[App] fetchTrainLookahead failed:', e);
        }
    }

    async showAddTrainDialog() {
        const defaultTrainId = `TRN_${Date.now().toString().slice(-6)}`;
        const values = await showFormModal({
            title: 'Add Train',
            message: 'Enter train placement and startup settings.',
            fields: [
                {
                    name: 'trainId',
                    label: 'Train ID',
                    type: 'text',
                    defaultValue: defaultTrainId,
                    required: true,
                    validate: (value) => String(value || '').trim() ? '' : 'Train ID is required.'
                },
                {
                    name: 'posX',
                    label: 'X Position',
                    type: 'number',
                    defaultValue: 0,
                    step: 1,
                    validate: (value) => Number.isFinite(value) ? '' : 'X position must be a number.'
                },
                {
                    name: 'posY',
                    label: 'Y Position',
                    type: 'number',
                    defaultValue: 0,
                    step: 1,
                    validate: (value) => Number.isFinite(value) ? '' : 'Y position must be a number.'
                },
                {
                    name: 'direction',
                    label: 'Direction',
                    type: 'select',
                    defaultValue: 'EAST',
                    options: [
                        { value: 'NORTH', label: 'North' },
                        { value: 'EAST', label: 'East' },
                        { value: 'SOUTH', label: 'South' },
                        { value: 'WEST', label: 'West' }
                    ]
                },
                {
                    name: 'maxSpeed',
                    label: 'Max Speed (km/h)',
                    type: 'number',
                    defaultValue: 100,
                    min: 1,
                    step: 1,
                    validate: (value) => Number.isFinite(value) && value > 0 ? '' : 'Max speed must be greater than 0.'
                }
            ],
            confirmText: 'Create Train',
            confirmVariant: 'primary'
        });

        if (!values) {
            return;
        }

        try {
            await window.api.createTrain({
                id: String(values.trainId || '').trim(),
                position: { x: Number(values.posX), y: Number(values.posY) },
                direction: values.direction || 'EAST',
                max_speed: Number(values.maxSpeed)
            });
            await this.refreshNetwork();
        } catch (err) {
            console.error('Failed to create train:', err);
            window.showToast('Failed to create train: ' + err.message, 'error', 4000);
        }
    }

    setupDebugPanel() {
        const canvas = this.renderer.canvas;
        if (!canvas) return;

        // Add mousemove handler for tile hover
        canvas.addEventListener('mousemove', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;

            const gridX = this.renderer.screenToGridX(x);
            const gridY = this.renderer.screenToGridY(y);

            // Update debug panel with tile details
            this.updateTileDetails(gridX, gridY);
        });

        // Add change listeners to debug checkboxes to trigger re-render
        const checkboxIds = ['debug-show-bits', 'debug-show-coords', 'debug-show-switches', 'debug-show-edges', 'debug-show-blocks', 'debug-show-signals', 'debug-show-block-boundaries'];
        checkboxIds.forEach(id => {
            const checkbox = document.getElementById(id);
            if (checkbox) {
                checkbox.addEventListener('change', () => {
                    // Re-render network with debug overlays
                    if (this.currentNetwork) {
                        this.renderer.drawNetwork(this.currentNetwork);
                    }
                });
            }
        });
        
        // Train position debug checkbox
        const trainPosCheckbox = document.getElementById('debug-show-train-position');
        if (trainPosCheckbox) {
            trainPosCheckbox.addEventListener('change', () => {
                if (this.selectedTrainId && this.currentNetwork) {
                    const train = this.currentNetwork.trains?.find(t => t.id === this.selectedTrainId);
                    if (train) {
                        this.updateTrainDebugPanel(train);
                        // Also trigger lookahead fetch immediately if checkbox is enabled
                        if (trainPosCheckbox.checked) {
                            this.fetchTrainLookahead(this.selectedTrainId, train);
                        }
                    }
                } else {
                    this.hideTrainDebugPanel();
                }
            });
        }
    }

    setupSettingsPanel() {
        const scaleSelect = document.getElementById('scale-select');
        const scaleApplyBtn = document.getElementById('scale-apply-custom');
        const scaleDisplay = document.getElementById('scale-current-display');
        const labelSpacingSelect = document.getElementById('label-spacing-select');

        if (!scaleSelect || !scaleApplyBtn || !scaleDisplay) {
            console.warn('[App] Settings panel elements not found');
            return;
        }

        // Setup label spacing selector
        if (labelSpacingSelect) {
            labelSpacingSelect.addEventListener('change', () => {
                setLabelSpacing(labelSpacingSelect.value);
                this.refreshNetwork();
            });
        }

        // Load current scale on init
        this.loadCurrentScale();

        const applyScale = async (scaleValue) => {
            if (!scaleValue) return;

            const scale = parseFloat(scaleValue);
            if (isNaN(scale) || scale <= 0) {
                window.showToast('Invalid scale value', 'error');
                return;
            }

            try {
                await window.api.setScale(scale);
                await this.loadCurrentScale();
                window.showToast(`Scale set to 1:${scale}`, 'success');
            } catch (e) {
                console.error('[App] Failed to set scale:', e);
                window.showToast('Failed to set scale', 'error');
            }
        };

        // Handle preset selection
        scaleSelect.addEventListener('change', () => {
            applyScale(scaleSelect.value);
        });

        // Handle apply button click
        scaleApplyBtn.addEventListener('click', () => {
            applyScale(scaleSelect.value);
        });
    }

    async loadCurrentScale() {
        const scaleDisplay = document.getElementById('scale-current-display');
        const scaleSelect = document.getElementById('scale-select');

        try {
            const data = await window.api.getScale();
            const scale = data.scale;
            const presets = data.presets || {};

            // Update display
            if (scale === 1.0) {
                scaleDisplay.textContent = 'Current: 1:1 (Full scale)';
            } else {
                scaleDisplay.textContent = `Current: 1:${scale}`;
            }

            // Match dropdown value to current scale
            // Dropdown values are numeric strings (e.g., "160")
            const scaleOptions = Array.from(scaleSelect.options);
            const matchingOption = scaleOptions.find(opt => parseFloat(opt.value) === scale);
            if (matchingOption) {
                scaleSelect.value = matchingOption.value;
            } else {
                scaleSelect.value = '';
            }
        } catch (e) {
            console.error('[App] Failed to load scale:', e);
            scaleDisplay.textContent = 'Current: Unknown';
        }
    }

    updateTileDetails(gridX, gridY) {
        const detailsDiv = document.getElementById('tile-details');
        if (!detailsDiv) return;

        if (!this.currentNetwork || !this.currentNetwork.tracks) {
            detailsDiv.innerHTML = '<p class="debug-hint">No network loaded</p>';
            return;
        }

        const track = this.currentNetwork.tracks.find(t => t.x === gridX && t.y === gridY);

        if (!track) {
            detailsDiv.innerHTML = '<p class="debug-hint">No track at this position</p>';
            return;
        }

        // Calculate ribi for display
        const RIBI = { NORTH: 1, EAST: 2, SOUTH: 4, WEST: 8 };
        let ribi = 0;
        if (track.directed_paths) {
            for (const p of track.directed_paths) {
                if (p.port1 === 'NORTH' || p.port2 === 'NORTH') ribi |= RIBI.NORTH;
                if (p.port1 === 'EAST' || p.port2 === 'EAST') ribi |= RIBI.EAST;
                if (p.port1 === 'SOUTH' || p.port2 === 'SOUTH') ribi |= RIBI.SOUTH;
                if (p.port1 === 'WEST' || p.port2 === 'WEST') ribi |= RIBI.WEST;
            }
        }

        let html = `
            <h4 class="debug-tile-header">Tile (${gridX}, ${gridY})</h4>
            <table class="debug-table">
                <tr><td class="debug-label">Type:</td><td class="debug-value">${track.type || 'TRACK'}</td></tr>
                <tr><td class="debug-label">Track Bits:</td><td class="debug-value">${track.track_bits || 0} (0b${(track.track_bits || 0).toString(2).padStart(6, '0')})</td></tr>
                <tr><td class="debug-label">Ribi:</td><td class="debug-value">${ribi}</td></tr>
        `;

        // Show active bits
        if (track.track_bits) {
            const bits = [];
            if (track.track_bits & 1) bits.push('NS');
            if (track.track_bits & 2) bits.push('EW');
            if (track.track_bits & 4) bits.push('NE');
            if (track.track_bits & 8) bits.push('NW');
            if (track.track_bits & 16) bits.push('SE');
            if (track.track_bits & 32) bits.push('SW');
            html += `<tr><td class="debug-label">Active:</td><td class="debug-value">${bits.join(', ') || 'None'}</td></tr>`;
        }

        // Dead-ends
        if (track.dead_end_ports && track.dead_end_ports.length > 0) {
            html += `<tr><td class="debug-label">Dead-Ends:</td><td class="debug-value-deadend">${track.dead_end_ports.join(', ')}</td></tr>`;
        }

        // Switch states
        if (track.switch_states && Object.keys(track.switch_states).length > 0) {
            const switches = Object.entries(track.switch_states)
                .map(([edge, pos]) => `${edge}:${pos}`)
                .join(', ');
            html += `<tr><td class="debug-label">Switches:</td><td class="debug-value-switches">${switches}</td></tr>`;
        }

        // Validation - check if forbidden
        const FORBIDDEN_TRACK_BITS = new Set([
            13, 22, 42, 49, 15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58,
            31, 47, 55, 59, 61, 62, 63
        ]);
        const isForbidden = FORBIDDEN_TRACK_BITS.has(track.track_bits);
        html += `<tr><td class="debug-label">Valid:</td><td class="${isForbidden ? 'debug-value-invalid' : 'debug-value-valid'}">${isForbidden ? 'FORBIDDEN' : 'Valid'}</td></tr>`;

        // Occupation
        if (track.occupied_by) {
            html += `<tr><td class="debug-label">Occupied:</td><td class="debug-value-occupied">${track.occupied_by}</td></tr>`;
        }
        if (track.reserved_by) {
            html += `<tr><td class="debug-label">Reserved:</td><td class="debug-value-reserved">${track.reserved_by}</td></tr>`;
        }

        html += '</table>';
        detailsDiv.innerHTML = html;
    }

    /**
     * Start polling for simulation updates (train movement, signal changes)
     * Only runs when simulation is active
     */
    startSimulationPolling() {
        if (this.simulationInterval) return;
        this.isSimulationRunning = true;
        this.simulationInterval = setInterval(async () => {
            await this.refreshNetwork();
            
            // Phase 4 COMPLETE: All panels now use StateManager subscriptions
            // No need for refreshCurrentEditor() - subscriptions handle all updates
            
            if (this.selectedTrainId) {
                const train = window.stateManager.get('trains', this.selectedTrainId);
                if (train) {
                    // Train Control Panel updates via subscription
                    // Only update debug panel here
                    this.updateTrainDebugPanel(train);

                    // Always refresh the lookahead for the selected train.
                    // The StateManager subscription only fires on train-data
                    // changes, so a parked train (e.g. EB latched, speed=0,
                    // position frozen) would leave the monitor stale even
                    // though signals, switches, and clearance ahead can still
                    // be evolving on the server. The throttle inside
                    // fetchTrainLookahead caps the actual fetch rate.
                    this.fetchTrainLookahead(this.selectedTrainId, train);
                }
            }
        }, 200);
    }

    /**
     * Stop simulation polling
     */
    stopSimulationPolling() {
        if (this.simulationInterval) {
            clearInterval(this.simulationInterval);
            this.simulationInterval = null;
        }
        this.isSimulationRunning = false;
        // One final refresh to show stopped state
        this.refreshNetwork();
    }

    startRestartReconnectFlow({ timeoutMs = 60000, pollIntervalMs = 500 } = {}) {
        const existing = document.querySelector('.restart-overlay');
        if (existing) existing.remove();

        const overlay = document.createElement('div');
        overlay.className = 'app-modal-overlay restart-overlay';
        overlay.innerHTML = `
            <div class="app-modal restart-card">
                <div class="restart-spinner" aria-hidden="true"></div>
                <div class="restart-title">Restarting server…</div>
                <div class="restart-sub">Reconnecting when the new process is ready.</div>
            </div>
        `;
        document.body.appendChild(overlay);

        const startedAt = Date.now();
        let stopped = false;

        const finishSuccess = () => {
            stopped = true;
            setTimeout(() => window.location.reload(), 300);
        };

        const finishTimeout = () => {
            stopped = true;
            const card = overlay.querySelector('.restart-card');
            card.innerHTML = `
                <div class="restart-title">Server did not come back</div>
                <div class="restart-sub">Check the terminal where the server was launched, then refresh manually.</div>
                <div class="restart-actions">
                    <button class="btn btn-secondary btn-sm" id="restart-reload-btn">Reload</button>
                </div>
            `;
            card.querySelector('#restart-reload-btn').addEventListener('click', () => {
                window.location.reload();
            });
        };

        const tick = async () => {
            if (stopped) return;
            try {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 800);
                const res = await fetch('/api/health', { signal: controller.signal });
                clearTimeout(timer);
                if (res.ok) {
                    finishSuccess();
                    return;
                }
            } catch (_e) {
                // Expected during exec — keep polling.
            }
            if (Date.now() - startedAt >= timeoutMs) {
                finishTimeout();
                return;
            }
            setTimeout(tick, pollIntervalMs);
        };

        setTimeout(tick, pollIntervalMs);
    }
}

// Toast Notification System
(function() {
    // Ensure container exists
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    // Toast icons for each type
    const ICONS = {
        error: '❌',
        success: '✅',
        warning: '⚠️',
        info: 'ℹ️'
    };

    let toastCounter = 0;

    /**
     * Show a toast notification
     * @param {string} message - Message to display
     * @param {string} type - 'error', 'success', 'warning', or 'info'
     * @param {number} duration - Duration in ms (default: 4000, 0 = no auto-dismiss)
     * @returns {HTMLElement} - The toast element
     */
    function showToast(message, type = 'info', duration = 4000) {
        const toastId = `toast-${toastCounter++}`;

        // Create toast element
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.id = toastId;
        toast.innerHTML = `
            <span class="toast-icon">${ICONS[type] || ICONS.info}</span>
            <div class="toast-message">${message}</div>
            <button class="toast-close" aria-label="Close">×</button>
        `;

        // Add to container
        container.appendChild(toast);

        // Close button handler
        const closeBtn = toast.querySelector('.toast-close');
        closeBtn.onclick = () => dismissToast(toastId);

        // Auto-dismiss after duration (if > 0)
        if (duration > 0) {
            setTimeout(() => dismissToast(toastId), duration);
        }

        return toast;
    }

    /**
     * Dismiss a toast by ID
     * @param {string} toastId - ID of toast to dismiss
     */
    function dismissToast(toastId) {
        const toast = document.getElementById(toastId);
        if (!toast) return;

        // Add fade-out animation
        toast.classList.add('toast-fade-out');

        // Remove after animation completes
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 300);
    }

    // Expose to global scope
    window.showToast = showToast;
    window.dismissToast = dismissToast;
})();

// Sidebar resize functionality
(function() {
    let isResizing = false;
    let currentSidebar = null;
    
    function initSidebarResize(sidebar, minWidth, maxWidth) {
        if (!sidebar) return;
        
        const isSecondarySidebar = sidebar.id === 'secondary-sidebar';
        
        sidebar.addEventListener('mousedown', (e) => {
            const rect = sidebar.getBoundingClientRect();
            const isNearRightEdge = e.clientX > rect.right - 8;
            
            // Both sidebars: resize on right edge
            if (isNearRightEdge) {
                isResizing = true;
                currentSidebar = sidebar;
                sidebar.classList.add('resizing');
                document.body.style.cursor = 'ew-resize';
                document.body.style.userSelect = 'none';
                e.preventDefault();
            }
        });
    }
    
    document.addEventListener('mousemove', (e) => {
        if (!isResizing || !currentSidebar) return;
        
        const rect = currentSidebar.getBoundingClientRect();
        const isSecondarySidebar = currentSidebar.id === 'secondary-sidebar';
        
        let newWidth;
        if (isSecondarySidebar) {
            // For secondary sidebar, resize from right edge (normal behavior)
            newWidth = e.clientX - rect.left;
        } else {
            // For main sidebar, resize from right edge
            newWidth = e.clientX - rect.left;
        }
        
        const minWidth = parseInt(getComputedStyle(currentSidebar).minWidth) || 200;
        const maxWidth = parseInt(getComputedStyle(currentSidebar).maxWidth) || 600;
        
        if (newWidth >= minWidth && newWidth <= maxWidth) {
            currentSidebar.style.width = newWidth + 'px';
            
            // Secondary sidebar position is now handled by flexbox layout
        }
    });
    
    document.addEventListener('mouseup', () => {
        if (isResizing && currentSidebar) {
            currentSidebar.classList.remove('resizing');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            isResizing = false;
            currentSidebar = null;
        }
    });
    
    // Initialize both sidebars
    document.addEventListener('DOMContentLoaded', () => {
        const mainSidebar = document.querySelector('.sidebar');
        const secondarySidebar = document.getElementById('secondary-sidebar');
        
        if (mainSidebar) {
            initSidebarResize(mainSidebar, 200, 600);
        }
        if (secondarySidebar) {
            initSidebarResize(secondarySidebar, 280, 500);
        }
    });
})();

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
