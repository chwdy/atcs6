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
                <div class="signal-properties" style="background: #1e2d23; padding: 16px; border-radius: 8px; margin-bottom: 16px; border-left: 4px solid ${stateColor};">

                    ${renderMultiSignalHeader(i + 1, signals.length, signal.signal_id, stateColor)}
                    ${renderSignalStateDisplay(state, stateColor)}

                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Signal Name</label>
                        <input type="text" class="signal-name-input" data-signal-id="${signal.signal_id}" value="${signal.name || ''}" placeholder="Enter signal name..."
                               style="width: 100%; padding: 10px 12px; background: #0d1a12; border: 1px solid #3a4a3e; color: #fff; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Detection Direction (Train travels →)</label>
                        <div style="display: flex; align-items: center; gap: 8px; background: #0d1a12; padding: 10px 12px; border: 1px solid #3a4a3e; border-radius: 6px;">
                            <span style="font-size: 13px; color: ${signal.detection_direction ? '#fff' : '#f55'}; font-weight: bold;">${signal.detection_direction || '???'}</span>
                            <span style="font-size: 10px; color: #666; margin-left: auto;">(Set at creation)</span>
                        </div>
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">Block to Monitor</label>
                        <select class="signal-block-select" data-signal-id="${signal.signal_id}" style="width: 100%; padding: 10px 12px; background: #0d1a12; border: 1px solid ${signal.monitored_block_id ? '#3a4a3e' : '#ff9500'}; color: #fff; border-radius: 6px; font-size: 13px; box-sizing: border-box;">
                            <option value="">-- None (Manual Control) --</option>
                            <option value="${signal.monitored_block_id || ''}" selected>${signal.monitored_block_id || 'None'} (Loading...)</option>
                        </select>
                        <div class="block-select-hint" data-signal-id="${signal.signal_id}" style="font-size: 10px; color: #666; margin-top: 6px;">
                            Signal shows RED when monitored block is occupied
                        </div>
                    </div>

                    <button class="save-signal-btn btn btn-primary btn-full text-base" data-signal-id="${signal.signal_id}">
                        Save Signal Properties
                    </button>
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

        const content = `
            <div class="signal-properties" style="background: #1e2d23; padding: 16px; border-radius: 8px; margin: 12px 0; border-left: 4px solid ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')};">

                <!-- Info Display Group -->
                <div class="info-display-group" style="background: #0d1a12; margin-bottom: 16px;">
                    <div class="info-display-header" style="color: #00ff88;">🚦 Signal Information</div>
                    <div class="info-display-item">
                        <span class="info-display-label">Signal ID</span>
                        <span class="info-display-value" style="color: #00d4ff;">${signal.signal_id}</span>
                    </div>
                    <div class="info-display-item">
                        <span class="info-display-label">Current State</span>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span style="display: inline-block; width: 12px; height: 12px; border-radius: 50%; background: ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')}; box-shadow: 0 0 6px ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')};"></span>
                            <span class="info-display-value" style="color: ${(signal.state || signal.signal_state || '').toLowerCase() === 'green' ? '#04d361' : ((signal.state || signal.signal_state || '').toLowerCase() === 'yellow' ? '#ffd93d' : '#e83f5b')};">
                                ${(signal.state || signal.signal_state || 'GREEN').toUpperCase()}
                            </span>
                        </div>
                    </div>
                    <div class="info-display-item">
                        <span class="info-display-label">Detection Direction</span>
                        <span class="info-display-value" style="color: ${signal.detection_direction ? '#fff' : '#f55'};">${signal.detection_direction || '???'}</span>
                    </div>
                </div>

                <!-- User Input Fields (Horizontal Layout) -->
                <div class="form-row-horizontal">
                    <label for="signal-name-input">Signal Name</label>
                    <input type="text" id="signal-name-input" value="${signal.name || ''}" placeholder="Enter signal name...">
                </div>

                <div class="form-row-horizontal">
                    <label for="signal-block-select">Block to Monitor</label>
                    <select id="signal-block-select" style="border-color: ${signal.monitored_block_id ? 'var(--border-color)' : '#ff9500'};">
                        <option value="">-- None (Manual Control) --</option>
                        <option value="${signal.monitored_block_id}" selected>${signal.monitored_block_id} (Loading...)</option>
                    </select>
                </div>
                <div id="block-select-hint" style="font-size: 10px; color: #666; margin-top: -6px; margin-bottom: 12px; text-align: right;">
                    Signal shows RED when monitored block is occupied
                </div>

                <button id="save-signal-btn" class="btn btn-primary btn-full text-base">
                    Save Signal Properties
                </button>
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
