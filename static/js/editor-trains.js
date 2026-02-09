class TrainEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._trainId = null;  // Currently displayed train ID
        this._unsubscribe = null;  // Unsubscribe function for StateManager
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
            trains = await this.api.getTrains();
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
                <div class="flex gap-8 mb-16">
                    <button id="create-route-btn" class="btn btn-primary btn-sm flex-1">
                        + Create Route
                    </button>
                    <button id="refresh-schedules-btn" class="btn btn-secondary btn-sm">
                        🔄
                    </button>
                </div>
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
                    ${schedule.stops.map((stop, idx) => `<div>${idx + 1}. ${stop.platform_id || stop.station_id || stop}</div>`).join('')}
                </div>
                ` : ''}
                <div class="schedule-actions mt-8">
                    <button class="btn btn-sm btn-danger delete-schedule-btn" data-schedule-id="${scheduleId}">Delete</button>
                </div>
            </div>
        `;
    }

    initSchedulesPanelHandlers() {
        const createRouteBtn = document.getElementById('create-route-btn');
        if (createRouteBtn) {
            createRouteBtn.onclick = async () => {
                await this.editor.showRoutesPanel();
            };
        }

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
        let onboard;
        try {
            // Fetch both train and onboard computer state in parallel
            [train, onboard] = await Promise.all([
                this.api.getTrain(trainId),
                this.api.getOnboardState(trainId)
            ]);
        } catch (err) {
            console.error('[TrainEditorHandler] Failed to fetch train/onboard:', err);
            window.showToast(`Failed to load train: ${err.message}`, 'error', 5000);
            return;
        }

        if (!train) {
            console.warn('[TrainEditorHandler] Train not found:', trainId);
            window.showToast(`Train ${trainId} not found`, 'error', 5000);
            return;
        }

        // Use OnboardComputer state as source of truth for control state
        const stateColors = {
            'off': '#888',
            'idle': '#ffd93d',
            'standby': '#00d4ff',
            'running_moving': '#04d361',
            'running_waiting': '#ff9500',
            'running_at_station': '#8257e5',
            'manual': '#e83f5b'
        };
        const trainState = onboard?.state || train.state || 'off';
        const stateColor = stateColors[trainState] || '#888';

        // Get path and destination info for ATO section
        const onboardAto = onboard?.ato_info;
        const assignedPathId = onboardAto?.path_id || train.assigned_path_id;
        let pathName = 'No path';
        let destinationName = 'No path';
        
        if (assignedPathId) {
            try {
                const path = await this.api.getPath(assignedPathId);
                if (path) {
                    pathName = path.path_id || assignedPathId;
                    // Get destination: prefer station name, fallback to block name
                    if (path.end_platform_id || path.end_station_id) {
                        const destinationPlatformId = path.end_platform_id || path.end_station_id;
                        const platform = await this.api.getPlatform(destinationPlatformId);
                        destinationName = platform?.name || destinationPlatformId;
                    } else if (path.end_block_id) {
                        const block = await this.api.getBlock(path.end_block_id);
                        destinationName = block?.name || path.end_block_id;
                    }
                }
            } catch (err) {
                console.warn('[TrainEditorHandler] Failed to fetch path info:', err);
                // Fallback to onboard info if available
                pathName = onboardAto?.path_id || assignedPathId;
                destinationName = onboardAto?.destination_station_name || onboardAto?.destination_block_id || 'No path';
            }
        } else {
            // No path assigned - check if onboard has destination info anyway
            if (onboardAto?.destination_station_name) {
                destinationName = onboardAto.destination_station_name;
            } else if (onboardAto?.destination_block_id) {
                destinationName = onboardAto.destination_block_id;
            }
        }

        // Use OnboardComputer state for mode determination
        const controlMode = onboard?.control_mode || 'ato';
        const isATO = controlMode === 'ato';
        const isManual = controlMode === 'manual';
        const isStopped = onboard?.is_stopped ?? true;
        const canSwitchModes = isStopped;
        const handlePosition = onboard?.handle_position || 'N';
        const powerOn = onboard?.power_on ?? false;
        const emergencyActive = onboard?.emergency_stop_active ?? false;
        const atoRunning = onboard?.ato_running ?? false;

        // Handle position order for display
        const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];
        const handleIndex = handleOrder.indexOf(handlePosition);
        
        const content = `
            <div class="train-control-panel" data-train-id="${trainId}">
                ${renderHeaderId(trainId, '#8257e5')}
                
                <!-- INFO SECTION -->
                <div class="info-box-track mb-12">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-muted uppercase tracking-wider">State</span>
                        ${train.block_section_id ? `<span class="badge badge-warning text-xxs">Block ${train.block_section_id}</span>` : ''}
                    </div>
                    
                    <div class="text-2xl font-bold mb-4 font-mono train-state-display" style="color: ${stateColor}">
                        ${trainState.toUpperCase().replace('_', ' ')}
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
                            <span class="text-track train-speed-display">${(onboard?.current_speed ?? train.current_speed ?? 0).toFixed(1)} <span class="text-muted">/ ${train.max_speed || 100} km/h</span></span>
                        </div>
                    </div>
                    
                    ${(onboard?.train_state || train.state) === 'running_waiting' && train.wait_reason ? `
                        <div class="mt-4 pt-4 border-t border-gray-700 text-xs text-warning train-wait-reason">
                            <span class="block text-muted mb-1">Waiting Reason:</span>
                            ${train.wait_reason}
                        </div>
                    ` : ''}
                </div>
                
                <!-- ONBOARD COMPUTER STATUS -->
                <div class="info-box-block mb-12" style="border-left-color: ${powerOn ? (emergencyActive ? '#e83f5b' : '#04d361') : '#888'};">
                    <div class="text-xs text-muted uppercase tracking-wider mb-4">Onboard Computer</div>
                    <div class="flex items-center justify-between gap-6 px-5">
                        <div class="flex flex-col items-center gap-1 text-center">
                            <span class="text-xs text-muted">Power</span>
                            <div class="text-sm font-mono obc-power-display" style="color: ${powerOn ? '#04d361' : '#888'};">${powerOn ? 'ON' : 'OFF'}</div>
                        </div>
                        <div class="flex flex-col items-center gap-1 text-center">
                            <span class="text-xs text-muted">Mode</span>
                            <div class="text-sm font-mono obc-mode-display" style="color: ${isManual ? '#e83f5b' : '#00d4ff'};">${controlMode.toUpperCase()}</div>
                        </div>
                        <div class="flex flex-col items-center gap-1 text-center">
                            <span class="text-xs text-muted">Handle</span>
                            <div class="text-sm font-mono obc-handle-display" style="color: ${handlePosition.startsWith('P') ? '#04d361' : (handlePosition.startsWith('B') || handlePosition === 'EB' ? '#e83f5b' : '#ffd93d')};">${handlePosition}</div>
                        </div>
                    </div>
                    ${emergencyActive ? `
                        <div class="mt-4 text-center">
                            <span class="badge badge-danger text-xs">🚨 EMERGENCY STOP ACTIVE</span>
                        </div>
                    ` : ''}
                    ${atoRunning ? `
                        <div class="mt-4 text-center">
                            <span class="badge badge-success text-xs">▶ ATO RUNNING</span>
                        </div>
                    ` : ''}
                </div>
                
                <!-- TRAIN MONITOR BUTTON (moved up) -->
                <div class="mt-8">
                    <button id="train-monitor-btn" class="btn btn-info btn-full btn-sm">📊 Open Train Monitor</button>
                </div>
                
                <!-- TRAIN CONTROL SECTION -->
                <div class="control-section mt-16 pt-12" style="border-top: 1px solid var(--border-color);">
                    <h4 class="text-sm text-muted mb-12">Train Control</h4>
                    
                    <!-- EMERGENCY STOP (prominent at top) -->
                    <div class="mb-16">
                        ${emergencyActive ? `
                            <div class="flex items-center gap-8">
                                <button id="train-emergency-btn" class="btn btn-danger flex-3" style="font-size: 14px; padding: 12px;">
                                    🚨 EMERGENCY STOP
                                </button>
                                <button id="train-emergency-reset-btn" class="btn btn-warning flex-1" style="font-size: 14px; padding: 12px;">
                                    RESET
                                </button>
                            </div>
                        ` : `
                            <button id="train-emergency-btn" class="btn btn-danger btn-full" style="font-size: 14px; padding: 12px;">
                                🚨 EMERGENCY STOP
                            </button>
                        `}
                    </div>
                    
                    <!-- Power Control -->
                    <div class="mb-12">
                        <label class="text-xs text-muted mb-4 block">Power</label>
                        <div class="flex gap-8">
                            <button id="train-power-on-btn" class="btn btn-primary btn-sm flex-1" ${powerOn ? 'disabled' : ''}>⚡ On</button>
                            <button id="train-power-off-btn" class="btn btn-secondary btn-sm flex-1" ${!powerOn || !isStopped ? 'disabled' : ''}>⏻ Off</button>
                        </div>
                    </div>
                    
                    <!-- Cab Control (replaces direction panel) -->
                    <div class="mb-12">
                        <label class="text-xs text-muted mb-4 block">Cab Control</label>
                        <div class="flex gap-8 mb-4">
                            <button id="train-cab-a-btn" class="btn btn-outline btn-sm flex-1 ${onboard?.active_cab === 'A' ? 'btn-active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>Cab A</button>
                            <button id="train-cab-b-btn" class="btn btn-outline btn-sm flex-1 ${onboard?.active_cab === 'B' ? 'btn-active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>Cab B</button>
                            <button id="train-cab-off-btn" class="btn btn-outline btn-sm flex-1 ${!onboard?.active_cab ? 'btn-active' : ''}" ${!powerOn || !isStopped || !onboard?.active_cab ? 'disabled' : ''}>Off</button>
                        </div>
                        <div id="train-cab-status" class="text-xs text-center" style="color: ${onboard?.active_cab ? '#00d4ff' : '#ffd93d'};">
                            ${onboard?.active_cab ? `Cab ${onboard.active_cab} Active` : 'No Cab Selected'}
                        </div>
                    </div>
                    
                    <!-- Control Mode Toggle -->
                    <div class="mb-12">
                        <label class="text-xs text-muted mb-4 block">Control Mode</label>
                        <div class="flex gap-8">
                            <button id="train-mode-ato-btn" class="btn btn-outline btn-sm flex-1 ${isATO ? 'btn-active' : ''}" ${!canSwitchModes ? 'disabled' : ''}>ATO</button>
                            <button id="train-mode-manual-btn" class="btn btn-outline btn-sm flex-1 ${isManual ? 'btn-active' : ''}" ${!canSwitchModes ? 'disabled' : ''}>Manual</button>
                        </div>
                    </div>
                    
                    <!-- ATO Controls -->
                    <div id="ato-controls" class="${isManual ? 'hidden' : ''}">
                        <div class="mb-8 pb-8" style="border-bottom: 1px solid var(--border-color);">
                            <div class="text-xs text-muted mb-2">Path</div>
                            <div class="text-sm text-info font-mono mb-4 train-ato-path-display">${pathName}</div>
                            <div class="text-xs text-muted mb-2">Destination</div>
                            <div class="text-sm text-station font-mono train-ato-destination-display">${destinationName}</div>
                        </div>
                        <div class="flex gap-8">
                            <button id="train-ato-go-btn" class="btn btn-success btn-sm flex-1" ${!onboard?.active_cab || atoRunning ? 'disabled' : ''}>▶ Go</button>
                            <button id="train-ato-stop-btn" class="btn btn-danger btn-sm flex-1" ${!atoRunning ? 'disabled' : ''}>⏹ Stop</button>
                        </div>
                    </div>
                    
                    <!-- Manual Controls -->
                    <div id="manual-controls" class="${isATO ? 'hidden' : ''}">
                        <div class="mb-8">
                            <label class="text-xs text-muted mb-4 block">Travel Direction</label>
                            <div class="flex gap-8">
                                <button id="train-travel-fwd-btn" class="btn btn-outline btn-sm flex-1 ${onboard?.travel_direction === 'forward' ? 'btn-active' : ''}" ${!isManual || !isStopped ? 'disabled' : ''}>▶ Forward</button>
                                <button id="train-travel-rev-btn" class="btn btn-outline btn-sm flex-1 ${onboard?.travel_direction === 'reverse' ? 'btn-active' : ''}" ${!isManual || !isStopped ? 'disabled' : ''}>◀ Reverse</button>
                            </div>
                        </div>
                        
                        <!-- Handle Position Control (New) -->
                        <div class="mb-12">
                            <label class="text-xs text-muted mb-4 block">Throttle/Brake Handle</label>
                            <div class="handle-control">
                                <div class="flex justify-between text-xxs text-muted mb-2">
                                    <span>P5 (Full Power)</span>
                                    <span>N</span>
                                    <span>EB</span>
                                </div>
                                <input type="range" id="train-handle-slider" class="slider-full" 
                                    min="0" max="14" step="1" value="${handleIndex >= 0 ? handleIndex : 5}"
                                    ${!isManual || emergencyActive ? 'disabled' : ''}>
                                <div class="text-center mt-4">
                                    <span id="handle-position-label" class="text-lg font-mono font-bold" style="color: ${handlePosition.startsWith('P') ? '#04d361' : (handlePosition.startsWith('B') || handlePosition === 'EB' ? '#e83f5b' : '#ffd93d')};">
                                        ${handlePosition}
                                    </span>
                                    <span class="text-xs text-muted block mt-1" id="handle-description">
                                        ${handlePosition === 'N' ? 'Coast (no power, no braking)' :
                                          handlePosition.startsWith('P') ? `Power ${handlePosition.slice(1)} - Accelerating` :
                                          handlePosition === 'EB' ? 'Emergency Brake' :
                                          `Brake ${handlePosition.slice(1)} - Decelerating`}
                                    </span>
                                    <span class="text-xs block mt-4 train-handle-reason" id="handle-reason">
                                        ${onboard?.handle_reason || ''}
                                    </span>
                                </div>
                            </div>
                        </div>
                        
                        <!-- Legacy speed slider for debug (hidden by default) -->
                        <div class="hidden">
                            <label class="property-label">Speed: <span id="manual-speed-label">${train.manual_target_speed || 0}</span> km/h</label>
                            <input type="range" id="train-manual-speed-slider" class="slider-full" min="0" max="${train.max_speed || 100}" step="1" value="${train.manual_target_speed || 0}" ${train.state !== 'manual' ? 'disabled' : ''}>
                        </div>
                    </div>
                </div>
                
                <!-- Train Position Debug Section (shown when debug checkbox is enabled) -->
                <div id="train-control-debug-section" class="mt-12 hidden" style="border-top: 1px solid var(--border-color); padding-top: 12px;">
                    <!-- Content injected by app.js updateTrainDebugPanel() -->
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
        /**
         * Helper to execute a button action with proper error handling and feedback.
         * All button actions should go through this to ensure consistent behavior.
         * 
         * @param {string} actionName - Human-readable action name for toast messages
         * @param {Function} action - Async function that performs the API call
         * @param {boolean} refreshOnSuccess - Whether to refresh panel on success (default: true)
         */
        const executeAction = async (actionName, action, refreshOnSuccess = true) => {
            console.log(`[TrainControl] Executing: ${actionName}`);
            try {
                const result = await action();
                console.log(`[TrainControl] ${actionName} result:`, result);
                
                // Check if result indicates success
                if (result && result.status === 'success') {
                    window.showToast(`${actionName}: Success`, 'success', 2000);
                } else if (result && result.error) {
                    // API returned an error in the response body
                    window.showToast(`${actionName}: ${result.error}`, 'error', 5000);
                    return;
                }
                
                if (refreshOnSuccess) {
                    await window.app.refreshNetwork();
                    await this.showTrainControlPanel(trainId);
                }
            } catch (err) {
                console.error(`[TrainControl] ${actionName} failed:`, err);
                window.showToast(`${actionName}: ${err.message}`, 'error', 5000);
            }
        };

        const waitForTrainStopped = async (timeoutMs = 8000, intervalMs = 250) => {
            const start = Date.now();
            while (Date.now() - start < timeoutMs) {
                try {
                    const onboardState = await this.api.getOnboardState(trainId);
                    if (onboardState?.is_stopped) {
                        return true;
                    }
                } catch (err) {
                    console.warn('[TrainControl] Failed to poll onboard state:', err);
                }

                await new Promise(resolve => setTimeout(resolve, intervalMs));
            }

            return false;
        };
        
        // Handle position order for slider
        const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];

        // ═══════════════════════════════════════════════════════════════════
        // POWER CONTROLS
        // ═══════════════════════════════════════════════════════════════════
        
        const powerOnBtn = document.getElementById('train-power-on-btn');
        const powerOffBtn = document.getElementById('train-power-off-btn');
        
        if (powerOnBtn) {
            powerOnBtn.onclick = () => executeAction('Power On', 
                () => window.api.trainPowerOn(trainId));
        }
        
        if (powerOffBtn) {
            powerOffBtn.onclick = () => executeAction('Power Off', 
                () => window.api.trainPowerOff(trainId));
        }

        // ═══════════════════════════════════════════════════════════════════
        // CAB CONTROLS
        // ═══════════════════════════════════════════════════════════════════
        
        const cabABtn = document.getElementById('train-cab-a-btn');
        const cabBBtn = document.getElementById('train-cab-b-btn');
        const cabOffBtn = document.getElementById('train-cab-off-btn');
        
        if (cabABtn) {
            cabABtn.onclick = () => executeAction('Set Cab A', 
                () => window.api.trainSetCab(trainId, 'A'));
        }
        
        if (cabBBtn) {
            cabBBtn.onclick = () => executeAction('Set Cab B', 
                () => window.api.trainSetCab(trainId, 'B'));
        }
        
        if (cabOffBtn) {
            cabOffBtn.onclick = () => executeAction('Cab Off', 
                () => window.api.trainSetCab(trainId, null));
        }

        // ═══════════════════════════════════════════════════════════════════
        // MODE CONTROLS (ATO / Manual)
        // ═══════════════════════════════════════════════════════════════════
        
        const modeATOBtn = document.getElementById('train-mode-ato-btn');
        const modeManualBtn = document.getElementById('train-mode-manual-btn');
        
        if (modeATOBtn) {
            modeATOBtn.onclick = () => executeAction('Switch to ATO', 
                () => window.api.setControlMode(trainId, 'ato'));
        }
        
        if (modeManualBtn) {
            modeManualBtn.onclick = () => executeAction('Switch to Manual', 
                () => window.api.setControlMode(trainId, 'manual'));
        }

        // ═══════════════════════════════════════════════════════════════════
        // ATO CONTROLS (Go / Stop)
        // ═══════════════════════════════════════════════════════════════════
        
        const atoGoBtn = document.getElementById('train-ato-go-btn');
        const atoStopBtn = document.getElementById('train-ato-stop-btn');
        
        if (atoGoBtn) {
            atoGoBtn.onclick = () => executeAction('ATO Go', 
                () => window.api.atoGo(trainId));
        }
        
        if (atoStopBtn) {
            atoStopBtn.onclick = () => executeAction('ATO Stop', 
                async () => {
                    const result = await window.api.atoStop(trainId);
                    if (result && result.status === 'success') {
                        await waitForTrainStopped();
                    }
                    return result;
                });
        }

        // ═══════════════════════════════════════════════════════════════════
        // MANUAL TRAVEL DIRECTION
        // ═══════════════════════════════════════════════════════════════════
        
        const travelFwdBtn = document.getElementById('train-travel-fwd-btn');
        const travelRevBtn = document.getElementById('train-travel-rev-btn');
        
        if (travelFwdBtn) {
            travelFwdBtn.onclick = () => executeAction('Set Forward', 
                () => window.api.trainSetTravelDirection(trainId, 'forward'));
        }
        
        if (travelRevBtn) {
            travelRevBtn.onclick = () => executeAction('Set Reverse', 
                () => window.api.trainSetTravelDirection(trainId, 'reverse'));
        }

        // ═══════════════════════════════════════════════════════════════════
        // HANDLE POSITION SLIDER
        // ═══════════════════════════════════════════════════════════════════
        
        const handleSlider = document.getElementById('train-handle-slider');
        const handleLabel = document.getElementById('handle-position-label');
        const handleDesc = document.getElementById('handle-description');
        
        if (handleSlider) {
            // Live update during drag (visual feedback only)
            handleSlider.oninput = () => {
                const position = handleOrder[parseInt(handleSlider.value)];
                if (handleLabel) {
                    handleLabel.textContent = position;
                    // Update color based on position type
                    if (position.startsWith('P')) {
                        handleLabel.style.color = '#04d361';  // Green for power
                    } else if (position.startsWith('B') || position === 'EB') {
                        handleLabel.style.color = '#e83f5b';  // Red for braking
                    } else {
                        handleLabel.style.color = '#ffd93d';  // Yellow for neutral
                    }
                }
                if (handleDesc) {
                    if (position === 'N') {
                        handleDesc.textContent = 'Coast (no power, no braking)';
                    } else if (position.startsWith('P')) {
                        handleDesc.textContent = `Power ${position.slice(1)} - Accelerating`;
                    } else if (position === 'EB') {
                        handleDesc.textContent = 'Emergency Brake';
                    } else {
                        handleDesc.textContent = `Brake ${position.slice(1)} - Decelerating`;
                    }
                }
            };
            
            // Commit on release
            handleSlider.onchange = async () => {
                const position = handleOrder[parseInt(handleSlider.value)];
                await executeAction(`Set Handle ${position}`, 
                    () => window.api.setHandle(trainId, position), 
                    false);  // Don't full refresh - just update OBC display
                
                // Quick update of OBC display
                try {
                    const onboard = await window.api.getOnboardState(trainId);
                    this._updateOnboardDisplay(onboard);
                } catch (e) {
                    // Ignore errors here, next poll will update
                }
            };
        }

        // ═══════════════════════════════════════════════════════════════════
        // LEGACY SPEED SLIDER (hidden)
        // ═══════════════════════════════════════════════════════════════════
        
        const speedSlider = document.getElementById('train-manual-speed-slider');
        const speedLabel = document.getElementById('manual-speed-label');
        
        if (speedSlider) {
            speedSlider.oninput = () => {
                if (speedLabel) {
                    speedLabel.textContent = speedSlider.value;
                }
            };
            speedSlider.onchange = () => executeAction('Set Manual Speed', 
                () => window.api.trainSetManualSpeed(trainId, parseFloat(speedSlider.value)),
                false);
        }

        // ═══════════════════════════════════════════════════════════════════
        // EMERGENCY STOP
        // ═══════════════════════════════════════════════════════════════════
        
        const emergencyBtn = document.getElementById('train-emergency-btn');
        if (emergencyBtn) {
            emergencyBtn.onclick = async () => {
                try {
                    await executeAction('Emergency Stop', 
                        () => window.api.setHandle(trainId, 'EB'));
                } catch (err) {
                    console.error('[TrainControl] Emergency toggle failed:', err);
                    window.showToast(`Emergency: ${err.message}`, 'error', 5000);
                }
            };
        }

        const emergencyResetBtn = document.getElementById('train-emergency-reset-btn');
        if (emergencyResetBtn) {
            emergencyResetBtn.onclick = async () => {
                try {
                    await executeAction('Release Emergency', 
                        () => window.api.setHandle(trainId, 'N'));
                } catch (err) {
                    console.error('[TrainControl] Emergency reset failed:', err);
                    window.showToast(`Emergency Reset: ${err.message}`, 'error', 5000);
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
        
        // ═══════════════════════════════════════════════════════════════
        // SUBSCRIPTION SETUP (NEW - Phase 3)
        // ═══════════════════════════════════════════════════════════════
        
        // Cleanup previous subscription if any
        this._cleanup();
        
        // Store current train ID
        this._trainId = trainId;
        
        // Subscribe to train updates from StateManager
        this._unsubscribe = window.stateManager.subscribe(
            'trains',
            trainId,
            (newTrain, oldTrain) => this._onTrainUpdate(newTrain, oldTrain),
            { immediate: false }  // Don't call immediately, we already have the data
        );
    }
    
    /**
     * Handle train updates from StateManager subscription.
     * This replaces the polling-based updateControlPanelValuesFromData.
     * @private
     */
    _onTrainUpdate(newTrain, oldTrain) {
        if (!newTrain) {
            // Train was deleted
            window.showToast('Train was deleted', 'warning');
            this.editor.hideSecondarySidebar();
            return;
        }
        
        // Always update display values - this is a live view (Control Panel pattern)
        // No "dirty" concept for control panels - they update in real-time
        this.updateControlPanelValuesFromData(newTrain);
    }
    
    /**
     * Cleanup subscription when panel is hidden or train changes.
     * @private
     */
    _cleanup() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
        this._trainId = null;
    }
    
    /**
     * Update OnboardComputer display elements without full panel refresh.
     * Used for quick updates after handle changes.
     * @private
     */
    _updateOnboardDisplay(onboard) {
        if (!onboard) return;
        
        const handlePosition = onboard.handle_position || 'N';
        const powerOn = onboard.power_on ?? false;
        const emergencyActive = onboard.emergency_stop_active ?? false;
        const controlMode = onboard.control_mode || 'ato';
        const atoRunning = onboard.ato_running ?? false;
        
        // Update OBC status display
        const powerDisplay = document.querySelector('.obc-power-display');
        const modeDisplay = document.querySelector('.obc-mode-display');
        const handleDisplay = document.querySelector('.obc-handle-display');
        
        if (powerDisplay) {
            powerDisplay.textContent = powerOn ? 'ON' : 'OFF';
            powerDisplay.style.color = powerOn ? '#04d361' : '#888';
        }
        
        if (modeDisplay) {
            modeDisplay.textContent = controlMode.toUpperCase();
            modeDisplay.style.color = controlMode === 'manual' ? '#e83f5b' : '#00d4ff';
        }
        
        if (handleDisplay) {
            handleDisplay.textContent = handlePosition;
            if (handlePosition.startsWith('P')) {
                handleDisplay.style.color = '#04d361';
            } else if (handlePosition.startsWith('B') || handlePosition === 'EB') {
                handleDisplay.style.color = '#e83f5b';
            } else {
                handleDisplay.style.color = '#ffd93d';
            }
        }
        
        // Update speed display
        const speedDisplay = document.querySelector('.train-speed-display');
        if (speedDisplay && onboard.current_speed !== undefined) {
            const maxSpeed = 100;  // Would need train data for actual max
            speedDisplay.innerHTML = `${onboard.current_speed.toFixed(1)} <span class="text-muted">km/h</span>`;
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

        // Delegate to the data-based update method
        this.updateControlPanelValuesFromData(train);
    }

    /**
     * Update train control panel values from train data object (no API call).
     * Used during simulation to avoid redundant API calls.
     * @param {Object} train - Train data object from currentNetwork
     */
    updateControlPanelValuesFromData(train) {
        if (!train) return;
        
        const panel = document.querySelector(`.train-control-panel[data-train-id="${train.id}"]`);
        if (!panel) return;

        const onboard = train.onboard || null;
        const trainState = onboard?.state || train.state || 'off';

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
            stateEl.style.color = stateColors[trainState] || '#888';
            stateEl.innerHTML = `
                ${trainState.toUpperCase().replace('_', ' ')}
            `;
        }
        
        // Update waiting reason
        let reasonEl = panel.querySelector('.train-wait-reason');
        if (trainState === 'running_waiting' && train.wait_reason) {
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

        // Update position - use new TrainPosition format
        const positionEl = panel.querySelector('.train-pos-display');
        if (positionEl && train.position && train.position.end_a) {
            const headEnd = train.position.leading_end === 'a' ? train.position.end_a : train.position.end_b;
            positionEl.textContent = `(${headEnd.tile[0]}, ${headEnd.tile[1]})`;
        }

        const directionEl = panel.querySelector('.train-dir-display');
        if (directionEl) {
            directionEl.textContent = train.direction || train.facing_direction || 'Unknown';
        }

        const speedEl = panel.querySelector('.train-speed-display');
        if (speedEl) {
            const currentSpeed = onboard?.current_speed ?? train.current_speed ?? 0;
            speedEl.innerHTML = `${currentSpeed.toFixed(1)} <span class="text-muted">/ ${train.max_speed || 100} km/h</span>`;
        }

        if (onboard) {
            const powerDisplay = panel.querySelector('.obc-power-display');
            const modeDisplay = panel.querySelector('.obc-mode-display');
            const handleDisplay = panel.querySelector('.obc-handle-display');

            const powerOn = onboard.power_on ?? false;
            const controlMode = onboard.control_mode || 'ato';
            const handlePosition = onboard.handle_position || 'N';

            if (powerDisplay) {
                powerDisplay.textContent = powerOn ? 'ON' : 'OFF';
                powerDisplay.style.color = powerOn ? '#04d361' : '#888';
            }

            if (modeDisplay) {
                modeDisplay.textContent = controlMode.toUpperCase();
                modeDisplay.style.color = controlMode === 'manual' ? '#e83f5b' : '#00d4ff';
            }

            if (handleDisplay) {
                handleDisplay.textContent = handlePosition;
                if (handlePosition.startsWith('P')) {
                    handleDisplay.style.color = '#04d361';
                } else if (handlePosition.startsWith('B') || handlePosition === 'EB') {
                    handleDisplay.style.color = '#e83f5b';
                } else {
                    handleDisplay.style.color = '#ffd93d';
                }
            }

            const pathDisplay = panel.querySelector('.train-ato-path-display');
            const destinationDisplay = panel.querySelector('.train-ato-destination-display');
            if (pathDisplay) {
                pathDisplay.textContent = onboard.ato_info?.path_id || train.assigned_path_id || 'No path';
            }
            if (destinationDisplay) {
                destinationDisplay.textContent = onboard.ato_info?.destination_station_name || onboard.ato_info?.destination_block_id || 'No path';
            }

            const handleSlider = document.getElementById('train-handle-slider');
            const handleLabel = document.getElementById('handle-position-label');
            const handleDesc = document.getElementById('handle-description');
            const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];
            const handleIndex = handleOrder.indexOf(handlePosition);

            if (handleSlider && handleIndex >= 0 && document.activeElement !== handleSlider) {
                handleSlider.value = handleIndex;
            }

            if (handleLabel) {
                handleLabel.textContent = handlePosition;
                if (handlePosition.startsWith('P')) {
                    handleLabel.style.color = '#04d361';
                } else if (handlePosition.startsWith('B') || handlePosition === 'EB') {
                    handleLabel.style.color = '#e83f5b';
                } else {
                    handleLabel.style.color = '#ffd93d';
                }
            }

            if (handleDesc) {
                if (handlePosition === 'N') {
                    handleDesc.textContent = 'Coast (no power, no braking)';
                } else if (handlePosition.startsWith('P')) {
                    handleDesc.textContent = `Power ${handlePosition.slice(1)} - Accelerating`;
                } else if (handlePosition === 'EB') {
                    handleDesc.textContent = 'Emergency Brake';
                } else {
                    handleDesc.textContent = `Brake ${handlePosition.slice(1)} - Decelerating`;
                }
            }

            // Update handle reason (ATO diagnostic)
            const handleReason = document.getElementById('handle-reason');
            if (handleReason) {
                handleReason.textContent = onboard?.handle_reason || '';
            }
        }

        const speedLabel = document.getElementById('manual-speed-label');
        const speedSlider = document.getElementById('train-manual-speed-slider');
        if (speedLabel && speedSlider && document.activeElement !== speedSlider) {
            speedLabel.textContent = train.manual_target_speed || 0;
            speedSlider.value = train.manual_target_speed || 0;
        }

        const powerOn = onboard?.power_on ?? false;
        const controlMode = onboard?.control_mode || 'ato';
        const isStopped = onboard?.is_stopped ?? true;
        const activeCab = onboard?.active_cab || null;
        const travelDirection = onboard?.travel_direction || train.travel_direction || 'forward';

        this.updateCabButtons(panel, powerOn, isStopped, activeCab);
        this.updateModeButtons(panel, isStopped, controlMode);
        this.updateTravelButtons(panel, isStopped, controlMode, travelDirection);

        this.updateButtonStates(train);
    }

    updateCabButtons(panel, powerOn, isStopped, activeCab) {
        const cabABtn = panel.querySelector('#train-cab-a-btn');
        const cabBBtn = panel.querySelector('#train-cab-b-btn');
        const cabOffBtn = panel.querySelector('#train-cab-off-btn');
        const cabStatus = panel.querySelector('#train-cab-status');

        if (cabABtn) {
            cabABtn.disabled = !powerOn || !isStopped;
            cabABtn.classList.toggle('btn-active', activeCab === 'A');
        }
        if (cabBBtn) {
            cabBBtn.disabled = !powerOn || !isStopped;
            cabBBtn.classList.toggle('btn-active', activeCab === 'B');
        }
        if (cabOffBtn) {
            const isOff = !activeCab;
            cabOffBtn.disabled = !powerOn || !isStopped || isOff;
            cabOffBtn.classList.toggle('btn-active', isOff);
        }
        if (cabStatus) {
            cabStatus.textContent = activeCab ? `Cab ${activeCab} Active` : 'No Cab Selected';
            cabStatus.style.color = activeCab ? '#00d4ff' : '#ffd93d';
        }
    }

    updateModeButtons(panel, isStopped, controlMode) {
        const modeATOBtn = panel.querySelector('#train-mode-ato-btn');
        const modeManualBtn = panel.querySelector('#train-mode-manual-btn');

        if (modeATOBtn) {
            modeATOBtn.disabled = !isStopped;
            modeATOBtn.classList.toggle('btn-active', controlMode === 'ato');
        }
        if (modeManualBtn) {
            modeManualBtn.disabled = !isStopped;
            modeManualBtn.classList.toggle('btn-active', controlMode === 'manual');
        }
    }

    updateTravelButtons(panel, isStopped, controlMode, travelDirection) {
        const travelFwdBtn = panel.querySelector('#train-travel-fwd-btn');
        const travelRevBtn = panel.querySelector('#train-travel-rev-btn');
        const isManual = controlMode === 'manual';

        if (travelFwdBtn) {
            travelFwdBtn.disabled = !isManual || !isStopped;
            travelFwdBtn.classList.toggle('btn-active', travelDirection === 'forward');
        }
        if (travelRevBtn) {
            travelRevBtn.disabled = !isManual || !isStopped;
            travelRevBtn.classList.toggle('btn-active', travelDirection === 'reverse');
        }
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
