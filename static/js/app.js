class App {
    constructor() {
        this.renderer = new Renderer('rail-canvas');
        this.editor = new Editor(this.renderer, window.api);
        this.trainMonitor = new TrainMonitor();
        this.simulationInterval = null;
        this.currentNetwork = null;
        this.isSimulationRunning = false;
        this.selectedTrainId = null;
        this.lastLookaheadFetch = 0;
        this.lookaheadThrottleMs = 100;
        this.lastLookaheadData = null;  // Store last lookahead for panel rebuilds

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

        document.getElementById('btn-reset').addEventListener('click', () => {
            window.api.controlSimulation('reset').catch(e => console.error('[App] Reset error:', e));
            this.stopSimulationPolling();
            this.updatePlaybackUI(false); // Ensure button shows Play
            this.refreshNetwork();
        });

        document.querySelectorAll('.speed-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const speed = parseFloat(e.target.dataset.speed);
                window.api.controlSimulation('set_speed', speed);
                
                // Update active state
                document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                
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
        if (!confirm(`Are you sure you want to delete "${name}"?`)) {
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
                const confirmed = confirm(
                    `This will clear the current layout:\n${summary}\n\nAre you sure you want to start fresh?`
                );

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
            this.currentNetwork = network;
            this.editor.setCachedNetwork(network);
            this.renderer.drawNetwork(network);
            // Validate that the current editor still exists (close if deleted)
            this.editor.validateCurrentEditor();
        } catch (e) {
            console.error('[App] Failed to refresh network:', e);
        }
    }

    selectTrain(trainId) {
        if (!this.currentNetwork || !this.currentNetwork.trains) return;
        
        const train = this.currentNetwork.trains.find(t => t.id === trainId);
        if (train) {
            this.selectedTrainId = trainId;
            // Pan to train position using new TrainPosition format
            if (train.position && train.position.end_a) {
                const headEnd = train.position.leading_end === 'a' ? train.position.end_a : train.position.end_b;
                this.renderer.panX = -headEnd.tile[0] * this.renderer.gridSize * this.renderer.scale;
                this.renderer.panY = headEnd.tile[1] * this.renderer.gridSize * this.renderer.scale;
                this.renderer.draw();
            }
            this.editor.showTrainControlPanel(trainId);
            this.trainMonitor.show(trainId, train.name || trainId);
            this.fetchTrainLookahead(trainId);
            this.updateTrainDebugPanel(train);
        }
    }
    
    deselectTrain() {
        this.selectedTrainId = null;
        this.lastLookaheadData = null;  // Clear stored lookahead
        this.trainMonitor.hide();
        this.hideTrainDebugPanel();
    }
    
    updateTrainDebugPanel(train) {
        const checkbox = document.getElementById('debug-show-train-position');
        
        // If debug is disabled, hide the debug container
        if (checkbox && !checkbox.checked) {
            const controlDebug = document.getElementById('train-control-debug-section');
            if (controlDebug) controlDebug.classList.add('hidden');
            return;
        }
        
        if (!train) return;

        // Target the Train Control panel debug section
        const controlPanelDebug = document.getElementById('train-control-debug-section');
        if (!controlPanelDebug) return;
        
        // Build HTML content
        let html = '<div class="train-debug-panel" style="background: transparent; border: none; padding: 0;">';
        
        // Basic info
        html += `<div class="train-debug-row"><span class="train-debug-label">Train ID:</span><span class="train-debug-value">${train.id}</span></div>`;
        html += `<div class="train-debug-row"><span class="train-debug-label">State:</span><span class="train-debug-value">${train.state || 'unknown'}</span></div>`;
        html += `<div class="train-debug-row"><span class="train-debug-label">Length:</span><span class="train-debug-value">${train.length_cm?.toFixed(1) || 0} cm</span></div>`;
        html += `<div class="train-debug-row"><span class="train-debug-label">Path Index:</span><span class="train-debug-value">${train.path_index != null ? train.path_index + 1 : '-'} / ${train.path_length ?? '-'}</span></div>`;
        html += `<div class="train-debug-row"><span class="train-debug-label">Distance on Track:</span><span class="train-debug-value">${train.distance_on_track_cm?.toFixed(2) ?? '-'} cm</span></div>`;
        
        // Current path
        if (train.current_path && train.current_path.length > 0) {
            html += `<div class="train-debug-subsection">Current Path (${train.current_path.length} tiles)</div>`;
            html += `<div class="train-debug-path">`;
            train.current_path.forEach((step, idx) => {
                const isCurrent = idx === train.path_index;
                const cls = isCurrent ? 'train-debug-path-current' : '';
                html += `<span class="train-debug-path-step ${cls}">(${step.x},${step.y})→${step.dir || '?'}</span>`;
            });
            html += `</div>`;
        }
        
        // TrainPosition with end_a/end_b
        if (train.position && train.position.end_a) {
            const p = train.position;
            html += `<div class="train-debug-subsection">`;
            html += `<div class="train-debug-subsection-title">End A (${p.leading_end === 'a' ? 'HEAD' : 'TAIL'})</div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Tile:</span><span class="train-debug-value text-track">(${p.end_a.tile[0]}, ${p.end_a.tile[1]})</span></div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Distance:</span><span class="train-debug-value">${p.end_a.distance_cm?.toFixed(1) || 0} cm</span></div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Entry Edge:</span><span class="train-debug-value">${p.end_a.entry_edge || '-'}</span></div>`;
            html += `</div>`;
            
            html += `<div class="train-debug-subsection">`;
            html += `<div class="train-debug-subsection-title">End B (${p.leading_end === 'b' ? 'HEAD' : 'TAIL'})</div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Tile:</span><span class="train-debug-value text-track">(${p.end_b.tile[0]}, ${p.end_b.tile[1]})</span></div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Distance:</span><span class="train-debug-value">${p.end_b.distance_cm?.toFixed(1) || 0} cm</span></div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Entry Edge:</span><span class="train-debug-value">${p.end_b.entry_edge || '-'}</span></div>`;
            html += `</div>`;
            
            html += `<div class="train-debug-subsection">`;
            html += `<div class="train-debug-subsection-title">Position Info</div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Leading End:</span><span class="train-debug-value">${p.leading_end?.toUpperCase() || '-'}</span></div>`;
            html += `<div class="train-debug-row"><span class="train-debug-label">Facing:</span><span class="train-debug-value">${p.facing_direction || '-'}</span></div>`;
            html += `</div>`;
            
            // Occupied tiles
            if (p.occupied_tiles && p.occupied_tiles.length > 0) {
                html += `<div class="train-debug-subsection">`;
                html += `<div class="train-debug-subsection-title">Occupied Tiles (${p.occupied_tiles.length})</div>`;
                html += `<div class="train-debug-tiles">`;
                p.occupied_tiles.forEach(t => {
                    html += `<span class="train-debug-tile">(${t[0]}, ${t[1]})</span>`;
                });
                html += `</div></div>`;
            }
            
            // Occupied blocks
            if (p.occupied_blocks && p.occupied_blocks.length > 0) {
                html += `<div class="train-debug-subsection">`;
                html += `<div class="train-debug-subsection-title">Occupied Blocks (${p.occupied_blocks.length})</div>`;
                html += `<div class="train-debug-tiles">`;
                p.occupied_blocks.forEach(b => {
                    html += `<span class="train-debug-tile" style="color: var(--accent-purple);">${b}</span>`;
                });
                html += `</div></div>`;
            }
        }
        
        // Rich tile data
        if (train.occupied_tiles_rich && train.occupied_tiles_rich.length > 0) {
            html += `<div class="train-debug-subsection">`;
            html += `<div class="train-debug-subsection-title">Rich Tile Data</div>`;
            train.occupied_tiles_rich.forEach((t, i) => {
                html += `<div class="train-debug-row"><span class="train-debug-label">Tile ${i} (${t.tile[0]},${t.tile[1]}):</span><span class="train-debug-value">${t.segment_start_cm?.toFixed(1)}-${t.segment_end_cm?.toFixed(1)} cm</span></div>`;
            });
            html += `</div>`;
        }
        
        // This is a placeholder for the lookahead section which will be updated separately
        html += `<div class="train-debug-lookahead"></div>`;
        
        html += '</div>'; // End wrapper
        
        // Inject into the new location in the Train Control panel
        if (controlPanelDebug) {
             controlPanelDebug.innerHTML = html;
             controlPanelDebug.classList.remove('hidden');
        }
        
        // Re-append stored lookahead data if available
        if (this.lastLookaheadData && this.selectedTrainId === train.id) {
            this.updateTrainDebugWithLookahead(this.lastLookaheadData);
        }
    }

    hideTrainDebugPanel() {
        const controlDebug = document.getElementById('train-control-debug-section');
        if (controlDebug) controlDebug.classList.add('hidden');
    }
    
    // Update debug panel with lookahead data for additional info
    updateTrainDebugWithLookahead(lookahead) {
        // We now append to the train control panel
        const targetContent = document.getElementById('train-control-debug-section');
        
        if (!targetContent) return;
        
        // Find if there's already a lookahead section and replace it, else append
        let existingLookahead = targetContent.querySelector('.train-debug-lookahead');
        
        let extra = '';
        
        // Speed & Position Info
        extra += `<div class="train-debug-subsection">`;
        extra += `<div class="train-debug-subsection-title">Lookahead - Speed & Position</div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">Current Speed:</span><span class="train-debug-value">${lookahead.current_speed_kmh?.toFixed(1) || 0} km/h</span></div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">Target Speed:</span><span class="train-debug-value">${lookahead.target_speed_kmh?.toFixed(1) || 0} km/h</span></div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">ATC Target:</span><span class="train-debug-value">${lookahead.atc_target_speed_kmh?.toFixed(1) || 0} km/h</span></div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">Max Speed:</span><span class="train-debug-value">${lookahead.max_speed_kmh?.toFixed(1) || 0} km/h</span></div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">Distance into Path:</span><span class="train-debug-value">${lookahead.distance_into_path_cm?.toFixed(1) || 0} cm</span></div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">To Next Tile:</span><span class="train-debug-value">${lookahead.distance_to_next_tile_cm?.toFixed(1) || '-'} cm</span></div>`;
        extra += `<div class="train-debug-row"><span class="train-debug-label">Clearance:</span><span class="train-debug-value text-track">${lookahead.clearance_cm?.toFixed(1) || 0} cm</span></div>`;
        if (lookahead.speed_limit_reason) {
            extra += `<div class="train-debug-row"><span class="train-debug-label">Speed Limit:</span><span class="train-debug-value text-signal-danger">${lookahead.speed_limit_reason}</span></div>`;
        }
        if (lookahead.wait_reason) {
            extra += `<div class="train-debug-row"><span class="train-debug-label">Wait Reason:</span><span class="train-debug-value text-signal-danger">${lookahead.wait_reason}</span></div>`;
        }
        extra += `</div>`;
        
        // Blocks ahead
        if (lookahead.blocks && lookahead.blocks.length > 0) {
            extra += `<div class="train-debug-subsection">`;
            extra += `<div class="train-debug-subsection-title">Lookahead - Blocks (${lookahead.blocks.length})</div>`;
            lookahead.blocks.slice(0, 5).forEach((block, i) => {
                const stateColor = block.state === 'free' ? 'text-success' : 
                                   block.state === 'occupied' ? 'text-danger' : 
                                   block.state === 'reserved' ? 'text-warning' : '';
                extra += `<div class="train-debug-row">`;
                extra += `<span class="train-debug-label">${block.name || block.id}:</span>`;
                extra += `<span class="train-debug-value ${stateColor}">${block.state} @ ${block.start_cm?.toFixed(0)}-${block.end_cm?.toFixed(0)} cm</span>`;
                extra += `</div>`;
            });
            if (lookahead.blocks.length > 5) {
                extra += `<div class="train-debug-row text-muted text-xs">...and ${lookahead.blocks.length - 5} more</div>`;
            }
            extra += `</div>`;
        }
        
        // Signals ahead
        if (lookahead.signals && lookahead.signals.length > 0) {
            extra += `<div class="train-debug-subsection">`;
            extra += `<div class="train-debug-subsection-title">Lookahead - Signals (${lookahead.signals.length})</div>`;
            lookahead.signals.slice(0, 5).forEach((signal, i) => {
                const aspectColor = signal.state === 'green' ? 'text-success' :
                                    signal.state === 'yellow' ? 'text-warning' :
                                    signal.state === 'red' ? 'text-danger' : '';
                extra += `<div class="train-debug-row">`;
                extra += `<span class="train-debug-label">${signal.id}:</span>`;
                extra += `<span class="train-debug-value ${aspectColor}">${signal.state} @ ${signal.distance_cm?.toFixed(0)} cm</span>`;
                extra += `</div>`;
            });
            if (lookahead.signals.length > 5) {
                extra += `<div class="train-debug-row text-muted text-xs">...and ${lookahead.signals.length - 5} more</div>`;
            }
            extra += `</div>`;
        }
        
        // Stations ahead
        if (lookahead.stations && lookahead.stations.length > 0) {
            extra += `<div class="train-debug-subsection">`;
            extra += `<div class="train-debug-subsection-title">Lookahead - Stations (${lookahead.stations.length})</div>`;
            lookahead.stations.forEach((station, i) => {
                extra += `<div class="train-debug-row">`;
                extra += `<span class="train-debug-label">${station.name || station.station_id}:</span>`;
                extra += `<span class="train-debug-value text-station">@ ${station.distance_cm?.toFixed(0)} cm (${station.length_cm?.toFixed(0)} cm long)</span>`;
                extra += `</div>`;
            });
            extra += `</div>`;
        }
        
        // Switches ahead
        if (lookahead.switches && lookahead.switches.length > 0) {
            extra += `<div class="train-debug-subsection">`;
            extra += `<div class="train-debug-subsection-title">Lookahead - Switches (${lookahead.switches.length})</div>`;
            lookahead.switches.slice(0, 5).forEach((sw, i) => {
                extra += `<div class="train-debug-row">`;
                extra += `<span class="train-debug-label">(${sw.x},${sw.y}):</span>`;
                extra += `<span class="train-debug-value">${sw.position || 'unknown'} @ ${sw.distance_cm?.toFixed(0)} cm</span>`;
                extra += `</div>`;
            });
            if (lookahead.switches.length > 5) {
                extra += `<div class="train-debug-row text-muted text-xs">...and ${lookahead.switches.length - 5} more</div>`;
            }
            extra += `</div>`;
        }
        
        if (existingLookahead) {
            existingLookahead.outerHTML = `<div class="train-debug-lookahead">${extra}</div>`;
        } else {
            targetContent.insertAdjacentHTML('beforeend', `<div class="train-debug-lookahead">${extra}</div>`);
        }
    }
    
    openSecondarySidebar(title, content) {
        if (this.editor && this.editor.ui) {
            this.editor.ui.showSecondarySidebar(title, content);
        }
    }
    
    closeSecondarySidebar() {
        if (this.editor && this.editor.ui) {
            this.editor.ui.hideSecondarySidebar();
        }
    }

    
    async fetchTrainLookahead(trainId) {
        if (!trainId) return;
        // Check if debug is enabled, otherwise we don't need to fetch
        const debugCheckbox = document.getElementById('debug-show-train-position');
        if (debugCheckbox && !debugCheckbox.checked) return;
        
        const now = Date.now();
        if (now - this.lastLookaheadFetch < this.lookaheadThrottleMs) return;
        this.lastLookaheadFetch = now;
        
        try {
            // Use train data from network (which includes path_tile_info)
            // instead of the non-existent lookahead endpoint
            if (!this.currentNetwork?.trains) return;
            const train = this.currentNetwork.trains.find(t => t.id === trainId);
            if (!train) return;
            
            if (this.selectedTrainId === trainId) {
                this.lastLookaheadData = train;  // Store for panel rebuilds
                if (this.trainMonitor.isVisible()) {
                    this.trainMonitor.update(train);
                }
                // Use trainMonitor's processed lookaheadData for debug panel
                // This has the correctly named fields (current_speed_kmh, etc.)
                const lookaheadData = this.trainMonitor.lookaheadData;
                if (lookaheadData) {
                    // Add max_speed from train data
                    lookaheadData.max_speed_kmh = train.max_speed || 0;
                    // Add distance_to_next_tile_cm calculation
                    if (train.path_tile_info && train.path_tile_info.length > 0) {
                        const headIndex = train.leading_end === 'a' ? train.end_a_index : train.end_b_index;
                        const headDistanceCm = train.leading_end === 'a' ? train.end_a_distance_cm : train.end_b_distance_cm;
                        const headTileInfo = train.path_tile_info[headIndex];
                        if (headTileInfo) {
                            lookaheadData.distance_to_next_tile_cm = (headTileInfo.length_cm || 50) - (headDistanceCm || 0);
                        }
                    }
                    this.updateTrainDebugWithLookahead(lookaheadData);
                }
            }
        } catch (e) {
            console.error('[App] fetchTrainLookahead failed:', e);
        }
    }

    showAddTrainDialog() {
        // Simple prompt-based dialog for now
        const trainId = prompt('Enter train ID:');
        if (!trainId) return;
        
        const posX = parseInt(prompt('Enter X position:', '0'));
        const posY = parseInt(prompt('Enter Y position:', '0'));
        
        if (isNaN(posX) || isNaN(posY)) return;
        
        window.api.createTrain({
            id: trainId,
            position: { x: posX, y: posY },
            direction: 'EAST',
            max_speed: 100
        }).then(() => {
            this.refreshNetwork();
        }).catch(err => {
            console.error('Failed to create train:', err);
            alert('Failed to create train');
        });
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
                            this.fetchTrainLookahead(this.selectedTrainId);
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
            if (this.editor) {
                this.editor.refreshCurrentEditor();
            }
            if (this.selectedTrainId) {
                // Update debug panel (checks debug checkbox internally)
                const train = this.currentNetwork?.trains?.find(t => t.id === this.selectedTrainId);
                if (train) {
                    this.updateTrainDebugPanel(train);
                }
                
                // Fetch lookahead if needed (for both Monitor and Debug Panel)
                // fetchTrainLookahead checks the debug checkbox, so we might miss updates for Monitor if debug is off
                // We should probably allow fetching if Monitor is visible OR Debug is on.
                // For now, we just ensure it's called so the Debug Panel updates even if Monitor is hidden.
                await this.fetchTrainLookahead(this.selectedTrainId);
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
