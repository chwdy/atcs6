// ============================================================================
// editor-blocks.js - Block Editor Handler
// ============================================================================
// Requires: constants.js, helpers.js, api.js
// Handles all block-related editor functionality
// ============================================================================

class BlockEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._blockId = null;
        this._unsubscribe = null;
        this._localState = null;
        this._isDirty = false;
        this._isUserInteracting = false;  // For tile picker drag operations
    }

    // Convenience getters for common editor properties
    get api() { return this.editor.api; }
    get cachedBlocks() { return this.editor.cachedBlocks; }
    set cachedBlocks(value) { this.editor.cachedBlocks = value; }
    get selectedBlockId() { return this.editor.selectedBlockId; }
    set selectedBlockId(value) { this.editor.selectedBlockId = value; }
    get blockExtendMode() { return this.editor.blockExtendMode; }
    set blockExtendMode(value) { this.editor.blockExtendMode = value; }
    get selectedElement() { return this.editor.selectedElement; }

    // === Block Manager Methods ===

    setup() {
        // Block Manager UI has been removed - blocks are now managed via
        // the secondary sidebar block editor. Keep the block cache refresh.
        this.refreshList();
    }

    async refreshList() {
        try {
            const blocks = await this.api.getBlocks();
            this.cachedBlocks = blocks;
            // No longer renders to UI - Block Manager panel removed
        } catch (err) {
            console.error('[BlockEditorHandler] Failed to refresh blocks:', err);
        }
    }

    select(blockId) {
        this.selectedBlockId = blockId;
        // Block selection now only highlights tracks on canvas
        // UI list no longer exists
    }

    getColor(blockId) {
        // Match renderer's HSL hash-based color generation
        if (!blockId) return '#666';
        let hash = 0;
        for (let i = 0; i < blockId.length; i++) {
            hash = blockId.charCodeAt(i) + ((hash << 5) - hash);
        }
        const hue = Math.abs(hash) % 360;
        return `hsl(${hue}, 70%, 50%)`;
    }

    // === Block Editor Panel ===

    async showEditor(blockId) {
        // Track this editor for auto-refresh
        this.editor._currentEditorType = 'block';
        this.editor._currentEditorId = blockId;

        // Fetch fresh block data from the API to ensure we have current state
        let block;
        try {
            block = await this.api.getBlock(blockId);
            if (block.error) {
                console.warn('[BlockEditorHandler] Block not found for editor:', blockId, block.error);
                return;
            }
        } catch (err) {
            // Fallback to cached data if API fails
            block = this.cachedBlocks?.find(b => b.block_id === blockId);
            if (!block) {
                console.warn('[BlockEditorHandler] Block not found for editor:', blockId);
                return;
            }
        }

        const blockColor = this.getColor(blockId);
        const stateColor = block.state === 'free' ? '#04d361' :
                          block.state === 'reserved' ? '#ffd93d' :
                          block.state === 'occupied' ? '#e83f5b' :
                          block.state === 'caution' ? '#ff9500' : '#888';

        const content = `
            <div class="block-editor-panel" data-block-id="${blockId}" style="background: transparent;">
                ${renderHeaderId(blockId, blockColor)}
                
                <!-- INFO SECTION (Read-only, styled like Train Control) -->
                <div class="info-box-track mb-12">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-muted uppercase tracking-wider">State</span>
                        ${block.occupied_by ? `<span class="badge badge-warning text-xxs">🚂 ${block.occupied_by}</span>` : ''}
                    </div>
                    
                    <div class="text-2xl font-bold mb-4 font-mono" style="color: ${stateColor}" id="block-state-display">
                        ${(block.state || 'free').toUpperCase()}
                    </div>
                    
                    <div class="grid grid-cols-2 gap-y-2 gap-x-4 text-sm font-mono">
                        <div>
                            <span class="text-muted text-xs block">Track Count</span>
                            <span class="text-track font-bold">${block.track_count || 0} tiles</span>
                        </div>
                        <div>
                            <span class="text-muted text-xs block">Length</span>
                            <span class="text-info">${block.length_cm || 0} cm</span>
                        </div>
                    </div>
                </div>
                
                <!-- CONTROL BUTTONS -->
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Actions</h4>
                    <div class="flex gap-8">
                        <button id="cycle-block-state-btn" class="btn btn-secondary btn-sm flex-1">🔄 Cycle State</button>
                    </div>
                </div>
                
                <!-- TILES LIST (Scrollable with extend buttons) -->
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Tiles</h4>
                    ${renderTileList(block.sorted_positions, {
                        entityType: 'block',
                        extendStartBtnId: 'extend-block-start-btn',
                        extendEndBtnId: 'extend-block-end-btn',
                        removeBtnClass: 'delete-block-tile-btn'
                    })}
                </div>
                
                <!-- EDITABLE PROPERTIES SECTION -->
                <div class="control-section mt-12 pt-12" style="border-top: 1px solid var(--border-color);">
                    <h4 class="text-sm text-muted mb-8">Properties</h4>
                    
                    ${renderTextInput('block-name-input', 'Block Name', block.name || '', { placeholder: 'Enter block name...' })}
                    
                    <div class="grid grid-cols-2 gap-8 mb-8">
                        <div class="form-group mb-0">
                            <label class="text-xs text-muted mb-4 block">Length (cm)</label>
                            <input type="number" id="block-length-input" class="property-input" value="${block.length_cm || 0}" min="0" step="1">
                        </div>
                        <div class="form-group mb-0">
                            <label class="text-xs text-muted mb-4 block">Min Radius (cm)</label>
                            <input type="number" id="block-radius-input" class="property-input" value="${block.min_radius_cm || ''}" min="0" step="1" placeholder="Not set">
                        </div>
                    </div>
                    
                    <div class="form-group mb-8">
                        <label class="text-xs text-muted mb-4 block">Max Speed (km/h)</label>
                        <input type="number" id="block-speed-input" class="property-input" value="${block.max_speed_kmh || ''}" min="0" step="1" placeholder="Not set">
                    </div>

                    <div class="flex gap-8">
                        <button id="save-block-btn" class="btn btn-primary btn-sm flex-1">💾 Save</button>
                        <button id="delete-block-btn" class="btn btn-danger btn-sm flex-1">🗑 Delete</button>
                    </div>
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar(`📦 Block Editor`, content);
        
        // Setup subscription before handlers
        this._setupSubscription(blockId, block);
        
        // Initialize event handlers after content is rendered
        setTimeout(() => this.initEditorHandlers(blockId), 0);
    }

    async initEditorHandlers(blockId) {
        const saveBtn = document.getElementById('save-block-btn');
        const deleteBtn = document.getElementById('delete-block-btn');
        const cycleBtn = document.getElementById('cycle-block-state-btn');
        const nameInput = document.getElementById('block-name-input');
        const lengthInput = document.getElementById('block-length-input');
        const radiusInput = document.getElementById('block-radius-input');
        const speedInput = document.getElementById('block-speed-input');

        // Cycle state button
        if (cycleBtn) {
            cycleBtn.onclick = async () => {
                try {
                    const result = await this.api.cycleBlockState(blockId);
                    await window.app.refreshNetwork();

                    // Update the status light and text directly without re-rendering
                    const newState = result.state || 'FREE';
                    const stateColor = newState === 'free' ? '#04d361' :
                                       newState === 'reserved' ? '#ffd93d' :
                                       newState === 'occupied' ? '#e83f5b' :
                                       newState === 'caution' ? '#ff9500' : '#888';

                    // Find and update the status light (first span in the state row)
                    const panel = document.querySelector(`.block-editor-panel[data-block-id="${blockId}"]`);
                    if (panel) {
                        const stateDisplay = panel.querySelector('#block-state-display');
                        if (stateDisplay) {
                            stateDisplay.textContent = newState.toUpperCase();
                            stateDisplay.style.color = stateColor;
                        }
                        // Update the light (sibling span before state display)
                        const stateRow = stateDisplay?.parentElement;
                        if (stateRow) {
                            const light = stateRow.querySelector('span:first-child');
                            if (light) {
                                light.style.background = stateColor;
                                light.style.boxShadow = `0 0 8px ${stateColor}`;
                            }
                        }
                    }
                } catch (err) {
                    console.error('Failed to cycle block state:', err);
                    window.showToast('Failed to cycle block state: ' + err.message, 'error', 5000);
                }
            };
        }

        // Save button
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {};

                if (nameInput) {
                    updates.name = nameInput.value.trim();  // Allow empty string
                }
                if (lengthInput) {
                    const val = parseFloat(lengthInput.value);
                    if (!isNaN(val) && val >= 0) {
                        updates.length_cm = val;
                    }
                }
                if (radiusInput) {
                    const val = radiusInput.value.trim();
                    if (val === '') {
                        updates.min_radius_cm = null;
                    } else {
                        const numVal = parseFloat(val);
                        if (!isNaN(numVal) && numVal >= 0) {
                            updates.min_radius_cm = numVal;
                        }
                    }
                }
                if (speedInput) {
                    const val = speedInput.value.trim();
                    if (val === '') {
                        updates.max_speed_kmh = null;
                    } else {
                        const numVal = parseFloat(val);
                        if (!isNaN(numVal) && numVal >= 0) {
                            updates.max_speed_kmh = numVal;
                        }
                    }
                }

                console.log('[BlockEditorHandler] Saving block updates:', blockId, updates);
                try {
                    const result = await this.api.updateBlock(blockId, updates);
                    console.log('[BlockEditorHandler] Block save result:', result);
                    await window.app.refreshNetwork();
                    // Also refresh the blocks cache
                    await this.refreshList();
                    // Re-open to show updated values
                    this.showEditor(blockId);
                    // Also refresh main properties panel if it shows this block
                    if (this.selectedElement?.block_section_id === blockId) {
                        await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                } catch (err) {
                    console.error('Failed to save block properties:', err);
                    window.showToast('Failed to save block properties: ' + err.message, 'error', 5000);
                }
            };
        }

        // Delete button
        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete block "${blockId}"? This will remove block assignment from all tracks.`)) {
                    return;
                }
                // Remember selected element before delete
                const selectedPos = this.selectedElement ? { x: this.selectedElement.x, y: this.selectedElement.y } : null;
                const wasShowingThisBlock = this.selectedElement?.block_section_id === blockId;
                try {
                    await this.api.deleteBlock(blockId);
                    await window.app.refreshNetwork();
                    this.editor.hideSecondarySidebar();
                    // Refresh main properties panel if it was showing this block
                    if (wasShowingThisBlock && selectedPos) {
                        await this.editor.selectElement(selectedPos);
                    }
                } catch (err) {
                    console.error('Failed to delete block:', err);
                    window.showToast('Failed to delete block: ' + err.message, 'error', 5000);
                }
            };
        }

        // Extend Start button
        const extendBlockStartBtn = document.getElementById('extend-block-start-btn');
        if (extendBlockStartBtn) {
            extendBlockStartBtn.onclick = () => {
                this.enterExtendMode(blockId, 'start');
            };
        }

        // Extend End button
        const extendBlockEndBtn = document.getElementById('extend-block-end-btn');
        if (extendBlockEndBtn) {
            extendBlockEndBtn.onclick = () => {
                this.enterExtendMode(blockId, 'end');
            };
        }

        // Tile delete buttons
        const deleteBlockTileBtns = document.querySelectorAll('.delete-block-tile-btn');
        deleteBlockTileBtns.forEach(btn => {
            const x = parseInt(btn.dataset.x);
            const y = parseInt(btn.dataset.y);
            btn.onclick = async () => {
                try {
                    await this.api.removeBlockTiles(blockId, [{x, y}]);
                    await window.app.refreshNetwork();
                    // Refresh block editor
                    this.showEditor(blockId);
                    // Refresh main properties panel if showing this tile
                    if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
                        await this.editor.selectElement({ x, y });
                    }
                } catch (err) {
                    console.error('Failed to remove block tile:', err);
                    window.showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
                }
            };
        });
    }

    // === Block Extend Mode ===

    async enterExtendMode(blockId, direction) {
        console.log(`[BlockEditorHandler] Auto-extending block: ${blockId} (${direction})`);
        
        try {
            // Get block data
            const block = await this.api.getBlock(blockId);
            if (!block || block.error) {
                throw new Error('Block not found');
            }

            // Get sorted positions
            const sortedPositions = block.sorted_positions || block.track_positions;

            if (!sortedPositions || sortedPositions.length === 0) {
                throw new Error('Block has no positions');
            }

            const tracks = this.editor.cachedNetwork?.tracks || [];
            const orientation = detectOrientation(sortedPositions, tracks);
            console.log(`[BlockEditorHandler] Detected block orientation: ${orientation}`);

            const nextTile = calculateNextTile(sortedPositions, orientation, direction, tracks);
            console.log(`[BlockEditorHandler] Auto-extending to tile (${nextTile.x}, ${nextTile.y}) with track bits ${nextTile.trackBits}`);

            // Extend the block (use force=false to let backend validate)
            await this.api.extendBlock(blockId, [[nextTile.x, nextTile.y]], false);
            await window.app.refreshNetwork();
            
            // Refresh block editor
            this.showEditor(blockId);
            
            // Refresh main properties panel if showing this tile
            if (this.selectedElement?.x === nextTile.x && this.selectedElement?.y === nextTile.y) {
                await this.editor.selectElement({ x: nextTile.x, y: nextTile.y });
            }
            
            window.showToast(`Block extended ${direction} to (${nextTile.x}, ${nextTile.y})`, 'success', 3000);
        } catch (err) {
            console.error('Failed to auto-extend block:', err);
            window.showToast(`Failed to extend block: ${err.message}`, 'error', 5000);
        }
    }

    exitExtendMode() {
        if (this.blockExtendMode?.active) {
            console.log('[BlockEditorHandler] Exiting block extend mode');
            this.blockExtendMode = { active: false };
            document.body.style.cursor = 'default';
            
            // Restore header
            const secondarySidebar = document.getElementById('secondary-sidebar-content');
            if (secondarySidebar) {
                const header = secondarySidebar.closest('.secondary-sidebar')?.querySelector('h3');
                if (header) {
                    header.textContent = '📦 Block Editor';
                    header.style.color = '';
                }
            }
        }
    }

    async handleExtendClick(x, y) {
        if (!this.blockExtendMode?.active) return false;
        
        const { blockId, direction } = this.blockExtendMode;
        
        try {
            // Validate the tile can be extended
            const canExtend = await this.api.canExtendBlock(blockId, x, y);
            
            if (!canExtend.can_extend) {
                // Check if it requires force flags
                if (canExtend.requires_force) {
                    window.showToast(`Tile has signal - use force mode to extend (not yet implemented)`, 'warning', 5000);
                } else if (canExtend.requires_force_overwrite) {
                    window.showToast(`Tile in another block - use force overwrite (not yet implemented)`, 'warning', 5000);
                } else {
                    window.showToast(`Cannot extend: ${canExtend.reason}`, 'error', 5000);
                }
                return true; // Handled, but failed
            }
            
            // Extend the block
            await this.api.extendBlock(blockId, [[x, y]]);
            await window.app.refreshNetwork();
            
            // Exit extend mode
            this.exitExtendMode();
            
            // Refresh block editor
            this.showEditor(blockId);
            
            // Refresh main properties panel if showing this tile
            if (this.selectedElement?.x === x && this.selectedElement?.y === y) {
                await this.editor.selectElement({ x, y });
            }
            
            window.showToast(`Tile (${x}, ${y}) added to block`, 'success', 5000);
            return true;
        } catch (err) {
            console.error('Failed to extend block:', err);
            window.showToast(`Failed to extend block: ${err.message}`, 'error', 5000);
            return true;
        }
    }

    // === Block Dialog Methods ===

    async editDialog(blockId) {
        try {
            const blockData = await this.api.getBlock(blockId);
            if (blockData.error) {
                window.showToast(blockData.error, 'error', 5000);
                return;
            }

            // Show block properties modal
            const result = await this.showPropertiesModal({
                blockId: blockId,
                blockData: blockData
            });

            if (!result) return; // User cancelled

            // Update block with new values
            await this.api.updateBlock(blockId, result);
            await this.refreshList();
        } catch (err) {
            console.error('[BlockEditorHandler] Failed to update block:', err);
            window.showToast('Failed to update block: ' + err.message, 'error', 5000);
        }
    }

    async delete(blockId) {
        if (!confirm(`Delete block "${blockId}"?\nTracks will be unassigned from this block.`)) return;

        try {
            await this.api.deleteBlock(blockId);
            if (this.selectedBlockId === blockId) {
                this.selectedBlockId = null;
            }
            await this.refreshList();
            await window.app.refreshNetwork();
        } catch (err) {
            console.error('[BlockEditorHandler] Failed to delete block:', err);
            window.showToast('Failed to delete block: ' + err.message, 'error', 5000);
        }
    }

    // === Block Modal Dialogs ===

    /**
     * Show a modal dialog for block creation or extension
     * @param {Object} options - Modal options
     * @param {string} options.mode - 'create' or 'extend'
     * @param {number} options.trackCount - Number of tracks in path
     * @param {string[]} options.existingBlocks - List of existing block IDs in path (for extend mode)
     * @returns {Promise<Object|null>} - User's choice or null if cancelled
     */
    showModal(options) {
        return new Promise((resolve) => {
            const { mode, trackCount, existingBlocks = [] } = options;

            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'block-modal-overlay';

            let modalContent = '';

            if (mode === 'create') {
                // Create new block form
                const defaultId = `BLK_${Date.now().toString(16).toUpperCase().slice(-6)}`;
                modalContent = `
                    <div class="block-modal">
                        <h3>Create New Block</h3>
                        <div class="modal-body">
                            <p class="modal-info">Creating block with <strong>${trackCount}</strong> tracks</p>
                            <div class="form-group">
                                <label for="modal-block-id">Block ID</label>
                                <input type="text" id="modal-block-id" value="${defaultId}" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-block-name">Block Name (optional)</label>
                                <input type="text" id="modal-block-name" placeholder="e.g., Main Line West" autocomplete="off">
                            </div>
                            <div class="form-group">
                                <label for="modal-max-speed">Max Speed km/h (optional)</label>
                                <input type="number" id="modal-max-speed" placeholder="e.g., 60" min="0" step="1">
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="btn btn-primary" id="modal-confirm">Create Block</button>
                            <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                        </div>
                    </div>
                `;
            } else if (mode === 'extend') {
                // Extend existing block
                if (existingBlocks.length === 0) {
                    // No existing blocks - shouldn't happen in extend mode
                    resolve(null);
                    return;
                } else if (existingBlocks.length === 1) {
                    // Single block to extend
                    const blockId = existingBlocks[0];
                    const block = this.cachedBlocks?.find(b => b.block_id === blockId);
                    modalContent = `
                        <div class="block-modal">
                            <h3>Extend Block</h3>
                            <div class="modal-body">
                                <p class="modal-info">Extending block <strong>${blockId}</strong></p>
                                ${block?.name ? `<p class="modal-subinfo">${block.name}</p>` : ''}
                                <p class="modal-detail">Adding <strong>${trackCount}</strong> tracks to this block.</p>
                                ${block ? `<p class="modal-detail">Current size: ${block.track_count || 0} tracks</p>` : ''}
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" data-block-id="${blockId}">Extend Block</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
                } else {
                    // Multiple blocks - user must choose
                    const blockOptions = existingBlocks.map(blockId => {
                        const block = this.cachedBlocks?.find(b => b.block_id === blockId);
                        return `
                            <div class="block-selection-item" data-block-id="${blockId}">
                                <input type="radio" name="extend-block" value="${blockId}" id="block-${blockId}">
                                <label for="block-${blockId}">
                                    <span class="block-id">${blockId}</span>
                                    ${block?.name ? `<span class="block-name">${block.name}</span>` : ''}
                                    ${block?.station_names ? `<span class="block-name" style="color: #eebb00; margin-left: 5px;">[${block.station_names.join(', ')}]</span>` : ''}
                                    <span class="block-track-count">${block?.track_count || 0} tracks</span>
                                </label>
                            </div>
                        `;
                    }).join('');

                    modalContent = `
                        <div class="block-modal">
                            <h3>Extend Block</h3>
                            <div class="modal-body">
                                <p class="modal-info">Path covers <strong>${existingBlocks.length}</strong> blocks</p>
                                <p class="modal-warning">Select which block to extend. Other block assignments will be overwritten.</p>
                                <div class="block-selection-list">
                                    ${blockOptions}
                                </div>
                            </div>
                            <div class="modal-footer">
                                <button class="btn btn-primary" id="modal-confirm" disabled>Extend Selected</button>
                                <button class="btn btn-secondary" id="modal-cancel">Cancel</button>
                            </div>
                        </div>
                    `;
                }
            }

            overlay.innerHTML = modalContent;
            document.body.appendChild(overlay);

            // Focus first input
            const firstInput = overlay.querySelector('input[type="text"], input[type="number"]');
            if (firstInput) {
                setTimeout(() => firstInput.focus(), 50);
            }

            // Handle radio button selection for multi-block extend
            const radioButtons = overlay.querySelectorAll('input[name="extend-block"]');
            const confirmBtn = overlay.querySelector('#modal-confirm');

            radioButtons.forEach(radio => {
                radio.addEventListener('change', () => {
                    confirmBtn.disabled = false;
                    confirmBtn.dataset.blockId = radio.value;
                });
            });

            // Handle confirm
            confirmBtn?.addEventListener('click', () => {
                if (mode === 'create') {
                    const blockId = overlay.querySelector('#modal-block-id')?.value?.trim();
                    const name = overlay.querySelector('#modal-block-name')?.value?.trim();
                    const maxSpeed = overlay.querySelector('#modal-max-speed')?.value;

                    if (!blockId) {
                        window.showToast('Block ID is required', 'error', 5000);
                        return;
                    }

                    overlay.remove();
                    resolve({
                        action: 'create',
                        blockId,
                        name: name || '',
                        maxSpeed: maxSpeed ? parseFloat(maxSpeed) : null
                    });
                } else if (mode === 'extend') {
                    const blockId = confirmBtn.dataset.blockId;
                    if (!blockId) {
                        window.showToast('Please select a block', 'error', 5000);
                        return;
                    }

                    overlay.remove();
                    resolve({
                        action: 'extend',
                        blockId
                    });
                }
            });

            // Handle cancel
            overlay.querySelector('#modal-cancel')?.addEventListener('click', () => {
                overlay.remove();
                resolve(null);
            });

            // Handle overlay click (close)
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.remove();
                    resolve(null);
                }
            });

            // Handle Escape key
            const handleEscape = (e) => {
                if (e.key === 'Escape') {
                    overlay.remove();
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            };
            document.addEventListener('keydown', handleEscape);

            // Handle Enter key for create mode
            if (mode === 'create') {
                const handleEnter = (e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                        confirmBtn?.click();
                    }
                };
                overlay.addEventListener('keydown', handleEnter);
            }
        });
    }

    /**
     * Show a modal dialog for editing block properties
     * @param {Object} options - Modal options
     * @param {string} options.blockId - The block ID to edit
     * @param {Object} options.blockData - The block data from API
     * @returns {Promise<Object|null>} Block updates or null if cancelled
     */
    showPropertiesModal(options) {
        return new Promise((resolve) => {
            const { blockId, blockData } = options;

            // Create modal overlay
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';

            const modal = document.createElement('div');
            modal.className = 'modal';
            modal.innerHTML = `
                <h3>Edit Block Properties</h3>
                
                <!-- Info Display Group -->
                <div class="info-display-group">
                    <div class="info-display-header">Block Information</div>
                    <div class="info-display-item">
                        <span class="info-display-label">Block ID</span>
                        <span class="info-display-value">${blockId}</span>
                    </div>
                    <div class="info-display-item">
                        <span class="info-display-label">Current State</span>
                        <span class="info-badge state-${blockData.state || 'free'}">
                            ${(blockData.state || 'free').toUpperCase()}
                        </span>
                    </div>
                    ${blockData.monitoring_signals && blockData.monitoring_signals.length > 0 ? `
                    <div class="info-display-item" style="flex-direction: column; align-items: flex-start; gap: 6px;">
                        <span class="info-display-label">Monitoring Signals</span>
                        <div class="signal-list" id="monitoring-signals-list" style="width: 100%; max-height: 80px;">
                            ${this.editor.renderSignalList ? this.editor.renderSignalList(blockData.monitoring_signals) : ''}
                        </div>
                    </div>
                    ` : ''}
                </div>

                <!-- User Input Fields (Horizontal Layout) -->
                <div class="form-row-horizontal">
                    <label for="modal-block-name">Name</label>
                    <input type="text" id="modal-block-name" value="${blockData.name || ''}"
                           placeholder="Block name">
                </div>
                <div class="form-row-horizontal">
                    <label for="modal-block-length">Length (cm)</label>
                    <input type="number" id="modal-block-length" value="${blockData.length_cm || 0}"
                           min="0" step="0.1" placeholder="0.0">
                </div>
                <div class="form-row-horizontal">
                    <label for="modal-block-speed">Max Speed (km/h)</label>
                    <input type="number" id="modal-block-speed" value="${blockData.max_speed_kmh || ''}"
                           min="0" step="1" placeholder="Optional">
                </div>

                <div class="button-group">
                    <button class="btn btn-primary" id="save-block-btn">Save</button>
                    <button class="btn btn-secondary" id="cancel-block-btn">Cancel</button>
                </div>
            `;

            overlay.appendChild(modal);
            document.body.appendChild(overlay);

            const nameInput = modal.querySelector('#modal-block-name');
            const lengthInput = modal.querySelector('#modal-block-length');
            const speedInput = modal.querySelector('#modal-block-speed');
            const saveBtn = modal.querySelector('#save-block-btn');
            const cancelBtn = modal.querySelector('#cancel-block-btn');

            // Focus first input
            setTimeout(() => nameInput.focus(), 50);

            // Handle save
            saveBtn.addEventListener('click', () => {
                const name = nameInput.value.trim();
                const lengthCm = parseFloat(lengthInput.value) || 0;
                const maxSpeedKmh = speedInput.value.trim() ? parseFloat(speedInput.value) : null;

                if (lengthCm < 0) {
                    window.showToast('Length cannot be negative', 'error', 5000);
                    return;
                }

                if (maxSpeedKmh !== null && maxSpeedKmh <= 0) {
                    window.showToast('Max speed must be positive', 'error', 5000);
                    return;
                }

                document.body.removeChild(overlay);
                resolve({
                    name,
                    length_cm: lengthCm,
                    max_speed_kmh: maxSpeedKmh
                });
            });

            // Handle cancel
            cancelBtn.addEventListener('click', () => {
                document.body.removeChild(overlay);
                resolve(null);
            });

            // Handle Enter key
            const handleEnter = (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    saveBtn.click();
                }
            };
            modal.addEventListener('keydown', handleEnter);

            // Handle Escape key
            const handleEscape = (e) => {
                if (e.key === 'Escape') {
                    document.body.removeChild(overlay);
                    document.removeEventListener('keydown', handleEscape);
                    resolve(null);
                }
            };
            document.addEventListener('keydown', handleEscape);

            // Handle overlay click
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    document.body.removeChild(overlay);
                    resolve(null);
                }
            });
        });
    }
    
    // ═══════════════════════════════════════════════════════════════
    // SUBSCRIPTION METHODS (Phase 4)
    // ═══════════════════════════════════════════════════════════════
    
    _setupSubscription(blockId, blockData) {
        this._cleanup();
        this._blockId = blockId;
        this._localState = JSON.parse(JSON.stringify(blockData));
        this._isDirty = false;
        
        this._unsubscribe = window.stateManager.subscribe(
            'blocks',
            blockId,
            (newBlock, oldBlock) => this._onBlockUpdate(newBlock, oldBlock),
            { immediate: false }
        );
    }
    
    _onBlockUpdate(newBlock, oldBlock) {
        if (!newBlock) {
            window.showToast('Block was deleted', 'warning');
            this.editor.hideSecondarySidebar();
            return;
        }
        
        if (this._isUserInteracting) return;  // Don't interrupt tile picker drag
        
        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }
        
        if (this._isDirty) return;
        
        this._localState = JSON.parse(JSON.stringify(newBlock));
    }
    
    _cleanup() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
        this._blockId = null;
        this._localState = null;
        this._isDirty = false;
        this._isUserInteracting = false;
    }
}
