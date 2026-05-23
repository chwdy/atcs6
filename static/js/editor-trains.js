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
        const controller = window.stateManager.getController(train.id);
        const state = controller?.state || 'off';
        const stateClass = this.getTrainStateVisualClass(state);

        // Get head position from controller span
        let position = { x: 0, y: 0 };
        const span = controller?.span;
        if (span && span.end_a) {
            const headEnd = span.leading_end === 'a' ? span.end_a : span.end_b;
            position = { x: headEnd.tile[0], y: headEnd.tile[1] };
        }

        return `
            <div class="train-summary-row mb-8">
                <div class="flex items-center gap-12 w-full p-2">
                    <div class="flex flex-col items-center gap-1">
                        <div class="train-status-dot train-state-dot ${stateClass}"></div>
                    </div>
                    <div class="train-info flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <span class="train-id font-mono font-bold text-white">${train.id}</span>
                            <span class="text-xs font-bold uppercase tracking-wider train-state-text ${stateClass}">
                                ${state.toUpperCase().replace('RUNNING_', '')}
                            </span>
                        </div>
                        <div class="flex justify-between items-end">
                            <div class="text-xs text-muted">
                                <div>📍 (${position.x}, ${position.y}) ${train.facing_direction ? `→ ${train.facing_direction}` : ''}</div>
                                <div>🚀 ${(controller?.current_speed || 0).toFixed(1)} km/h</div>
                            </div>
                            <div class="flex gap-4">
                                <button class="edit-btn btn-sm" onclick="window.app.selectTrain('${train.id}')">Edit</button>
                                <button class="btn btn-sm btn-danger btn-danger-solid delete-train-btn" data-train-id="${train.id}">Delete</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    getTrainStateVisualClass(state) {
        const map = {
            off: 'train-state-off',
            idle: 'train-state-idle',
            standby: 'train-state-standby',
            running_moving: 'train-state-running',
            running_at_station: 'train-state-station'
        };
        return map[state] || 'train-state-off';
    }

    getHandleVisualClass(handlePosition) {
        if (handlePosition?.startsWith('P')) return 'handle-power';
        if (handlePosition?.startsWith('B') || handlePosition === 'EB') return 'handle-brake';
        return 'handle-neutral';
    }

    getObcStateClass(powerOn, emergencyActive) {
        if (!powerOn) return 'obc-power-off';
        return emergencyActive ? 'obc-power-emergency' : 'obc-power-on';
    }

    _replaceVisualClass(element, allowedClasses, nextClass) {
        if (!element) return;
        allowedClasses.forEach(cls => element.classList.remove(cls));
        if (nextClass) {
            element.classList.add(nextClass);
        }
    }

    /**
     * Render the cab speed readout: "<big current>/<small max> km/h".
     * Speed shows one decimal under 10 km/h (slow-moving precision matters),
     * integer otherwise.
     */
    _formatSpeedReadout(currentSpeed, maxSpeed) {
        const v = Number(currentSpeed) || 0;
        const formatted = v < 10 ? v.toFixed(1) : Math.round(v).toString();
        return `<span class="tc-speed-current">${formatted}</span><span class="tc-speed-max">/${maxSpeed}</span><span class="tc-speed-unit">km/h</span>`;
    }

    /**
     * Format ATO ETA. Prefers controller.time_to_target_sec (a backend
     * integration of the operational_limit speed curve to the stop point).
     * Falls back to a coarse distance/speed estimate when the curve isn't
     * published. Output anchors the duration with a sim-clock arrival so
     * the operator can sanity-check it against the displayed simulation
     * time without doing the math.
     */
    _formatATOEta(controller, clearanceCm, currentSpeed) {
        const ato = controller?.time_to_target_sec;
        let etaSec = null;
        if (ato != null && Number.isFinite(ato) && ato > 0) {
            etaSec = ato;
        } else {
            // Fallback: distance-over-speed estimate using the path-end point
            // from the lookahead list and the ATO target speed.  Both distance
            // (model cm) and speed (prototype km/h) need the same conversion
            // factor that the backend uses, exposed as speed_conversion_factor.
            const convFactor = controller?.speed_conversion_factor;
            const lookahead = controller?.lookahead ?? [];
            const pathEnd = lookahead.findLast?.(lp => lp.is_path_end)
                ?? [...lookahead].reverse().find(lp => lp.is_path_end);
            const distCm = pathEnd?.distance_cm ?? clearanceCm;
            const atoSpeed = controller?.target_speed;
            const speed = (atoSpeed != null && atoSpeed > 0.5) ? atoSpeed : currentSpeed;
            if (distCm != null && Number.isFinite(distCm) && speed > 0.5 && convFactor > 0) {
                etaSec = distCm / (speed * convFactor);
            }
        }
        if (etaSec == null) return '—';

        const duration = this._formatDuration(etaSec);
        const simTime = window.stateManager?.simulationTime;
        if (typeof simTime === 'number' && Number.isFinite(simTime)) {
            const arrival = this._formatSimClock(simTime + etaSec);
            return `${arrival} (${duration})`;
        }
        return duration;
    }

    _formatDuration(etaSec) {
        if (etaSec < 1) return '<1s';
        if (etaSec < 60) return `${Math.round(etaSec)}s`;
        const mins = Math.floor(etaSec / 60);
        const secs = Math.round(etaSec % 60);
        return `${mins}m ${secs}s`;
    }

    _formatSimClock(totalSec, { subsecond = false } = {}) {
        const t = Math.max(0, totalSec);
        const h = Math.floor(t / 3600);
        const m = Math.floor((t % 3600) / 60);
        const s = Math.floor(t % 60);
        const hh = String(h).padStart(2, '0');
        const mm = String(m).padStart(2, '0');
        const ss = String(s).padStart(2, '0');
        if (subsecond) {
            const ds = Math.floor((t % 1) * 10);
            return `${hh}:${mm}:${ss}.${ds}`;
        }
        return `${hh}:${mm}:${ss}`;
    }

    /**
     * Middle-ellipsis truncation. "PATH_DEMO_TRAIN_TO_STATION_EAST" with
     * max=22 → "PATH_DEMO_TR…ION_EAST" — keeps both ends visible so the
     * driver can still recognize the prefix and the destination suffix.
     */
    _middleTruncate(str, max) {
        if (!str) return '';
        const s = String(str);
        if (s.length <= max) return s;
        const keep = max - 1;          // reserve 1 char for the ellipsis
        const head = Math.ceil(keep / 2);
        const tail = Math.floor(keep / 2);
        return `${s.slice(0, head)}…${s.slice(-tail)}`;
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
                const confirmed = await showConfirmModal({
                    title: 'Delete Train',
                    message: `Delete train "${trainId}"?`,
                    detail: 'This action cannot be undone.',
                    confirmText: 'Delete',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;

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
                const confirmed = await showConfirmModal({
                    title: 'Delete Schedule',
                    message: `Delete schedule "${scheduleId}"?`,
                    detail: 'All stops in this schedule will be removed.',
                    confirmText: 'Delete',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;

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
        let controller;
        let dispatcher;
        try {
            const trainResp = await this.api.getTrain(trainId);
            train = trainResp?.train;
            controller = trainResp?.controller;
            dispatcher = trainResp?.dispatcher_progress;
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

        const trainState = controller?.state || 'off';
        const trainStateClass = this.getTrainStateVisualClass(trainState);

        // Get path and destination info for ATO section
        const onboardAto = controller?.ato_info;
        const assignedPathId = onboardAto?.path_id || controller?.assigned_path_id;
        let pathName = 'No path';
        let destinationName = 'No path';

        if (assignedPathId) {
            try {
                const path = await this.api.getPath(assignedPathId);
                if (path) {
                    pathName = path.path_id || assignedPathId;
                    // Get destination: prefer station name, fallback to block name
                    const destinationBlockId =
                        path.authority_end_block_id
                        || path.requested_destination_block_id;
                    if (path.end_platform_id || path.end_station_id) {
                        const destinationPlatformId = path.end_platform_id || path.end_station_id;
                        const platform = await this.api.getPlatform(destinationPlatformId);
                        destinationName = platform?.name || destinationPlatformId;
                    } else if (destinationBlockId) {
                        const block = await this.api.getBlock(destinationBlockId);
                        // Check if block belongs to a station, show station name if so
                        if (block?.station_id) {
                            const station = await this.api.getStation(block.station_id);
                            destinationName = station?.name || block?.name || destinationBlockId;
                        } else {
                            destinationName = block?.name || destinationBlockId;
                        }
                    }
                }
            } catch (err) {
                console.warn('[TrainEditorHandler] Failed to fetch path info:', err);
                window.showToast('Could not load path details — showing cached data', 'warning', 5000);
                pathName = onboardAto?.path_id || assignedPathId;
                destinationName = onboardAto?.destination_station_name || onboardAto?.destination_block_id || 'No path';
            }
        } else {
            // No path assigned - check if controller has destination info anyway
            if (onboardAto?.destination_station_name) {
                destinationName = onboardAto.destination_station_name;
            } else if (onboardAto?.destination_block_id) {
                destinationName = onboardAto.destination_block_id;
            }
        }

        // Use Controller state for mode determination
        const controlMode = controller?.control_mode || 'ato';
        const isATO = controlMode === 'ato';
        const isManual = controlMode === 'manual';
        const isStopped = controller?.is_stopped ?? true;
        const canSwitchModes = isStopped;
        const handlePosition = controller?.handle_position || 'N';
        const powerOn = controller?.power_on ?? false;
        const emergencyActive = controller?.eb_active ?? false;
        const atoRunning = controller?.ato_running ?? false;
        const handleClass = this.getHandleVisualClass(handlePosition);
        const obcStateClass = this.getObcStateClass(powerOn, emergencyActive);
        const manualModeType = controller?.manual_mode_type || 'simulation';
        const isHandleMode = manualModeType === 'simulation';
        const isPowerPercent = manualModeType === 'power_percent';
        const manualPowerPercent = controller?.manual_power_percent ?? 0;

        // Handle position order for display
        const handleOrder = ['P5', 'P4', 'P3', 'P2', 'P1', 'N', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'EB'];
        const handleIndex = handleOrder.indexOf(handlePosition);

        const headPos = (controller?.span && controller.span.end_a)
            ? (controller.span.leading_end === 'a' ? controller.span.end_a.tile : controller.span.end_b.tile)
            : null;
        const posStr = headPos ? `(${headPos[0]}, ${headPos[1]})` : '—';

        // Span end-A / end-B details for the Status zone (formerly Debug).
        const span = controller?.span || null;
        const endA = span?.end_a || null;
        const endB = span?.end_b || null;
        const leadingA = span?.leading_end === 'a';
        const endATileStr = endA?.tile ? `(${endA.tile[0]}, ${endA.tile[1]})` : '—';
        const endBTileStr = endB?.tile ? `(${endB.tile[0]}, ${endB.tile[1]})` : '—';
        const endADistStr = endA && Number.isFinite(endA.distance_cm) ? `${endA.distance_cm.toFixed(1)} cm` : '—';
        const endBDistStr = endB && Number.isFinite(endB.distance_cm) ? `${endB.distance_cm.toFixed(1)} cm` : '—';
        const endAEdgeStr = endA?.entry_edge || '—';
        const endBEdgeStr = endB?.entry_edge || '—';

        // Path index (relocated from Debug to Task zone).
        const pathIndexStr = (dispatcher?.current_resource_index != null && Array.isArray(dispatcher?.resource_sequence) && dispatcher.resource_sequence.length > 0)
            ? `${dispatcher.current_resource_index + 1} / ${dispatcher.resource_sequence.length}`
            : '—';

        // Simulation clock — sourced from StateManager's network-wide scalar.
        const simTimeRaw = window.stateManager?.simulationTime;
        const simClockStr = (typeof simTimeRaw === 'number' && Number.isFinite(simTimeRaw))
            ? this._formatSimClock(simTimeRaw, { subsecond: true })
            : '—';

        const handleDescription =
            handlePosition === 'N' ? 'Coast (no power, no braking)' :
            handlePosition.startsWith('P') ? `Power ${handlePosition.slice(1)} — Accelerating` :
            handlePosition === 'EB' ? 'Emergency Brake' :
            `Brake ${handlePosition.slice(1)} — Decelerating`;

        // ATO speed metrics
        const currentSpeed = (controller?.current_speed ?? 0);
        const targetSpeed = controller?.target_speed ?? null;
        const atcLimit = controller?.atc_speed_limit ?? null;
        const blockLimit = controller?.current_block_speed_limit ?? null;
        const maxSpeed = train.max_speed || 100;

        // ATO destination metrics
        const clearanceCm = controller?.clearance_ahead_cm ?? null;
        const distanceM = clearanceCm != null && Number.isFinite(clearanceCm)
            ? (clearanceCm / 100).toFixed(1)
            : '—';
        // Backend integrates the operational_limit speed curve to give us a
        // proper ATO ETA. Fall back to a coarse distance/speed estimate only
        // when the curve isn't published.
        const etaText = this._formatATOEta(controller, clearanceCm, currentSpeed);

        // Route metadata. The backend composes route_run_id as
        // `${route_id}#${run_number}`. Plan ID is the route_id portion;
        // Run ID is the suffix instance number — surface them separately.
        const fullRunId = controller?.route_run_id || null;
        const planId = fullRunId ? fullRunId.split('#')[0] : (train.assigned_route_id || null);
        const runId = fullRunId ? `#${fullRunId.split('#')[1] || ''}` : null;
        const requiredCab = controller?.ato_info?.required_cab || '—';
        const pathHeading = controller?.ato_info?.path_heading || '—';
        const truncatedPath = pathName ? this._middleTruncate(pathName, 22) : '—';

        // Speed display — current value + max as a small "/N" suffix.
        const speedHtml = this._formatSpeedReadout(currentSpeed, maxSpeed);

        // Speed-limit reason banner content (also seen in train monitor footer)
        const limitReason = controller?.speed_limit_reason || '';

        const content = `
            <div class="tc-panel train-control-panel" data-train-id="${trainId}">

                <!-- ZONE 1: TASK -->
                <section class="tc-section tc-section-task ${obcStateClass}">
                    <header class="tc-status-headline">
                        <span class="tc-train-id train-id">${trainId}</span>
                        <span class="tc-state-badge train-state-display ${trainStateClass}">${trainState.toUpperCase().replace('_', ' ')}</span>
                        <span id="obc-ato-running-badge" class="tc-running-badge ${atoRunning ? '' : 'hidden'}">▶ ATO</span>
                        <span id="train-eb-badge" class="tc-eb-badge tc-status-emergency ${emergencyActive ? '' : 'hidden'}">EB ACTIVE</span>
                    </header>
                    <dl class="tc-status-fields">
                        <div class="tc-field">
                            <dt class="tc-field-label">Plan ID</dt>
                            <dd class="tc-field-value tc-val-info" id="train-plan-id-val" title="${planId || ''}">${planId || '—'}</dd>
                        </div>
                        <div class="tc-field">
                            <dt class="tc-field-label">Run ID</dt>
                            <dd class="tc-field-value" id="train-run-id-val" title="${runId || ''}">${runId ? this._middleTruncate(runId, 22) : '—'}</dd>
                        </div>
                        <div class="tc-field">
                            <dt class="tc-field-label">Path</dt>
                            <dd class="tc-field-value train-ato-path-display" id="train-path-id-val" title="${pathName || ''}">${truncatedPath}</dd>
                        </div>
                        <div class="tc-field">
                            <dt class="tc-field-label">Path Index</dt>
                            <dd class="tc-field-value" id="train-path-index-val">${pathIndexStr}</dd>
                        </div>
                        <div class="tc-field tc-field-wide">
                            <dt class="tc-field-label">Next Destination</dt>
                            <dd class="tc-field-value train-ato-destination-display" title="${destinationName || ''}">${destinationName}</dd>
                        </div>
                    </dl>
                </section>

                <!-- ZONE 2: TRAIN STATUS — HMI (cab-style display) -->
                <!--
                  3-column grid placed by grid-template-areas (see train-control.css).
                  Centre column holds the hero speed (1/3 width, 3 rows tall); left and
                  right columns flow side info above / beside / below it. The rows-N
                  classes only control font-size; placement comes from named areas.
                -->
                <section class="tc-section tc-section-status">
                    <div class="tc-hmi">
                        <!-- Top header row (full width, NOT part of the speed-hero grid): time left, train ID right -->
                        <div class="tc-hmi-header">
                            <div id="hmi-time" class="rows-1 tc-hmi-time">${simClockStr}</div>
                            <div id="hmi-train-id" class="rows-1 tc-hmi-meta">${trainId}${train.car_number ? ` · ${train.car_number} cars` : ''}</div>
                        </div>

                        <!-- EB alert (full width, conditional, sits above the speed grid when active) -->
                        <div id="hmi-eb-alert" class="rows-1 tc-hmi-alert tc-alert-eb ${emergencyActive ? '' : 'hidden'}">EB ACTIVE</div>

                        <!-- 3-column grid: speed hero (1/3 center) | ATC/ATO right | handle left bottom | cab-run right bottom -->
                        <div class="tc-hmi-grid">
                            <div id="hmi-speed" class="col-full rows-3 tc-hmi-speed train-speed-display">${Number.isFinite(currentSpeed) ? Math.round(currentSpeed) : '—'}</div>
                            <div id="hmi-atc" class="col-r rows-1 tc-hmi-atc">ATC ${atcLimit != null && Number.isFinite(atcLimit) ? Math.round(atcLimit) : '—'}</div>
                            <div id="hmi-ato" class="col-r rows-1 tc-hmi-ato ${isATO ? '' : 'hidden'}">ATO ${targetSpeed != null && Number.isFinite(targetSpeed) ? Math.round(targetSpeed) : '—'}</div>
                            <div id="hmi-dwell-remaining" class="tc-hmi-dwell-rem hidden"></div>
                            <div id="hmi-dwell-until" class="tc-hmi-dwell-until hidden"></div>
                            <div id="hmi-handle" class="col-l rows-2 tc-hmi-handle ${handleClass}">${handlePosition}</div>
                            <div id="hmi-cab-run" class="col-r rows-1 tc-hmi-cab-run">CAB ${controller?.cab || '—'}${runId ? ` · RUN ${runId}` : ''}</div>
                        </div>

                        <!-- Footer (full width): next stop (L) · distance (center) · ETA (R) -->
                        <div class="tc-hmi-footer">
                            <span id="hmi-next-stop" class="hmi-footer-l" title="${destinationName || ''}">${destinationName && destinationName !== 'No path' ? `Next: ${destinationName}` : ''}</span>
                            <span id="hmi-distance" class="hmi-footer-c">${clearanceCm != null && Number.isFinite(clearanceCm) ? `${Math.round(clearanceCm / 100)} m` : '—'}</span>
                            <span id="hmi-eta" class="hmi-footer-r">${etaText && etaText !== '—' ? `ETA ${etaText}` : ''}</span>
                        </div>
                    </div>
                </section>

                <!-- ZONE 3: CONTROLS -->
                <section class="tc-section tc-section-controls">
                    <div class="tc-section-title">Controls</div>
                    <div class="tc-controls-pair">
                        <div class="tc-row">
                            <span class="tc-row-label">Power</span>
                            <div class="tc-segment" role="group">
                                <button type="button" id="train-power-on-btn" class="tc-seg-btn switch-btn ${powerOn ? 'active' : ''}" ${powerOn ? 'disabled' : ''}>On</button>
                                <button type="button" id="train-power-off-btn" class="tc-seg-btn switch-btn ${!powerOn ? 'active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>Off</button>
                            </div>
                        </div>
                        <div class="tc-row">
                            <span class="tc-row-label">Mode</span>
                            <div class="tc-segment" role="group">
                                <button type="button" id="train-mode-ato-btn" class="tc-seg-btn switch-btn ${isATO ? 'active' : ''}" ${!canSwitchModes ? 'disabled' : ''}>ATO</button>
                                <button type="button" id="train-mode-manual-btn" class="tc-seg-btn switch-btn ${isManual ? 'active' : ''}" ${!canSwitchModes ? 'disabled' : ''}>Manual</button>
                            </div>
                        </div>
                    </div>
                    <div class="tc-row tc-row-cab">
                        <span class="tc-row-label">Cab</span>
                        <div class="tc-segment" role="group">
                            <button type="button" id="train-cab-a-btn" class="tc-seg-btn switch-btn ${controller?.cab === 'A' ? 'active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>A</button>
                            <button type="button" id="train-cab-off-btn" class="tc-seg-btn switch-btn ${!controller?.cab ? 'active' : ''}" ${!powerOn || !isStopped || !controller?.cab ? 'disabled' : ''}>Off</button>
                            <button type="button" id="train-cab-b-btn" class="tc-seg-btn switch-btn ${controller?.cab === 'B' ? 'active' : ''}" ${!powerOn || !isStopped ? 'disabled' : ''}>B</button>
                        </div>
                        <button id="train-monitor-btn" type="button" class="tc-btn tc-btn-ghost tc-monitor-btn">Monitor →</button>
                    </div>

                    <!-- ATO Go/Stop (visible when control_mode==='ato') -->
                    <div id="ato-controls" class="tc-branch tc-ato-display ${isATO ? '' : 'hidden'}">
                        <div class="tc-actions tc-ato-actions">
                            <button id="train-ato-go-btn" class="tc-btn tc-btn-success tc-btn-hero" ${!powerOn || atoRunning ? 'disabled' : ''}>
                                <span class="tc-btn-glyph">▶</span><span class="tc-btn-text">Go</span>
                            </button>
                            <button id="train-ato-stop-btn" class="tc-btn tc-btn-danger tc-btn-hero" ${!atoRunning ? 'disabled' : ''}>
                                <span class="tc-btn-glyph">■</span><span class="tc-btn-text">Stop</span>
                            </button>
                        </div>
                    </div>

                    <!-- Manual rows (visible when control_mode==='manual') -->
                    <div id="manual-controls" class="tc-branch tc-manual-display ${isManual ? '' : 'hidden'}">
                        <div class="tc-row">
                            <span class="tc-row-label">Input</span>
                            <div class="tc-segment" role="group" aria-label="Manual input mode">
                                <button type="button" id="train-manual-mode-handle-btn" class="tc-seg-btn ${isHandleMode ? 'active' : ''}" ${!isManual || !isStopped ? 'disabled' : ''}>Handle</button>
                                <button type="button" id="train-manual-mode-power-btn" class="tc-seg-btn ${isPowerPercent ? 'active' : ''}" ${!isManual || !isStopped ? 'disabled' : ''}>Power %</button>
                            </div>
                        </div>

                        <div id="handle-mode-block" class="tc-handle-block ${isHandleMode ? '' : 'hidden'}">
                            <div class="tc-handle-zones" aria-hidden="true">
                                <span class="tc-handle-zone tc-zone-power">POWER</span>
                                <span class="tc-handle-zone tc-zone-neutral">N</span>
                                <span class="tc-handle-zone tc-zone-brake">BRAKE</span>
                                <span class="tc-handle-zone tc-zone-eb">EB</span>
                            </div>
                            <div class="tc-handle-ticks" aria-hidden="true">
                                ${handleOrder.map((p, i) => `<span class="tc-handle-tick ${i === handleIndex ? 'active' : ''}" data-handle="${p}">${p}</span>`).join('')}
                            </div>
                            <input type="range" id="train-handle-slider" class="tc-handle-slider"
                                min="0" max="14" step="1" value="${handleIndex >= 0 ? handleIndex : 5}"
                                ${!isManual || emergencyActive ? 'disabled' : ''}>
                            <div class="tc-handle-readout">
                                <span id="handle-position-label" class="tc-handle-pos handle-position-display ${handleClass}">${handlePosition}</span>
                                <span id="handle-description" class="tc-handle-desc handle-description">${handleDescription}</span>
                                <span id="handle-reason" class="tc-handle-reason train-handle-reason">${controller?.handle_reason || ''}</span>
                            </div>
                        </div>

                        <div id="power-percent-block" class="tc-power-block ${isPowerPercent ? '' : 'hidden'}">
                            <div class="tc-power-readout">
                                <span class="tc-power-value" id="train-power-percent-val">${manualPowerPercent}</span>
                                <span class="tc-power-unit">%</span>
                            </div>
                            <input type="range" id="train-power-percent-slider" class="tc-power-slider"
                                min="0" max="100" step="1" value="${manualPowerPercent}"
                                ${!isManual || !isPowerPercent || emergencyActive ? 'disabled' : ''}>
                            <div class="tc-power-bar">
                                <div class="tc-power-fill" id="train-power-percent-fill" style="width: ${manualPowerPercent}%"></div>
                            </div>
                            <div class="tc-power-hint">Direct throttle command (0–100). Set Cab + Travel direction first.</div>
                        </div>
                    </div>

                    <!-- EMERGENCY -->
                    <div class="tc-emergency-row">
                        <button id="train-emergency-btn" class="tc-eb-btn" type="button">EMERGENCY STOP</button>
                        <button id="train-emergency-reset-btn" class="tc-eb-reset-btn ${emergencyActive ? '' : 'hidden'}" type="button">Reset</button>
                    </div>
                </section>

                <!-- ZONE 4: DEBUG -->
                <section class="tc-section tc-section-debug">
                    <div class="tc-section-title">Debug</div>
                    <!-- Content injected by app.js:updateTrainDebugPanel.
                         Visibility gated by #debug-show-train-position checkbox. -->
                    <div id="train-control-debug-section" class="train-control-debug-section hidden"></div>

                    <!-- Train geometry + read-only properties (moved out of Status HMI).
                         Gated by the same #debug-show-train-position checkbox. -->
                    <div id="train-debug-geometry" class="train-control-debug-section hidden">
                        <div class="tc-section-subtitle">Train geometry</div>
                        <dl class="tc-status-fields tc-span-grid">
                            <div class="tc-field">
                                <dt class="tc-field-label" id="train-span-end-a-label">End A ${leadingA ? '(HEAD)' : '(TAIL)'}</dt>
                                <dd class="tc-field-value train-pos-display" id="train-span-end-a-tile">${endATileStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label" id="train-span-end-b-label">End B ${leadingA ? '(TAIL)' : '(HEAD)'}</dt>
                                <dd class="tc-field-value" id="train-span-end-b-tile">${endBTileStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End A dist</dt>
                                <dd class="tc-field-value" id="train-span-end-a-dist">${endADistStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End B dist</dt>
                                <dd class="tc-field-value" id="train-span-end-b-dist">${endBDistStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End A edge</dt>
                                <dd class="tc-field-value" id="train-span-end-a-edge">${endAEdgeStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">End B edge</dt>
                                <dd class="tc-field-value" id="train-span-end-b-edge">${endBEdgeStr}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Facing</dt>
                                <dd class="tc-field-value train-dir-display">${train.facing_direction || '—'}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Block</dt>
                                <dd class="tc-field-value">${train.block_section_id || '—'}</dd>
                            </div>
                        </dl>
                        <div class="tc-section-subtitle">Properties (read-only)</div>
                        <dl class="tc-status-fields">
                            <div class="tc-field">
                                <dt class="tc-field-label">Car #</dt>
                                <dd class="tc-field-value" id="train-status-car-val">${train.car_number || '—'}</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Length</dt>
                                <dd class="tc-field-value" id="train-status-length-val">${train.length_cm || 0} cm</dd>
                            </div>
                            <div class="tc-field">
                                <dt class="tc-field-label">Max Speed</dt>
                                <dd class="tc-field-value" id="train-status-maxspeed-val">${train.max_speed || 100} km/h</dd>
                            </div>
                        </dl>
                    </div>

                    <!-- Editable properties form (bottom of panel) -->
                    <details class="tc-disclosure">
                        <summary>Edit Properties</summary>
                        <div class="tc-disclosure-body">
                            <div class="tc-row">
                                <label class="tc-row-label" for="train-car-number-input">Car #</label>
                                <input type="text" id="train-car-number-input" value="${train.car_number || ''}" placeholder="Car number">
                            </div>
                            <div class="tc-row">
                                <label class="tc-row-label" for="train-length-input">Length</label>
                                <input type="number" id="train-length-input" value="${train.length_cm || 0}" min="0" step="10">
                            </div>
                            <div class="tc-row">
                                <label class="tc-row-label" for="train-max-speed-input">Max Speed</label>
                                <input type="number" id="train-max-speed-input" value="${train.max_speed || 100}" min="1" max="500">
                            </div>
                            <div class="tc-footer">
                                <button id="save-train-btn" class="tc-btn tc-btn-primary">Save</button>
                            </div>
                        </div>
                    </details>
                </section>
            </div>
        `;

        this.editor.showSecondarySidebar('🚂 Train Control', content);

        // Check debug checkbox immediately and update visibility
        const debugCheckbox = document.getElementById('debug-show-train-position');
        const debugSection = document.getElementById('train-control-debug-section');
        const debugGeometry = document.getElementById('train-debug-geometry');
        if (debugCheckbox && debugCheckbox.checked) {
            if (debugSection) debugSection.classList.remove('hidden');
            if (debugGeometry) debugGeometry.classList.remove('hidden');
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

                if (!refreshOnSuccess) return;

                // Train mutation endpoints return the freshly-updated train,
                // controller, and dispatcher progress in the same shape as
                // _train_to_dict(). Push it into StateManager directly so the
                // existing subscription patches the panel on the same frame —
                // a refreshNetwork() round-trip can lose the race against the
                // next sim tick and leave the panel showing stale values
                // (the "Cab needs two clicks" bug).
                if (result && (result.controller || result.train)) {
                    if (result.train) {
                        window.stateManager.updateEntity('trains', trainId, result.train);
                    }
                    if (result.controller) {
                        window.stateManager.updateEntity('controllers', trainId, result.controller);
                    }
                    if (result.dispatcher_progress !== undefined) {
                        window.stateManager.updateEntity(
                            'dispatcherProgress', trainId, result.dispatcher_progress
                        );
                    }
                    return;
                }

                // Fallback for endpoints that do not echo the post-mutation
                // state (shouldn't happen in v1, but keep us correct).
                await window.app.refreshNetwork();
            } catch (err) {
                console.error(`[TrainControl] ${actionName} failed:`, err);
                window.showToast(`${actionName}: ${err.message}`, 'error', 5000);
            }
        };

        const waitForTrainStopped = async (timeoutMs = 8000, intervalMs = 250) => {
            const start = Date.now();
            while (Date.now() - start < timeoutMs) {
                try {
                    const trainResp = await this.api.getTrain(trainId);
                    if (trainResp?.controller?.is_stopped) {
                        return true;
                    }
                } catch (err) {
                    console.warn('[TrainControl] Failed to poll controller state:', err);
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
            // Power-off is latched in v1: the OFF transition lands one or
            // two ticks later, after Phase 3 consumes pending_power_off.
            // The next /api/network poll fires the controller subscription
            // and reconciles the panel — no manual deferred refresh needed.
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
                    this._replaceVisualClass(
                        handleLabel,
                        ['handle-power', 'handle-brake', 'handle-neutral'],
                        this.getHandleVisualClass(position)
                    );
                }
                if (handleDesc) {
                    if (position === 'N') {
                        handleDesc.textContent = 'Coast (no power, no braking)';
                    } else if (position.startsWith('P')) {
                        handleDesc.textContent = `Power ${position.slice(1)} — Accelerating`;
                    } else if (position === 'EB') {
                        handleDesc.textContent = 'Emergency Brake';
                    } else {
                        handleDesc.textContent = `Brake ${position.slice(1)} — Decelerating`;
                    }
                }
                // Light up the matching tick label
                document.querySelectorAll('.tc-handle-tick').forEach((el, idx) => {
                    el.classList.toggle('active', idx === parseInt(handleSlider.value));
                });
            };

            // Commit on release
            handleSlider.onchange = async () => {
                const position = handleOrder[parseInt(handleSlider.value)];
                await executeAction(`Set Handle ${position}`,
                    () => window.api.setHandle(trainId, position),
                    false);  // Don't full refresh - just update OBC display

                // Quick update of OBC display
                try {
                    const trainResp = await window.api.getTrain(trainId);
                    this._updateControllerDisplay(trainResp?.controller);
                } catch (e) {
                    // Ignore errors here, next poll will update
                }
            };
        }

        // ═══════════════════════════════════════════════════════════════════
        // MANUAL MODE-TYPE TOGGLE (Handle / Power %)
        // ═══════════════════════════════════════════════════════════════════

        const manualModeHandleBtn = document.getElementById('train-manual-mode-handle-btn');
        const manualModePowerBtn = document.getElementById('train-manual-mode-power-btn');

        if (manualModeHandleBtn) {
            manualModeHandleBtn.onclick = () => executeAction('Set Input: Handle',
                () => window.api.setManualModeType(trainId, 'simulation'));
        }
        if (manualModePowerBtn) {
            manualModePowerBtn.onclick = () => executeAction('Set Input: Power %',
                () => window.api.setManualModeType(trainId, 'power_percent'));
        }

        // ═══════════════════════════════════════════════════════════════════
        // POWER-PERCENT SLIDER (manual_mode_type == 'power_percent')
        // ═══════════════════════════════════════════════════════════════════

        const powerSlider = document.getElementById('train-power-percent-slider');
        const powerVal = document.getElementById('train-power-percent-val');
        const powerFill = document.getElementById('train-power-percent-fill');

        if (powerSlider) {
            powerSlider.oninput = () => {
                const pct = parseInt(powerSlider.value, 10);
                if (powerVal) powerVal.textContent = pct;
                if (powerFill) powerFill.style.width = `${pct}%`;
            };
            powerSlider.onchange = async () => {
                const pct = parseInt(powerSlider.value, 10);
                await executeAction(`Set Power ${pct}%`,
                    () => window.api.setManualPowerPercent(trainId, pct),
                    false);
            };
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
        const maxSpeedInput = document.getElementById('train-max-speed-input');
        const lengthInput = document.getElementById('train-length-input');
        const carNumberInput = document.getElementById('train-car-number-input');

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
                if (carNumberInput) {
                    updates.car_number = carNumberInput.value;
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
        const unsubTrain = window.stateManager.subscribe(
            'trains',
            trainId,
            (newTrain, oldTrain) => this._onTrainUpdate(newTrain, oldTrain),
            { immediate: false }  // Don't call immediately, we already have the data
        );

        // Also subscribe to controller updates so the panel re-renders when
        // runtime fields (state, speed, handle, mode, etc.) change. The new
        // sibling-payload shape places runtime data on the controller record.
        const unsubController = window.stateManager.subscribe(
            'controllers',
            trainId,
            () => {
                const train = window.stateManager.get('trains', trainId);
                if (train) {
                    this._onTrainUpdate(train, train);
                }
            },
            { immediate: false }
        );

        // Path Index (Task zone) is sourced from dispatcher progress.
        // Subscribe so the index updates when the path advances.
        const unsubDispatcher = window.stateManager.subscribe(
            'dispatcherProgress',
            trainId,
            () => {
                const train = window.stateManager.get('trains', trainId);
                if (train) {
                    this._onTrainUpdate(train, train);
                }
            },
            { immediate: false }
        );

        // Keep the HMI clock live even when the train is parked and no entity
        // data changes between poll cycles.
        const unsubClock = window.stateManager.subscribeSimClock((simTime) => {
            const panel = document.querySelector(`.tc-panel[data-train-id="${trainId}"]`);
            const simTimeEl = panel?.querySelector('#hmi-time');
            if (simTimeEl) {
                simTimeEl.textContent = this._formatSimClock(simTime, { subsecond: true });
            }
        });

        // Self-contained dwell poll: the HMI's dwell rows need data that lives
        // on /api/trains/<id>/lookahead. The panel owns this fetch directly
        // rather than reading window.app.lastLookaheadPayload, which is the
        // Train Monitor's cache — coupling the two panels would mean closing
        // the monitor would silently empty the dwell display.
        this._dwellLookahead = null;
        const dwellPoll = setInterval(async () => {
            if (this._trainId !== trainId) return;
            try {
                const payload = await this.api.getTrainLookahead(trainId);
                this._dwellLookahead = payload;
                const train = window.stateManager.get('trains', trainId);
                if (train) this.updateControlPanelValuesFromData(train);
            } catch (e) { /* ignore transient errors */ }
        }, 250);

        this._unsubscribe = () => {
            try { unsubTrain && unsubTrain(); } catch (e) { /* ignore */ }
            try { unsubController && unsubController(); } catch (e) { /* ignore */ }
            try { unsubDispatcher && unsubDispatcher(); } catch (e) { /* ignore */ }
            try { unsubClock && unsubClock(); } catch (e) { /* ignore */ }
            try { clearInterval(dwellPoll); } catch (e) { /* ignore */ }
            this._dwellLookahead = null;
        };
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
     * Update Controller display elements without full panel refresh.
     * Used for quick updates after handle changes.
     * @private
     */
    _updateControllerDisplay(controller) {
        if (!controller) return;

        const handlePosition = controller.handle_position || 'N';
        const powerOn = controller.power_on ?? false;
        const emergencyActive = controller.eb_active ?? false;
        const controlMode = controller.control_mode || 'ato';
        const atoRunning = controller.ato_running ?? false;

        // Update OBC status display
        const powerDisplay = document.querySelector('.obc-power-display');
        const modeDisplay = document.querySelector('.obc-mode-display');
        const handleDisplay = document.querySelector('.obc-handle-display');

        if (powerDisplay) {
            powerDisplay.textContent = powerOn ? 'ON' : 'OFF';
            this._replaceVisualClass(powerDisplay, ['power-on', 'power-off'], powerOn ? 'power-on' : 'power-off');
        }

        if (modeDisplay) {
            modeDisplay.textContent = controlMode.toUpperCase();
            this._replaceVisualClass(modeDisplay, ['mode-manual', 'mode-ato'], controlMode === 'manual' ? 'mode-manual' : 'mode-ato');
        }

        if (handleDisplay) {
            handleDisplay.textContent = handlePosition;
            this._replaceVisualClass(handleDisplay, ['handle-power', 'handle-brake', 'handle-neutral'], this.getHandleVisualClass(handlePosition));
        }

        const atoBadge = document.querySelector('#obc-ato-running-badge');
        if (atoBadge) {
            if (atoRunning) {
                atoBadge.textContent = '▶ ATO RUNNING';
                atoBadge.classList.remove('hidden');
                atoBadge.classList.add('badge', 'badge-success', 'text-xs');
            } else {
                atoBadge.textContent = '';
                atoBadge.classList.add('hidden');
                atoBadge.classList.remove('badge', 'badge-success', 'text-xs');
            }
        }

        // Update speed display
        const speedDisplay = document.querySelector('.train-speed-display');
        if (speedDisplay && controller.current_speed !== undefined) {
            const maxSpeed = 100;  // Would need train data for actual max
            speedDisplay.innerHTML = `${controller.current_speed.toFixed(1)} <span class="text-muted">km/h</span>`;
        }
    }

    async updateTrainControlPanelValues(trainId) {
        const panel = document.querySelector(`.train-control-panel[data-train-id="${trainId}"]`);
        if (!panel) return;

        let train;
        try {
            const trainResp = await this.api.getTrain(trainId);
            train = trainResp?.train;
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

        const controller = window.stateManager.getController(train.id) || null;
        const trainState = controller?.state || 'off';

        const stateEl = panel.querySelector('.train-state-display');
        if (stateEl) {
            this._replaceVisualClass(
                stateEl,
                ['train-state-off', 'train-state-idle', 'train-state-standby', 'train-state-running', 'train-state-waiting', 'train-state-station', 'train-state-manual'],
                this.getTrainStateVisualClass(trainState)
            );
            stateEl.innerHTML = `
                ${trainState.toUpperCase().replace('_', ' ')}
            `;
        }

        // Update position - use controller span
        const positionEl = panel.querySelector('.train-pos-display');
        const span = controller?.span;
        if (positionEl && span && span.end_a) {
            const headEnd = span.leading_end === 'a' ? span.end_a : span.end_b;
            positionEl.textContent = `(${headEnd.tile[0]}, ${headEnd.tile[1]})`;
        }

        const directionEl = panel.querySelector('.train-dir-display');
        if (directionEl) {
            directionEl.textContent = train.facing_direction || 'Unknown';
        }

        const speedEl = panel.querySelector('#hmi-speed');
        if (speedEl) {
            const currentSpeed = controller?.current_speed ?? 0;
            speedEl.textContent = Number.isFinite(currentSpeed) ? Math.round(currentSpeed) : '—';
        }

        if (controller) {
            const powerDisplay = panel.querySelector('.obc-power-display');
            const modeDisplay = panel.querySelector('.obc-mode-display');
            const handleDisplay = panel.querySelector('.obc-handle-display');

            const powerOn = controller.power_on ?? false;
            const controlMode = controller.control_mode || 'ato';
            const handlePosition = controller.handle_position || 'N';
            const atoRunning = controller.ato_running ?? false;

            if (powerDisplay) {
                powerDisplay.textContent = powerOn ? 'ON' : 'OFF';
                this._replaceVisualClass(powerDisplay, ['power-on', 'power-off'], powerOn ? 'power-on' : 'power-off');
            }

            if (modeDisplay) {
                modeDisplay.textContent = controlMode.toUpperCase();
                this._replaceVisualClass(modeDisplay, ['mode-manual', 'mode-ato'], controlMode === 'manual' ? 'mode-manual' : 'mode-ato');
            }

            if (handleDisplay) {
                handleDisplay.textContent = handlePosition;
                this._replaceVisualClass(handleDisplay, ['handle-power', 'handle-brake', 'handle-neutral'], this.getHandleVisualClass(handlePosition));
            }

            const atoBadge = panel.querySelector('#obc-ato-running-badge');
            if (atoBadge) {
                if (atoRunning) {
                    atoBadge.textContent = '▶ ATO RUNNING';
                    atoBadge.classList.remove('hidden');
                    atoBadge.classList.add('badge', 'badge-success', 'text-xs');
                } else {
                    atoBadge.textContent = '';
                    atoBadge.classList.add('hidden');
                    atoBadge.classList.remove('badge', 'badge-success', 'text-xs');
                }
            }

            // Apply OBC accent class to the Task section wrapper.
            const obcBox = panel.querySelector('.tc-section-task');
            if (obcBox) {
                this._replaceVisualClass(obcBox, ['obc-power-off', 'obc-power-on', 'obc-power-emergency'], this.getObcStateClass(powerOn, controller.eb_active ?? false));
            }

            // EB badge (Task zone headline) — toggle visibility on the live
            // eb_active flag instead of relying on a re-render.
            const ebBadge = panel.querySelector('#train-eb-badge');
            if (ebBadge) {
                ebBadge.classList.toggle('hidden', !(controller.eb_active ?? false));
            }

            const pathDisplay = panel.querySelector('#train-path-id-val');
            const destinationDisplays = panel.querySelectorAll('.train-ato-destination-display');
            const resolvedPathId = controller.ato_info?.path_id || controller.assigned_path_id || null;
            if (pathDisplay) {
                pathDisplay.textContent = resolvedPathId
                    ? this._middleTruncate(resolvedPathId, 22)
                    : '—';
                pathDisplay.title = resolvedPathId || '';
            }
            if (destinationDisplays.length) {
                const onboardDestination = controller.ato_info?.destination_station_name || controller.ato_info?.destination_block_id || controller.ato_destination_block_id || null;
                destinationDisplays.forEach((destinationDisplay) => {
                    if (onboardDestination) {
                        destinationDisplay.textContent = onboardDestination;
                        destinationDisplay.title = onboardDestination;
                    } else if (resolvedPathId) {
                        const previousDestination = (destinationDisplay.textContent || '').trim();
                        if (!previousDestination || previousDestination === 'No path') {
                            destinationDisplay.textContent = 'Resolving…';
                        }
                    } else {
                        destinationDisplay.textContent = 'No path';
                        destinationDisplay.title = '';
                    }
                });
            }

            // Plan ID and Run ID — derived from controller.route_run_id
            // (`${route_id}#${run_number}`); fall back to train.assigned_route_id
            // for plan_id when the run hasn't started yet.
            const fullRunId = controller.route_run_id || null;
            const livePlanId = fullRunId ? fullRunId.split('#')[0] : (train.assigned_route_id || null);
            const liveRunSuffix = fullRunId ? `#${fullRunId.split('#')[1] || ''}` : null;
            const planEl = panel.querySelector('#train-plan-id-val');
            if (planEl) {
                planEl.textContent = livePlanId ? this._middleTruncate(livePlanId, 22) : '—';
                planEl.title = livePlanId || '';
            }
            const runEl = panel.querySelector('#train-run-id-val');
            if (runEl) {
                runEl.textContent = liveRunSuffix || '—';
                runEl.title = fullRunId || '';
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
                this._replaceVisualClass(handleLabel, ['handle-power', 'handle-brake', 'handle-neutral'], this.getHandleVisualClass(handlePosition));
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
                handleReason.textContent = controller?.handle_reason || '';
            }

            // ── HMI live-updates: ATC / ATO / distance / ETA / alerts ──
            const atcLimitEl = document.getElementById('hmi-atc');
            if (atcLimitEl) {
                const atc = controller.atc_speed_limit;
                atcLimitEl.textContent = `ATC ${atc != null && Number.isFinite(atc) ? Math.round(atc) : '—'}`;
            }
            const targetSpeedEl = document.getElementById('hmi-ato');
            if (targetSpeedEl) {
                const tgt = controller.target_speed;
                targetSpeedEl.textContent = `ATO ${tgt != null && Number.isFinite(tgt) ? Math.round(tgt) : '—'}`;
                // ATO row only visible when control mode is ATO.
                targetSpeedEl.classList.toggle('hidden', controlMode !== 'ato');
            }

            const distEl = document.getElementById('hmi-distance');
            const etaEl = document.getElementById('hmi-eta');
            const clearance = controller.clearance_ahead_cm;
            const liveSpeed = controller.current_speed ?? 0;
            if (distEl) {
                distEl.textContent = clearance != null && Number.isFinite(clearance)
                    ? `${Math.round(clearance / 100)} m`
                    : '—';
            }
            if (etaEl) {
                const etaTextLive = this._formatATOEta(controller, clearance, liveSpeed);
                etaEl.textContent = etaTextLive && etaTextLive !== '—' ? `ETA ${etaTextLive}` : '';
            }

            // EB alert (HMI, full-width banner). Mirrors controller.eb_active.
            const ebAlertEl = document.getElementById('hmi-eb-alert');
            if (ebAlertEl) {
                ebAlertEl.classList.toggle('hidden', !(controller.eb_active ?? false));
            }

            // ATO vs Manual branch visibility — used to be set only on
            // first paint; now toggled live so the new layout reacts to a
            // single Mode button click without a panel rebuild.
            const atoBranch = panel.querySelector('#ato-controls');
            const manualBranch = panel.querySelector('#manual-controls');
            if (atoBranch) atoBranch.classList.toggle('hidden', controlMode !== 'ato');
            if (manualBranch) manualBranch.classList.toggle('hidden', controlMode !== 'manual');

            // Manual mode-type toggle and slider visibility
            const handleBlock = document.getElementById('handle-mode-block');
            const powerBlock = document.getElementById('power-percent-block');
            const manualModeType = controller.manual_mode_type || 'simulation';
            if (handleBlock && powerBlock) {
                handleBlock.classList.toggle('hidden', manualModeType !== 'simulation');
                powerBlock.classList.toggle('hidden', manualModeType !== 'power_percent');
            }
            const handleToggleBtn = document.getElementById('train-manual-mode-handle-btn');
            const powerToggleBtn = document.getElementById('train-manual-mode-power-btn');
            if (handleToggleBtn) handleToggleBtn.classList.toggle('active', manualModeType === 'simulation');
            if (powerToggleBtn) powerToggleBtn.classList.toggle('active', manualModeType === 'power_percent');

            // Power-percent slider live value (only update when not dragging)
            const powerSlider = document.getElementById('train-power-percent-slider');
            const powerVal = document.getElementById('train-power-percent-val');
            const powerFill = document.getElementById('train-power-percent-fill');
            const pctValue = controller.manual_power_percent ?? 0;
            if (powerSlider && document.activeElement !== powerSlider) {
                powerSlider.value = pctValue;
            }
            if (powerVal) powerVal.textContent = pctValue;
            if (powerFill) powerFill.style.width = `${pctValue}%`;

            // ATO running badge restyle (drop legacy badge classes added by old code)
            const atoBadgeEl = document.querySelector('#obc-ato-running-badge');
            if (atoBadgeEl) {
                if (atoRunning) {
                    atoBadgeEl.textContent = '▶ ATO';
                    atoBadgeEl.classList.remove('hidden', 'badge', 'badge-success', 'text-xs');
                    atoBadgeEl.classList.add('tc-running-badge');
                } else {
                    atoBadgeEl.textContent = '';
                    atoBadgeEl.classList.add('hidden');
                    atoBadgeEl.classList.remove('badge', 'badge-success', 'text-xs');
                }
            }

            // ── HMI read-only cells (time, train id + cars, handle, cab + run) ──
            const simTimeEl = panel.querySelector('#hmi-time');
            if (simTimeEl) {
                const simTime = window.stateManager?.simulationTime;
                simTimeEl.textContent =
                    (typeof simTime === 'number' && Number.isFinite(simTime))
                        ? this._formatSimClock(simTime, { subsecond: true })
                        : '—';
            }
            const dwellRemEl = panel.querySelector('#hmi-dwell-remaining');
            const dwellUntilEl = panel.querySelector('#hmi-dwell-until');
            if (dwellRemEl && dwellUntilEl) {
                const lookahead = this._dwellLookahead;
                const isDwell = lookahead?.current_task_kind === 'dwell';
                const remaining = lookahead?.dwell_remaining_sec ?? null;
                const finishSec = lookahead?.dwell_finish_sim_sec ?? null;
                if (isDwell && remaining !== null) {
                    dwellRemEl.textContent = `${Math.ceil(remaining)}s left`;
                    dwellRemEl.classList.remove('hidden');
                    if (finishSec !== null) {
                        dwellUntilEl.textContent = `${this._formatSimClock(finishSec)} ←`;
                        dwellUntilEl.classList.remove('hidden');
                    } else {
                        dwellUntilEl.classList.add('hidden');
                    }
                } else {
                    dwellRemEl.classList.add('hidden');
                    dwellUntilEl.classList.add('hidden');
                }
            }
            const trainIdEl = panel.querySelector('#hmi-train-id');
            if (trainIdEl) {
                const carCount = train.car_number;
                trainIdEl.textContent = carCount ? `${train.id} · ${carCount} cars` : train.id;
            }
            const handleHmi = panel.querySelector('#hmi-handle');
            if (handleHmi) {
                handleHmi.textContent = handlePosition;
                this._replaceVisualClass(
                    handleHmi,
                    ['handle-power', 'handle-brake', 'handle-neutral'],
                    this.getHandleVisualClass(handlePosition)
                );
            }
            const cabRunEl = panel.querySelector('#hmi-cab-run');
            if (cabRunEl) {
                const cab = controller.cab || '—';
                const liveRunSuffix = (controller.route_run_id || '').includes('#')
                    ? `#${controller.route_run_id.split('#')[1] || ''}`
                    : '';
                cabRunEl.textContent = liveRunSuffix ? `CAB ${cab} · RUN ${liveRunSuffix}` : `CAB ${cab}`;
            }

            // HMI next-stop footer cell needs the "Next: " prefix, so it's not part of
            // the .train-ato-destination-display batch update below.
            const nextStopEl = panel.querySelector('#hmi-next-stop');
            if (nextStopEl) {
                const onboardDestination = controller.ato_info?.destination_station_name
                    || controller.ato_info?.destination_block_id
                    || controller.ato_destination_block_id
                    || null;
                if (onboardDestination) {
                    nextStopEl.textContent = `Next: ${onboardDestination}`;
                    nextStopEl.title = onboardDestination;
                } else {
                    nextStopEl.textContent = '';
                    nextStopEl.title = '';
                }
            }

            // ── Span end A / end B (Status zone, formerly Debug) ──
            const span = controller.span || null;
            const endA = span?.end_a || null;
            const endB = span?.end_b || null;
            const leadingA = span?.leading_end === 'a';
            const endALabel = panel.querySelector('#train-span-end-a-label');
            const endBLabel = panel.querySelector('#train-span-end-b-label');
            if (endALabel) endALabel.textContent = `End A ${leadingA ? '(HEAD)' : '(TAIL)'}`;
            if (endBLabel) endBLabel.textContent = `End B ${leadingA ? '(TAIL)' : '(HEAD)'}`;
            const endATileEl = panel.querySelector('#train-span-end-a-tile');
            if (endATileEl) endATileEl.textContent = endA?.tile ? `(${endA.tile[0]}, ${endA.tile[1]})` : '—';
            const endBTileEl = panel.querySelector('#train-span-end-b-tile');
            if (endBTileEl) endBTileEl.textContent = endB?.tile ? `(${endB.tile[0]}, ${endB.tile[1]})` : '—';
            const endADistEl = panel.querySelector('#train-span-end-a-dist');
            if (endADistEl) endADistEl.textContent = endA && Number.isFinite(endA.distance_cm) ? `${endA.distance_cm.toFixed(1)} cm` : '—';
            const endBDistEl = panel.querySelector('#train-span-end-b-dist');
            if (endBDistEl) endBDistEl.textContent = endB && Number.isFinite(endB.distance_cm) ? `${endB.distance_cm.toFixed(1)} cm` : '—';
            const endAEdgeEl = panel.querySelector('#train-span-end-a-edge');
            if (endAEdgeEl) endAEdgeEl.textContent = endA?.entry_edge || '—';
            const endBEdgeEl = panel.querySelector('#train-span-end-b-edge');
            if (endBEdgeEl) endBEdgeEl.textContent = endB?.entry_edge || '—';

            // EB reset button (Controls zone) — toggle visibility on eb_active.
            const ebResetBtn = panel.querySelector('#train-emergency-reset-btn');
            if (ebResetBtn) {
                ebResetBtn.classList.toggle('hidden', !(controller.eb_active ?? false));
            }
        }

        // ── Task-zone Path Index (sourced from dispatcher progress) ──
        const dispatcher = window.stateManager.getDispatcherProgress(train.id);
        const pathIndexEl = panel.querySelector('#train-path-index-val');
        if (pathIndexEl) {
            if (dispatcher && dispatcher.current_resource_index != null && Array.isArray(dispatcher.resource_sequence) && dispatcher.resource_sequence.length > 0) {
                pathIndexEl.textContent = `${dispatcher.current_resource_index + 1} / ${dispatcher.resource_sequence.length}`;
            } else {
                pathIndexEl.textContent = '—';
            }
        }

        // ── Status-zone read-only Properties mirror (Train.* fields) ──
        const carEl = panel.querySelector('#train-status-car-val');
        if (carEl) carEl.textContent = train.car_number || '—';
        const lengthEl = panel.querySelector('#train-status-length-val');
        if (lengthEl) lengthEl.textContent = `${train.length_cm || 0} cm`;
        const maxSpeedEl = panel.querySelector('#train-status-maxspeed-val');
        if (maxSpeedEl) maxSpeedEl.textContent = `${train.max_speed || 100} km/h`;

        const powerOn = controller?.power_on ?? false;
        const controlMode = controller?.control_mode || 'ato';
        const isStopped = controller?.is_stopped ?? true;
        const activeCab = controller?.cab || null;

        this.updateCabButtons(panel, powerOn, isStopped, activeCab);
        this.updateModeButtons(panel, isStopped, controlMode);

        this.updateButtonStates(train);

        // Debug zone stays live across controller/dispatcher ticks because
        // the panel update fires from those same subscriptions. Skipping
        // this when the debug checkbox is off is handled inside the call.
        if (window.app && typeof window.app.updateTrainDebugPanel === 'function') {
            window.app.updateTrainDebugPanel(train);
        }
    }

    updateCabButtons(panel, powerOn, isStopped, activeCab) {
        const cabABtn = panel.querySelector('#train-cab-a-btn');
        const cabBBtn = panel.querySelector('#train-cab-b-btn');
        const cabOffBtn = panel.querySelector('#train-cab-off-btn');

        if (cabABtn) {
            cabABtn.disabled = !powerOn || !isStopped;
            cabABtn.classList.toggle('active', activeCab === 'A');
        }
        if (cabBBtn) {
            cabBBtn.disabled = !powerOn || !isStopped;
            cabBBtn.classList.toggle('active', activeCab === 'B');
        }
        if (cabOffBtn) {
            const isOff = !activeCab;
            cabOffBtn.disabled = !powerOn || !isStopped || isOff;
            cabOffBtn.classList.toggle('active', isOff);
        }
    }

    updateModeButtons(panel, isStopped, controlMode) {
        const modeATOBtn = panel.querySelector('#train-mode-ato-btn');
        const modeManualBtn = panel.querySelector('#train-mode-manual-btn');

        if (modeATOBtn) {
            modeATOBtn.disabled = !isStopped;
            modeATOBtn.classList.toggle('active', controlMode === 'ato');
        }
        if (modeManualBtn) {
            modeManualBtn.disabled = !isStopped;
            modeManualBtn.classList.toggle('active', controlMode === 'manual');
        }
    }

    updateButtonStates(train) {
        const controller = window.stateManager.getController(train.id) || null;
        const state = controller?.state;
        const powerOn = controller?.power_on ?? false;
        // In v1, manual is a control mode (controller.control_mode), not a state.
        const isManual = controller?.control_mode === 'manual';

        const powerOnBtn = document.getElementById('train-power-on-btn');
        const powerOffBtn = document.getElementById('train-power-off-btn');
        if (powerOnBtn) {
            powerOnBtn.disabled = powerOn;
            powerOnBtn.classList.toggle('active', powerOn);
        }
        if (powerOffBtn) {
            powerOffBtn.disabled = !powerOn;
            powerOffBtn.classList.toggle('active', !powerOn);
        }

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
        if (manualOnBtn) manualOnBtn.disabled = isManual;
        if (manualOffBtn) manualOffBtn.disabled = !isManual;
    }
}
