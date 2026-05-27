// renderer-stations.js — draws a single label per station group at the
// centroid of its platforms. Reuses the shared track-/platform-avoiding
// label picker. Hovering a station label expands it (full name) and
// highlights the member platform bodies.

class RendererStationHandler {
    constructor(renderer) {
        this.renderer = renderer;
        // Cached per-frame placements: [{ stationId, labelX, labelY, boxW, boxH, platformIds, fullName, abbrev }]
        this._placements = [];
        // Station id under the mouse this frame, or null. Other modules
        // (e.g. editor selection) can read this on click.
        this.hoveredStationId = null;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }
    get mouseX() { return this.renderer.mouseX; }
    get mouseY() { return this.renderer.mouseY; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

    drawStations() {
        const stations = this.renderer.currentNetwork?.stations;
        if (!stations || !stations.length) {
            this._placements = [];
            this.hoveredStationId = null;
            return;
        }

        // Step 1: place every station's label.
        this._placements = stations.map(s => this._placeLabel(s)).filter(Boolean);

        // Step 2: hover hit-test against the placed (collapsed) rects.
        this.hoveredStationId = null;
        for (const p of this._placements) {
            if (this._mouseInRect(p.labelX, p.labelY, p.boxW, p.boxH)) {
                this.hoveredStationId = p.stationId;
                break;
            }
        }

        // Step 3: if a station is hovered, draw highlights for its platforms
        // (drawn BEFORE labels so the label sits on top).
        if (this.hoveredStationId) {
            const hovered = this._placements.find(p => p.stationId === this.hoveredStationId);
            if (hovered) this._drawPlatformHighlights(hovered.platformIds);
        }

        // Step 4: draw every label.
        for (const p of this._placements) {
            const isHovered = p.stationId === this.hoveredStationId;
            this._drawLabel(p, isHovered);
        }
    }

    abbreviate(name) {
        if (!name) return '';
        const words = String(name).split(/[\s\-_]+/).filter(w => w.length > 0);
        if (words.length >= 2) {
            // "Station 1" -> "S1", "Central Junction" -> "CJ"
            return words.slice(0, 4).map(w => w[0].toUpperCase()).join('');
        }
        const consonants = String(name).replace(/[aeiou]/gi, '').toUpperCase();
        if (consonants.length >= 2) return consonants.slice(0, 3);
        return String(name).slice(0, 3).toUpperCase();
    }

    _placeLabel(station) {
        if (!station) return null;
        // Prefer the backend-computed centroid; fall back to averaging the
        // platforms' positions if it's missing.
        let gridX, gridY;
        if (Array.isArray(station.center_position) && station.center_position.length >= 2) {
            [gridX, gridY] = station.center_position;
        } else if (Array.isArray(station.platforms)) {
            let sx = 0, sy = 0, n = 0;
            for (const pf of station.platforms) {
                for (const pos of (pf.positions || [])) {
                    sx += pos[0]; sy += pos[1]; n++;
                }
            }
            if (n === 0) return null;
            gridX = sx / n; gridY = sy / n;
        } else {
            return null;
        }

        const cx = this.gridToScreenX(gridX, gridY);
        const cy = this.gridToScreenY(gridX, gridY);

        const name = station.name || station.station_id || 'Station';
        const abbrev = this.abbreviate(name);

        // Measure the collapsed label so placement is stable across hover.
        this.ctx.save();
        const s = this.scale;
        const fontSize = 8 * s;
        this.ctx.font = `bold ${fontSize}px sans-serif`;
        const textWidth = this.ctx.measureText(abbrev).width;
        const padding = 4 * s;
        const boxW = textWidth + padding * 2;
        const boxH = 12 * s;
        this.ctx.restore();

        // The station label sits AT the centroid so it visually anchors to
        // the platform group's middle. We only allow tiny perturbations
        // (half a tile) when the centroid lands directly on a platform body
        // — the label is meant to be "between" the platforms, not on one.
        const tileSize = this.gridSize * s;
        const platformBodies = this.renderer.platformHandler?.getPlatformBodySegments?.() || [];
        const step = tileSize * 0.5;
        const candidates = [
            { dx:  0, dy:  0 },   // centroid — strongly preferred
            { dx:  0, dy: -step }, // small nudges if centroid sits on a body
            { dx:  0, dy:  step },
            { dx: -step, dy: 0 },
            { dx:  step, dy: 0 },
        ];

        let bestX = cx, bestY = cy, bestScore = -Infinity;
        for (let i = 0; i < candidates.length; i++) {
            const c = candidates[i];
            const lx = cx + c.dx;
            const ly = cy + c.dy;
            const bx = lx - boxW / 2;
            const by = ly - boxH / 2;
            // Only platform BODIES count as obstacles; tracks are thin and
            // the label sitting on a track is acceptable + matches user intent.
            const dist = this.renderer.distRectToSegments(bx, by, boxW, boxH, platformBodies);
            // Heavy preference for the centroid (i=0). Only nudge if the
            // centroid badly overlaps a body (dist near 0).
            const distScore = Math.min(dist, 12);
            const centroidBias = i === 0 ? 8 : 0;
            const score = distScore + centroidBias - i * 0.1;
            if (score > bestScore) {
                bestScore = score;
                bestX = lx;
                bestY = ly;
            }
        }

        const platformIds = Array.isArray(station.platform_ids) ? station.platform_ids
            : (station.platforms || []).map(pf => pf.platform_id);

        return {
            stationId: station.station_id,
            fullName: name,
            abbrev,
            labelX: bestX,
            labelY: bestY,
            boxW,
            boxH,
            platformIds,
        };
    }

    _drawLabel(placement, isHovered) {
        const s = this.scale;
        const { labelX, labelY, abbrev, fullName } = placement;
        const displayText = isHovered ? fullName : abbrev;
        const fontSize = isHovered ? 10 * s : 8 * s;
        const padding = 4 * s;

        this.ctx.save();
        this.ctx.font = `bold ${fontSize}px sans-serif`;
        const textWidth = this.ctx.measureText(displayText).width;
        const boxH = (isHovered ? 14 : 12) * s;
        const boxW = textWidth + padding * 2;
        const bx = labelX - boxW / 2;
        const by = labelY - boxH / 2;
        const radius = 4 * s;

        // Body: amber-tinted to distinguish from platform labels.
        this.ctx.fillStyle = isHovered ? 'rgba(255, 184, 28, 0.95)' : 'rgba(255, 184, 28, 0.85)';
        this._roundRect(bx, by, boxW, boxH, radius);
        this.ctx.fill();

        // Border
        this.ctx.strokeStyle = isHovered ? 'rgba(255, 255, 255, 0.9)' : 'rgba(0, 0, 0, 0.4)';
        this.ctx.lineWidth = isHovered ? 1.5 * s : 1 * s;
        this._roundRect(bx, by, boxW, boxH, radius);
        this.ctx.stroke();

        // Text
        this.ctx.fillStyle = '#1a1a1a';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(displayText, labelX, labelY);

        this.ctx.restore();
    }

    _drawPlatformHighlights(platformIds) {
        const ph = this.renderer.platformHandler;
        if (!ph?._platformBodySegments) return;
        const s = this.scale;
        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 184, 28, 0.95)';
        this.ctx.lineWidth = 2.5 * s;
        this.ctx.lineJoin = 'round';
        for (const body of ph._platformBodySegments) {
            if (!platformIds.includes(body.id)) continue;
            for (const seg of body.segments) {
                this.ctx.beginPath();
                this.ctx.moveTo(seg.x1, seg.y1);
                this.ctx.lineTo(seg.x2, seg.y2);
                this.ctx.stroke();
            }
        }
        this.ctx.restore();
    }

    _mouseInRect(cx, cy, w, h) {
        const mx = this.mouseX, my = this.mouseY;
        if (mx < 0 || my < 0) return false;
        const bx = cx - w / 2, by = cy - h / 2;
        return mx >= bx && mx <= bx + w && my >= by && my <= by + h;
    }

    /**
     * Resolve the station id at a given canvas-relative point, or null.
     * Useful for click-to-select wiring.
     */
    stationAt(x, y) {
        for (const p of this._placements) {
            const bx = p.labelX - p.boxW / 2;
            const by = p.labelY - p.boxH / 2;
            if (x >= bx && x <= bx + p.boxW && y >= by && y <= by + p.boxH) {
                return p.stationId;
            }
        }
        return null;
    }

    _roundRect(x, y, w, h, r) {
        this.ctx.beginPath();
        this.ctx.moveTo(x + r, y);
        this.ctx.lineTo(x + w - r, y);
        this.ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        this.ctx.lineTo(x + w, y + h - r);
        this.ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        this.ctx.lineTo(x + r, y + h);
        this.ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        this.ctx.lineTo(x, y + r);
        this.ctx.quadraticCurveTo(x, y, x + r, y);
        this.ctx.closePath();
    }
}
