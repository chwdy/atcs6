class RendererTrainHandler {
    constructor(renderer) {
        this.renderer = renderer;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }
    get currentNetwork() { return this.renderer.currentNetwork; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

    drawTrain(train) {
        if (!train.position) return;

        const baseX = train.position.x;
        const baseY = train.position.y;
        const distance = train.position.distance || 0.0;
        const direction = train.direction || 'EAST';
        
        const positionOffset = distance - 0.5;
        
        let interpX = baseX;
        let interpY = baseY;
        
        if (direction === 'NORTH') {
            interpY = baseY + positionOffset;
        } else if (direction === 'SOUTH') {
            interpY = baseY - positionOffset;
        } else if (direction === 'EAST') {
            interpX = baseX + positionOffset;
        } else if (direction === 'WEST') {
            interpX = baseX - positionOffset;
        }

        const cx = this.gridToScreenX(interpX, interpY);
        const cy = this.gridToScreenY(interpX, interpY);
        const s = this.scale;
        const tileSize = this.gridSize * s;

        let rotation = 0;
        if (direction === 'NORTH') rotation = -Math.PI / 4;
        else if (direction === 'EAST') rotation = Math.PI / 4;
        else if (direction === 'SOUTH') rotation = Math.PI * 3 / 4;
        else if (direction === 'WEST') rotation = -Math.PI * 3 / 4;

        this.ctx.save();
        this.ctx.translate(cx, cy);
        this.ctx.rotate(rotation);

        const triSize = 8 * s;
        this.ctx.fillStyle = '#ffffff';
        this.ctx.beginPath();
        this.ctx.moveTo(triSize, 0);
        this.ctx.lineTo(-triSize * 0.6, -triSize * 0.6);
        this.ctx.lineTo(-triSize * 0.6, triSize * 0.6);
        this.ctx.closePath();
        this.ctx.fill();

        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.stroke();

        this.ctx.restore();

        const tracks = this.currentNetwork && this.currentNetwork.tracks
            ? this.currentNetwork.tracks
            : [];
        const track = tracks.find(t => t.x === baseX && t.y === baseY);
        const trackBits = track ? track.track_bits : 0;
        const quadrant = getBestLabelQuadrant(trackBits, train.direction || 'EAST');
        const offset = QUADRANT_OFFSETS[quadrant];

        const labelX = cx + offset.dx * tileSize;
        const labelY = cy + offset.dy * tileSize;

        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
        this.ctx.lineWidth = 1 * s;
        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(labelX, labelY);
        this.ctx.stroke();
        this.ctx.restore();

        const labelText = train.id;
        this.ctx.save();
        this.ctx.font = `bold ${9 * s}px sans-serif`;
        const textMetrics = this.ctx.measureText(labelText);
        const textWidth = textMetrics.width;
        const textHeight = 9 * s;
        const padding = 4 * s;
        const boxWidth = textWidth + padding * 2;
        const boxHeight = textHeight + padding * 1.5;

        this.ctx.fillStyle = 'rgba(130, 87, 229, 0.9)';
        const cornerRadius = 3 * s;
        const boxX = labelX - boxWidth / 2;
        const boxY = labelY - boxHeight / 2;

        this.ctx.beginPath();
        this.ctx.moveTo(boxX + cornerRadius, boxY);
        this.ctx.lineTo(boxX + boxWidth - cornerRadius, boxY);
        this.ctx.quadraticCurveTo(boxX + boxWidth, boxY, boxX + boxWidth, boxY + cornerRadius);
        this.ctx.lineTo(boxX + boxWidth, boxY + boxHeight - cornerRadius);
        this.ctx.quadraticCurveTo(boxX + boxWidth, boxY + boxHeight, boxX + boxWidth - cornerRadius, boxY + boxHeight);
        this.ctx.lineTo(boxX + cornerRadius, boxY + boxHeight);
        this.ctx.quadraticCurveTo(boxX, boxY + boxHeight, boxX, boxY + boxHeight - cornerRadius);
        this.ctx.lineTo(boxX, boxY + cornerRadius);
        this.ctx.quadraticCurveTo(boxX, boxY, boxX + cornerRadius, boxY);
        this.ctx.closePath();
        this.ctx.fill();

        this.ctx.fillStyle = '#ffffff';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(labelText, labelX, labelY);
        this.ctx.restore();
    }
}
