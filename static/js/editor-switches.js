// ============================================================================
// editor-switches.js - Switch Editor Handler
// ============================================================================
// Requires: constants.js, helpers.js, api.js
// Handles all switch-related editor functionality
// ============================================================================

class SwitchEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._switchId = null;
        this._unsubscribe = null;
        this._localState = null;
        this._isDirty = false;
    }

    // Convenience getters for common editor properties
    get api() { return this.editor.api; }
    get selectedElement() { return this.editor.selectedElement; }

    // === Switch Editor Panel ===

    async showEditor(switchId) {
        // Track this editor for auto-refresh
        this.editor._currentEditorType = 'switch';
        this.editor._currentEditorId = switchId;

        // Fetch switch data
        let switchData;
        try {
            switchData = await this.api.getSwitch(switchId);
            if (switchData.error) {
                window.showToast('Switch not found', 'error', 5000);
                return;
            }
        } catch (err) {
            console.error('[SwitchEditorHandler] Failed to get switch:', err);
            window.showToast('Failed to load switch', 'error', 5000);
            return;
        }

        // Get current track state for switch_state
        let track;
        try {
            track = await this.api.getTrack(switchData.x, switchData.y);
        } catch (err) {
            console.error('[SwitchEditorHandler] Failed to get track:', err);
        }

        const currentState = track?.switch_state ?? 0;
        const stateText = currentState === 0 ? 'MAIN' : 'DIVERGE';
        const stateColor = currentState === 0 ? '#04d361' : '#ffd93d';
        const isLocked = switchData.locked_by_path !== null;
        const lockColor = isLocked ? '#e83f5b' : '#04d361';
        const lockText = isLocked ? `Locked by ${switchData.locked_by_path}` : 'Unlocked';

        const content = `
            <!-- INFO SECTION (Read-only) -->
            <div class="info-box-track">
                <!-- Large State Display -->
                <div style="text-align: center; margin-bottom: 12px;">
                    <div style="display: flex; align-items: center; justify-content: center; gap: 10px;">
                        <span class="train-status-dot" style="background: ${stateColor}; box-shadow: 0 0 8px ${stateColor};"></span>
                        <span style="font-size: 24px; font-weight: bold; color: ${stateColor};">${stateText}</span>
                    </div>
                    <div class="text-muted text-xs" style="margin-top: 4px;">State ${currentState}</div>
                </div>

                <!-- Info Grid -->
                <div class="property-grid" style="gap: 8px;">
                    <div class="property-item">
                        <label class="property-label">Switch ID</label>
                        <div class="value-display text-info">${switchData.switch_id}</div>
                    </div>
                    <div class="property-item">
                        <label class="property-label">Position</label>
                        <div class="value-display text-muted">(${switchData.x}, ${switchData.y})</div>
                    </div>
                    <div class="property-item">
                        <label class="property-label">Type</label>
                        <div class="value-display">${switchData.switch_type || 'standard'}</div>
                    </div>
                    <div class="property-item">
                        <label class="property-label">Lock Status</label>
                        <div class="value-display" style="color: ${lockColor};">${lockText}</div>
                    </div>
                </div>
            </div>

            <!-- CONTROL BUTTONS -->
            <div class="control-section">
                <div class="control-section-header">Controls</div>
                <div class="flex gap-8">
                    <button id="toggle-switch-btn" class="btn ${isLocked ? 'btn-secondary' : 'btn-primary'} flex-1" ${isLocked ? 'disabled' : ''}>
                        Toggle State
                    </button>
                </div>
                ${isLocked ? `<div class="text-warning text-xs" style="margin-top: 8px;">Switch is locked and cannot be toggled</div>` : ''}
            </div>

            <!-- LENGTH PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Path Lengths (cm)</div>
                <div class="property-grid" style="gap: 12px;">
                    <div class="property-item">
                        <label class="property-label">MAIN Path Length</label>
                        <input type="number" id="switch-main-length" class="property-input" value="${switchData.main_length_cm || 0}" min="0" step="0.1" placeholder="Length in cm">
                    </div>
                    <div class="property-item">
                        <label class="property-label">DIVERGE Path Length</label>
                        <input type="number" id="switch-diverge-length" class="property-input" value="${switchData.diverge_length_cm || 0}" min="0" step="0.1" placeholder="Length in cm">
                    </div>
                </div>
            </div>

            <!-- RADIUS PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Path Radii (cm)</div>
                <div class="property-grid" style="gap: 12px;">
                    <div class="property-item">
                        <label class="property-label">MAIN Path Radius</label>
                        <input type="number" id="switch-main-radius" class="property-input" value="${switchData.main_radius_cm || ''}" min="0" step="0.1" placeholder="None (straight)">
                    </div>
                    <div class="property-item">
                        <label class="property-label">DIVERGE Path Radius</label>
                        <input type="number" id="switch-diverge-radius" class="property-input" value="${switchData.diverge_radius_cm || ''}" min="0" step="0.1" placeholder="None (straight)">
                    </div>
                </div>
            </div>

            <!-- OTHER PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Properties</div>
                <div class="property-grid" style="gap: 12px;">
                    <div class="property-item">
                        <label class="property-label">Name</label>
                        <input type="text" id="switch-name" class="property-input" value="${switchData.name || ''}" placeholder="Switch name...">
                    </div>
                    <div class="property-item">
                        <label class="property-label">Notes</label>
                        <input type="text" id="switch-notes" class="property-input" value="${switchData.notes || ''}" placeholder="Notes...">
                    </div>
                </div>

                <div style="margin-top: 16px; display: flex; gap: 8px;">
                    <button id="save-switch-btn" class="btn btn-primary flex-1">Save Properties</button>
                    <button id="delete-switch-btn" class="btn btn-danger">Delete</button>
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('Switch Editor', content);

        // Bind event handlers
        this.bindEventHandlers(switchData);
    }

    bindEventHandlers(switchData) {
        // Toggle switch state
        const toggleBtn = document.getElementById('toggle-switch-btn');
        if (toggleBtn && !toggleBtn.disabled) {
            toggleBtn.onclick = async () => {
                try {
                    await this.api.toggleSwitchState(switchData.x, switchData.y);
                    window.showToast('Switch state toggled', 'success', 2000);
                    await window.app.refreshNetwork();
                    // Refresh editor
                    await this.showEditor(switchData.switch_id);
                } catch (err) {
                    console.error('[SwitchEditorHandler] Failed to toggle switch:', err);
                    window.showToast('Failed to toggle switch: ' + err.message, 'error', 5000);
                }
            };
        }

        // Save properties
        const saveBtn = document.getElementById('save-switch-btn');
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {
                    name: document.getElementById('switch-name')?.value?.trim() || '',
                    notes: document.getElementById('switch-notes')?.value?.trim() || '',
                    main_length_cm: parseFloat(document.getElementById('switch-main-length')?.value) || 0,
                    diverge_length_cm: parseFloat(document.getElementById('switch-diverge-length')?.value) || 0,
                    main_radius_cm: parseFloat(document.getElementById('switch-main-radius')?.value) || null,
                    diverge_radius_cm: parseFloat(document.getElementById('switch-diverge-radius')?.value) || null
                };

                try {
                    await this.api.updateSwitch(switchData.switch_id, updates);
                    window.showToast('Switch properties saved', 'success', 2000);
                    await window.app.refreshNetwork();
                } catch (err) {
                    console.error('[SwitchEditorHandler] Failed to save switch:', err);
                    window.showToast('Failed to save switch: ' + err.message, 'error', 5000);
                }
            };
        }

        // Delete switch
        const deleteBtn = document.getElementById('delete-switch-btn');
        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete switch ${switchData.switch_id}? This will remove the switch configuration but not the track.`)) {
                    return;
                }

                try {
                    await this.api.deleteSwitch(switchData.switch_id);
                    window.showToast('Switch deleted', 'success', 2000);
                    await window.app.refreshNetwork();
                    this.editor.hideSecondarySidebar();
                } catch (err) {
                    console.error('[SwitchEditorHandler] Failed to delete switch:', err);
                    window.showToast('Failed to delete switch: ' + err.message, 'error', 5000);
                }
            };
        }
        
        // ═══════════════════════════════════════════════════════════════
        // SUBSCRIPTION SETUP (Phase 4)
        // ═══════════════════════════════════════════════════════════════
        
        this._cleanup();
        this._switchId = switchData.switch_id;
        this._localState = JSON.parse(JSON.stringify(switchData));
        this._isDirty = false;
        
        this._unsubscribe = window.stateManager.subscribe(
            'switches',
            switchData.switch_id,
            (newSwitch, oldSwitch) => this._onSwitchUpdate(newSwitch, oldSwitch),
            { immediate: false }
        );
    }
    
    _onSwitchUpdate(newSwitch, oldSwitch) {
        if (!newSwitch) {
            window.showToast('Switch was deleted', 'warning');
            this.editor.hideSecondarySidebar();
            return;
        }
        
        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }
        
        if (this._isDirty) return;
        
        this._localState = JSON.parse(JSON.stringify(newSwitch));
        this._updateSwitchDisplay(newSwitch);
    }
    
    async _updateSwitchDisplay(switchData) {
        const track = await this.api.getTrack(switchData.x, switchData.y);
        const currentState = track?.switch_state ?? 0;
        const stateText = currentState === 0 ? 'MAIN' : 'DIVERGE';
        const stateColor = currentState === 0 ? '#04d361' : '#ffd93d';
        const isLocked = switchData.locked_by_path !== null;
        
        const stateDot = document.querySelector('.train-status-dot');
        if (stateDot) stateDot.style.background = stateColor;
        
        const stateSpan = stateDot?.nextElementSibling;
        if (stateSpan) {
            stateSpan.style.color = stateColor;
            stateSpan.textContent = stateText;
        }
        
        const lockBadge = document.querySelector('.badge-danger, .badge-success');
        if (lockBadge) {
            lockBadge.className = `badge ${isLocked ? 'badge-danger' : 'badge-success'} text-xxs`;
            lockBadge.textContent = isLocked ? 'LOCKED' : 'UNLOCKED';
        }
    }
    
    _cleanup() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
        this._switchId = null;
        this._localState = null;
        this._isDirty = false;
    }

    // === Switch List Rendering ===

    renderSwitchList(switches) {
        if (!switches || switches.length === 0) {
            return '<div class="text-muted text-sm" style="padding: 8px;">No switches</div>';
        }

        return switches.map(sw => {
            const stateText = sw.switch_state === 0 ? 'MAIN' : 'DIVERGE';
            const stateColor = sw.switch_state === 0 ? '#04d361' : '#ffd93d';
            const isLocked = sw.locked_by_path !== null;

            return `
                <div class="train-summary-row" data-switch-id="${sw.switch_id}">
                    <div class="flex items-center gap-8">
                        <span class="train-status-dot" style="background: ${stateColor};"></span>
                        <div>
                            <div class="text-sm font-bold text-track">${sw.switch_id}</div>
                            <div class="text-xs text-muted">(${sw.x}, ${sw.y})</div>
                        </div>
                    </div>
                    <div class="flex items-center gap-8">
                        ${isLocked ? '<span class="badge badge-danger text-xxs">LOCKED</span>' : ''}
                        <span class="text-xs" style="color: ${stateColor};">${stateText}</span>
                        <button class="btn btn-secondary btn-sm edit-switch-btn" data-switch-id="${sw.switch_id}">Edit</button>
                    </div>
                </div>
            `;
        }).join('');
    }

    initSwitchListHandlers() {
        const editBtns = document.querySelectorAll('.edit-switch-btn');
        editBtns.forEach(btn => {
            btn.onclick = (e) => {
                e.stopPropagation();
                const switchId = btn.dataset.switchId;
                if (switchId) {
                    this.showEditor(switchId);
                }
            };
        });
    }
}
