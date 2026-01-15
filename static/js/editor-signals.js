class SignalEditorHandler {
    constructor(editor) {
        this.editor = editor;
    }

    get api() { return this.editor.api; }
    get cachedBlocks() { return this.editor.cachedBlocks; }
    set cachedBlocks(value) { this.editor.cachedBlocks = value; }
    get selectedElement() { return this.editor.selectedElement; }

    async showSignalsEditorForTile(track) {
        const signals = track.signals || [];

        if (signals.length === 0) {
            this.editor.hideSecondarySidebar();
            return;
        }

        const firstSignalId = signals[0].signal_id || signals[0].id;
        this.editor._currentEditorType = 'signal';
        this.editor._currentEditorId = firstSignalId;

        let signalsContent = '';
        const signalDetails = [];

        for (let i = 0; i < signals.length; i++) {
            const signalId = signals[i].signal_id || signals[i].id;
            const signal = await this.api.getSignal(signalId);
            if (!signal || signal.error) continue;

            signalDetails.push({ signal, index: i });
            const state = (signal.state || signal.signal_state || 'green').toUpperCase();
            const stateColor = state === 'GREEN' ? '#04d361' : (state === 'YELLOW' ? '#ffd93d' : '#e83f5b');

            signalsContent += `
                <div class="info-box-track" style="margin-bottom: 16px; border-left: 4px solid ${stateColor};">
                    <!-- Header with signal number -->
                    ${signals.length > 1 ? `<div class="text-muted text-xs" style="margin-bottom: 8px;">Signal ${i + 1} of ${signals.length}</div>` : ''}

                    <!-- Large State Display (Left-aligned) -->
                    <div style="margin-bottom: 12px;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <span class="train-status-dot" style="background: ${stateColor}; box-shadow: 0 0 8px ${stateColor};"></span>
                            <span style="font-size: 24px; font-weight: bold; color: ${stateColor};">${state}</span>
                        </div>
                    </div>

                    <!-- Info Grid -->
                    <div class="property-grid" style="gap: 8px; margin-bottom: 16px;">
                        <div class="property-item">
                            <label class="property-label">Signal ID</label>
                            <div class="value-display text-info">${signal.signal_id}</div>
                        </div>
                        <div class="property-item">
                            <label class="property-label">Direction</label>
                            <div class="value-display" style="color: ${signal.detection_direction ? '#fff' : '#f55'};">${signal.detection_direction || '???'}</div>
                        </div>
                    </div>

                    <!-- Editable Fields -->
                    <div class="property-grid" style="gap: 12px;">
                        <div class="property-item">
                            <label class="property-label">Signal Name</label>
                            <input type="text" class="signal-name-input property-input" data-signal-id="${signal.signal_id}" value="${signal.name || ''}" placeholder="Enter signal name...">
                        </div>
                        <div class="property-item">
                            <label class="property-label">Block to Monitor</label>
                            <select class="signal-block-select property-input" data-signal-id="${signal.signal_id}" style="border-color: ${signal.monitored_block_id ? 'var(--border-color)' : '#ff9500'};">
                                <option value="">-- None (Manual Control) --</option>
                                <option value="${signal.monitored_block_id || ''}" selected>${signal.monitored_block_id || 'None'} (Loading...)</option>
                            </select>
                            <div class="block-select-hint text-muted text-xs" data-signal-id="${signal.signal_id}" style="margin-top: 4px;">
                                Signal shows RED when monitored block is occupied
                            </div>
                        </div>
                    </div>

                    <div style="margin-top: 16px;">
                        <button class="save-signal-btn btn btn-primary btn-full text-base" data-signal-id="${signal.signal_id}">
                            Save Signal Properties
                        </button>
                    </div>
                </div>
            `;
        }

        const title = signals.length > 1 ? `Signals (${signals.length})` : 'Signal Editor';
        this.editor.showSecondarySidebar(title, signalsContent);

        for (const { signal } of signalDetails) {
            try {
                const result = await this.api.getReachableBlocks(signal.signal_id);
                const reachableBlockIds = result.reachable_blocks || [];

                const reachableBlocks = [];
                for (const blockId of reachableBlockIds) {
                    let block = (this.cachedBlocks || []).find(b => b.block_id === blockId);
                    if (!block) {
                        try {
                            block = await this.api.getBlock(blockId);
                            if (block && !block.error) {
                                this.cachedBlocks = this.cachedBlocks || [];
                                this.cachedBlocks.push(block);
                            }
                        } catch (err) {
                            block = { block_id: blockId, name: null };
                        }
                    }
                    if (block) reachableBlocks.push(block);
                }

                const blockSelect = document.querySelector(`.signal-block-select[data-signal-id="${signal.signal_id}"]`);
                if (blockSelect) {
                    const currentValue = signal.monitored_block_id;
                    blockSelect.innerHTML = `
                        <option value="">-- None (Manual Control) --</option>
                        ${reachableBlocks.map(b => `
                            <option value="${b.block_id}" ${currentValue === b.block_id ? 'selected' : ''}>
                                ${b.block_id}${b.name ? ` (${b.name})` : ''}${b.station_names ? ` [${b.station_names.join(', ')}]` : ''}
                            </option>
                        `).join('')}
                    `;

                    const hintDiv = document.querySelector(`.block-select-hint[data-signal-id="${signal.signal_id}"]`);
                    if (hintDiv) {
                        if (reachableBlocks.length === 0) {
                            hintDiv.innerHTML = `<span style="color: #ff6b6b;">No reachable blocks found.</span>`;
                        } else {
                            hintDiv.innerHTML = `Signal shows RED when monitored block is occupied<br><span style="color: #888;">${reachableBlocks.length} reachable block(s)</span>`;
                        }
                    }
                }
            } catch (err) {
                console.warn('Failed to fetch reachable blocks for', signal.signal_id, err);
            }
        }

        const saveBtns = document.querySelectorAll('.save-signal-btn');
        saveBtns.forEach(btn => {
            btn.onclick = async () => {
                const signalId = btn.dataset.signalId;
                const nameInput = document.querySelector(`.signal-name-input[data-signal-id="${signalId}"]`);
                const blockSelect = document.querySelector(`.signal-block-select[data-signal-id="${signalId}"]`);

                const updates = {};
                if (nameInput) updates.name = nameInput.value.trim() || null;
                if (blockSelect) updates.monitored_block_id = blockSelect.value || null;

                try {
                    await this.api.updateSignal(signalId, updates);
                    window.showToast('Signal updated successfully', 'success', 5000);
                    await window.app.refreshNetwork();

                    if (this.selectedElement) {
                        await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                } catch (err) {
                    console.error('Failed to save signal properties:', err);
                    window.showToast('Failed to save signal properties: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    initSignalEditor(track) {
        const editSignalBtns = document.querySelectorAll('.edit-signal-btn');
        editSignalBtns.forEach(btn => {
            btn.onclick = () => {
                this.showSignalsEditorForTile(track);
            };
        });
    }

    async showSignalEditor(signalId) {
        this.editor._currentEditorType = 'signal';
        this.editor._currentEditorId = signalId;

        const signal = await this.api.getSignal(signalId);
        if (!signal || signal.error) {
            window.showToast('Signal not found', 'error', 5000);
            return;
        }

        const state = (signal.state || signal.signal_state || 'green').toLowerCase();
        const stateColor = state === 'green' ? '#04d361' : (state === 'yellow' ? '#ffd93d' : '#e83f5b');
        const stateText = state.toUpperCase();

        const content = `
            <!-- INFO SECTION (Read-only, styled like Train Control) -->
            <div class="info-box-track">
                <!-- Large State Display (Left-aligned) -->
                <div style="margin-bottom: 12px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span class="train-status-dot" style="background: ${stateColor}; box-shadow: 0 0 8px ${stateColor};"></span>
                        <span style="font-size: 24px; font-weight: bold; color: ${stateColor};">${stateText}</span>
                    </div>
                </div>

                <!-- Info Grid -->
                <div class="property-grid" style="gap: 8px;">
                    <div class="property-item">
                        <label class="property-label">Signal ID</label>
                        <div class="value-display text-info">${signal.signal_id}</div>
                    </div>
                    <div class="property-item">
                        <label class="property-label">Direction</label>
                        <div class="value-display" style="color: ${signal.detection_direction ? '#fff' : '#f55'};">${signal.detection_direction || '???'}</div>
                    </div>
                    <div class="property-item">
                        <label class="property-label">Position</label>
                        <div class="value-display text-muted">(${signal.x}, ${signal.y})</div>
                    </div>
                    <div class="property-item">
                        <label class="property-label">Monitored Block</label>
                        <div class="value-display text-track">${signal.monitored_block_id || 'None'}</div>
                    </div>
                </div>
            </div>

            <!-- EDITABLE FIELDS SECTION -->
            <div class="control-section">
                <div class="control-section-header">Properties</div>

                <div class="property-grid" style="gap: 12px;">
                    <div class="property-item">
                        <label class="property-label">Signal Name</label>
                        <input type="text" id="signal-name-input" class="property-input" value="${signal.name || ''}" placeholder="Enter signal name...">
                    </div>
                    <div class="property-item">
                        <label class="property-label">Block to Monitor</label>
                        <select id="signal-block-select" class="property-input" style="border-color: ${signal.monitored_block_id ? 'var(--border-color)' : '#ff9500'};">
                            <option value="">-- None (Manual Control) --</option>
                            <option value="${signal.monitored_block_id || ''}" selected>${signal.monitored_block_id || 'None'} (Loading...)</option>
                        </select>
                        <div id="block-select-hint" class="text-muted text-xs" style="margin-top: 4px;">
                            Signal shows RED when monitored block is occupied
                        </div>
                    </div>
                </div>

                <div style="margin-top: 16px;">
                    <button id="save-signal-btn" class="btn btn-primary btn-full text-base">
                        Save Signal Properties
                    </button>
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('Signal Editor', content);

        try {
            const result = await this.api.getReachableBlocks(signalId);
            const reachableBlockIds = result.reachable_blocks || [];

            const reachableBlocks = [];
            for (const blockId of reachableBlockIds) {
                let block = (this.cachedBlocks || []).find(b => b.block_id === blockId);
                if (!block) {
                    try {
                        block = await this.api.getBlock(blockId);
                        if (block && !block.error) {
                            this.cachedBlocks = this.cachedBlocks || [];
                            this.cachedBlocks.push(block);
                        }
                    } catch (err) {
                        block = { block_id: blockId, name: null };
                    }
                }
                if (block) reachableBlocks.push(block);
            }

            const blockSelect = document.getElementById('signal-block-select');
            if (blockSelect) {
                const currentValue = signal.monitored_block_id;
                blockSelect.innerHTML = `
                    <option value="">-- None (Manual Control) --</option>
                    ${reachableBlocks.map(b => `
                        <option value="${b.block_id}" ${currentValue === b.block_id ? 'selected' : ''}>
                            ${b.block_id}${b.name ? ` (${b.name})` : ''}${b.station_names ? ` [${b.station_names.join(', ')}]` : ''}
                        </option>
                    `).join('')}
                `;

                const hintDiv = document.getElementById('block-select-hint');
                if (hintDiv) {
                     if (reachableBlocks.length === 0) {
                        hintDiv.innerHTML = `<span style="color: #ff6b6b;">No reachable blocks found. Check track connectivity.</span>`;
                     } else {
                        hintDiv.innerHTML = `Signal shows RED when monitored block is occupied<br><span style="color: #888;">${reachableBlocks.length} reachable block(s)</span>`;
                     }
                }
            }
        } catch (err) {
            console.warn('Failed to fetch reachable blocks:', err);
        }

        const saveBtn = document.getElementById('save-signal-btn');
        if (saveBtn) {
            saveBtn.onclick = async () => {
                const nameInput = document.getElementById('signal-name-input');
                const blockSelect = document.getElementById('signal-block-select');

                const updates = {};
                if (nameInput) updates.name = nameInput.value.trim() || null;
                if (blockSelect) updates.monitored_block_id = blockSelect.value || null;

                try {
                    await this.api.updateSignal(signalId, updates);
                    window.showToast('Signal updated successfully', 'success', 'error', 5000);
                    await window.app.refreshNetwork();

                    if (this.selectedElement) {
                        await this.editor.selectElement({ x: this.selectedElement.x, y: this.selectedElement.y });
                    }
                } catch (err) {
                    console.error('Failed to save signal properties:', err);
                    window.showToast('Failed to save signal properties: ' + err.message, 'error', 5000);
                }
            };
        }
    }

    renderSignalList(signals) {
        if (!signals || signals.length === 0) {
            return '<div class="signal-item" style="color: #888; font-style: italic;">No signals</div>';
        }

        return signals.map(signal => `
            <div class="signal-item">
                <span style="color: #00ff88; font-weight: bold;">${signal.signal_id || 'Unknown'}</span>
                <span style="color: #888;"> → ${signal.detection_direction || 'N/A'}</span>
                <span class="signal-state-badge state-${signal.state || 'red'}">${(signal.state || 'red').toUpperCase()}</span>
            </div>
        `).join('');
    }

    async invalidateSignalsInPath(signalsInPath) {
        if (!signalsInPath || signalsInPath.length === 0) return;

        for (const signal of signalsInPath) {
            try {
                const signalData = await this.api.getSignalAt(signal.x, signal.y);
                if (signalData && signalData.signal_id) {
                    await this.api.updateSignal(signalData.signal_id, {
                        monitored_block_id: null
                    });
                }
            } catch (err) {
                console.warn(`[SignalEditorHandler] Failed to invalidate signal at (${signal.x}, ${signal.y}):`, err);
            }
        }
    }
}
