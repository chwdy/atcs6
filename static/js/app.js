class App {
    constructor() {
        console.log('[App] Initializing...');
        this.renderer = new Renderer('rail-canvas');
        console.log('[App] Renderer created');
        this.editor = new Editor(this.renderer, window.api);
        console.log('[App] Editor created');
        this.simulationInterval = null;
        this.currentNetwork = null;

        this.setupControls();
        console.log('[App] Controls setup complete');
        this.setupDebugPanel();
        console.log('[App] Debug panel setup complete');
        this.refreshNetwork();

        // Start polling loop
        this.startPolling();
        console.log('[App] Initialization complete');
    }

    setupControls() {
        console.log('[App] Setting up controls...');
        
        document.getElementById('btn-play').addEventListener('click', () => {
            console.log('[App] Play button clicked');
            window.api.controlSimulation('start').then(r => console.log('[App] Play response:', r)).catch(e => console.error('[App] Play error:', e));
        });

        document.getElementById('btn-pause').addEventListener('click', () => {
            console.log('[App] Pause button clicked');
            window.api.controlSimulation('stop').then(r => console.log('[App] Pause response:', r)).catch(e => console.error('[App] Pause error:', e));
        });

        document.getElementById('btn-reset').addEventListener('click', () => {
            console.log('[App] Reset button clicked');
            window.api.controlSimulation('reset').then(r => console.log('[App] Reset response:', r)).catch(e => console.error('[App] Reset error:', e));
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

        // Add train button
        const addTrainBtn = document.getElementById('btn-add-train');
        if (addTrainBtn) {
            addTrainBtn.addEventListener('click', () => {
                this.showAddTrainDialog();
            });
        }
    }

    async refreshNetwork() {
        console.log('[App] Refreshing network...');
        try {
            const network = await window.api.getNetwork();
            console.log('[App] Got network:', network);
            this.currentNetwork = network;
            this.editor.setCachedNetwork(network);
            this.renderer.drawNetwork(network);
            this.updateTrainsPanel(network.trains);
            console.log('[App] Network refresh complete');
        } catch (e) {
            console.error('[App] Failed to refresh network:', e);
        }
    }

    updateTrainsPanel(trains) {
        const panel = document.querySelector('.trains-panel .trains-list');
        if (!panel) return;
        
        if (!trains || trains.length === 0) {
            panel.innerHTML = '<p style="color: #666; font-size: 12px; padding: 8px;">No trains</p>';
            return;
        }
        
        panel.innerHTML = trains.map(train => {
            const statusColor = {
                'STOPPED': '#666',
                'RUNNING': '#04d361',
                'WAITING': '#ffcd1e',
                'BLOCKED': '#e83f5b'
            }[train.status] || '#666';
            
            const pos = train.position ? `(${train.position.x}, ${train.position.y})` : 'Unknown';
            
            return `
                <div class="train-item" data-train-id="${train.id}" style="
                    padding: 8px;
                    border-bottom: 1px solid #2a2a3a;
                    cursor: pointer;
                ">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span style="font-weight: bold; color: #8257e5;">${train.id}</span>
                        <span style="
                            color: ${statusColor};
                            font-size: 10px;
                            padding: 2px 6px;
                            background: ${statusColor}22;
                            border-radius: 3px;
                        ">${train.status || 'STOPPED'}</span>
                    </div>
                    <div style="font-size: 11px; color: #888; margin-top: 4px;">
                        <span>📍 ${pos}</span>
                        <span style="margin-left: 8px;">→ ${train.direction || 'EAST'}</span>
                    </div>
                    <div style="font-size: 11px; color: #888;">
                        <span>🚄 ${(train.current_speed || 0).toFixed(1)} / ${train.max_speed || 100} km/h</span>
                    </div>
                </div>
            `;
        }).join('');
        
        // Add click handlers for train selection
        panel.querySelectorAll('.train-item').forEach(item => {
            item.addEventListener('click', () => {
                const trainId = item.dataset.trainId;
                this.selectTrain(trainId);
            });
        });
    }

    selectTrain(trainId) {
        if (!this.currentNetwork || !this.currentNetwork.trains) return;
        
        const train = this.currentNetwork.trains.find(t => t.id === trainId);
        if (train) {
            this.updateTrainPropertiesPanel(train);
            
            // Pan to train location - adjust panX/panY to center on the train
            if (train.position) {
                // Calculate how far from center the train is in screen space
                // We want the train's grid position to be at screen center
                // gridToScreenX(x) = centerX + panX + x * gridSize * scale
                // We want gridToScreenX(x) = canvas.width/2, so:
                // panX = canvas.width/2 - centerX - x * gridSize * scale
                // Since centerX = canvas.width/2, this simplifies to:
                // panX = -x * gridSize * scale
                this.renderer.panX = -train.position.x * this.renderer.gridSize * this.renderer.scale;
                this.renderer.panY = train.position.y * this.renderer.gridSize * this.renderer.scale; // +Y because screen Y is inverted
                this.renderer.draw();
            }
        }
    }

    updateTrainPropertiesPanel(train) {
        const panel = document.querySelector('.properties-panel');
        if (!panel) return;
        
        panel.innerHTML = `
            <h3>Train: ${train.id}</h3>
            <div class="property-item">
                <label>Status:</label>
                <span>${train.status || 'STOPPED'}</span>
            </div>
            <div class="property-item">
                <label>Position:</label>
                <span>${train.position ? `(${train.position.x}, ${train.position.y})` : 'Unknown'}</span>
            </div>
            <div class="property-item">
                <label>Direction:</label>
                <span>${train.direction || 'EAST'}</span>
            </div>
            <div class="property-item">
                <label>Speed:</label>
                <span>${(train.current_speed || 0).toFixed(1)} / ${train.max_speed || 100} km/h</span>
            </div>
            <div class="property-item">
                <label>Length:</label>
                <span>${train.length || 1} units</span>
            </div>
            ${train.schedule ? `
            <div class="property-item">
                <label>Schedule:</label>
                <span>${train.schedule.stops ? train.schedule.stops.length + ' stops' : 'None'}</span>
            </div>
            ` : ''}
        `;
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
        const checkboxIds = ['debug-show-bits', 'debug-show-coords', 'debug-show-switches', 'debug-show-edges'];
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
            <h4 style="margin: 0 0 8px 0; color: #00ff88;">Tile (${gridX}, ${gridY})</h4>
            <table class="debug-table" style="width: 100%; font-size: 11px;">
                <tr><td style="color: #8257e5; font-weight: bold; padding-right: 8px;">Type:</td><td style="color: #e1e1e6; font-family: monospace;">${track.type || 'TRACK'}</td></tr>
                <tr><td style="color: #8257e5; font-weight: bold;">Track Bits:</td><td style="color: #e1e1e6; font-family: monospace;">${track.track_bits || 0} (0b${(track.track_bits || 0).toString(2).padStart(6, '0')})</td></tr>
                <tr><td style="color: #8257e5; font-weight: bold;">Ribi:</td><td style="color: #e1e1e6; font-family: monospace;">${ribi}</td></tr>
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
            html += `<tr><td style="color: #8257e5; font-weight: bold;">Active:</td><td style="color: #e1e1e6; font-family: monospace;">${bits.join(', ') || 'None'}</td></tr>`;
        }

        // Dead-ends
        if (track.dead_end_ports && track.dead_end_ports.length > 0) {
            html += `<tr><td style="color: #8257e5; font-weight: bold;">Dead-Ends:</td><td style="color: #ff9900; font-family: monospace;">${track.dead_end_ports.join(', ')}</td></tr>`;
        }

        // Switch states
        if (track.switch_states && Object.keys(track.switch_states).length > 0) {
            const switches = Object.entries(track.switch_states)
                .map(([edge, pos]) => `${edge}:${pos}`)
                .join(', ');
            html += `<tr><td style="color: #8257e5; font-weight: bold;">Switches:</td><td style="color: #ffcd1e; font-family: monospace;">${switches}</td></tr>`;
        }

        // Validation - check if forbidden
        const FORBIDDEN_TRACK_BITS = new Set([
            13, 22, 42, 49, 15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58,
            31, 47, 55, 59, 61, 62, 63
        ]);
        const isForbidden = FORBIDDEN_TRACK_BITS.has(track.track_bits);
        html += `<tr><td style="color: #8257e5; font-weight: bold;">Valid:</td><td style="color: ${isForbidden ? '#e83f5b' : '#04d361'}; font-weight: bold;">${isForbidden ? 'FORBIDDEN' : 'Valid'}</td></tr>`;

        // Occupation
        if (track.occupied_by) {
            html += `<tr><td style="color: #8257e5; font-weight: bold;">Occupied:</td><td style="color: #e83f5b; font-family: monospace;">${track.occupied_by}</td></tr>`;
        }
        if (track.reserved_by) {
            html += `<tr><td style="color: #8257e5; font-weight: bold;">Reserved:</td><td style="color: #ffcd1e; font-family: monospace;">${track.reserved_by}</td></tr>`;
        }

        html += '</table>';
        detailsDiv.innerHTML = html;
    }

    startPolling() {
        setInterval(async () => {
            await this.refreshNetwork();
        }, 500); // Poll every 500ms
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
