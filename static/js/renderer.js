class Renderer {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        this.gridSize = 40;
        this.offsetX = 0;
        this.offsetY = 0;
        this.scale = 1.0;

        this.resize();
        window.addEventListener('resize', () => this.resize());
    }

    resize() {
        this.canvas.width = this.canvas.parentElement.clientWidth;
        this.canvas.height = this.canvas.parentElement.clientHeight;
        this.draw();
    }

    clear() {
        this.ctx.fillStyle = '#0a0a0c';
        this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    }

    drawGrid() {
        this.ctx.save();
        this.ctx.strokeStyle = '#1e1e24';
        this.ctx.lineWidth = 1;

        const startX = Math.floor(-this.offsetX / this.scale / this.gridSize) * this.gridSize;
        const startY = Math.floor(-this.offsetY / this.scale / this.gridSize) * this.gridSize;

        const endX = startX + (this.canvas.width / this.scale) + this.gridSize;
        const endY = startY + (this.canvas.height / this.scale) + this.gridSize;

        this.ctx.translate(this.offsetX, this.offsetY);
        this.ctx.scale(this.scale, this.scale);

        this.ctx.beginPath();
        for (let x = startX; x <= endX; x += this.gridSize) {
            this.ctx.moveTo(x, startY);
            this.ctx.lineTo(x, endY);
        }
        for (let y = startY; y <= endY; y += this.gridSize) {
            this.ctx.moveTo(startX, y);
            this.ctx.lineTo(endX, y);
        }
        this.ctx.stroke();
        this.ctx.restore();
    }

    drawNetwork(network) {
        this.clear();
        this.drawGrid();

        if (!network || !network.tracks) return;

        this.ctx.save();
        this.ctx.translate(this.offsetX, this.offsetY);
        this.ctx.scale(this.scale, this.scale);

        // Draw tracks
        network.tracks.forEach(track => {
            this.drawTrack(track);
        });

        // Draw trains
        if (network.trains) {
            network.trains.forEach(train => {
                this.drawTrain(train);
            });
        }

        this.ctx.restore();
    }

    drawTrack(track) {
        const x = track.x * this.gridSize;
        const y = -track.y * this.gridSize; // Flip Y axis for standard cartesian
        const cx = x + this.gridSize / 2;
        const cy = y + this.gridSize / 2;

        this.ctx.save();
        this.ctx.translate(cx, cy);

        // Base track style
        this.ctx.strokeStyle = '#555';
        this.ctx.lineWidth = 4;
        this.ctx.lineCap = 'round';

        if (track.occupied_by) {
            this.ctx.strokeStyle = '#e83f5b'; // Red if occupied
        } else if (track.reserved_by) {
            this.ctx.strokeStyle = '#ffcd1e'; // Yellow if reserved
        }

        if (track.type === 'STRAIGHT') {
            // Determine orientation based on connections
            // Default horizontal
            let rotation = 0;
            if (track.connections && track.connections.NORTH) {
                rotation = Math.PI / 2;
            }
            this.ctx.rotate(rotation);

            this.ctx.beginPath();
            this.ctx.moveTo(-this.gridSize / 2, 0);
            this.ctx.lineTo(this.gridSize / 2, 0);
            this.ctx.stroke();

            // Draw sleepers
            this.ctx.strokeStyle = '#333';
            this.ctx.lineWidth = 2;
            for (let i = -4; i <= 4; i += 2) {
                this.ctx.beginPath();
                this.ctx.moveTo(i * this.gridSize / 10, -3);
                this.ctx.lineTo(i * this.gridSize / 10, 3);
                this.ctx.stroke();
            }

        } else if (track.type === 'CURVE') {
            // Simplified curve drawing
            // Need to know entry/exit to rotate properly
            // For now, just draw a circle arc
            this.ctx.beginPath();
            this.ctx.arc(-this.gridSize / 2, -this.gridSize / 2, this.gridSize, 0, Math.PI / 2);
            this.ctx.stroke();
        } else if (track.type === 'SWITCH') {
            this.ctx.beginPath();
            this.ctx.moveTo(-this.gridSize / 2, 0);
            this.ctx.lineTo(this.gridSize / 2, 0);
            this.ctx.stroke();

            // Diverging part
            this.ctx.beginPath();
            this.ctx.moveTo(-this.gridSize / 2, 0);
            this.ctx.quadraticCurveTo(0, 0, this.gridSize / 2, -this.gridSize / 2);
            this.ctx.stroke();
        } else if (track.type === 'STATION') {
            this.ctx.fillStyle = '#333';
            this.ctx.fillRect(-this.gridSize / 2 + 5, -this.gridSize / 2 + 5, this.gridSize - 10, this.gridSize - 10);

            this.ctx.fillStyle = '#fff';
            this.ctx.font = '10px sans-serif';
            this.ctx.textAlign = 'center';
            this.ctx.fillText('STA', 0, 4);
        } else if (track.type === 'SIGNAL') {
            this.ctx.beginPath();
            this.ctx.moveTo(-this.gridSize / 2, 0);
            this.ctx.lineTo(this.gridSize / 2, 0);
            this.ctx.stroke();

            // Signal head
            this.ctx.fillStyle = track.signal_state === 'GREEN' ? '#04d361' : '#e83f5b';
            this.ctx.beginPath();
            this.ctx.arc(0, -5, 4, 0, Math.PI * 2);
            this.ctx.fill();
        }

        this.ctx.restore();
    }

    drawTrain(train) {
        if (!train.position) return;

        const x = train.position.x * this.gridSize;
        const y = -train.position.y * this.gridSize;
        const cx = x + this.gridSize / 2;
        const cy = y + this.gridSize / 2;

        this.ctx.save();
        this.ctx.translate(cx, cy);

        // Rotate based on direction
        let rotation = 0;
        if (train.direction === 'NORTH') rotation = -Math.PI / 2;
        else if (train.direction === 'SOUTH') rotation = Math.PI / 2;
        else if (train.direction === 'WEST') rotation = Math.PI;

        this.ctx.rotate(rotation);

        // Draw train body
        this.ctx.fillStyle = '#8257e5';
        this.ctx.fillRect(-15, -6, 30, 12);

        // Draw cab
        this.ctx.fillStyle = '#9466ff';
        this.ctx.fillRect(5, -6, 10, 12);

        // Draw ID
        this.ctx.fillStyle = '#fff';
        this.ctx.font = '8px sans-serif';
        this.ctx.textAlign = 'center';
        this.ctx.fillText(train.id, 0, 3);

        this.ctx.restore();
    }

    // Helper to convert screen coords to grid coords
    screenToGrid(screenX, screenY) {
        const x = Math.floor((screenX - this.offsetX) / this.scale / this.gridSize);
        const y = -Math.floor((screenY - this.offsetY) / this.scale / this.gridSize); // Flip Y back
        return { x, y };
    }
}
