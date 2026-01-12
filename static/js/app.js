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
        this.lookaheadThrottleMs = 500;

        this.setupControls();
        this.setupDebugPanel();
        this.setupSettingsPanel();
        this.refreshNetwork();
    }

    setupControls() {
        document.getElementById('btn-play').addEventListener('click', () => {
            window.api.controlSimulation('start').catch(e => console.error('[App] Play error:', e));
            this.startSimulationPolling();
        });

        document.getElementById('btn-pause').addEventListener('click', () => {
            window.api.controlSimulation('stop').catch(e => console.error('[App] Pause error:', e));
            this.stopSimulationPolling();
        });

        document.getElementById('btn-reset').addEventListener('click', () => {
            window.api.controlSimulation('reset').catch(e => console.error('[App] Reset error:', e));
            this.stopSimulationPolling();
            this.refreshNetwork();
        });

        document.getElementById('speed-slider').addEventListener('input', (e) => {
            const speed = parseFloat(e.target.value);
            document.getElementById('speed-value').textContent = `${speed}x`;
            window.api.controlSimulation('set_speed', speed);
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
            if (train.position) {
                this.renderer.panX = -train.position.x * this.renderer.gridSize * this.renderer.scale;
                this.renderer.panY = train.position.y * this.renderer.gridSize * this.renderer.scale;
                this.renderer.draw();
            }
            this.editor.showTrainControlPanel(trainId);
            this.trainMonitor.show(trainId, train.name || trainId);
            this.fetchTrainLookahead(trainId);
        }
    }
    
    deselectTrain() {
        this.selectedTrainId = null;
        this.trainMonitor.hide();
    }
    
    async fetchTrainLookahead(trainId) {
        if (!trainId) return;
        
        const now = Date.now();
        if (now - this.lastLookaheadFetch < this.lookaheadThrottleMs) return;
        this.lastLookaheadFetch = now;
        
        try {
            const lookahead = await window.api.getTrainLookahead(trainId);
            if (this.selectedTrainId === trainId && this.trainMonitor.isVisible()) {
                this.trainMonitor.update(lookahead);
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
    }

    setupSettingsPanel() {
        const scaleSelect = document.getElementById('scale-select');
        const scaleCustomInput = document.getElementById('scale-custom');
        const scaleApplyBtn = document.getElementById('scale-apply-custom');
        const scaleDisplay = document.getElementById('scale-current-display');

        if (!scaleSelect || !scaleCustomInput || !scaleApplyBtn || !scaleDisplay) {
            console.warn('[App] Settings panel elements not found');
            return;
        }

        // Load current scale on init
        this.loadCurrentScale();

        // Handle preset selection
        scaleSelect.addEventListener('change', async () => {
            const preset = scaleSelect.value;
            if (!preset) return;

            try {
                await window.api.setScalePreset(preset);
                await this.loadCurrentScale();
                window.showToast(`Scale set to ${preset}`, 'success');
            } catch (e) {
                console.error('[App] Failed to set scale preset:', e);
                window.showToast('Failed to set scale', 'error');
            }
        });

        // Handle custom scale input
        scaleApplyBtn.addEventListener('click', async () => {
            const customScale = parseFloat(scaleCustomInput.value);
            if (isNaN(customScale) || customScale <= 0) {
                window.showToast('Invalid scale value', 'error');
                return;
            }

            try {
                await window.api.setScale(customScale);
                await this.loadCurrentScale();
                scaleSelect.value = ''; // Clear preset selection
                window.showToast(`Scale set to 1:${customScale}`, 'success');
            } catch (e) {
                console.error('[App] Failed to set custom scale:', e);
                window.showToast('Failed to set scale', 'error');
            }
        });

        // Allow Enter key to apply custom scale
        scaleCustomInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                scaleApplyBtn.click();
            }
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

            // Find matching preset
            const matchingPreset = Object.entries(presets).find(([name, val]) => val === scale);
            if (matchingPreset) {
                scaleSelect.value = matchingPreset[0];
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
            if (this.selectedTrainId && this.trainMonitor.isVisible()) {
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
            
            // Update secondary sidebar position when main sidebar resizes
            if (currentSidebar.classList.contains('sidebar')) {
                const secondary = document.getElementById('secondary-sidebar');
                if (secondary) {
                    secondary.style.left = newWidth + 'px';
                }
            }
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
