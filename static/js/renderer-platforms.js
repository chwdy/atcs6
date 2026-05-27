class RendererPlatformHandler {
    constructor(renderer) {
        this.renderer = renderer;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }
    get currentPlatforms() { return this.renderer.currentPlatforms; }
    get trackMap() { return this.renderer.trackMap; }
    get mouseX() { return this.renderer.mouseX; }
    get mouseY() { return this.renderer.mouseY; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

    drawPlatforms() {
        if (!this.currentPlatforms || this.currentPlatforms.length === 0) return;

        const tileSize = this.gridSize * this.scale;
        const half = tileSize / 2;
        const width = 10 * this.scale;
        this.renderer.platformBounds = [];

        // Pass 1: resolve geometry for every platform.
        const meta = [];
        for (const platform of this.currentPlatforms) {
            const m = this._resolvePlatformMeta(platform, half);
            if (m) meta.push(m);
        }

        // Cache body perimeters (used by label picker here and by train handler).
        this._platformBodySegments = meta.map(m => ({
            id: m.id,
            segments: this._computeBodySegments(m, half, width),
        }));

        // Pass 2: draw bodies then labels.
        for (const m of meta) {
            this.ctx.save();
            if (m.orientation && m.platformSides.length > 0) {
                this.drawOrientedPlatform(
                    m.point1, m.point2, m.orientation, m.platformSides, width, half,
                );
            }
            this.drawPlatformLabel(
                m.name, m.point1, m.point2, m.orientation, m.platformSides[0], half, m.id,
            );
            this.ctx.restore();
        }
    }

    _resolvePlatformMeta(platform, half) {
        if (!platform.positions || platform.positions.length === 0) return null;

        const positions = platform.positions;
        let orientation = platform.orientation || platform.initial_orientation;
        let platformSides = platform.platform_sides || [];
        const platformName = platform.name || platform.platform_id || platform.station_id || 'Platform';
        const platformId = platform.platform_id || platform.station_id || platformName;

        if (!orientation) {
            const firstPos = positions[0];
            const firstTrack = this.trackMap[`${firstPos[0]},${firstPos[1]}`];
            if (firstTrack) {
                const trackBits = firstTrack.track_bits || 0;
                if (trackBits & TRACK_BITS.NS) orientation = 'NS';
                else if (trackBits & TRACK_BITS.EW) orientation = 'EW';
                else if (trackBits & TRACK_BITS.NE || trackBits & TRACK_BITS.SW) orientation = 'NE_SW';
                else if (trackBits & TRACK_BITS.NW || trackBits & TRACK_BITS.SE) orientation = 'NW_SE';
            }
        }

        if (platformSides.length === 0 && orientation) {
            const sideMapping = {
                NS: { both: ['west', 'east'], left: ['west'], right: ['east'] },
                EW: { both: ['north', 'south'], left: ['north'], right: ['south'] },
                NE_SW: { both: ['northwest', 'southeast'], left: ['northwest'], right: ['southeast'] },
                NW_SE: { both: ['southwest', 'northeast'], left: ['southwest'], right: ['northeast'] }
            };
            const side = (platform.platform_side || 'both').toLowerCase();
            platformSides = sideMapping[orientation]?.[side] || ['west', 'east'];
        }

        const endpointTiles = this.findEndpointTiles(positions);
        if (!endpointTiles) return null;
        const [tile1, tile2] = endpointTiles;
        const track1 = this.trackMap[`${tile1[0]},${tile1[1]}`];
        const track2 = this.trackMap[`${tile2[0]},${tile2[1]}`];
        if (!track1 || !track2) return null;

        const endpoints1 = this.getPathEndpoints(tile1, track1.track_bits || 0, half);
        const endpoints2 = this.getPathEndpoints(tile2, track2.track_bits || 0, half);
        const longestPair = this.findLongestPair([...endpoints1, ...endpoints2]);
        if (!longestPair) return null;

        return {
            id: platformId,
            name: platformName,
            orientation,
            platformSides,
            point1: longestPair[0],
            point2: longestPair[1],
        };
    }

    _computeBodySegments(meta, half, width) {
        const { point1, point2, orientation, platformSides } = meta;
        let p1 = point1, p2 = point2;
        if (this.shouldSwapEndpoints(p1, p2, orientation)) [p1, p2] = [p2, p1];

        const dx = p2.x - p1.x, dy = p2.y - p1.y;
        const length = Math.hypot(dx, dy);
        if (length === 0) return [];

        const perpX = -dy / length, perpY = dx / length;
        const perpOffset = half * 0.3;
        const segs = [];

        for (const side of platformSides) {
            const sm = this.getSideMultiplier(side, orientation);
            const ox = perpX * perpOffset * sm, oy = perpY * perpOffset * sm;
            const innerS = { x: p1.x + ox, y: p1.y + oy };
            const innerE = { x: p2.x + ox, y: p2.y + oy };
            const outerS = { x: innerS.x + perpX * width * sm, y: innerS.y + perpY * width * sm };
            const outerE = { x: innerE.x + perpX * width * sm, y: innerE.y + perpY * width * sm };
            // 4 perimeter segments of the parallelogram body.
            segs.push(
                { x1: innerS.x, y1: innerS.y, x2: outerS.x, y2: outerS.y },
                { x1: outerS.x, y1: outerS.y, x2: outerE.x, y2: outerE.y },
                { x1: outerE.x, y1: outerE.y, x2: innerE.x, y2: innerE.y },
                { x1: innerE.x, y1: innerE.y, x2: innerS.x, y2: innerS.y },
            );
        }
        return segs;
    }

    getPlatformBodySegments(excludeId) {
        if (!this._platformBodySegments) return [];
        const out = [];
        for (const body of this._platformBodySegments) {
            if (body.id === excludeId) continue;
            for (const s of body.segments) out.push(s);
        }
        return out;
    }

    abbreviatePlatformName(name) {
        if (!name || name.length <= 4) return String(name || '').toUpperCase();

        const words = String(name).split(/[\s\-_]+/).filter(w => w.length > 0);
        if (words.length >= 2) {
            return words.slice(0, 4).map(w => w[0].toUpperCase()).join('');
        }

        const consonants = String(name).replace(/[aeiou]/gi, '').toUpperCase();
        if (consonants.length >= 3) {
            return consonants.slice(0, 4);
        }
        return String(name).slice(0, 4).toUpperCase();
    }

    drawPlatformLabel(fullName, point1, point2, orientation, side, half, selfId) {
        const abbrev = this.abbreviatePlatformName(fullName);

        const centerX = (point1.x + point2.x) / 2;
        const centerY = (point1.y + point2.y) / 2;

        const dx = point2.x - point1.x;
        const dy = point2.y - point1.y;
        const length = Math.sqrt(dx * dx + dy * dy);
        if (length === 0) return;

        const perpX = -dy / length;
        const perpY = dx / length;
        const dirX = dx / length;
        const dirY = dy / length;

        const sideMultiplier = this.getSideMultiplier(side, orientation);
        const spacingMultiplier = getLabelSpacingMultiplier();
        const labelOffset = half * spacingMultiplier + 18 * this.scale;
        const labelPadding = 3 * this.scale;

        // Pick position using the collapsed (abbrev) box so placement is stable
        // even when hover expands the text.
        this.ctx.font = `bold ${7 * this.scale}px sans-serif`;
        const abbrevWidth = this.ctx.measureText(abbrev).width;
        const collapsedW = abbrevWidth + labelPadding * 2;
        const collapsedH = 10 * this.scale;

        const { labelX, labelY } = this._pickPlatformLabelPosition(
            centerX, centerY, perpX, perpY, dirX, dirY, length,
            sideMultiplier, labelOffset, collapsedW, collapsedH, selfId,
        );

        // Hover hit-test: mouse must be inside the (collapsed) label rect itself.
        const collapsedBoxX = labelX - collapsedW / 2;
        const collapsedBoxY = labelY - collapsedH / 2;
        const isHovered =
            this.mouseX >= collapsedBoxX && this.mouseX <= collapsedBoxX + collapsedW &&
            this.mouseY >= collapsedBoxY && this.mouseY <= collapsedBoxY + collapsedH;

        const displayText = isHovered ? fullName : abbrev;
        const fontSize = isHovered ? 9 * this.scale : 7 * this.scale;
        this.ctx.font = `bold ${fontSize}px sans-serif`;
        const textWidth = this.ctx.measureText(displayText).width;
        const labelHeight = (isHovered ? 12 : 10) * this.scale;

        // Opaque enough that an overlapping label doesn't bleed through.
        // See the same fill rule on station and train labels.
        const bgAlpha = isHovered ? 0.92 : 0.85;
        this.ctx.fillStyle = `rgba(15, 15, 18, ${bgAlpha})`;
        this.ctx.fillRect(
            labelX - textWidth / 2 - labelPadding,
            labelY - labelHeight / 2,
            textWidth + labelPadding * 2,
            labelHeight
        );

        this.ctx.strokeStyle = isHovered ? 'rgba(255, 255, 255, 0.6)' : 'rgba(255, 255, 255, 0.3)';
        this.ctx.lineWidth = 1;
        this.ctx.strokeRect(
            labelX - textWidth / 2 - labelPadding,
            labelY - labelHeight / 2,
            textWidth + labelPadding * 2,
            labelHeight
        );

        this.ctx.fillStyle = '#ffffff';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(displayText, labelX, labelY);

        this.renderer.platformBounds.push({
            x: labelX,
            y: labelY,
            r: Math.max(collapsedW, collapsedH) / 2,
            name: fullName,
            abbrev,
        });
    }

    _pickPlatformLabelPosition(cx, cy, perpX, perpY, dirX, dirY, length, defaultSm, off, boxW, boxH, selfId) {
        // Candidates: current side / opposite side, each at center and at +/- shift along the platform.
        const along = length * 0.35;
        const candidates = [
            { sm:  defaultSm, alongT:  0  },  // default
            { sm: -defaultSm, alongT:  0  },  // flip side
            { sm:  defaultSm, alongT: -along },
            { sm:  defaultSm, alongT:  along },
            { sm: -defaultSm, alongT: -along },
            { sm: -defaultSm, alongT:  along },
        ];

        const obstacles = this.renderer.getNearbyTrackSegments(cx, cy, off * 3)
            .concat(this.getPlatformBodySegments(selfId));

        let bestX = cx, bestY = cy, bestScore = -Infinity;
        for (let i = 0; i < candidates.length; i++) {
            const c = candidates[i];
            const lx = cx + perpX * off * c.sm + dirX * c.alongT;
            const ly = cy + perpY * off * c.sm + dirY * c.alongT;
            const bx = lx - boxW / 2;
            const by = ly - boxH / 2;

            const dist = this.renderer.distRectToSegments(bx, by, boxW, boxH, obstacles);
            const distScore = Math.min(dist, 20);
            // Strongly prefer the default (matches existing platform_side semantics);
            // the picker only deviates when the default clearly overlaps.
            const preferenceBonus = (candidates.length - i) * 1.5;
            const score = distScore + preferenceBonus;

            if (score > bestScore) {
                bestScore = score;
                bestX = lx;
                bestY = ly;
            }
        }
        return { labelX: bestX, labelY: bestY };
    }

    findEndpointTiles(positions) {
        if (positions.length === 0) return null;
        if (positions.length === 1) return [positions[0], positions[0]];

        let maxDist = -1;
        let endpointPair = null;

        for (let i = 0; i < positions.length; i++) {
            for (let j = i + 1; j < positions.length; j++) {
                const dx = positions[j][0] - positions[i][0];
                const dy = positions[j][1] - positions[i][1];
                const dist = dx * dx + dy * dy;
                if (dist > maxDist) {
                    maxDist = dist;
                    endpointPair = [positions[i], positions[j]];
                }
            }
        }

        return endpointPair;
    }

    getPathEndpoints(tilePos, trackBits, half) {
        const centerX = this.gridToScreenX(tilePos[0], tilePos[1]);
        const centerY = this.gridToScreenY(tilePos[0], tilePos[1]);

        const edgePositions = {
            N: { x: centerX + half / 2, y: centerY - half / 2 },
            S: { x: centerX - half / 2, y: centerY + half / 2 },
            E: { x: centerX + half / 2, y: centerY + half / 2 },
            W: { x: centerX - half / 2, y: centerY - half / 2 }
        };

        const endpoints = [];

        if (trackBits & TRACK_BITS.NS) {
            endpoints.push(edgePositions.N, edgePositions.S);
        }
        if (trackBits & TRACK_BITS.EW) {
            endpoints.push(edgePositions.E, edgePositions.W);
        }
        if (trackBits & TRACK_BITS.NE) {
            endpoints.push(edgePositions.N, edgePositions.E);
        }
        if (trackBits & TRACK_BITS.NW) {
            endpoints.push(edgePositions.N, edgePositions.W);
        }
        if (trackBits & TRACK_BITS.SE) {
            endpoints.push(edgePositions.S, edgePositions.E);
        }
        if (trackBits & TRACK_BITS.SW) {
            endpoints.push(edgePositions.S, edgePositions.W);
        }

        return endpoints.slice(0, 2);
    }

    findLongestPair(points) {
        if (points.length < 2) return null;

        let maxDist = -1;
        let longestPair = null;

        for (let i = 0; i < points.length; i++) {
            for (let j = i + 1; j < points.length; j++) {
                const dx = points[j].x - points[i].x;
                const dy = points[j].y - points[i].y;
                const dist = dx * dx + dy * dy;
                if (dist > maxDist) {
                    maxDist = dist;
                    longestPair = [points[i], points[j]];
                }
            }
        }

        return longestPair;
    }

    drawOrientedPlatform(point1, point2, orientation, platformSides, width, half) {
        this.ctx.fillStyle = '#5a5a7a';
        this.ctx.strokeStyle = '#3a3a5a';
        this.ctx.lineWidth = 1 * this.scale;

        let p1 = point1;
        let p2 = point2;

        const needsSwap = this.shouldSwapEndpoints(p1, p2, orientation);
        if (needsSwap) {
            [p1, p2] = [p2, p1];
        }

        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;
        const length = Math.sqrt(dx * dx + dy * dy);
        if (length === 0) return;

        const dirX = dx / length;
        const dirY = dy / length;
        const perpX = -dirY;
        const perpY = dirX;

        const perpOffset = half * 0.3;

        for (const side of platformSides) {
            const sideMultiplier = this.getSideMultiplier(side, orientation);

            const offsetX = perpX * perpOffset * sideMultiplier;
            const offsetY = perpY * perpOffset * sideMultiplier;

            const innerStartX = p1.x + offsetX;
            const innerStartY = p1.y + offsetY;
            const innerEndX = p2.x + offsetX;
            const innerEndY = p2.y + offsetY;

            const outerStartX = innerStartX + perpX * width * sideMultiplier;
            const outerStartY = innerStartY + perpY * width * sideMultiplier;
            const outerEndX = innerEndX + perpX * width * sideMultiplier;
            const outerEndY = innerEndY + perpY * width * sideMultiplier;

            this.drawPlatformQuad(
                innerStartX, innerStartY,
                outerStartX, outerStartY,
                outerEndX, outerEndY,
                innerEndX, innerEndY
            );
        }
    }

    getSideMultiplier(side, orientation) {
        const sideMap = {
            NS: { east: 1, west: -1 },
            EW: { south: 1, north: -1 },
            NE_SW: { southeast: 1, northwest: -1 },
            NW_SE: { northeast: 1, southwest: -1 }
        };

        return sideMap[orientation]?.[side] || 1;
    }

    shouldSwapEndpoints(p1, p2, orientation) {
        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;

        switch (orientation) {
            case 'NS':
                return dy < 0;
            case 'EW':
                return dx > 0;
            case 'NE_SW':
                return (dx + dy) > 0;
            case 'NW_SE':
                return dx < 0;
            default:
                return false;
        }
    }

    drawPlatformQuad(x1, y1, x2, y2, x3, y3, x4, y4) {
        this.ctx.beginPath();
        this.ctx.moveTo(x1, y1);
        this.ctx.lineTo(x2, y2);
        this.ctx.lineTo(x3, y3);
        this.ctx.lineTo(x4, y4);
        this.ctx.closePath();
        this.ctx.fill();
        this.ctx.stroke();
    }
}
