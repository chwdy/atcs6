class TrainMonitor {
    constructor() {
        this.bar = document.getElementById('train-monitor-bar');
        this.canvas = document.getElementById('train-monitor-canvas');
        this.ctx = this.canvas.getContext('2d');
        this.titleEl = document.getElementById('train-monitor-title');
        this.currentSpeedEl = document.getElementById('monitor-current-speed');
        this.targetSpeedEl = document.getElementById('monitor-target-speed');
        this.closeBtn = document.getElementById('train-monitor-close');
        
        this.lookaheadData = null;
        this.trainId = null;
        
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
        this.render();
    }
    
    render() {
        const data = this.lookaheadData;
        if (!data) return;
        
        const rect = this.canvas.getBoundingClientRect();
        const width = rect.width;
        const height = rect.height;
        
        this.ctx.clearRect(0, 0, width, height);
        
        const padding = { left: 10, right: 10, top: 8, bottom: 20 };
        const trackY = height - padding.bottom - 10;
        const trackWidth = width - padding.left - padding.right;
        
        const maxDistanceCm = data.max_distance_cm || 100000;
        const cmToX = (cm) => padding.left + (cm / maxDistanceCm) * trackWidth;
        
        this.drawTrack(padding.left, trackY, trackWidth);
        this.drawTrainPosition(padding.left, trackY);
        this.drawBlocks(data.blocks || [], cmToX, trackY);
        this.drawSignals(data.signals || [], cmToX, trackY);
        this.drawStations(data.stations || [], cmToX, trackY, height);
        this.drawDistanceScale(padding.left, height - 5, trackWidth, maxDistanceCm);
    }
    
    drawTrack(x, y, width) {
        this.ctx.strokeStyle = '#444';
        this.ctx.lineWidth = 4;
        this.ctx.beginPath();
        this.ctx.moveTo(x, y);
        this.ctx.lineTo(x + width, y);
        this.ctx.stroke();
    }
    
    drawTrainPosition(x, y) {
        this.ctx.fillStyle = '#ffcd1e';
        this.ctx.beginPath();
        this.ctx.moveTo(x, y - 8);
        this.ctx.lineTo(x + 10, y);
        this.ctx.lineTo(x, y + 8);
        this.ctx.closePath();
        this.ctx.fill();
    }
    
    drawBlocks(blocks, cmToX, trackY) {
        const stateColors = {
            'free': '#04d361',
            'reserved': '#8257e5',
            'occupied': '#e83f5b',
            'caution': '#ffcd1e'
        };
        
        blocks.forEach(block => {
            const startX = cmToX(block.start_cm);
            const endX = cmToX(block.end_cm);
            const blockWidth = Math.max(endX - startX, 4);
            
            this.ctx.fillStyle = stateColors[block.state] || '#666';
            this.ctx.globalAlpha = 0.4;
            this.ctx.fillRect(startX, trackY - 6, blockWidth, 12);
            this.ctx.globalAlpha = 1.0;
            
            this.ctx.strokeStyle = stateColors[block.state] || '#666';
            this.ctx.lineWidth = 1;
            this.ctx.strokeRect(startX, trackY - 6, blockWidth, 12);
        });
    }
    
    drawSignals(signals, cmToX, trackY) {
        const signalColors = {
            'green': '#04d361',
            'yellow': '#ffcd1e',
            'double_yellow': '#ffcd1e',
            'red': '#e83f5b'
        };
        
        signals.forEach(signal => {
            const x = cmToX(signal.distance_cm);
            const state = signal.state ? signal.state.toLowerCase() : null;
            const color = signalColors[state] || '#888';
            
            this.ctx.strokeStyle = '#666';
            this.ctx.lineWidth = 2;
            this.ctx.beginPath();
            this.ctx.moveTo(x, trackY - 6);
            this.ctx.lineTo(x, trackY - 25);
            this.ctx.stroke();
            
            this.ctx.fillStyle = color;
            this.ctx.beginPath();
            this.ctx.arc(x, trackY - 30, 5, 0, Math.PI * 2);
            this.ctx.fill();
            
            if (state === 'double_yellow') {
                this.ctx.beginPath();
                this.ctx.arc(x, trackY - 40, 5, 0, Math.PI * 2);
                this.ctx.fill();
            }
        });
    }
    
    drawStations(stations, cmToX, trackY, height) {
        stations.forEach(station => {
            const x = cmToX(station.distance_cm);
            
            this.ctx.fillStyle = '#3498db';
            this.ctx.fillRect(x - 2, trackY - 20, 4, 14);
            
            this.ctx.font = '10px Inter, sans-serif';
            this.ctx.fillStyle = '#a8a8b3';
            this.ctx.textAlign = 'center';
            const name = station.name || station.station_id || 'Station';
            const displayName = name.length > 10 ? name.substring(0, 10) + '...' : name;
            this.ctx.fillText(displayName, x, trackY - 24);
        });
    }
    
    drawDistanceScale(x, y, width, maxCm) {
        this.ctx.font = '9px JetBrains Mono, monospace';
        this.ctx.fillStyle = '#666';
        this.ctx.textAlign = 'left';
        this.ctx.fillText('0m', x, y);
        
        this.ctx.textAlign = 'center';
        const midM = Math.round(maxCm / 200);
        this.ctx.fillText(`${midM}m`, x + width / 2, y);
        
        this.ctx.textAlign = 'right';
        const maxM = Math.round(maxCm / 100);
        this.ctx.fillText(`${maxM}m`, x + width, y);
    }
}
