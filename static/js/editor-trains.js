class TrainEditorHandler {
    constructor(editor) {
        this.editor = editor;
    }

    get api() { return this.editor.api; }

    // showTrainEditor and initTrainEditorHandlers removed - merged into showTrainControlPanel

    showAddTrainDialog() {
        const content = `
            <div class="add-train-panel">
                <div class="form-group">
                    <label for="train-id-input">Train ID</label>
                    <input type="text" id="train-id-input" placeholder="e.g., TRAIN_01">
                </div>
                <div class="form-group">
                    <label for="train-position-x">Position X</label>
                    <input type="number" id="train-position-x" value="0">
                </div>
                <div class="form-group">
                    <label for="train-position-y">Position Y</label>
                    <input type="number" id="train-position-y" value="0">
                </div>
                <div class="form-group">
                    <label for="train-direction">Direction</label>
                    <select id="train-direction">
                        <option value="NORTH">North</option>
                        <option value="EAST" selected>East</option>
                        <option value="SOUTH">South</option>
                        <option value="WEST">West</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="train-max-speed">Max Speed (km/h)</label>
                    <input type="number" id="train-max-speed" value="100" min="1" max="200">
                </div>
                <div class="form-group">
                    <label for="train-length">Length (units)</label>
                    <input type="number" id="train-length" value="1" min="1" max="10">
                </div>
                <button id="btn-confirm-add-train" class="btn btn-primary btn-full mt-16">🚂 Add Train</button>
            </div>
        `;

        this.editor.showSecondarySidebar('🚂 Add Train', content);

        setTimeout(() => {
            const addBtn = document.getElementById('btn-confirm-add-train');
            if (addBtn) {
                addBtn.onclick = async () => {
                    const trainId = document.getElementById('train-id-input').value.trim();
                    const posX = parseInt(document.getElementById('train-position-x').value) || 0;
                    const posY = parseInt(document.getElementById('train-position-y').value) || 0;
                    const direction = document.getElementById('train-direction').value;
                    const maxSpeed = parseInt(document.getElementById('train-max-speed').value) || 100;
                    const length = parseFloat(document.getElementById('train-length').value) || 1;

                    if (!trainId) {
                        window.showToast('Please enter a train ID', 'error', 3000);
                        return;
                    }

                    try {
                        await window.api.createTrain({
                            id: trainId,
                            position: { x: posX, y: posY },
                            direction: direction,
                            max_speed: maxSpeed,
                            length: length
                        });
                        window.showToast(`Train "${trainId}" created successfully`, 'success', 3000);
                        this.editor.hideSecondarySidebar();
                        window.app.refreshNetwork();
                    } catch (err) {
                        console.error('[TrainEditorHandler] Failed to create train:', err);
                        window.showToast('Failed to create train: ' + err.message, 'error', 5000);
                    }
                };
            }

            const idInput = document.getElementById('train-id-input');
            if (idInput && !idInput.value) {
                idInput.value = `TRAIN_${Date.now().toString().slice(-4)}`;
            }
        }, 0);
    }

    async showTrainsPanel() {
        this.editor._currentEditorType = 'trains-list';
        this.editor._currentEditorId = null;

        let trains = [];
        try {
            const result = await this.api.getTrains();
            trains = result.trains || [];
        } catch (err) {
            console.error('[TrainEditorHandler] Failed to load trains:', err);
            window.showToast('Failed to load trains: ' + err.message, 'error', 5000);
            return;
        }

        let content = `
            <div class="trains-panel">
                <div class="flex gap-8 mb-16">
                    <button id="refresh-trains-btn" class="btn btn-secondary btn-sm flex-1">
                        🔄 Refresh
                    </button>
                    <button id="add-new-train-btn" class="btn btn-primary btn-sm flex-1">
                        🚂 Add
                    </button>
                </div>
        `;

        if (trains.length === 0) {
            content += '<div class="trains-empty">No trains in network</div>';
        } else {
            content += '<div class="trains-list">';
            for (const train of trains) {
                content += this.renderTrainListItem(train);
            }
            content += '</div>';
        }

        content += '</div>';

        this.editor.showSecondarySidebar('🚂 Trains', content);

        setTimeout(() => this.initTrainsPanelHandlers(), 0);
    }

    renderTrainListItem(train) {
        const state = train.state || 'off';
        // Map states to consistent colors
        const stateColorMap = {
            'running_moving': '#04d361', // Success green
            'running_waiting': '#ff9500', // Warning orange
            'running_at_station': '#8257e5', // Purple
            'off': '#888', // Grey
            'idle': '#ffd93d', // Yellow
            'standby': '#00d4ff', // Cyan
            'manual': '#e83f5b' // Red/Pink
        };
        const stateColor = stateColorMap[state] || '#888';
        
        // Get head position from new TrainPosition format
        let position = { x: 0, y: 0 };
        if (train.position && train.position.end_a) {
            const headEnd = train.position.leading_end === 'a' ? train.position.end_a : train.position.end_b;
            position = { x: headEnd.tile[0], y: headEnd.tile[1] };
        }

        return `
            <div class="train-summary-row mb-8">
                <div class="flex items-center gap-12 w-full p-2">
                    <div class="flex flex-col items-center gap-1">
                        <div class="train-status-dot" style="background-color: ${stateColor}"></div>
                    </div>
                    <div class="train-info flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="train-id font-mono font-bold text-white">${train.id}</span>
                            <span class="text-xs font-bold uppercase tracking-wider" style="color: ${stateColor}">
                                ${state.toUpperCase().replace('RUNNING_', '')}
                            </span>
                        </div>
                        <div class="flex justify-between items-end">
                            <div class="text-xs text-muted">
                                <div>📍 (${position.x}, ${position.y}) ${train.facing_direction ? `→ ${train.facing_direction}` : ''}</div>
                                <div>🚀 ${(train.current_speed || 0).toFixed(1)} km/h</div>
                            </div>
                            <div class="flex gap-4">
                                <button class="edit-btn btn-sm" onclick="window.app.selectTrain('${train.id}')">Edit</button>
                                <button class="btn btn-sm btn-danger text-white delete-train-btn" style="background-color: var(--danger-color);" data-train-id="${train.id}">Delete</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    initTrainsPanelHandlers() {
        const refreshBtn = document.getElementById('refresh-trains-btn');
        if (refreshBtn) {
            refreshBtn.onclick = async () => {
                await this.showTrainsPanel();
            };
        }

        const addBtn = document.getElementById('add-new-train-btn');
        if (addBtn) {
            addBtn.onclick = () => {
                this.showAddTrainDialog();
            };
        }

        const selectBtns = document.querySelectorAll('.select-train-btn');
        selectBtns.forEach(btn => {
            btn.onclick = () => {
                const trainId = btn.dataset.trainId;
                window.app.selectTrain(trainId);
                this.editor.hideSecondarySidebar();
            };
        });

        const deleteBtns = document.querySelectorAll('.delete-train-btn');
        deleteBtns.forEach(btn => {
            btn.onclick = async () => {
                const trainId = btn.dataset.trainId;
                if (!confirm(`Delete train "${trainId}"?`)) return;

                try {
                    await window.api.deleteTrain(trainId);
                    window.showToast(`Train "${trainId}" deleted`, 'success', 3000);
                    await this.showTrainsPanel();
                    window.app.refreshNetwork();
                } catch (err) {
                    console.error('[TrainEditorHandler] Failed to delete train:', err);
                    window.showToast('Failed to delete train: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    async showSchedulesPanel() {
        this.editor._currentEditorType = 'schedules-list';
        this.editor._currentEditorId = null;

        let schedules = [];
        try {
            schedules = await this.api.getSchedules();
        } catch (err) {
            console.error('[TrainEditorHandler] Failed to load schedules:', err);
            window.showToast('Failed to load schedules: ' + err.message, 'error', 5000);
            return;
        }

        let content = `
            <div class="schedules-panel">
                <button id="refresh-schedules-btn" class="btn btn-secondary btn-full text-sm mb-16">
                    🔄 Refresh Schedules
                </button>
        `;

        if (schedules.length === 0) {
            content += '<div class="empty-state text-muted text-center">No schedules defined</div>';
        } else {
            content += '<div class="schedules-list">';
            for (const schedule of schedules) {
                content += this.renderScheduleListItem(schedule);
            }
            content += '</div>';
        }

        content += '</div>';

        this.editor.showSecondarySidebar('📅 Schedules', content);

        setTimeout(() => this.initSchedulesPanelHandlers(), 0);
    }

    renderScheduleListItem(schedule) {
        const trainId = schedule.train_id || 'Unknown';
        const stopCount = (schedule.stops && schedule.stops.length) || 0;
        const priority = schedule.priority || 0;
        const scheduleId = schedule.schedule_id || schedule.id || 'unknown';

        return `
            <div class="schedule-item" data-schedule-id="${scheduleId}">
                <div class="schedule-header">
                    <span class="schedule-train text-sm font-bold">🚂 ${trainId}</span>
                    <span class="schedule-priority text-xs text-muted">Priority: ${priority}</span>
                </div>
                <div class="schedule-stops text-xs text-muted">
                    ${stopCount} stop(s)
                </div>
                ${schedule.stops && schedule.stops.length > 0 ? `
                <div class="schedule-stop-list-compact text-xs schedule-stop-list">
                    ${schedule.stops.map((stop, idx) => `<div>${idx + 1}. ${stop.station_id || stop}</div>`).join('')}
                </div>
                ` : ''}
                <div class="schedule-actions mt-8">
                    <button class="btn btn-sm btn-danger delete-schedule-btn" data-schedule-id="${scheduleId}">Delete</button>
                </div>
            </div>
        `;
    }

    initSchedulesPanelHandlers() {
        const refreshBtn = document.getElementById('refresh-schedules-btn');
        if (refreshBtn) {
            refreshBtn.onclick = async () => {
                await this.showSchedulesPanel();
            };
        }

        const deleteBtns = document.querySelectorAll('.delete-schedule-btn');
        deleteBtns.forEach(btn => {
            btn.onclick = async () => {
                const scheduleId = btn.dataset.scheduleId;
                if (!confirm(`Delete schedule "${scheduleId}"?`)) return;

                try {
                    await window.api.deleteSchedule(scheduleId);
                    window.showToast(`Schedule "${scheduleId}" deleted`, 'success', 3000);
                    await this.showSchedulesPanel();
                } catch (err) {
                    console.error('[TrainEditorHandler] Failed to delete schedule:', err);
                    window.showToast('Failed to delete schedule: ' + err.message, 'error', 5000);
                }
            };
        });
    }

    async showTrainControlPanel(trainId) {
        this.editor._currentEditorType = 'train-control';
        this.editor._currentEditorId = trainId;

        let train;
        try {
            train = await this.api.getTrain(trainId);
        } catch (err) {
            console.error('[TrainEditorHandler] Failed to fetch train:', err);
            window.showToast(`Failed to load train: ${err.message}`, 'error', 5000);
            return;
        }

        if (!train) {
            console.warn('[TrainEditorHandler] Train not found:', trainId);
            window.showToast(`Train ${trainId} not found`, 'error', 5000);
            return;
        }

        const stateColors = {
            'off': '#888',
            'idle': '#ffd93d',
            'standby': '#00d4ff',
            'running_moving': '#04d361',
            'running_waiting': '#ff9500',
            'running_at_station': '#8257e5',
            'manual': '#e83f5b'
        };
        const stateColor = stateColors[train.state] || '#888';

        const content = `
            <div class="train-control-panel" data-train-id="${trainId}">
                ${renderHeaderId(trainId, '#8257e5')}
                
                <!-- INFO SECTION (Styled like Track Bits) -->
                <div class="info-box-track mb-12">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-muted uppercase tracking-wider">State</span>
                        ${train.block_section_id ? `<span class="badge badge-warning text-xxs">Block ${train.block_section_id}</span>` : ''}
                    </div>
                    
                    <div class="text-2xl font-bold mb-4 font-mono train-state-display" style="color: ${stateColor}">
                        ${(train.state || 'off').toUpperCase().replace('_', ' ')}
                    </div>
                    
                    <div class="grid grid-cols-2 gap-y-2 gap-x-4 text-sm font-mono">
                        <div>
                            <span class="text-muted text-xs block">Position</span>
                            <span class="text-info train-pos-display">${train.position && train.position.end_a ? `(${train.position.leading_end === 'a' ? train.position.end_a.tile[0] : train.position.end_b.tile[0]}, ${train.position.leading_end === 'a' ? train.position.end_a.tile[1] : train.position.end_b.tile[1]})` : 'Unknown'}</span>
                        </div>
                        <div>
                            <span class="text-muted text-xs block">Direction</span>
                            <span class="text-station train-dir-display">${train.direction || train.facing_direction || 'Unknown'}</span>
                        </div>
                        <div class="col-span-2">
                            <span class="text-muted text-xs block">Speed</span>
                            <span class="text-track train-speed-display">${(train.current_speed || 0).toFixed(1)} <span class="text-muted">/ ${train.max_speed || 100} km/h</span></span>
                        </div>
                    </div>
                    
                     ${train.state === 'running_waiting' && train.wait_reason ? `
                        <div class="mt-4 pt-4 border-t border-gray-700 text-xs text-warning train-wait-reason">
                            <span class="block text-muted mb-1">Waiting Reason:</span>
                            ${train.wait_reason}
                        </div>
                    ` : ''}
                </div>
                
                <div class="control-section mt-16">
                    <h4 class="text-sm text-muted mb-8">Power Control</h4>
                    <div class="flex gap-8">
                        <button id="train-power-on-btn" class="btn btn-primary btn-sm flex-1" ${train.state !== 'off' ? 'disabled' : ''}>⚡ Power On</button>
                        <button id="train-power-off-btn" class="btn btn-secondary btn-sm flex-1" ${train.state === 'off' ? 'disabled' : ''}>⏻ Power Off</button>
                    </div>
                </div>
                
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Direction</h4>
                    <div class="flex gap-8">
                        <button id="train-dir-north-btn" class="btn btn-outline btn-sm flex-1" ${train.state !== 'idle' ? 'disabled' : ''}>↑ N</button>
                        <button id="train-dir-east-btn" class="btn btn-outline btn-sm flex-1" ${train.state !== 'idle' ? 'disabled' : ''}>→ E</button>
                        <button id="train-dir-south-btn" class="btn btn-outline btn-sm flex-1" ${train.state !== 'idle' ? 'disabled' : ''}>↓ S</button>
                        <button id="train-dir-west-btn" class="btn btn-outline btn-sm flex-1" ${train.state !== 'idle' ? 'disabled' : ''}>← W</button>
                    </div>
                    <div class="mt-8">
                        <button id="train-clear-dir-btn" class="btn btn-outline btn-sm btn-full" ${train.state !== 'standby' ? 'disabled' : ''}>✖ Clear Direction</button>
                    </div>
                </div>
                
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Movement</h4>
                    <div class="flex gap-8">
                        <button id="train-depart-btn" class="btn btn-success btn-sm flex-1" ${train.state !== 'standby' ? 'disabled' : ''}>▶ Depart</button>
                        <button id="train-stop-btn" class="btn btn-danger btn-sm flex-1" ${!(train.state && train.state.startsWith('running')) ? 'disabled' : ''}>⏹ Stop</button>
                    </div>
                </div>
                
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Manual Control</h4>
                    <div class="flex gap-8">
                        <button id="train-manual-on-btn" class="btn btn-warning btn-sm flex-1" ${train.state === 'manual' ? 'disabled' : ''}>🎮 Enable</button>
                        <button id="train-manual-off-btn" class="btn btn-secondary btn-sm flex-1" ${train.state !== 'manual' ? 'disabled' : ''}>🤖 Disable</button>
                    </div>
                    <div class="mt-8">
                        <label class="property-label">Travel Direction</label>
                        <div class="flex gap-8">
                            <button id="train-travel-fwd-btn" class="btn btn-outline btn-sm flex-1 ${train.travel_direction === 'forward' ? 'btn-active' : ''}" ${train.state !== 'manual' ? 'disabled' : ''}>▶ Forward</button>
                            <button id="train-travel-rev-btn" class="btn btn-outline btn-sm flex-1 ${train.travel_direction === 'reverse' ? 'btn-active' : ''}" ${train.state !== 'manual' ? 'disabled' : ''}>◀ Reverse</button>
                        </div>
                    </div>
                    <div class="mt-8">
                        <label class="property-label">Manual Speed: <span id="manual-speed-label">${train.manual_target_speed || 0}</span> km/h</label>
                        <input type="range" id="train-manual-speed-slider" class="slider-full" min="0" max="${train.max_speed || 100}" step="1" value="${train.manual_target_speed || 0}" ${train.state !== 'manual' ? 'disabled' : ''}>
                    </div>
                </div>
                
                <div class="mt-16">
                    <button id="train-turnaround-btn" class="btn btn-info btn-full btn-sm">↩ Turn Around</button>
                </div>
                
                <div class="mt-12">
                    <button id="train-emergency-btn" class="btn btn-danger btn-full btn-sm">🚨 Emergency Stop</button>
                </div>
                
                <!-- Train Position Debug Section (shown when debug checkbox is enabled) -->
                <div id="train-control-debug-section" class="mt-12 hidden" style="border-top: 1px solid var(--border-color); padding-top: 12px;">
                    <!-- Content injected by app.js updateTrainDebugPanel() -->
                </div>
                
                <div class="mt-8">
                    <button id="train-monitor-btn" class="btn btn-info btn-full btn-sm">📊 Open Train Monitor</button>
                </div>

                <!-- EDITABLE PROPERTIES SECTION -->
                <div class="control-section mt-12 pt-12" style="border-top: 1px solid var(--border-color);">
                    <h4 class="text-sm text-muted mb-8">Properties</h4>
                    
                    <div class="grid grid-cols-2 gap-8 mb-8">
                        <div class="form-group mb-0">
                            <label class="text-xs text-muted mb-4 block">Max Speed (km/h)</label>
                            <input type="number" id="train-max-speed-input" class="prop-input" value="${train.max_speed || 100}" min="1" max="500">
                        </div>
                        <div class="form-group mb-0">
                            <label class="text-xs text-muted mb-4 block">Length (cm)</label>
                            <input type="number" id="train-length-input" class="prop-input" value="${train.length_cm || 0}" min="0" step="10">
                        </div>
                    </div>

                    <div class="flex gap-8">
                         <button id="save-train-btn" class="btn btn-primary btn-sm flex-1">💾 Save Changes</button>
                         <button id="delete-train-btn" class="btn btn-danger btn-sm flex-1">🗑 Delete Train</button>
                    </div>
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('🚂 Train Control', content);

        // Check debug checkbox immediately and update visibility
        const debugCheckbox = document.getElementById('debug-show-train-position');
        const debugSection = document.getElementById('train-control-debug-section');
        if (debugCheckbox && debugCheckbox.checked && debugSection) {
            debugSection.classList.remove('hidden');
            // Trigger an immediate update to populate content
            if (window.app) {
                window.app.updateTrainDebugPanel(train);
            }
        }

        setTimeout(() => this.initTrainControlHandlers(trainId), 0);
    }

    async initTrainControlHandlers(trainId) {
        const refresh = async () => {
            await window.app.refreshNetwork();
            await this.showTrainControlPanel(trainId);
        };

        // Power controls
        const powerOnBtn = document.getElementById('train-power-on-btn');
        const powerOffBtn = document.getElementById('train-power-off-btn');
        
        if (powerOnBtn) {
            powerOnBtn.onclick = async () => {
                try {
                    await window.api.trainPowerOn(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }
        
        if (powerOffBtn) {
            powerOffBtn.onclick = async () => {
                try {
                    await window.api.trainPowerOff(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        // Direction controls
        ['north', 'east', 'south', 'west'].forEach(dir => {
            const btn = document.getElementById(`train-dir-${dir}-btn`);
            if (btn) {
                btn.onclick = async () => {
                    try {
                        await window.api.trainSetDirection(trainId, dir.toUpperCase());
                        await refresh();
                    } catch (err) {
                        window.showToast('Failed: ' + err.message, 'error', 5000);
                    }
                };
            }
        });

        // Clear direction
        const clearDirBtn = document.getElementById('train-clear-dir-btn');
        if (clearDirBtn) {
            clearDirBtn.onclick = async () => {
                try {
                    await window.api.trainClearDirection(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        // Movement controls
        const departBtn = document.getElementById('train-depart-btn');
        const stopBtn = document.getElementById('train-stop-btn');
        
        if (departBtn) {
            departBtn.onclick = async () => {
                try {
                    await window.api.trainDepart(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }
        
        if (stopBtn) {
            stopBtn.onclick = async () => {
                try {
                    await window.api.trainArrive(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        // Manual control
        const manualOnBtn = document.getElementById('train-manual-on-btn');
        const manualOffBtn = document.getElementById('train-manual-off-btn');
        
        if (manualOnBtn) {
            manualOnBtn.onclick = async () => {
                try {
                    await window.api.trainEnableManual(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }
        
        if (manualOffBtn) {
            manualOffBtn.onclick = async () => {
                try {
                    await window.api.trainDisableManual(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        const travelFwdBtn = document.getElementById('train-travel-fwd-btn');
        const travelRevBtn = document.getElementById('train-travel-rev-btn');
        
        if (travelFwdBtn) {
            travelFwdBtn.onclick = async () => {
                try {
                    await window.api.trainSetTravelDirection(trainId, 'forward');
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }
        
        if (travelRevBtn) {
            travelRevBtn.onclick = async () => {
                try {
                    await window.api.trainSetTravelDirection(trainId, 'reverse');
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        const speedSlider = document.getElementById('train-manual-speed-slider');
        const speedLabel = document.getElementById('manual-speed-label');
        
        if (speedSlider) {
            speedSlider.oninput = () => {
                if (speedLabel) {
                    speedLabel.textContent = speedSlider.value;
                }
            };
            speedSlider.onchange = async () => {
                try {
                    await window.api.trainSetManualSpeed(trainId, parseFloat(speedSlider.value));
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        // Turn around
        const turnaroundBtn = document.getElementById('train-turnaround-btn');
        if (turnaroundBtn) {
            turnaroundBtn.onclick = async () => {
                try {
                    await window.api.trainTurnAround(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        // Emergency stop
        const emergencyBtn = document.getElementById('train-emergency-btn');
        if (emergencyBtn) {
            emergencyBtn.onclick = async () => {
                try {
                    await window.api.trainEmergencyStop(trainId);
                    await refresh();
                } catch (err) {
                    window.showToast('Failed: ' + err.message, 'error', 5000);
                }
            };
        }

        // Edit properties button - opens the train editor
        // (Removed as properties are now inline)
        
        const saveBtn = document.getElementById('save-train-btn');
        const deleteBtn = document.getElementById('delete-train-btn');
        const maxSpeedInput = document.getElementById('train-max-speed-input');
        const lengthInput = document.getElementById('train-length-input');

        if (saveBtn) {
            saveBtn.onclick = async () => {
                const updates = {};

                if (maxSpeedInput) {
                    const val = parseFloat(maxSpeedInput.value);
                    if (!isNaN(val) && val > 0) {
                        updates.max_speed = val;
                    }
                }
                if (lengthInput) {
                    const val = parseFloat(lengthInput.value);
                    if (!isNaN(val) && val >= 0) {
                        updates.length_cm = val;
                    }
                }

                try {
                    await this.api.updateTrain(trainId, updates);
                    await window.app.refreshNetwork();
                    this.showTrainControlPanel(trainId);
                    window.showToast('Train properties saved', 'success', 5000);
                } catch (err) {
                    console.error('Failed to save train properties:', err);
                    window.showToast('Failed to save train properties: ' + err.message, 'error', 5000);
                }
            };
        }

        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete train "${trainId}"? This action cannot be undone.`)) {
                    return;
                }
                try {
                    await this.api.deleteTrain(trainId);
                    await window.app.refreshNetwork();
                    this.editor.hideSecondarySidebar();
                    window.showToast('Train deleted', 'success', 5000);
                } catch (err) {
                    console.error('Failed to delete train:', err);
                    window.showToast('Failed to delete train: ' + err.message, 'error', 5000);
                }
            };
        }

        const monitorBtn = document.getElementById('train-monitor-btn');
        if (monitorBtn) {
            monitorBtn.onclick = () => {
                window.app.selectTrain(trainId);
            };
        }
    }

    async updateTrainControlPanelValues(trainId) {
        const panel = document.querySelector(`.train-control-panel[data-train-id="${trainId}"]`);
        if (!panel) return;

        let train;
        try {
            train = await this.api.getTrain(trainId);
        } catch (err) {
            return;
        }
        if (!train) return;

        const stateColors = {
            'off': '#888',
            'idle': '#ffd93d',
            'standby': '#00d4ff',
            'running_moving': '#04d361',
            'running_waiting': '#ff9500',
            'running_at_station': '#8257e5',
            'manual': '#e83f5b'
        };

        const stateEl = panel.querySelector('.train-state-display');
        if (stateEl) {
            stateEl.style.color = stateColors[train.state] || '#888';
            stateEl.innerHTML = `
                ${(train.state || 'off').toUpperCase().replace('_', ' ')}
            `;
        }
        
        // Update waiting reason
        let reasonEl = panel.querySelector('.train-wait-reason');
        if (train.state === 'running_waiting' && train.wait_reason) {
            if (!reasonEl) {
                // If element doesn't exist but should, we might need to recreate the info block, 
                // but simpler to just append if missing or update parent. 
                // For simplicity, we'll assume the basic structure exists.
                // If it's too complex to inject, we can just reload the panel, but that flickers.
                // Let's try to update the text content if it exists.
            } else {
                 reasonEl.innerHTML = `<span class="block text-muted mb-1">Waiting Reason:</span> ${train.wait_reason}`;
            }
        } else if (reasonEl) {
            reasonEl.remove();
        }

        const positionEl = panel.querySelector('.train-pos-display');
        if (positionEl && train.position_legacy) {
            positionEl.textContent = `(${train.position_legacy.x}, ${train.position_legacy.y})`;
        }

        const directionEl = panel.querySelector('.train-dir-display');
        if (directionEl) {
            directionEl.textContent = train.direction || train.facing_direction || 'Unknown';
        }

        const speedEl = panel.querySelector('.train-speed-display');
        if (speedEl) {
            speedEl.innerHTML = `${(train.current_speed || 0).toFixed(1)} <span class="text-muted">/ ${train.max_speed || 100} km/h</span>`;
        }

        const speedLabel = document.getElementById('manual-speed-label');
        const speedSlider = document.getElementById('train-manual-speed-slider');
        if (speedLabel && speedSlider && document.activeElement !== speedSlider) {
            speedLabel.textContent = train.manual_target_speed || 0;
            speedSlider.value = train.manual_target_speed || 0;
        }

        this.updateButtonStates(train);
    }

    updateButtonStates(train) {
        const state = train.state;
        
        const powerOnBtn = document.getElementById('train-power-on-btn');
        const powerOffBtn = document.getElementById('train-power-off-btn');
        if (powerOnBtn) powerOnBtn.disabled = state !== 'off';
        if (powerOffBtn) powerOffBtn.disabled = state === 'off';

        ['north', 'east', 'south', 'west'].forEach(dir => {
            const btn = document.getElementById(`train-dir-${dir}-btn`);
            if (btn) btn.disabled = state !== 'idle';
        });

        const clearDirBtn = document.getElementById('train-clear-dir-btn');
        if (clearDirBtn) clearDirBtn.disabled = state !== 'standby';

        const departBtn = document.getElementById('train-depart-btn');
        const stopBtn = document.getElementById('train-stop-btn');
        if (departBtn) departBtn.disabled = state !== 'standby';
        if (stopBtn) stopBtn.disabled = !(state && state.startsWith('running'));

        const manualOnBtn = document.getElementById('train-manual-on-btn');
        const manualOffBtn = document.getElementById('train-manual-off-btn');
        if (manualOnBtn) manualOnBtn.disabled = state === 'manual';
        if (manualOffBtn) manualOffBtn.disabled = state !== 'manual';

        const travelFwdBtn = document.getElementById('train-travel-fwd-btn');
        const travelRevBtn = document.getElementById('train-travel-rev-btn');
        const speedSlider = document.getElementById('train-manual-speed-slider');
        if (travelFwdBtn) travelFwdBtn.disabled = state !== 'manual';
        if (travelRevBtn) travelRevBtn.disabled = state !== 'manual';
        if (speedSlider) speedSlider.disabled = state !== 'manual';

        if (travelFwdBtn) {
            travelFwdBtn.classList.toggle('btn-active', train.travel_direction === 'forward');
        }
        if (travelRevBtn) {
            travelRevBtn.classList.toggle('btn-active', train.travel_direction === 'reverse');
        }
    }
}
