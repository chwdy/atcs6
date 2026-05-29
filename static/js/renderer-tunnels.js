class RendererTunnelHandler {
    constructor(renderer) {
        this.renderer = renderer;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

    drawTunnels(tunnels, pendingEndpoint, opts = {}) {
        if (!Array.isArray(tunnels)) tunnels = [];
        const { showLinks = false } = opts;

        if (showLinks) {
            tunnels.forEach(pair => this._drawLink(pair.a, pair.b));
        }

        // Assign a stable per-pair label "T1", "T2" by index of the
        // pair record. The backend already collapses the symmetric
        // dict into one record per pair.
        tunnels.forEach((pair, idx) => {
            const label = `T${idx + 1}`;
            this._drawMouth(pair.a, label, '#9b59ff');
            this._drawMouth(pair.b, label, '#9b59ff');
        });

        if (pendingEndpoint) {
            this._drawMouth(pendingEndpoint, '?', '#ffd93d', { pending: true });
        }
    }

    _drawLink(epA, epB) {
        const aPos = this._endpointScreenPos(epA);
        const bPos = this._endpointScreenPos(epB);
        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(155, 89, 255, 0.75)';
        this.ctx.lineWidth = Math.max(1, 1.5 * this.scale);
        this.ctx.setLineDash([6 * this.scale, 4 * this.scale]);
        this.ctx.beginPath();
        this.ctx.moveTo(aPos.x, aPos.y);
        this.ctx.lineTo(bPos.x, bPos.y);
        this.ctx.stroke();
        this.ctx.restore();
    }

    _endpointScreenPos(ep) {
        const cellSize = this.gridSize * this.scale;
        const half = cellSize / 2;
        const cx = this.gridToScreenX(ep.x, ep.y);
        const cy = this.gridToScreenY(ep.x, ep.y);
        const edge = String(ep.edge).toUpperCase();
        const edgePoints = this.renderer.trackHandler.getEdgePoints(half);
        const edgePoint = edgePoints[edge] || { x: 0, y: 0 };
        return { x: cx + edgePoint.x, y: cy + edgePoint.y };
    }

    _drawMouth(ep, label, color, opts = {}) {
        const { pending = false } = opts;
        const cellSize = this.gridSize * this.scale;
        const half = cellSize / 2;
        const cx = this.gridToScreenX(ep.x, ep.y);
        const cy = this.gridToScreenY(ep.x, ep.y);
        const edge = String(ep.edge).toUpperCase();

        // Push the glyph to the tile's edge midpoint in screen space.
        // Edge offsets here are in the isometric world the renderer uses;
        // each cardinal maps to one of the four tile corners (see
        // getEdgePoints in renderer-tracks.js).
        const edgePoints = this.renderer.trackHandler.getEdgePoints(half);
        const edgePoint = edgePoints[edge] || { x: 0, y: 0 };

        this.ctx.save();
        this.ctx.translate(cx + edgePoint.x, cy + edgePoint.y);

        // Outer ring.
        this.ctx.beginPath();
        this.ctx.arc(0, 0, 9 * this.scale, 0, Math.PI * 2);
        this.ctx.fillStyle = pending ? 'rgba(255, 217, 61, 0.25)' : 'rgba(155, 89, 255, 0.18)';
        this.ctx.fill();
        this.ctx.strokeStyle = color;
        this.ctx.lineWidth = Math.max(1.5, 2 * this.scale);
        this.ctx.stroke();

        // Label.
        this.ctx.fillStyle = '#fff';
        this.ctx.font = `${Math.max(9, 10 * this.scale)}px Inter, sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(label, 0, 0);

        this.ctx.restore();
    }
}
