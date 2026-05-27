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

    // Identity tier: place labels, run hover hit-test, draw the non-hovered
    // labels. Called early in the render loop so anything live (trains, route
    // overlays) renders on top — a static station label must not occlude a
    // moving train. The hovered label is deferred to drawHoverOverlay().
    drawStations() {
        const stations = this.renderer.currentNetwork?.stations;
        if (!stations || !stations.length) {
            this._placements = [];
            this.hoveredStationId = null;
            return;
        }

        this._placements = stations.map(s => this._placeLabel(s)).filter(Boolean);

        this.hoveredStationId = null;
        for (const p of this._placements) {
            if (this._mouseInRect(p.labelX, p.labelY, p.boxW, p.boxH)) {
                this.hoveredStationId = p.stationId;
                break;
            }
        }

        for (const p of this._placements) {
            if (p.stationId === this.hoveredStationId) continue;
            this._drawLabel(p, false);
            // Register the station rect into the shared label registry so
            // train labels can avoid overlap. The hovered station is excluded
            // because its expanded overlay paints AFTER trains (drawHoverOverlay)
            // — its bounds shouldn't constrain train placement.
            this.renderer.labelBounds.push({
                x: p.labelX - p.boxW / 2,
                y: p.labelY - p.boxH / 2,
                w: p.boxW,
                h: p.boxH,
                tier: 'station',
            });
        }
    }

    // Hover tier: the hovered label + its platform highlights, drawn at the
    // top of the canvas stack so user attention always wins over liveness.
    // Called at the end of the render loop.
    drawHoverOverlay() {
        if (!this.hoveredStationId) return;
        const hovered = this._placements.find(p => p.stationId === this.hoveredStationId);
        if (!hovered) return;
        this._drawPlatformHighlights(hovered.platformIds);
        this._drawLabel(hovered, true);
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
        const fontSize = 9 * s;
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
        // Stations sit one tier above platform labels (platforms are 7px / 9px
        // hover). Size carries the hierarchy; chrome stays neutral so identity
        // doesn't compete with state.
        const fontSize = isHovered ? 11 * s : 9 * s;
        const padding = 4 * s;

        this.ctx.save();
        this.ctx.font = `bold ${fontSize}px sans-serif`;
        const textWidth = this.ctx.measureText(displayText).width;
        const boxH = (isHovered ? 14 : 12) * s;
        const boxW = textWidth + padding * 2;
        const bx = labelX - boxW / 2;
        const by = labelY - boxH / 2;
        const radius = 4 * s;

        // Fills are opaque (≥85%) so an overlapping label doesn't show
        // through and read as visual mush. Categorical color lives in the
        // border + a small mix into the dark base, not in a saturated fill.
        // Default: near-opaque neutral dark. Hover: dark base mixed with
        // ~15% amber so the warning hue is perceptible without becoming an
        // alert pill.
        const fillColor = isHovered ? 'rgba(50, 35, 5, 0.92)' : 'rgba(15, 15, 18, 0.92)';
        const borderColor = isHovered ? 'rgba(255, 205, 30, 0.9)' : 'rgba(255, 255, 255, 0.45)';
        // Borders are screen-pixel capped so they read as hairlines at any
        // zoom — see the same pattern in _drawPlatformHighlights and the
        // train label.
        const borderWidth = isHovered
            ? Math.min(Math.max(1.2, s * 0.4), 2.5)
            : Math.min(Math.max(0.8, s * 0.25), 1.5);

        this.ctx.fillStyle = fillColor;
        this._roundRect(bx, by, boxW, boxH, radius);
        this.ctx.fill();

        this.ctx.strokeStyle = borderColor;
        this.ctx.lineWidth = borderWidth;
        this._roundRect(bx, by, boxW, boxH, radius);
        this.ctx.stroke();

        this.ctx.fillStyle = '#ffffff';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(displayText, labelX, labelY);

        this.ctx.restore();
    }

    _drawPlatformHighlights(platformIds) {
        const ph = this.renderer.platformHandler;
        if (!ph?._platformBodySegments) return;
        const s = this.scale;
        // Highlights are context for the hovered label, not the focus. Cap the
        // stroke at ~2.5px screen width so it doesn't grow into a wall of amber
        // at high zoom, and match the label's border color exactly so the eye
        // reads them as one connected gesture.
        const strokeW = Math.min(Math.max(1.5, s * 0.5), 2.5);

        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 205, 30, 0.7)';
        this.ctx.lineWidth = strokeW;
        this.ctx.lineJoin = 'round';
        this.ctx.lineCap = 'round';

        for (const body of ph._platformBodySegments) {
            if (!platformIds.includes(body.id)) continue;
            // The body's segments come in groups of 4 per platform side
            // (parallelogram perimeter: innerS->outerS->outerE->innerE->innerS).
            // Draw each group as one closed path so corners join cleanly.
            for (let i = 0; i + 3 < body.segments.length; i += 4) {
                const seg0 = body.segments[i];
                const seg1 = body.segments[i + 1];
                const seg2 = body.segments[i + 2];
                this.ctx.beginPath();
                this.ctx.moveTo(seg0.x1, seg0.y1);
                this.ctx.lineTo(seg0.x2, seg0.y2);
                this.ctx.lineTo(seg1.x2, seg1.y2);
                this.ctx.lineTo(seg2.x2, seg2.y2);
                this.ctx.closePath();
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
