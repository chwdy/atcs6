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

    getSwitchStateClass(switchState) {
        return switchState === 0 ? 'switch-state-main' : 'switch-state-diverge';
    }

    _replaceSwitchStateClass(element, nextClass) {
        if (!element) return;
        element.classList.remove('switch-state-main', 'switch-state-diverge');
        element.classList.add(nextClass);
    }

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
        const stateClass = this.getSwitchStateClass(currentState);
        const isLocked = switchData.locked_by_path !== null;
        const lockText = isLocked ? `Locked by ${switchData.locked_by_path}` : 'Unlocked';

        const content = `
            <!-- INFO SECTION (Read-only) -->
            <div class="info-box-track info-display-large">
                <!-- Large State Display -->
                <div class="switch-state-header">
                    <div class="switch-state-row">
                        <span class="train-status-dot switch-state-dot ${stateClass}"></span>
                        <span class="switch-state-title ${stateClass}">${stateText}</span>
                    </div>
                    <div class="info-meta mt-4">State ${currentState}</div>
                </div>

                <!-- Info Grid -->
                <div class="property-grid signal-grid-tight info-display-small">
                    <div class="property-item">
                        <div class="info-label">Switch ID</div>
                        <div class="info-value text-info">${switchData.switch_id}</div>
                    </div>
                    <div class="property-item">
                        <div class="info-label">Position</div>
                        <div class="info-value text-muted">(${switchData.x}, ${switchData.y})</div>
                    </div>
                    <div class="property-item">
                        <div class="info-label">Type</div>
                        <div class="info-value">${switchData.switch_type || 'standard'}</div>
                    </div>
                    <div class="property-item">
                        <div class="info-label">Lock Status</div>
                        <div class="info-value ${isLocked ? 'text-danger' : 'text-success'}">${lockText}</div>
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
                ${isLocked ? '<div class="text-warning text-xs mt-8">Switch is locked and cannot be toggled</div>' : ''}
            </div>

            <!-- LENGTH PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Path Lengths (cm)</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-main-length">MAIN Path Length</label>
                    <input type="number" id="switch-main-length" class="input-small-field input-small-field-compact" value="${switchData.main_length_cm || 0}" min="0" step="0.1" placeholder="Length in cm">
                </div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-diverge-length">DIVERGE Path Length</label>
                    <input type="number" id="switch-diverge-length" class="input-small-field input-small-field-compact" value="${switchData.diverge_length_cm || 0}" min="0" step="0.1" placeholder="Length in cm">
                </div>
            </div>

            <!-- RADIUS PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Path Radii (cm)</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-main-radius">MAIN Path Radius</label>
                    <input type="number" id="switch-main-radius" class="input-small-field input-small-field-compact" value="${switchData.main_radius_cm || ''}" min="0" step="0.1" placeholder="None (straight)">
                </div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="switch-diverge-radius">DIVERGE Path Radius</label>
                    <input type="number" id="switch-diverge-radius" class="input-small-field input-small-field-compact" value="${switchData.diverge_radius_cm || ''}" min="0" step="0.1" placeholder="None (straight)">
                </div>
            </div>

            <!-- OTHER PROPERTIES SECTION -->
            <div class="control-section">
                <div class="control-section-header">Properties</div>
                <div class="input-large-container mb-8">
                    <label class="input-large-label">Switch Name</label>
                    <input type="text" id="switch-name" class="input-large-field" value="${switchData.name || ''}" placeholder="Switch name...">
                </div>
                <div class="property-item mb-8">
                    <label class="property-label">Notes</label>
                    <input type="text" id="switch-notes" class="property-input" value="${switchData.notes || ''}" placeholder="Notes...">
                </div>

                <div class="switch-action-row mt-16">
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
                    // Get current track state to determine toggle direction
                    const track = await this.api.getTrack(switchData.x, switchData.y);
                    const currentState = track?.switch_state ?? 0;
                    const newState = currentState === 0 ? 1 : 0;

                    await this.api.setSwitchState(switchData.switch_id, newState);
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
                const confirmed = await showConfirmModal({
                    title: 'Delete Switch',
                    message: `Delete switch "${switchData.switch_id}"?`,
                    detail: 'The switch configuration will be removed, but track geometry will remain.',
                    confirmText: 'Delete',
                    confirmVariant: 'danger'
                });
                if (!confirmed) {
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
        const stateClass = this.getSwitchStateClass(currentState);
        const isLocked = switchData.locked_by_path !== null;
        
        const stateDot = document.querySelector('.train-status-dot');
        this._replaceSwitchStateClass(stateDot, stateClass);
        
        const stateSpan = stateDot?.nextElementSibling;
        if (stateSpan) {
            this._replaceSwitchStateClass(stateSpan, stateClass);
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
            return '<div class="text-muted text-sm p-8">No switches</div>';
        }

        return switches.map(sw => {
            const stateText = sw.switch_state === 0 ? 'MAIN' : 'DIVERGE';
            const stateClass = this.getSwitchStateClass(sw.switch_state);
            const isLocked = sw.locked_by_path !== null;

            return `
                <div class="train-summary-row" data-switch-id="${sw.switch_id}">
                    <div class="flex items-center gap-8">
                        <span class="train-status-dot switch-state-dot ${stateClass}"></span>
                        <div>
                            <div class="text-sm font-bold text-track">${sw.switch_id}</div>
                            <div class="text-xs text-muted">(${sw.x}, ${sw.y})</div>
                        </div>
                    </div>
                    <div class="flex items-center gap-8">
                        ${isLocked ? '<span class="badge badge-danger text-xxs">LOCKED</span>' : ''}
                        <span class="text-xs switch-state-inline ${stateClass}">${stateText}</span>
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
