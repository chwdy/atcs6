class StationEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._stationId = null;
        this._unsubscribe = null;
        this._localState = null;
        this._isDirty = false;
        this._isUserInteracting = false;
    }

    get api() { return this.editor.api; }
    get cachedNetwork() { return this.editor.cachedNetwork; }
    get selectedElement() { return this.editor.selectedElement; }
    get stationExtendMode() { return this.editor.stationExtendMode; }
    set stationExtendMode(value) { this.editor.stationExtendMode = value; }

    showEditor(stationId) {
        this.editor._currentEditorType = 'station';
        this.editor._currentEditorId = stationId;

        const stations = this.cachedNetwork && this.cachedNetwork.stations
            ? this.cachedNetwork.stations
            : [];
        const station = stations.find(s => s.station_id === stationId);
        if (!station) {
            console.warn('[StationEditorHandler] Station not found for editor:', stationId);
            return;
        }

        const blocks = this.cachedNetwork && this.cachedNetwork.blocks
            ? this.cachedNetwork.blocks
            : [];

        const content = `
            <div class="station-editor-panel" data-station-id="${stationId}" style="background: transparent;">
                
                <!-- TOP: READ-ONLY INFO SECTION -->
                <div class="info-box-track mb-12">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-muted uppercase tracking-wider">Station</span>
                    </div>
                    
                    <!-- Station ID Header -->
                    <div class="value-display value-display-mono font-bold text-white mb-8" style="border-color: #ffd93d;">${stationId}</div>
                    
                    ${station.name ? `<div class="text-lg font-bold mb-4" style="color: #ffd93d;">${station.name}</div>` : ''}
                    
                    <div class="grid grid-cols-2 gap-y-2 gap-x-4 text-sm font-mono">
                        <div>
                            <span class="text-muted text-xs block">Track Count</span>
                            <span class="text-track font-bold">${station.positions ? station.positions.length : 0} tiles</span>
                        </div>
                        <div>
                            <span class="text-muted text-xs block">Orientation</span>
                            <span class="text-info font-bold">${station.orientation || 'Auto'}</span>
                        </div>
                    </div>
                </div>
                
                <!-- MIDDLE: TILE EDITING TOOLS -->
                <div class="control-section">
                    <h4 class="text-sm text-muted mb-8">Tiles</h4>
                    ${renderTileListWithBlocks(station.sorted_positions, station.block_assignments_sorted, blocks, {
                        entityType: 'station',
                        extendStartBtnId: 'extend-start-btn',
                        extendEndBtnId: 'extend-end-btn',
                        removeBtnClass: 'delete-tile-btn',
                        assignBtnClass: 'assign-block-btn'
                    })}
                </div>
                
                <!-- BOTTOM: EDITABLE PROPERTIES -->
                <div class="control-section mt-12 pt-12" style="border-top: 1px solid var(--border-color);">
                    <h4 class="text-sm text-muted mb-8">Properties</h4>
                    
                    ${renderTextInput('station-name-input', 'Station Name', station.name || '', 'Enter station name...')}
                    ${renderNumberInput('station-length-input', 'Platform Length (cm)', station.length_cm || 0, { min: 0, step: 10 })}
                    ${renderDropdown('station-side-input', 'Platform Side', station.platform_side, [
                        { value: 'both', label: 'Both Sides' },
                        { value: 'left', label: 'Left Side' },
                        { value: 'right', label: 'Right Side' }
                    ])}
                    ${renderNumberInput('station-speed-input', 'Top Speed (km/h)', station.top_speed_kmh || '', { min: 0, step: 5, placeholder: 'Not set' })}
                    
                    <div class="flex gap-8 mt-12">
                        <button id="save-station-btn" class="btn btn-primary btn-sm flex-1">💾 Save</button>
                        <button id="delete-station-btn" class="btn btn-danger btn-sm flex-1">🗑 Delete</button>
                    </div>
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('🚉 Station Editor', content);

        // Setup subscription
        this._setupSubscription(stationId, station);

        setTimeout(() => this.initEditorHandlers(stationId), 0);
    }

    async initEditorHandlers(stationId) {
        const saveBtn = document.getElementById('save-station-btn');
        const deleteBtn = document.getElementById('delete-station-btn');
        const nameInput = document.getElementById('station-name-input');
        const lengthInput = document.getElementById('station-length-input');
        const sideInput = document.getElementById('station-side-input');
        const speedInput = document.getElementById('station-speed-input');

        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {};

                if (nameInput) {
                    updates.name = nameInput.value.trim() || '';
                }
                if (lengthInput) {
                    const val = parseFloat(lengthInput.value);
                    if (!isNaN(val) && val >= 0) {
                        updates.length_cm = val;
                    }
                }
                if (sideInput) {
                    updates.platform_side = sideInput.value;
                }
                if (speedInput) {
                    const val = speedInput.value.trim();
                    if (val === '') {
                        updates.top_speed_kmh = null;
                    } else {
                        const numVal = parseFloat(val);
                        if (!isNaN(numVal) && numVal >= 0) {
                            updates.top_speed_kmh = numVal;
                        }
                    }
                }

                try {
                    await this.api.updateStation(stationId, updates);
                    await window.app.refreshNetwork();
                    this.showEditor(stationId);
                    if (this.selectedElement && this.selectedElement.station_id === stationId) {
                        await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                    window.showToast('Station properties saved', 'success', 5000);
                } catch (err) {
                    console.error('Failed to save station properties:', err);
                    window.showToast('Failed to save station properties: ' + err.message, 'error', 5000);
                }
            };
        }

        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete station "${stationId}"? This will remove the station from all tracks.`)) {
                    return;
                }
                const selectedPos = this.selectedElement ? { x: this.selectedElement.x, y: this.selectedElement.y } : null;
                const wasShowingThisStation = this.selectedElement && this.selectedElement.station_id === stationId;
                try {
                    await this.api.deleteStation(stationId);
                    await window.app.refreshNetwork();
                    this.editor.hideSecondarySidebar();
                    if (wasShowingThisStation && selectedPos) {
                        await this.editor.selectElement(selectedPos);
                    }
                    window.showToast('Station deleted', 'success', 5000);
                } catch (err) {
                    console.error('Failed to delete station:', err);
                    window.showToast('Failed to delete station: ' + err.message, 'error', 5000);
                }
            };
        }

        const extendStartBtn = document.getElementById('extend-start-btn');
        if (extendStartBtn) {
            extendStartBtn.onclick = () => {
                this.enterExtendMode(stationId, 'start');
            };
        }

        const extendEndBtn = document.getElementById('extend-end-btn');
        if (extendEndBtn) {
            extendEndBtn.onclick = () => {
                this.enterExtendMode(stationId, 'end');
            };
        }

        const deleteTileBtns = document.querySelectorAll('.delete-tile-btn');
        deleteTileBtns.forEach(btn => {
            const x = parseInt(btn.dataset.x);
            const y = parseInt(btn.dataset.y);
            
            btn.onmouseenter = () => {
                btn.style.background = '#e83f5b';
                btn.style.color = 'white';
            };
            btn.onmouseleave = () => {
                btn.style.background = 'transparent';
                btn.style.color = '#e83f5b';
            };
            
            btn.onclick = async () => {
                try {
                    await this.api.removeStationTile(stationId, x, y);
                    await window.app.refreshNetwork();
                    
                    const stationStillExists = (
                        this.cachedNetwork && this.cachedNetwork.stations
                            ? this.cachedNetwork.stations
                            : []
                    ).some(s => s.station_id === stationId);
                    
                    if (stationStillExists) {
                        this.showEditor(stationId);
                    } else {
                        this.editor.hideSecondarySidebar();
                        window.showToast('Station deleted (last tile removed)', 'success', 5000);
                    }
                    
                    if (this.selectedElement && this.selectedElement.x === x && this.selectedElement && this.selectedElement.y === y) {
                        await this.editor.selectElement({ x, y });
                    }
                    
                    window.showToast(`Tile (${x}, ${y}) removed from station`, 'success', 5000);
                } catch (err) {
                    console.error(`Failed to remove tile (${x}, ${y}) from station:`, err);
                    window.showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
                }
            };
        });

        const assignBlockBtns = document.querySelectorAll('.assign-block-btn');
        assignBlockBtns.forEach(btn => {
            const x = parseInt(btn.dataset.x);
            const y = parseInt(btn.dataset.y);
            const index = parseInt(btn.dataset.index);
            
            btn.onclick = async () => {
                await this.showBlockAssignmentDialog(stationId, x, y, index);
            };
        });
    }

    async showBlockAssignmentDialog(stationId, x, y, tileIndex) {
        const station = (
            this.cachedNetwork && this.cachedNetwork.stations
                ? this.cachedNetwork.stations
                : []
        ).find(s => s.station_id === stationId);
        if (!station) return;

        const blocks = this.cachedNetwork && this.cachedNetwork.blocks ? this.cachedNetwork.blocks : [];
        const currentBlock = station.block_assignments_sorted && station.block_assignments_sorted[tileIndex]
            ? station.block_assignments_sorted[tileIndex]
            : null;

        const blockOptions = blocks.map(b => ({
            value: b.block_id,
            label: `${b.block_id} (${b.state})`
        }));

        blockOptions.unshift({ value: '', label: '-- No Block --' });

        const dialogHtml = `
            <div style="background: #1a1a2e; border: 2px solid #00d4ff; border-radius: 8px; padding: 20px; max-width: 400px;">
                <h4 style="color: #00d4ff; margin: 0 0 16px 0;">Assign Tile to Block</h4>
                <div style="margin-bottom: 16px;">
                    <div style="color: #aaa; font-size: 14px;">Tile: <span style="color: #fff; font-family: monospace;">(${x}, ${y})</span></div>
                    ${currentBlock ? `<div style="color: #aaa; font-size: 14px; margin-top: 4px;">Current: <span style="color: #ffd93d;">${currentBlock}</span></div>` : ''}
                </div>
                <select id="block-assign-select" style="width: 100%; padding: 8px; background: #2a2a3e; color: #fff; border: 1px solid #3a3a4e; border-radius: 4px; margin-bottom: 16px;">
                    ${blockOptions.map(opt => 
                        `<option value="${opt.value}" ${opt.value === currentBlock ? 'selected' : ''}>${opt.label}</option>`
                    ).join('')}
                </select>
                <div style="display: flex; gap: 12px;">
                    <button id="block-assign-save" style="flex: 1; padding: 10px; background: #00d4ff; color: #0a0a0f; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">
                        Save
                    </button>
                    <button id="block-assign-cancel" style="padding: 10px 20px; background: transparent; color: #e83f5b; border: 1px solid #e83f5b; border-radius: 4px; cursor: pointer;">
                        Cancel
                    </button>
                </div>
            </div>
        `;

        const overlay = document.createElement('div');
        overlay.id = 'block-assign-overlay';
        overlay.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; z-index: 10000;';
        overlay.innerHTML = dialogHtml;
        document.body.appendChild(overlay);

        const saveBtn = overlay.querySelector('#block-assign-save');
        const cancelBtn = overlay.querySelector('#block-assign-cancel');
        const select = overlay.querySelector('#block-assign-select');

        saveBtn.onclick = async () => {
            const selectedBlockId = select.value;
            
            try {
                if (selectedBlockId === '') {
                    if (currentBlock) {
                        await this.api.unassignTileFromBlock(stationId, x, y);
                        window.showToast(`Tile (${x}, ${y}) unassigned from block`, 'success', 5000);
                    }
                } else {
                    await this.api.assignTileToBlock(stationId, x, y, selectedBlockId);
                    window.showToast(`Tile (${x}, ${y}) assigned to ${selectedBlockId}`, 'success', 5000);
                }
                
                await window.app.refreshNetwork();
                this.showEditor(stationId);
                
                document.body.removeChild(overlay);
            } catch (err) {
                console.error('Failed to assign tile to block:', err);
                window.showToast(`Failed to assign tile: ${err.message}`, 'error', 5000);
            }
        };

        cancelBtn.onclick = () => {
            document.body.removeChild(overlay);
        };

        overlay.onclick = (e) => {
            if (e.target === overlay) {
                document.body.removeChild(overlay);
            }
        };
    }

    async enterExtendMode(stationId, direction) {
        console.log(`[StationEditorHandler] Auto-extending station: ${stationId} (${direction})`);
        
        try {
        const stations = this.cachedNetwork && this.cachedNetwork.stations
            ? this.cachedNetwork.stations
            : [];
        const station = stations.find(s => s.station_id === stationId);
        if (!station) {
            throw new Error('Station not found');
        }

            const orientation = station.orientation || station.initial_orientation;
            const sortedPositions = station.sorted_positions || station.positions;

            console.log('[StationEditorHandler] Station data:', {
                stationId,
                orientation,
                sortedPositions: JSON.stringify(sortedPositions),
                direction
            });
            
            sortedPositions.forEach((pos, idx) => {
                const track = this.cachedNetwork.tracks.find(t => t.x === pos[0] && t.y === pos[1]);
                console.log(`[StationEditorHandler] Position ${idx}: (${pos[0]}, ${pos[1]}) x+y=${pos[0] + pos[1]} track_bits=${track ? track.track_bits : undefined}`);
            });

            if (!orientation) {
                throw new Error('Cannot determine station orientation');
            }

            const tracks = this.cachedNetwork && this.cachedNetwork.tracks ? this.cachedNetwork.tracks : [];
            const nextTile = calculateNextTile(sortedPositions, orientation, direction, tracks);
            console.log(`[StationEditorHandler] Auto-extending to tile (${nextTile.x}, ${nextTile.y}) with track bits ${nextTile.trackBits}`);

            const canExtend = await this.api.canExtendStation(stationId, nextTile.x, nextTile.y);
            
            if (!canExtend.can_extend) {
                window.showToast(`Cannot extend ${direction}: ${canExtend.reason}`, 'error', 5000);
                return;
            }
            
            await this.api.extendStation(stationId, [[nextTile.x, nextTile.y]]);
            await window.app.refreshNetwork();
            
            this.showEditor(stationId);
            
            if (this.selectedElement && this.selectedElement.x === nextTile.x && this.selectedElement && this.selectedElement.y === nextTile.y) {
                await this.editor.selectElement({ x: nextTile.x, y: nextTile.y });
            }
            
            window.showToast(`Station extended ${direction} to (${nextTile.x}, ${nextTile.y})`, 'success', 3000);
        } catch (err) {
            console.error('Failed to auto-extend station:', err);
            window.showToast(`Failed to extend station: ${err.message}`, 'error', 5000);
        }
    }

    exitExtendMode() {
        if (this.stationExtendMode && this.stationExtendMode.active) {
            this.stationExtendMode.active = false;
            this.stationExtendMode.stationId = null;
        }
    }

    async handleExtendClick(x, y) {
        if (!this.stationExtendMode || !this.stationExtendMode.active) {
            return false;
        }

        const { stationId, direction } = this.stationExtendMode;
        
        try {
            const canExtend = await this.api.canExtendStation(stationId, x, y);
            
            if (!canExtend.can_extend) {
                window.showToast(`Cannot extend: ${canExtend.reason}`, 'error', 5000);
                return true;
            }
            
            await this.api.extendStation(stationId, [[x, y]]);
            await window.app.refreshNetwork();
            
            this.exitExtendMode();
            
            this.showEditor(stationId);
            
            if (this.selectedElement && this.selectedElement.x === x && this.selectedElement && this.selectedElement.y === y) {
                await this.editor.selectElement({ x, y });
            }
            
            window.showToast(`Tile (${x}, ${y}) added to station`, 'success', 5000);
            return true;
        } catch (err) {
            console.error('Failed to extend station:', err);
            window.showToast(`Failed to extend station: ${err.message}`, 'error', 5000);
            return true;
        }
    }

    showModal(options) {
        return new Promise((resolve) => {
            const { mode, x, y, existingBlock, station } = options;

            const overlay = document.createElement('div');
            overlay.className = 'block-modal-overlay';

            const defaultId = station && station.station_id || `STA_${Date.now().toString(16).toUpperCase().slice(-6)}`;

            let modalContent = '';

            if (mode === 'create') {
                modalContent = `
                    <div class="block-modal">
                        <h3>Create New Station</h3>
                        <div class="modal-body">
                            ${existingBlock ? `
                                <p class="modal-warning">
                                    ⚠️ This will split block <strong>${existingBlock}</strong> into separate blocks
                                </p>
                            ` : ''}
                            <div class="form-group">
                                <label for="modal-station-id">Station ID</label>
                                <input type="text" id="modal-station-id" value="${defaultId}" required>
                            </div>
                            <div class="form-group">
                                <label for="modal-station-name">Station Name</label>
                                <input type="text" id="modal-station-name" placeholder="e.g., Central Station" required>
                            </div>
                            <div class="form-group">
                                <label for="modal-length-cm">Platform Length (cm)</label>
                                <input type="number" id="modal-length-cm" placeholder="e.g., 500" min="0" step="10" value="500">
                                <small>Physical length of the platform in centimeters</small>
                            </div>
                            <div class="form-group">
                                <label for="modal-platform-side">Platform Side</label>
                                <select id="modal-platform-side">
                                    <option value="both">Both Sides</option>
                                    <option value="left">Left Side Only</option>
                                    <option value="right">Right Side Only</option>
                                </select>
                                <small>Which side(s) of the track have platforms</small>
                            </div>
                            <div class="form-group">
                                <label for="modal-top-speed">Top Speed (km/h)</label>
                                <input type="number" id="modal-top-speed" placeholder="e.g., 40" min="0" step="5">
                                <small>Optional speed limit through the station</small>
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Create Station</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
            } else {
                modalContent = `
                    <div class="block-modal">
                        <h3>Edit Station: ${station.station_id}</h3>
                        <div class="modal-body">
                            <div class="form-group">
                                <label>Station ID</label>
                                <input type="text" value="${station.station_id}" disabled>
                                <small>Station ID cannot be changed</small>
                            </div>
                            <div class="form-group">
                                <label for="modal-station-name">Station Name</label>
                                <input type="text" id="modal-station-name" value="${station.name || ''}" required>
                            </div>
                            <div class="form-group">
                                <label for="modal-length-cm">Platform Length (cm)</label>
                                <input type="number" id="modal-length-cm" value="${station.length_cm || 0}" min="0" step="10">
                            </div>
                            <div class="form-group">
                                <label for="modal-platform-side">Platform Side</label>
                                <select id="modal-platform-side">
                                    <option value="both" ${station.platform_side === 'both' ? 'selected' : ''}>Both Sides</option>
                                    <option value="left" ${station.platform_side === 'left' ? 'selected' : ''}>Left Side Only</option>
                                    <option value="right" ${station.platform_side === 'right' ? 'selected' : ''}>Right Side Only</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label for="modal-top-speed">Top Speed (km/h)</label>
                                <input type="number" id="modal-top-speed" value="${station.top_speed_kmh || ''}" min="0" step="5">
                            </div>
                            <div class="form-group">
                                <label>Tiles</label>
                                <div class="info-text">${station.tile_count || 0} tiles</div>
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Save Changes</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
            }

            overlay.innerHTML = modalContent;
            document.body.appendChild(overlay);

            const stationIdInput = overlay.querySelector('#modal-station-id');
            const stationNameInput = overlay.querySelector('#modal-station-name');
            const lengthInput = overlay.querySelector('#modal-length-cm');
            const platformSideSelect = overlay.querySelector('#modal-platform-side');
            const topSpeedInput = overlay.querySelector('#modal-top-speed');
            const confirmBtn = overlay.querySelector('#modal-confirm');
            const cancelBtn = overlay.querySelector('#modal-cancel');

            confirmBtn.addEventListener('click', () => {
                const name = stationNameInput.value.trim();
                const lengthCm = parseFloat(lengthInput.value) || 0;
                const platformSide = platformSideSelect.value;
                const topSpeedKmh = topSpeedInput.value ? parseFloat(topSpeedInput.value) : null;

                if (!name) {
                    window.showToast('Station name is required', 'error', 5000);
                    stationNameInput.focus();
                    return;
                }

                if (mode === 'create') {
                    const stationId = stationIdInput.value.trim();
                    if (!stationId) {
                        window.showToast('Station ID is required', 'error', 5000);
                        stationIdInput.focus();
                        return;
                    }
                }

                if (lengthCm < 0) {
                    window.showToast('Length cannot be negative', 'error', 5000);
                    lengthInput.focus();
                    return;
                }

                if (topSpeedKmh !== null && topSpeedKmh <= 0) {
                    window.showToast('Top speed must be positive', 'error', 5000);
                    topSpeedInput.focus();
                    return;
                }

                overlay.remove();
                document.removeEventListener('keydown', handleEscape);

                resolve({
                    stationId: mode === 'create' ? stationIdInput.value.trim() : station.station_id,
                    name,
                    lengthCm,
                    platformSide,
                    topSpeedKmh
                });
            });

            cancelBtn.addEventListener('click', () => {
                overlay.remove();
                document.removeEventListener('keydown', handleEscape);
                resolve(null);
            });

            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.remove();
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            });

            const handleEscape = (e) => {
                if (e.key === 'Escape') {
                    overlay.remove();
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            };
            document.addEventListener('keydown', handleEscape);

            const handleEnter = (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    confirmBtn && confirmBtn.click();
                }
            };
            overlay.addEventListener('keydown', handleEnter);

            setTimeout(() => {
                if (mode === 'create') {
                    stationNameInput && stationNameInput.focus();
                } else {
                    stationNameInput && stationNameInput.focus();
                    stationNameInput && stationNameInput.select();
                }
            }, 50);
        });
    }
    
    // ═══════════════════════════════════════════════════════════════
    // SUBSCRIPTION METHODS (Phase 4)
    // ═══════════════════════════════════════════════════════════════
    
    _setupSubscription(stationId, stationData) {
        this._cleanup();
        this._stationId = stationId;
        this._localState = JSON.parse(JSON.stringify(stationData));
        this._isDirty = false;
        
        this._unsubscribe = window.stateManager.subscribe(
            'stations',
            stationId,
            (newStation, oldStation) => this._onStationUpdate(newStation, oldStation),
            { immediate: false }
        );
    }
    
    _onStationUpdate(newStation, oldStation) {
        if (!newStation) {
            window.showToast('Station was deleted', 'warning');
            this.editor.hideSecondarySidebar();
            return;
        }
        
        if (this._isUserInteracting) return;
        
        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }
        
        if (this._isDirty) return;
        
        this._localState = JSON.parse(JSON.stringify(newStation));
    }
    
    _cleanup() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
        this._stationId = null;
        this._localState = null;
        this._isDirty = false;
        this._isUserInteracting = false;
    }
}
