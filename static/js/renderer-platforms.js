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
        this.renderer.platformBounds = [];

        for (const platform of this.currentPlatforms) {
            if (!platform.positions || platform.positions.length === 0) continue;

            const positions = platform.positions;
            if (positions.length === 0) continue;

            let orientation = platform.orientation || platform.initial_orientation;
            let platformSides = platform.platform_sides || [];
            const platformName = platform.name || platform.platform_id || platform.station_id || 'Platform';

            if (!orientation && positions.length > 0) {
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
            if (!endpointTiles) continue;

            const [tile1, tile2] = endpointTiles;
            const track1 = this.trackMap[`${tile1[0]},${tile1[1]}`];
            const track2 = this.trackMap[`${tile2[0]},${tile2[1]}`];
            if (!track1 || !track2) continue;

            const endpoints1 = this.getPathEndpoints(tile1, track1.track_bits || 0, half);
            const endpoints2 = this.getPathEndpoints(tile2, track2.track_bits || 0, half);

            const longestPair = this.findLongestPair([...endpoints1, ...endpoints2]);
            if (!longestPair) continue;

            this.ctx.save();

            if (orientation && platformSides.length > 0) {
                this.drawOrientedPlatform(
                    longestPair[0], longestPair[1], orientation, platformSides,
                    10 * this.scale, half
                );
            }

            this.drawPlatformLabel(
                platformName,
                longestPair[0], longestPair[1],
                orientation, platformSides[0],
                half
            );

            this.ctx.restore();
        }
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

    drawPlatformLabel(fullName, point1, point2, orientation, side, half) {
        const abbrev = this.abbreviatePlatformName(fullName);

        const centerX = (point1.x + point2.x) / 2;
        const centerY = (point1.y + point2.y) / 2;

        const dx = point2.x - point1.x;
        const dy = point2.y - point1.y;
        const length = Math.sqrt(dx * dx + dy * dy);
        if (length === 0) return;

        const perpX = -dy / length;
        const perpY = dx / length;

        const sideMultiplier = this.getSideMultiplier(side, orientation);
        const spacingMultiplier = getLabelSpacingMultiplier();
        const labelOffset = half * spacingMultiplier + 18 * this.scale;
        const labelX = centerX + perpX * labelOffset * sideMultiplier;
        const labelY = centerY + perpY * labelOffset * sideMultiplier;

        const hoverRadius = 30 * this.scale;
        const distToMouse = Math.sqrt(
            Math.pow(this.mouseX - centerX, 2) +
            Math.pow(this.mouseY - centerY, 2)
        );
        const isHovered = distToMouse < hoverRadius + length / 2;

        const displayText = isHovered ? fullName : abbrev;

        const fontSize = isHovered ? 9 * this.scale : 7 * this.scale;
        this.ctx.font = `bold ${fontSize}px sans-serif`;

        const textWidth = this.ctx.measureText(displayText).width;
        const labelPadding = 3 * this.scale;
        const labelHeight = (isHovered ? 12 : 10) * this.scale;

        const bgAlpha = isHovered ? 0.8 : 0.5;
        this.ctx.fillStyle = `rgba(0, 0, 0, ${bgAlpha})`;
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
            x: centerX,
            y: centerY,
            r: hoverRadius + length / 2,
            name: fullName,
            abbrev,
        });
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
