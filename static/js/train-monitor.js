class TrainMonitor {
    constructor() {
        this.bar = document.getElementById('train-monitor-bar');
        this.canvas = document.getElementById('train-monitor-canvas');
        this.ctx = this.canvas.getContext('2d');
        this.titleEl = document.getElementById('train-monitor-title');
        this.currentSpeedEl = document.getElementById('monitor-current-speed');
        this.targetSpeedEl = document.getElementById('monitor-target-speed');
        this.waitReasonEl = document.getElementById('monitor-wait-reason');
        this.speedLimitReasonEl = document.getElementById('monitor-speed-limit-reason');
        this.speedCmsEl = document.getElementById('monitor-speed-cms');
        this.clearanceEl = document.getElementById('monitor-clearance');
        this.upcomingEl = document.getElementById('monitor-upcoming');
        this.closeBtn = document.getElementById('train-monitor-close');
        
        this.lookaheadData = null;
        this.trainId = null;
        this.currentScale = 160;
        
        this.loadScale();
        this.currentScale = 160;
        
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
    
    getTrainId() {
        return this.trainId;
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
    
    update(lookaheadData) {
        this.lookaheadData = lookaheadData;
        this.currentSpeedEl.textContent = Math.round(lookaheadData.current_speed_kmh || 0);
        this.targetSpeedEl.textContent = Math.round(lookaheadData.target_speed_kmh || 0);
        
        if (this.speedCmsEl) {
            const speedCms = ((lookaheadData.current_speed_kmh || 0) * 27.78 / this.currentScale).toFixed(1);
            this.speedCmsEl.textContent = speedCms;
        }
        if (this.clearanceEl) {
            const clearanceM = ((lookaheadData.clearance_cm || 0) / 100).toFixed(1);
            this.clearanceEl.textContent = clearanceM;
        }
        
        if (this.waitReasonEl) {
            if (lookaheadData.state === 'running_waiting' && lookaheadData.wait_reason) {
                this.waitReasonEl.textContent = lookaheadData.wait_reason;
                this.waitReasonEl.classList.remove('hidden');
            } else {
                this.waitReasonEl.classList.add('hidden');
            }
        }
        
        if (this.speedLimitReasonEl) {
            const speedLimitReason = lookaheadData.speed_limit_reason;
            if (speedLimitReason && lookaheadData.state !== 'running_waiting') {
                this.speedLimitReasonEl.textContent = speedLimitReason;
                this.speedLimitReasonEl.classList.remove('hidden');
            } else {
                this.speedLimitReasonEl.classList.add('hidden');
            }
        }
        
        this.updateUpcomingElements(lookaheadData);
        this.render();
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
                name: `Switch (${sw.x},${sw.y})`,
                distance: sw.distance_cm,
                length: 0,
                position: sw.position,
                sequence: sw.sequence || 0
            });
        });
        
        elements.sort((a, b) => {
            if (Math.abs(a.distance - b.distance) < 0.01) {
                return a.sequence - b.sequence;
            }
            return a.distance - b.distance;
        });
        
        const upcoming = elements.slice(0, 3);
        
        if (upcoming.length === 0) {
            this.upcomingEl.innerHTML = '<span class="text-secondary">No elements ahead</span>';
            return;
        }
        
        const trainDist = data.distance_into_path_cm || 0;
        
        this.upcomingEl.innerHTML = upcoming.map(el => {
            const distAhead = Math.max(0, el.distance - trainDist);
            const distM = (distAhead / 100).toFixed(1);
            const lengthM = (el.length / 100).toFixed(1);
            
            let icon = '▣';
            let colorClass = '';
            if (el.type === 'station') {
                icon = '🚉';
            } else if (el.type === 'switch') {
                icon = '⇋';
            } else if (el.type === 'block') {
                colorClass = `state-${el.state || 'unknown'}`;
            }
            
            return `<div class="upcoming-item ${colorClass}">
                <span class="upcoming-icon">${icon}</span>
                <span class="upcoming-name">${el.name}</span>
                <span class="upcoming-dist">${distM}m</span>
                ${el.length > 0 ? `<span class="upcoming-len">(${lengthM}m)</span>` : ''}
            </div>`;
        }).join('');
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
        
        const padding = { left: 10, right: 10, top: 8, bottom: 50 };
        const blockTrackY = height * 0.4;
        const stationTrackY = height * 0.65;
        const trackWidth = width - padding.left - padding.right;
        
        const trainDist = data.distance_into_path_cm || 0;
        
        const maxAheadElement = this.getMaxDistanceAhead(data, trainDist);
        const lookaheadDistance = Math.max(2000, Math.min(maxAheadElement + 1000, 10000));
        const visibleStartCm = trainDist;
        const visibleEndCm = trainDist + lookaheadDistance;
        const visibleRangeCm = visibleEndCm - visibleStartCm;
        const cmToX = (cm) => padding.left + ((cm - visibleStartCm) / visibleRangeCm) * trackWidth;
        
        this.drawTrackLine(padding.left, blockTrackY, trackWidth, '#333');
        this.drawBlocks(data.blocks || [], cmToX, blockTrackY);
        this.drawSwitches(data.switches || [], cmToX, blockTrackY);
        this.drawSignals(data.signals || [], cmToX, blockTrackY);
        
        if ((data.stations || []).length > 0) {
            this.drawTrackLine(padding.left, stationTrackY, trackWidth, '#2a4a5f');
            this.drawStations(data.stations || [], cmToX, stationTrackY);
        }
        
        const trainX = cmToX(trainDist);
        this.drawTrainPosition(trainX, blockTrackY);
        if ((data.stations || []).length > 0) {
            this.drawTrainPosition(trainX, stationTrackY);
        }
        
        this.drawDistanceScale(padding.left, height - 5, trackWidth, trainDist, visibleRangeCm);
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
        const blockHeight = 16;
        
        blocks.forEach(block => {
            const startX = cmToX(block.start_cm);
            const endX = cmToX(block.end_cm);
            
            if (endX < 0 || startX > this.canvas.width) return;
            
            const blockWidth = Math.max(endX - startX, 2);
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
            
            if (blockWidth > 30) {
                const name = block.name || block.id || '';
                const displayName = name.length > 12 ? name.substring(0, 10) + '..' : name;
                this.ctx.font = '9px Inter, sans-serif';
                this.ctx.fillStyle = '#fff';
                this.ctx.textAlign = 'center';
                this.ctx.textBaseline = 'middle';
                this.ctx.fillText(displayName, startX + blockWidth/2, trackY);
            }
            
            const labelY = trackY + blockHeight/2 + 12;
            
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
            
            const name = station.name || station.station_id || 'Station';
            const displayName = name.length > 12 ? name.substring(0, 10) + '..' : name;
            this.ctx.font = '9px Inter, sans-serif';
            this.ctx.fillStyle = '#fff';
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'middle';
            this.ctx.fillText(displayName, startX + stationWidth/2, trackY);
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
}
