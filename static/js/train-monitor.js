class TrainMonitor {
    constructor() {
        this.bar = document.getElementById('train-monitor-bar');
        this.canvas = document.getElementById('train-monitor-canvas');
        this.ctx = this.canvas.getContext('2d');
        this.titleEl = document.getElementById('train-monitor-title');
        this.currentSpeedEl = document.getElementById('monitor-current-speed');
        this.atcSpeedEl = document.getElementById('monitor-atc-speed');
        this.atoSpeedEl = document.getElementById('monitor-ato-speed');
        this.waitReasonEl = document.getElementById('monitor-wait-reason');
        this.speedLimitReasonEl = document.getElementById('monitor-speed-limit-reason');
        this.speedCmsEl = document.getElementById('monitor-speed-cms');
        this.clearanceEl = document.getElementById('monitor-clearance');
        this.upcomingEl = document.getElementById('monitor-upcoming');
        this.upcomingContainer = document.getElementById('monitor-upcoming-container');
        this.monitorContent = document.querySelector('.train-monitor-content');
        this.resizerEl = document.getElementById('train-monitor-resizer');
        this.closeBtn = document.getElementById('train-monitor-close');
        
        this.lookaheadData = null;
        this.trainId = null;
        this.currentScale = 160;
        
        this.loadScale();
        
        this.viewDistance = 500; // Default to 5m
        this.hoveredBlock = null;
        this.blockRects = [];
        this.isColumnResizing = false;
        
        this.initViewDistanceButtons();
        this.initColumnResizer();
        
        this.viewLookaheadBtn = document.getElementById('view-lookahead-btn');
        if (this.viewLookaheadBtn) {
            this.viewLookaheadBtn.addEventListener('click', () => this.dumpLookaheadData());
        }
        
        this.canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
        this.canvas.addEventListener('mouseleave', () => {
            this.hoveredBlock = null;
            this.render();
        });
        
        this.stateColors = {
            'free': '#04d361',
            'reserved': '#8257e5',
            'occupied': '#e83f5b',
            'caution': '#ffcd1e'
        };
        
        this.signalColors = {
            'green': '#04d361',
            'yellow': '#ffcd1e',
            'double_yellow': '#ffcd1e',
            'red': '#e83f5b'
        };
        
        this.closeBtn.addEventListener('click', () => this.hide());
        window.addEventListener('resize', () => this.resizeCanvas());
    }
    
    show(trainId, trainName) {
        this.trainId = trainId;
        this.titleEl.textContent = trainName || `Train ${trainId}`;
        this.bar.classList.remove('hidden');
        this.resizeCanvas();
    }
    
    hide() {
        this.bar.classList.add('hidden');
        this.trainId = null;
        this.lookaheadData = null;
    }
    
    isVisible() {
        return !this.bar.classList.contains('hidden');
    }

    initViewDistanceButtons() {
        const buttons = document.querySelectorAll('.view-dist-btn');
        buttons.forEach(btn => {
            btn.addEventListener('click', () => {
                buttons.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.viewDistance = parseInt(btn.dataset.distance, 10);
                this.render();
            });
        });
    }

    initColumnResizer() {
        if (!this.resizerEl || !this.upcomingContainer || !this.monitorContent) {
            return;
        }

        const onMouseMove = (e) => {
            if (!this.isColumnResizing) return;

            const contentRect = this.monitorContent.getBoundingClientRect();
            const minWidth = parseInt(getComputedStyle(this.upcomingContainer).minWidth, 10) || 140;
            const maxWidth = parseInt(getComputedStyle(this.upcomingContainer).maxWidth, 10) || 320;
            let newWidth = contentRect.right - e.clientX;

            newWidth = Math.max(minWidth, Math.min(maxWidth, newWidth));
            this.upcomingContainer.style.width = `${newWidth}px`;
            this.upcomingContainer.style.flex = `0 0 ${newWidth}px`;
            this.resizeCanvas();
        };

        this.resizerEl.addEventListener('mousedown', (e) => {
            this.isColumnResizing = true;
            this.resizerEl.classList.add('resizing');
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });

        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', () => {
            if (!this.isColumnResizing) return;
            this.isColumnResizing = false;
            this.resizerEl.classList.remove('resizing');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }

    dumpLookaheadData() {
        if (this.lookaheadData) {
            if (window.app && window.app.editor) {
                window.app.editor.showFullJSONViewer(this.lookaheadData, 'Train Lookahead Data');
            } else {
                console.log('=== LOOKAHEAD DATA ===');
                console.log(JSON.stringify(this.lookaheadData, null, 2));
                if (this.lookaheadData.blocks) console.table(this.lookaheadData.blocks);
                if (this.lookaheadData.signals) console.table(this.lookaheadData.signals);
                if (this.lookaheadData.switches) console.table(this.lookaheadData.switches);
                if (this.lookaheadData.stations) console.table(this.lookaheadData.stations);
            }
        } else {
            console.log('No lookahead data available');
            if (window.showToast) window.showToast('No lookahead data available', 'info');
        }
    }

    handleMouseMove(e) {
        if (!this.lookaheadData) return;
        
        const rect = this.canvas.getBoundingClientRect();
        const mouseX = (e.clientX - rect.left) * window.devicePixelRatio;
        const mouseY = (e.clientY - rect.top) * window.devicePixelRatio;
        
        let found = null;
        for (const block of this.blockRects) {
            if (mouseX >= block.x && mouseX <= block.x + block.w &&
                mouseY >= block.y && mouseY <= block.y + block.h) {
                found = block.data;
                break;
            }
        }
        
        if (this.hoveredBlock !== found) {
            this.hoveredBlock = found;
            this.render();
        }
    }
    
    async loadScale() {
        try {
            const data = await window.api.getScale();
            this.currentScale = data.scale || 160;
        } catch (e) {
            console.error('[TrainMonitor] Failed to load scale:', e);
        }
    }
    
    resizeCanvas() {
        const rect = this.canvas.getBoundingClientRect();
        this.canvas.width = rect.width * window.devicePixelRatio;
        this.canvas.height = rect.height * window.devicePixelRatio;
        this.ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
        if (this.lookaheadData) {
            this.render();
        }
    }
    
    update(train) {
        // Check for path-based data
        if (!train) {
            console.warn('[TrainMonitor] No train data');
            this.lookaheadData = null;
            this.render();
            return;
        }
        
        // ========================================================================
        // LOOKAHEAD DATA SOURCE
        // ========================================================================
        // Primary: lookahead_tile_info (new format with precise distances)
        //
        // Data comes from Train.to_dict() which now builds lookahead from current_path
        // starting at path_index (train head position)
        const pathInfo = train.lookahead_tile_info || [];
        const pathTiles = train.lookahead_tiles || [];
        const hasPathData = pathInfo.length > 0;
        
        if (!hasPathData) {
            console.warn('[TrainMonitor] Missing or empty lookahead data', {
                trainId: train.id,
                hasLookaheadTileInfo: !!train.lookahead_tile_info,
                pathLength: train.path_length,
                pathIndex: train.path_index
            });
            
            // Build minimal lookahead data with just speed/state info.
            // target_speed and speed_limit_reason live on the controller in v1
            // and are exposed via train.onboard (merged in by the backend).
            this.lookaheadData = {
                current_speed_kmh: train.speed || 0,
                target_speed_kmh: train.onboard?.target_speed ?? 0,
                state: train.state,
                wait_reason: train.wait_reason,
                speed_limit_reason: train.onboard?.speed_limit_reason ?? null,
                blocks: [],
                signals: [],
                switches: [],
                stations: [],
                distance_into_path_cm: 0,
                clearance_cm: 0
            };
            
            // Update UI elements with minimal data
            this.currentSpeedEl.textContent = Math.round(this.lookaheadData.current_speed_kmh || 0);
            // For minimal data, we don't have speed curves, so show target speed for both
            const fallbackSpeed = Math.round(this.lookaheadData.target_speed_kmh || 0);
            if (this.atcSpeedEl) this.atcSpeedEl.textContent = fallbackSpeed;
            if (this.atoSpeedEl) this.atoSpeedEl.textContent = fallbackSpeed;
            
            if (this.speedCmsEl) {
                const speedCms = ((this.lookaheadData.current_speed_kmh || 0) * 27.78 / this.currentScale).toFixed(1);
                this.speedCmsEl.textContent = speedCms;
            }
            if (this.clearanceEl) {
                this.clearanceEl.textContent = '0.0';
            }
            
            // wait_reason is set by the train/controller whenever the train
            // is stalled. running_waiting state was removed in v1; the wait
            // reason field is now the single source of "waiting" UI.
            if (this.waitReasonEl) {
                if (this.lookaheadData.wait_reason) {
                    this.waitReasonEl.textContent = this.lookaheadData.wait_reason;
                    this.waitReasonEl.classList.remove('hidden');
                } else {
                    this.waitReasonEl.classList.add('hidden');
                }
            }

            if (this.speedLimitReasonEl) {
                const speedLimitReason = this.lookaheadData.speed_limit_reason;
                if (speedLimitReason && !this.lookaheadData.wait_reason) {
                    this.speedLimitReasonEl.textContent = speedLimitReason;
                    this.speedLimitReasonEl.classList.remove('hidden');
                } else {
                    this.speedLimitReasonEl.classList.add('hidden');
                }
            }

            this.updateUpcomingElements(this.lookaheadData);
            this.render();
            return;
        }
        
        // ========================================================================
        // BUILD LOOKAHEAD VISUALIZATION DATA
        // ========================================================================
        // Now lookahead_tile_info contains ALL tiles from head position to end of path
        // Each tile has distance_to_tile_cm (distance from train head to tile start)
        //
        // We process this into blocks, signals, switches for display
        const blocks = [];
        const signals = [];
        const switches = [];
        const stations = [];
        
        const seenBlocks = new Set();
        
        // Process each tile in lookahead (already ordered from head forward)
        for (let i = 0; i < pathInfo.length; i++) {
            const info = pathInfo[i];
            const tile = pathTiles[i];
            if (!info) continue;
            
            // distance_to_tile_cm: distance from train head to START of this tile
            // For first tile (head tile), this is negative (head already into tile)
            // For subsequent tiles, this is positive
            const distanceToTileStart = info.distance_to_tile_cm ?? 0;
            
            // Train's travel direction on this tile (N/E/S/W)
            const travelDirection = info.travel_direction;
            
            // For blocks: use actual distance (can be negative for current block)
            // This allows the current block to scroll left as train advances
            const blockTileStart = info.distance_to_block_start_cm;
            
            // For signals/switches: use max(0, dist) since they are point elements
            const pointElementStart = Math.max(0, distanceToTileStart);
            
            // Add block if not seen yet
            if (info.block_id && !seenBlocks.has(info.block_id)) {
                seenBlocks.add(info.block_id);
                
                // Find all consecutive tiles in this block to calculate total length
                let blockLength = 0;
                let tileCount = 0;
                for (let j = i; j < pathInfo.length; j++) {
                    if (pathInfo[j]?.block_id === info.block_id) {
                        blockLength += pathInfo[j].length_cm 
                        tileCount++;
                    } else {
                        break;
                    }
                }
                
                blocks.push({
                    id: info.block_id,
                    name: info.block_id,
                    state: info.block_state || 'free',
                    start_cm: blockTileStart,
                    end_cm: blockTileStart + blockLength,
                    length_cm: blockLength,
                    tile_count: tileCount,
                    sequence: blocks.length
                });
            }
            
            // Add signals on this tile - only if facing train's travel direction
            if (info.signals && info.signals.length > 0 && travelDirection) {
                info.signals.forEach(sig => {
                    // Only include signals where detection_direction matches train's travel direction
                    // Signal direction can be single letter (N/E/S/W) or full name (NORTH/EAST/etc)
                    const sigDir = (sig.direction || '').toUpperCase();
                    const trainDir = travelDirection.toUpperCase();
                    const dirMatches = sigDir === trainDir || 
                                       sigDir === trainDir[0] || 
                                       sigDir[0] === trainDir[0];
                    
                    if (dirMatches) {
                        signals.push({
                            id: sig.id,
                            name: sig.name || sig.id,
                            state: sig.state,
                            distance_cm: pointElementStart,
                            sequence: signals.length
                        });
                    }
                });
            }
            
            // Add switch if present
            if (info.is_switch && tile && tile.length >= 2) {
                switches.push({
                    x: tile[0],
                    y: tile[1],
                    name: info.switch_name || `Switch (${tile[0]},${tile[1]})`,
                    position: info.switch_state === 0 ? 'main' : 'diverging',
                    distance_cm: pointElementStart,
                    locked: info.switch_locked || false,
                    sequence: switches.length
                });
            }
        }
        
        // Calculate total clearance (distance from head to end of path)
        let totalClearance = 0;
        if (pathInfo.length > 0) {
            const lastTile = pathInfo[pathInfo.length - 1];
            const lastTileStart = lastTile.distance_to_tile_cm ?? 0;
            totalClearance = Math.max(0, lastTileStart) + (lastTile.length_cm || 50);
        }
        
        // Build lookahead-compatible data structure.
        // target_speed and speed_limit_reason now come from train.onboard
        // (the controller snapshot merged into the train payload by the
        // backend) rather than from train fields, which were removed in v1.
        this.lookaheadData = {
            current_speed_kmh: train.speed || 0,
            target_speed_kmh: train.onboard?.target_speed ?? 0,
            state: train.state,
            wait_reason: train.wait_reason,
            speed_limit_reason: train.onboard?.speed_limit_reason ?? null,
            blocks: blocks,
            signals: signals,
            switches: switches,
            stations: stations,
            distance_into_path_cm: 0, // Head is always at 0 (we measure from head)
            clearance_cm: totalClearance
        };
        
        // Update UI elements
        this.currentSpeedEl.textContent = Math.round(this.lookaheadData.current_speed_kmh || 0);
        // For update() method, we don't have speed curves yet - use fallback
        const fallbackSpeed2 = Math.round(this.lookaheadData.target_speed_kmh || 0);
        if (this.atcSpeedEl) this.atcSpeedEl.textContent = fallbackSpeed2;
        if (this.atoSpeedEl) this.atoSpeedEl.textContent = fallbackSpeed2;
        
        if (this.speedCmsEl) {
            const speedCms = ((this.lookaheadData.current_speed_kmh || 0) * 27.78 / this.currentScale).toFixed(1);
            this.speedCmsEl.textContent = speedCms;
        }
        if (this.clearanceEl) {
            const clearanceM = ((this.lookaheadData.clearance_cm || 0) / 100).toFixed(1);
            this.clearanceEl.textContent = clearanceM;
        }
        
        if (this.waitReasonEl) {
            if (this.lookaheadData.wait_reason) {
                this.waitReasonEl.textContent = this.lookaheadData.wait_reason;
                this.waitReasonEl.classList.remove('hidden');
            } else {
                this.waitReasonEl.classList.add('hidden');
            }
        }

        if (this.speedLimitReasonEl) {
            const speedLimitReason = this.lookaheadData.speed_limit_reason;
            if (speedLimitReason && !this.lookaheadData.wait_reason) {
                this.speedLimitReasonEl.textContent = speedLimitReason;
                this.speedLimitReasonEl.classList.remove('hidden');
            } else {
                this.speedLimitReasonEl.classList.add('hidden');
            }
        }

        // NEW: Check for configuration warnings
        // User requirement: Show warnings but allow operation (non-blocking)
        if (this.lookaheadData.warnings && this.lookaheadData.warnings.length > 0) {
            this._displayConfigurationWarnings(this.lookaheadData.warnings, this.lookaheadData.train_id);
        }
        
        this.updateUpcomingElements(this.lookaheadData);
        this.render();
    }

    updateFromController(train, payload) {
        if (!train) {
            console.warn('[TrainMonitor] No train data');
            this.lookaheadData = null;
            this.render();
            return;
        }

        const lookahead = payload && payload.lookahead ? payload.lookahead : null;
        const speedLimitReason =
            payload?.speed_limit_reason ?? train.onboard?.speed_limit_reason ?? null;

        const safetyMarginCm = lookahead?.safety_margin_cm ?? 25;
        const clearanceCm = lookahead?.clearance_cm || 0;
        const rawTargetStopCm = lookahead?.target_stop_cm ?? (clearanceCm - safetyMarginCm);
        const targetStopCm = Math.max(0, rawTargetStopCm);

        this.lookaheadData = {
            current_speed_kmh: train.speed || 0,
            target_speed_kmh: train.onboard?.target_speed ?? 0,
            state: train.state,
            wait_reason: train.wait_reason,
            speed_limit_reason: speedLimitReason,
            blocks: lookahead?.blocks || [],
            signals: lookahead?.signals || [],
            switches: lookahead?.switches || [],
            stations: lookahead?.stations || [],
            distance_into_path_cm: 0,
            clearance_cm: clearanceCm,
            safety_margin_cm: safetyMarginCm,
            target_stop_cm: targetStopCm,
            speed_curves: lookahead?.speed_curves || []
        };

        this.currentSpeedEl.textContent = Math.round(this.lookaheadData.current_speed_kmh || 0);
        
        // Extract ATC and ATO speeds from speed_curves if available
        const curves = this.lookaheadData.speed_curves || [];
        let atcSpeed = this.lookaheadData.target_speed_kmh || 0;
        let atoSpeed = this.lookaheadData.target_speed_kmh || 0;
        if (curves.length > 0) {
            atcSpeed = curves[0].atc_limit ?? atcSpeed;
            atoSpeed = curves[0].operational_limit ?? atoSpeed;
        }
        if (this.atcSpeedEl) this.atcSpeedEl.textContent = Math.round(atcSpeed);
        if (this.atoSpeedEl) this.atoSpeedEl.textContent = Math.round(atoSpeed);

        if (this.speedCmsEl) {
            const speedCms = ((this.lookaheadData.current_speed_kmh || 0) * 27.78 / this.currentScale).toFixed(1);
            this.speedCmsEl.textContent = speedCms;
        }
        if (this.clearanceEl) {
            const clearanceM = ((this.lookaheadData.clearance_cm || 0) / 100).toFixed(1);
            this.clearanceEl.textContent = clearanceM;
        }
        
        // Update distance to next destination (target stop point)
        const nextDestContainer = document.getElementById('monitor-next-dest-container');
        const nextDestEl = document.getElementById('monitor-next-dest-dist');
        if (nextDestContainer && nextDestEl && this.lookaheadData.target_stop_cm > 0) {
            const targetStopM = (this.lookaheadData.target_stop_cm / 100).toFixed(1);
            nextDestEl.textContent = targetStopM;
            nextDestContainer.classList.remove('hidden');
        } else if (nextDestContainer) {
            nextDestContainer.classList.add('hidden');
        }

        if (this.waitReasonEl) {
            if (this.lookaheadData.wait_reason) {
                this.waitReasonEl.textContent = this.lookaheadData.wait_reason;
                this.waitReasonEl.classList.remove('hidden');
            } else {
                this.waitReasonEl.classList.add('hidden');
            }
        }

        if (this.speedLimitReasonEl) {
            const speedLimitReason = this.lookaheadData.speed_limit_reason;
            if (speedLimitReason && !this.lookaheadData.wait_reason) {
                this.speedLimitReasonEl.textContent = speedLimitReason;
                this.speedLimitReasonEl.classList.remove('hidden');
            } else {
                this.speedLimitReasonEl.classList.add('hidden');
            }
        }

        this.updateUpcomingElements(this.lookaheadData);
        this.render();
    }
    
    _displayConfigurationWarnings(warnings, trainId) {
        // Display configuration warnings via toast notifications.
        // Track which warnings we've already shown to avoid spam
        const warningKey = warnings.map(w => `${w.type}_${w.position || w.block_id || w.station_id}`).join('|');
        
        // Only show warnings once per unique set (avoid repeated toasts)
        if (this.lastWarningKey === warningKey) {
            return;
        }
        this.lastWarningKey = warningKey;
        
        for (const warning of warnings) {
            let message = '';
            switch (warning.type) {
                case 'block_length_not_configured':
                    message = `⚠️ Block ${warning.block_id} has no length configured. Please set length in Block Editor.`;
                    break;
                case 'station_length_not_configured':
                    message = `⚠️ Platform ${warning.station_id} has no length configured. Please set length in Platform Editor.`;
                    break;
                case 'switch_not_configured':
                    message = `⚠️ Switch at (${warning.position[0]}, ${warning.position[1]}) [${warning.state}] has no length configured.`;
                    break;
                case 'switch_entity_missing':
                    message = `⚠️ Switch at (${warning.position[0]}, ${warning.position[1]}) has no configuration entity.`;
                    break;
                case 'block_not_found':
                    message = `⚠️ Block ${warning.block_id} not found in network.`;
                    break;
                case 'station_not_found':
                    message = `⚠️ Platform ${warning.station_id} not found in network.`;
                    break;
                default:
                    message = warning.message || 'Unknown configuration warning';
            }
            
            // Show warning toast (auto-dismiss after 8 seconds)
            if (window.showToast) {
                window.showToast(message, 'warning', 8000);
            }
        }
        
        // Show summary warning
        if (window.showToast) {
            window.showToast(
                `Train ${trainId}: ${warnings.length} configuration warning(s). Operation allowed but physics may be inaccurate.`,
                'warning',
                6000
            );
        }
    }
    
    updateUpcomingElements(data) {
        if (!this.upcomingEl) return;
        
        const elements = [];
        
        (data.blocks || []).forEach(block => {
            elements.push({
                type: 'block',
                name: block.name || block.id,
                distance: block.start_cm,
                length: block.length_cm || 0,
                state: block.state,
                sequence: block.sequence || 0
            });
        });
        
        (data.stations || []).forEach(station => {
            elements.push({
                type: 'station',
                name: station.name || station.station_id,
                distance: station.distance_cm,
                length: station.length_cm || 0,
                sequence: station.sequence || 0
            });
        });
        
        (data.switches || []).forEach(sw => {
            elements.push({
                type: 'switch',
                name: sw.name || `Switch (${sw.x},${sw.y})`,
                distance: sw.distance_cm,
                length: 0,
                position: sw.position,
                sequence: sw.sequence || 0
            });
        });
        
        (data.signals || []).forEach(sig => {
            elements.push({
                type: 'signal',
                name: sig.name || sig.id,
                distance: sig.distance_cm,
                length: 0,
                state: sig.state,
                sequence: sig.sequence || 0
            });
        });
        
        elements.sort((a, b) => {
            if (Math.abs(a.distance - b.distance) < 0.01) {
                return a.sequence - b.sequence;
            }
            return a.distance - b.distance;
        });
        
        const upcoming = elements;
        
        if (upcoming.length === 0) {
            this.upcomingEl.innerHTML = '<span class="text-secondary">No elements ahead</span>';
            return;
        }
        
        const trainDist = data.distance_into_path_cm || 0;
        
        this.upcomingEl.innerHTML = upcoming.map(el => {
            const distToStart = el.distance - trainDist;
            const distToEnd = (el.distance + el.length) - trainDist;
            const totalLength = el.length;
            
            const startM = (distToStart / 100).toFixed(1);
            const endM = (distToEnd / 100).toFixed(1);
            const lengthM = (totalLength / 100).toFixed(1);
            
            let icon = '▣';
            let colorClass = '';
            if (el.type === 'station') {
                icon = '🚉';
            } else if (el.type === 'switch') {
                icon = '⇋';
            } else if (el.type === 'signal') {
                icon = '🚦';
                colorClass = `signal-${el.state || 'unknown'}`;
            } else if (el.type === 'block') {
                colorClass = `state-${el.state || 'unknown'}`;
            }
            
            return `<div class="upcoming-item ${colorClass}">
                <span class="upcoming-icon">${icon}</span>
                <span class="upcoming-name">${el.name}</span>
                <div class="upcoming-distances">
                    <span class="dist-start" title="Distance to start">${startM}m</span>
                    ${el.length > 0 ? `<span class="dist-end" title="Distance to end">→ ${endM}m</span>` : ''}
                    ${el.length > 0 ? `<span class="dist-length" title="Length">(${lengthM}m)</span>` : ''}
                </div>
            </div>`;
        }).join('');
    }

    fitTextToWidth(text, maxWidth) {
        if (!text || maxWidth <= 0) return '';
        if (this.ctx.measureText(text).width <= maxWidth) {
            return text;
        }

        const ellipsis = '...';
        let end = text.length;
        while (end > 0) {
            const candidate = text.slice(0, end).trimEnd() + ellipsis;
            if (this.ctx.measureText(candidate).width <= maxWidth) {
                return candidate;
            }
            end -= 1;
        }
        return '';
    }
    
    getMaxDistanceAhead(data, trainDist) {
        /**
         * Calculate the furthest element ahead of train in lookahead data.
         * Returns distance in cm from train's current position.
         */
        let maxDist = 0;
        
        // Check blocks (use end position)
        (data.blocks || []).forEach(b => {
            const blockEnd = b.end_cm;
            if (blockEnd > trainDist) {
                maxDist = Math.max(maxDist, blockEnd);
            }
        });
        
        // Check stations (use distance + length)
        (data.stations || []).forEach(s => {
            const stationEnd = s.distance_cm + (s.length_cm || 0);
            if (stationEnd > trainDist) {
                maxDist = Math.max(maxDist, stationEnd);
            }
        });
        
        // Check signals (point elements, use distance only)
        (data.signals || []).forEach(sig => {
            if (sig.distance_cm > trainDist) {
                maxDist = Math.max(maxDist, sig.distance_cm);
            }
        });
        
        // Check switches (point elements, use distance only)
        (data.switches || []).forEach(sw => {
            if (sw.distance_cm > trainDist) {
                maxDist = Math.max(maxDist, sw.distance_cm);
            }
        });
        
        // Return distance ahead of train (not absolute position)
        return Math.max(0, maxDist - trainDist);
    }
    
    render() {
        const data = this.lookaheadData;
        if (!data) return;
        
        const rect = this.canvas.getBoundingClientRect();
        const width = rect.width;
        const height = rect.height;
        
        this.ctx.clearRect(0, 0, width, height);
        
        const padding = { left: 30, right: 10, top: 15, bottom: 25 };
        
        // Even spacing layout with consistent gaps
        const availableHeight = height - padding.top - padding.bottom;
        const sectionGap = 8;
        
        // Station section: 15% at bottom
        const stationHeight = Math.max(availableHeight * 0.15, 28);
        const stationEndY = height - padding.bottom;
        const stationStartY = stationEndY - stationHeight;
        const stationTrackY = stationStartY + (stationHeight / 2);
        
        // Block section: fixed height to fit content (block + 2 lines of text)
        // Block rect: 16px centered at trackY (8px above, 8px below)
        // Text line 1: at trackY + 18 (10px below block bottom)
        // Text line 2: at trackY + 28 (20px below block bottom)
        // Need 8px margin at bottom after text
        // Total: trackY + 28 + 8 = trackY + 36
        // If trackY is centered at 24px: section = 24 + 36 = 60px
        const blockHeight = 60;
        const blockStartY = stationStartY - sectionGap - blockHeight;
        const blockTrackY = blockStartY + 24;  // Centered at 24px from section top
        
        // ATC section: from top padding to block section, respecting 55% target
        const targetAtcHeight = availableHeight * 0.55;
        const maxAtcEndY = blockStartY - sectionGap;
        const atcEndY = Math.min(padding.top + targetAtcHeight, maxAtcEndY);
        const speedCurveAreaHeight = atcEndY - padding.top;
        const trackWidth = width - padding.left - padding.right;
        
        const trainDist = data.distance_into_path_cm || 0;
        
        let lookaheadDistance;
        if (this.viewDistance > 0) {
            lookaheadDistance = this.viewDistance;
        } else {
            const maxAheadElement = this.getMaxDistanceAhead(data, trainDist);
            lookaheadDistance = Math.max(2000, Math.min(maxAheadElement + 1000, 10000));
        }

        const visibleStartCm = trainDist;
        const visibleEndCm = trainDist + lookaheadDistance;
        const visibleRangeCm = visibleEndCm - visibleStartCm;
        const cmToX = (cm) => padding.left + ((cm - visibleStartCm) / visibleRangeCm) * trackWidth;
        
        // Draw speed curves (ATC and operational limits)
        if (data.speed_curves && data.speed_curves.length > 0) {
            this.drawSpeedCurves(
                data.speed_curves,
                cmToX,
                data.current_speed_kmh,
                data.target_speed_kmh,
                padding,
                speedCurveAreaHeight
            );
        }
        
        // Draw grey overlay for area already passed (behind train, negative distances)
        const trainX = cmToX(trainDist);
        if (trainX > padding.left) {
            const passedWidth = trainX - padding.left;
            this.ctx.fillStyle = '#1a1a1a';
            this.ctx.fillRect(padding.left, 0, passedWidth, height - padding.bottom);
        }
        
        // Draw grey overlay beyond clearance distance (unknown territory)
        const clearanceCm = data.clearance_cm || 0;
        if (clearanceCm > 0 && clearanceCm < lookaheadDistance) {
            const clearanceX = cmToX(trainDist + clearanceCm);
            const greyWidth = width - clearanceX;
            if (greyWidth > 0) {
                this.ctx.fillStyle = '#1a1a1a';
                this.ctx.fillRect(clearanceX, 0, greyWidth, height - padding.bottom);
            }
        }
        
        // Draw safety margin indicator (target stop point)
        const safetyMarginCm = data.safety_margin_cm || 25;
        const rawTargetStopCm = data.target_stop_cm ?? (clearanceCm - safetyMarginCm);
        const targetStopCm = Math.max(0, rawTargetStopCm);
        if (targetStopCm > 0 && targetStopCm < lookaheadDistance) {
            const targetStopX = cmToX(trainDist + targetStopCm);
            // Draw vertical dashed line at target stop point
            this.ctx.save();
            this.ctx.strokeStyle = '#ff6b6b';
            this.ctx.lineWidth = 1.5;
            this.ctx.setLineDash([4, 3]);
            this.ctx.beginPath();
            this.ctx.moveTo(targetStopX, padding.top);
            this.ctx.lineTo(targetStopX, height - padding.bottom);
            this.ctx.stroke();
            this.ctx.setLineDash([]);
            
            // Draw safety margin zone (between target stop and clearance)
            if (clearanceCm > targetStopCm) {
                const clearanceX = cmToX(trainDist + clearanceCm);
                this.ctx.fillStyle = 'rgba(255, 107, 107, 0.15)';
                this.ctx.fillRect(targetStopX, padding.top, clearanceX - targetStopX, speedCurveAreaHeight);
            }
            this.ctx.restore();
        }
        
        this.drawTrackLine(padding.left, blockTrackY, trackWidth, '#333');
        this.drawBlocks(data.blocks || [], cmToX, blockTrackY);
        this.drawSwitches(data.switches || [], cmToX, blockTrackY);
        this.drawSignals(data.signals || [], cmToX, blockTrackY);
        
        if ((data.stations || []).length > 0) {
            this.drawTrackLine(padding.left, stationTrackY, trackWidth, '#2a4a5f');
            this.drawStations(data.stations || [], cmToX, stationTrackY);
        }
        
        this.drawTrainPosition(trainX, blockTrackY);
        if ((data.stations || []).length > 0) {
            this.drawTrainPosition(trainX, stationTrackY);
        }
        
        this.drawDistanceScale(padding.left, height - 5, trackWidth, trainDist, visibleRangeCm);
        
        this.drawHoverTooltip();
    }
    
    drawTrackLine(x, y, width, color) {
        this.ctx.strokeStyle = color;
        this.ctx.lineWidth = 3;
        this.ctx.beginPath();
        this.ctx.moveTo(x, y);
        this.ctx.lineTo(x + width, y);
        this.ctx.stroke();
    }
    
    drawTrainPosition(x, y) {
        this.ctx.fillStyle = '#ffcd1e';
        this.ctx.beginPath();
        this.ctx.moveTo(x, y - 6);
        this.ctx.lineTo(x + 8, y);
        this.ctx.lineTo(x, y + 6);
        this.ctx.closePath();
        this.ctx.fill();
    }
    
    drawBlocks(blocks, cmToX, trackY) {
        this.blockRects = [];
        const blockHeight = 16;
        
        blocks.forEach(block => {
            const startX = cmToX(block.start_cm);
            const endX = cmToX(block.end_cm);
            
            if (endX < 0 || startX > this.canvas.width) return;
            
            const blockWidth = Math.max(endX - startX, 2);
            
            this.blockRects.push({
                x: startX,
                y: trackY - blockHeight/2,
                w: blockWidth,
                h: blockHeight,
                data: block
            });

            const color = this.stateColors[block.state] || '#666';
            
            this.ctx.fillStyle = color;
            this.ctx.globalAlpha = 0.3;
            this.ctx.fillRect(startX, trackY - blockHeight/2, blockWidth, blockHeight);
            this.ctx.globalAlpha = 1.0;
            
            this.ctx.strokeStyle = color;
            this.ctx.lineWidth = 2;
            this.ctx.strokeRect(startX, trackY - blockHeight/2, blockWidth, blockHeight);
            
            const tileCount = block.tile_count || 1;
            if (tileCount > 1 && blockWidth > 10) {
                this.ctx.strokeStyle = color;
                this.ctx.lineWidth = 1;
                this.ctx.globalAlpha = 0.5;
                const tileWidth = blockWidth / tileCount;
                for (let i = 1; i < tileCount; i++) {
                    const dividerX = startX + i * tileWidth;
                    this.ctx.beginPath();
                    this.ctx.moveTo(dividerX, trackY - blockHeight/2);
                    this.ctx.lineTo(dividerX, trackY + blockHeight/2);
                    this.ctx.stroke();
                }
                this.ctx.globalAlpha = 1.0;
            }
            
            // Draw block name (similar to station graph)
            const name = block.name || block.id || '';
            this.ctx.font = '9px Inter, sans-serif';
            if (blockWidth > 30) {
                const labelMaxWidth = Math.max(0, blockWidth - 6);
                const displayName = this.fitTextToWidth(name, labelMaxWidth);
                if (displayName) {
                    this.ctx.fillStyle = '#fff';
                    this.ctx.textAlign = 'center';
                    this.ctx.textBaseline = 'middle';
                    this.ctx.fillText(displayName, startX + blockWidth/2, trackY);
                }
            }
            
            // Draw measurements below the block
            const labelY = trackY + blockHeight/2 + 10;
            
            const lengthM = (block.length_cm / 100).toFixed(1);
            const lengthLabel = `${lengthM}m (${tileCount} tile${tileCount > 1 ? 's' : ''})`;
            
            this.ctx.font = '8px JetBrains Mono, monospace';
            this.ctx.fillStyle = '#999';
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'top';
            this.ctx.fillText(lengthLabel, startX + blockWidth/2, labelY);
            
            const startM = (block.start_cm / 100).toFixed(1);
            const endM = (block.end_cm / 100).toFixed(1);
            const posLabel = `${startM}m - ${endM}m`;
            
            this.ctx.fillText(posLabel, startX + blockWidth/2, labelY + 10);
        });
    }
    
    drawSignals(signals, cmToX, trackY) {
        signals.forEach(signal => {
            const x = cmToX(signal.distance_cm);
            
            if (x < 0 || x > this.canvas.width) return;
            
            const state = signal.state ? signal.state.toLowerCase() : null;
            const color = this.signalColors[state] || '#888';
            
            this.ctx.strokeStyle = '#555';
            this.ctx.lineWidth = 2;
            this.ctx.beginPath();
            this.ctx.moveTo(x, trackY - 8);
            this.ctx.lineTo(x, trackY - 22);
            this.ctx.stroke();
            
            this.ctx.fillStyle = color;
            this.ctx.beginPath();
            this.ctx.arc(x, trackY - 26, 4, 0, Math.PI * 2);
            this.ctx.fill();
            
            if (state === 'double_yellow') {
                this.ctx.beginPath();
                this.ctx.arc(x, trackY - 34, 4, 0, Math.PI * 2);
                this.ctx.fill();
            }
        });
    }
    
    drawSwitches(switches, cmToX, trackY) {
        if (!switches) return;
        
        switches.forEach(sw => {
            const x = cmToX(sw.distance_cm);
            
            if (x < 0 || x > this.canvas.width) return;
            
            const isMain = sw.position === 'main' || sw.position === 0;
            
            // Draw orange lock circle if switch is locked
            if (sw.locked) {
                this.ctx.strokeStyle = '#ff8c00'; // Orange
                this.ctx.lineWidth = 2;
                this.ctx.beginPath();
                this.ctx.arc(x, trackY, 8, 0, Math.PI * 2);
                this.ctx.stroke();
            }
            
            this.ctx.strokeStyle = isMain ? '#04d361' : '#ffcd1e';
            this.ctx.lineWidth = 2.5;
            
            this.ctx.beginPath();
            this.ctx.moveTo(x - 5, trackY - 4);
            this.ctx.lineTo(x + 5, trackY + 4);
            this.ctx.stroke();
            
            this.ctx.beginPath();
            this.ctx.moveTo(x - 5, trackY + 4);
            this.ctx.lineTo(x + 5, trackY - 4);
            this.ctx.stroke();
            
            this.ctx.fillStyle = isMain ? '#04d361' : '#ffcd1e';
            this.ctx.beginPath();
            this.ctx.arc(x, trackY, 3, 0, Math.PI * 2);
            this.ctx.fill();
        });
    }
    
    drawStations(stations, cmToX, trackY) {
        const stationHeight = 14;
        
        stations.forEach(station => {
            const startX = cmToX(station.distance_cm);
            const stationLength = station.length_cm || 50;
            const endX = cmToX(station.distance_cm + stationLength);
            
            if (endX < 0 || startX > this.canvas.width) return;
            
            const stationWidth = Math.max(endX - startX, 10);
            
            this.ctx.fillStyle = '#3498db';
            this.ctx.globalAlpha = 0.4;
            this.ctx.fillRect(startX, trackY - stationHeight/2, stationWidth, stationHeight);
            this.ctx.globalAlpha = 1.0;
            
            this.ctx.strokeStyle = '#3498db';
            this.ctx.lineWidth = 1.5;
            this.ctx.strokeRect(startX, trackY - stationHeight/2, stationWidth, stationHeight);
            
            const tileCount = station.tile_count || 1;
            if (tileCount > 1) {
                this.ctx.strokeStyle = '#3498db';
                this.ctx.lineWidth = 0.5;
                this.ctx.globalAlpha = 0.6;
                const tileWidth = stationWidth / tileCount;
                for (let i = 1; i < tileCount; i++) {
                    const dividerX = startX + i * tileWidth;
                    this.ctx.beginPath();
                    this.ctx.moveTo(dividerX, trackY - stationHeight/2);
                    this.ctx.lineTo(dividerX, trackY + stationHeight/2);
                    this.ctx.stroke();
                }
                this.ctx.globalAlpha = 1.0;
            }
            
            const name = station.name || station.platform_id || station.station_id || 'Platform';
            this.ctx.font = '9px Inter, sans-serif';
            if (stationWidth > 30) {
                const labelMaxWidth = Math.max(0, stationWidth - 6);
                const displayName = this.fitTextToWidth(name, labelMaxWidth);
                if (displayName) {
                    this.ctx.fillStyle = '#fff';
                    this.ctx.textAlign = 'center';
                    this.ctx.textBaseline = 'middle';
                    this.ctx.fillText(displayName, startX + stationWidth/2, trackY);
                }
            }
        });
    }
    
    drawDistanceScale(x, y, width, trainDistCm, rangeCm) {
        this.ctx.font = '9px JetBrains Mono, monospace';
        this.ctx.fillStyle = '#666';
        this.ctx.textBaseline = 'bottom';
        this.ctx.textAlign = 'left';
        this.ctx.fillText('0m', x, y);
        
        this.ctx.textAlign = 'center';
        const midM = Math.round(rangeCm / 200);
        this.ctx.fillText(`${midM}m`, x + width / 2, y);
        
        this.ctx.textAlign = 'right';
        const maxM = Math.round(rangeCm / 100);
        this.ctx.fillText(`${maxM}m`, x + width, y);
    }

    drawHoverTooltip() {
        if (!this.hoveredBlock) return;
        
        const block = this.hoveredBlock;
        const padding = 8;
        const lineHeight = 14;
        
        const lines = [
            `Block: ${block.name || block.id}`,
            `State: ${block.state}`,
            `Length: ${(block.length_cm/100).toFixed(1)}m`,
            `Start: ${(block.start_cm/100).toFixed(1)}m`
        ];
        
        this.ctx.font = '11px Inter, sans-serif';
        let maxWidth = 0;
        lines.forEach(line => {
            const w = this.ctx.measureText(line).width;
            maxWidth = Math.max(maxWidth, w);
        });
        
        const width = maxWidth + padding * 2;
        const height = lines.length * lineHeight + padding * 2;
        
        const x = this.canvas.width / 2 - width / 2;
        const y = 10;
        
        this.ctx.fillStyle = 'rgba(0, 0, 0, 0.85)';
        this.ctx.fillRect(x, y, width, height);
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
        this.ctx.strokeRect(x, y, width, height);
        
        this.ctx.fillStyle = '#fff';
        this.ctx.textAlign = 'left';
        this.ctx.textBaseline = 'top';
        
        lines.forEach((line, i) => {
            this.ctx.fillText(line, x + padding, y + padding + i * lineHeight);
        });
    }

    drawSpeedCurves(speedCurves, cmToX, currentSpeed, targetSpeed, padding, areaHeight) {
        if (!speedCurves || speedCurves.length === 0) return;
        
        const curveTop = padding.top;
        const curveBottom = curveTop + areaHeight;
        
        let maxSpeed = 80;
        speedCurves.forEach(p => {
            maxSpeed = Math.max(maxSpeed, p.atc_limit, p.operational_limit);
        });
        maxSpeed = Math.ceil(maxSpeed / 10) * 10;
        
        const speedToY = (speed) => {
            const ratio = speed / maxSpeed;
            return curveBottom - ratio * areaHeight;
        };
        
        this.ctx.font = '8px JetBrains Mono, monospace';
        this.ctx.fillStyle = '#666';
        this.ctx.textAlign = 'right';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(`${maxSpeed}`, padding.left - 5, curveTop);
        this.ctx.fillText(`${Math.round(maxSpeed/2)}`, padding.left - 5, curveTop + areaHeight/2);
        this.ctx.fillText('0', padding.left - 5, curveBottom);
        
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();
        this.ctx.moveTo(padding.left, curveTop);
        this.ctx.lineTo(padding.left, curveBottom);
        this.ctx.stroke();
        
        // Draw horizontal reference lines every 20km/h
        this.ctx.strokeStyle = '#444';
        this.ctx.lineWidth = 0.5;
        this.ctx.setLineDash([2, 3]);
        for (let speed = 20; speed < maxSpeed; speed += 20) {
            const y = speedToY(speed);
            this.ctx.beginPath();
            this.ctx.moveTo(padding.left, y);
            this.ctx.lineTo(this.canvas.width - padding.right, y);
            this.ctx.stroke();
            
            // Draw speed label on the left with same color/size as y-axis labels
            this.ctx.font = '8px JetBrains Mono, monospace';
            this.ctx.fillStyle = '#666';
            this.ctx.textAlign = 'right';
            this.ctx.textBaseline = 'middle';
            this.ctx.fillText(`${speed}`, padding.left - 5, y);
        }
        this.ctx.setLineDash([]);
        
        // Draw solid reference line at 0 km/h (bottom of the speed curve area)
        this.ctx.strokeStyle = '#555';
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();
        this.ctx.moveTo(padding.left, curveBottom);
        this.ctx.lineTo(this.canvas.width - padding.right, curveBottom);
        this.ctx.stroke();
        
        this.ctx.strokeStyle = '#e83f5b';
        this.ctx.lineWidth = 1.5;
        this.ctx.beginPath();
        speedCurves.forEach((point, i) => {
            const x = cmToX(point.distance_cm);
            const y = speedToY(point.atc_limit);
            if (x < padding.left || x > this.canvas.width - padding.right) return;
            if (i === 0) this.ctx.moveTo(x, y);
            else this.ctx.lineTo(x, y);
        });
        this.ctx.stroke();
        
        this.ctx.strokeStyle = '#04d361';
        this.ctx.lineWidth = 2;
        this.ctx.beginPath();
        speedCurves.forEach((point, i) => {
            const x = cmToX(point.distance_cm);
            const y = speedToY(point.operational_limit);
            if (x < padding.left || x > this.canvas.width - padding.right) return;
            if (i === 0) this.ctx.moveTo(x, y);
            else this.ctx.lineTo(x, y);
        });
        this.ctx.stroke();
        
        if (currentSpeed > 0) {
            const currentY = speedToY(currentSpeed);
            const triangleSize = 6;
            const labelAreaWidth = 25;
            const tipX = padding.left - labelAreaWidth;
            const lineExtension = labelAreaWidth + 2;
            
            // Draw yellow line extending across the full y-axis label width and 2px into graph
            this.ctx.beginPath();
            this.ctx.strokeStyle = '#ffcd1e';
            this.ctx.lineWidth = 1.5;
            this.ctx.moveTo(tipX, currentY);
            this.ctx.lineTo(tipX + lineExtension, currentY);
            this.ctx.stroke();
            
            // Draw yellow triangle indicator on the left of y-axis
            this.ctx.beginPath();
            this.ctx.moveTo(tipX, currentY);
            this.ctx.lineTo(tipX - triangleSize, currentY - triangleSize/2);
            this.ctx.lineTo(tipX - triangleSize, currentY + triangleSize/2);
            this.ctx.closePath();
            this.ctx.fillStyle = '#ffcd1e';  // Yellow
            this.ctx.fill();
            this.ctx.strokeStyle = '#fff';
            this.ctx.lineWidth = 1;
            this.ctx.stroke();
            
            // Draw current speed value on the right
            this.ctx.fillStyle = '#ffcd1e';
            this.ctx.textAlign = 'left';
            this.ctx.fillText(`${Math.round(currentSpeed)}`, this.canvas.width - padding.right + 3, currentY);
        }
        
        if (targetSpeed > 0 && targetSpeed < maxSpeed) {
            const targetY = speedToY(targetSpeed);
            this.ctx.strokeStyle = '#00bcd4';
            this.ctx.lineWidth = 1;
            this.ctx.setLineDash([2, 4]);
            this.ctx.beginPath();
            this.ctx.moveTo(padding.left, targetY);
            this.ctx.lineTo(this.canvas.width - padding.right, targetY);
            this.ctx.stroke();
            this.ctx.setLineDash([]);
        }
        
        this.ctx.font = '8px Inter, sans-serif';
        const legendX = padding.left + 10;
        const legendY = curveTop + 8;
        
        this.ctx.fillStyle = '#e83f5b';
        this.ctx.fillRect(legendX, legendY - 4, 12, 2);
        this.ctx.fillStyle = '#999';
        this.ctx.textAlign = 'left';
        this.ctx.fillText('ATC', legendX + 15, legendY);
        
        this.ctx.fillStyle = '#04d361';
        this.ctx.fillRect(legendX + 45, legendY - 4, 12, 2);
        this.ctx.fillStyle = '#999';
        this.ctx.fillText('ATO', legendX + 60, legendY);
    }
}
