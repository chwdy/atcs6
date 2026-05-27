class RendererBlockHandler {
    constructor(renderer) {
        this.renderer = renderer;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }
    get currentBlocks() { return this.renderer.currentBlocks; }
    get blockColorCache() { return this.renderer.blockColorCache; }
    set blockColorCache(value) { this.renderer.blockColorCache = value; }
    get blockColorCounter() { return this.renderer.blockColorCounter; }
    set blockColorCounter(value) { this.renderer.blockColorCounter = value; }
    get mouseX() { return this.renderer.mouseX; }
    get mouseY() { return this.renderer.mouseY; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

    getBlockColor(blockId) {
        if (!blockId) return null;

        if (this.blockColorCache[blockId] !== undefined) {
            const hue = this.blockColorCache[blockId];
            return `hsla(${hue}, 70%, 50%, 0.6)`;
        }

        const goldenAngle = 137.508;
        const hue = (this.blockColorCounter * goldenAngle) % 360;

        this.blockColorCache[blockId] = hue;
        this.blockColorCounter++;

        return `hsla(${hue}, 70%, 50%, 0.6)`;
    }

    drawBlockIdColors(tracks) {
        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;

        for (const track of tracks) {
            if (!track.block_section_id) continue;

            const cx = this.gridToScreenX(track.x, track.y);
            const cy = this.gridToScreenY(track.x, track.y);
            const color = this.getBlockColor(track.block_section_id);

            this.ctx.save();
            this.ctx.translate(cx, cy);

            const trackBits = track.track_bits || 0;
            const segments = getActiveSegments(trackBits);
            const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

            for (const [e1, e2] of segments) {
                const start = edgePoints[e1];
                const end = edgePoints[e2];
                if (start && end) {
                    this.renderer.trackHandler.drawGridSegment(start, end, DEBUG_BLOCK_WIDTH * this.scale, e1, e2, color);
                }
            }

            this.ctx.restore();
        }
    }

    drawBlockBoundaryMarkers(tracks) {
        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;

        const trackMap = {};
        for (const track of tracks) {
            trackMap[`${track.x},${track.y}`] = track;
        }

        const neighborDeltas = {
            'N': { dx: 0, dy: 1 },
            'E': { dx: 1, dy: 0 },
            'S': { dx: 0, dy: -1 },
            'W': { dx: -1, dy: 0 }
        };

        for (const track of tracks) {
            if (!track.block_section_id) continue;

            const cx = this.gridToScreenX(track.x, track.y);
            const cy = this.gridToScreenY(track.x, track.y);
            const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

            const trackBits = track.track_bits || 0;
            const connectedEdges = trackBitsToEdges(trackBits);

            const segments = getActiveSegments(trackBits);

            for (const edge of connectedEdges) {
                const delta = neighborDeltas[edge];
                if (!delta) continue;

                const neighborX = track.x + delta.dx;
                const neighborY = track.y + delta.dy;
                const neighbor = trackMap[`${neighborX},${neighborY}`];

                if (!neighbor) continue;

                const neighborBlockId = neighbor.block_section_id;
                if (neighborBlockId === track.block_section_id) continue;

                const ep = edgePoints[edge];
                if (!ep) continue;

                let segmentPartner = null;
                for (const [e1, e2] of segments) {
                    if (e1 === edge) {
                        segmentPartner = e2;
                        break;
                    } else if (e2 === edge) {
                        segmentPartner = e1;
                        break;
                    }
                }

                if (!segmentPartner) continue;

                const epStart = edgePoints[edge];
                const epEnd = edgePoints[segmentPartner];
                const dx = epEnd.x - epStart.x;
                const dy = epEnd.y - epStart.y;
                const length = Math.sqrt(dx * dx + dy * dy);

                const markerDepth = (TRACK_WIDTH * this.scale) / 2;
                const segmentStart = epStart;
                const segmentEnd = {
                    x: epStart.x + (dx / length) * markerDepth,
                    y: epStart.y + (dy / length) * markerDepth
                };

                const opposite_edge = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' }[edge];
                this.ctx.save();
                this.ctx.translate(cx, cy);
                this.renderer.trackHandler.drawGridSegment(
                    segmentStart,
                    segmentEnd,
                    TRACK_WIDTH * this.scale,
                    edge,
                    opposite_edge,
                    '#666'
                );
                this.ctx.restore();
            }
        }
    }

    drawBlockDetailLabels(tracks) {
        const blockTracks = {};
        for (const track of tracks) {
            if (!track.block_section_id) continue;
            if (!blockTracks[track.block_section_id]) {
                blockTracks[track.block_section_id] = [];
            }
            blockTracks[track.block_section_id].push(track);
        }

        const tileSize = this.gridSize * this.scale;
        const halfTile = tileSize / 2;

        let hoveredBlockId = null;
        let hoveredBlockData = null;

        for (const [blockId, blockTrackList] of Object.entries(blockTracks)) {
            if (blockTrackList.length === 0) continue;

            const centerIndex = Math.floor(blockTrackList.length / 2);
            const centerTrack = blockTrackList[centerIndex];
            const cx = this.gridToScreenX(centerTrack.x, centerTrack.y);
            const cy = this.gridToScreenY(centerTrack.x, centerTrack.y);

            const offsetY = tileSize * 0.35;
            const offsetX = tileSize * 0.3;
            const neOffsetX = 10;
            const neOffsetY = -10;

            const labelX = cx + offsetX + neOffsetX;
            const labelY = cy + offsetY + neOffsetY;
            const labelWidth = 100 * this.scale;
            const labelHeight = 40 * this.scale;

            if (this.mouseX >= labelX - 5 && this.mouseX <= labelX + labelWidth + 5 &&
                this.mouseY >= labelY - 5 && this.mouseY <= labelY + labelHeight + 5) {
                hoveredBlockId = blockId;
                hoveredBlockData = {
                    blockTrackList,
                    centerTrack,
                    cx, cy
                };
                break;
            }
        }

        for (const [blockId, blockTrackList] of Object.entries(blockTracks)) {
            if (blockId === hoveredBlockId) continue;
            if (blockTrackList.length === 0) continue;

            const centerIndex = Math.floor(blockTrackList.length / 2);
            const centerTrack = blockTrackList[centerIndex];
            const cx = this.gridToScreenX(centerTrack.x, centerTrack.y);
            const cy = this.gridToScreenY(centerTrack.x, centerTrack.y);

            const blockInfo = this.currentBlocks && this.currentBlocks[blockId]
                ? this.currentBlocks[blockId]
                : {};
            const blockName = blockInfo.name || '';
            const shortId = blockId.length > 10 ? blockId.slice(-6) : blockId;
            const displayName = blockName || shortId;

            // Use dynamic spacing based on user preference
            const spacingMultiplier = getLabelSpacingMultiplier();
            const offsetY = tileSize * spacingMultiplier;
            const offsetX = tileSize * (spacingMultiplier - 0.05);
            const neOffsetX = 10;
            const neOffsetY = -10;

            this.ctx.save();

            const fontSize = Math.max(5, 8 * this.scale);
            this.ctx.font = `${fontSize}px sans-serif`;
            this.ctx.textAlign = 'left';
            this.ctx.textBaseline = 'top';
            const lineHeight = fontSize + 2;

            const line1 = displayName;
            const line1Width = this.ctx.measureText(line1).width;

            const padding = 1;
            const boxX = cx + offsetX + neOffsetX - padding;
            const boxY = cy + offsetY + neOffsetY - padding;
            const boxWidth = line1Width + padding * 2;
            const boxHeight = lineHeight + padding * 2;

            this.ctx.fillStyle = 'rgba(13, 16, 32, 0.7)';
            this.ctx.strokeStyle = 'rgba(100, 100, 150, 0.4)';
            this.ctx.lineWidth = 1;
            this.ctx.beginPath();
            this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
            this.ctx.fill();
            this.ctx.stroke();

            this.ctx.fillStyle = 'rgba(221, 221, 221, 0.7)';
            this.ctx.fillText(line1, cx + offsetX + neOffsetX, cy + offsetY + neOffsetY);

            this.ctx.restore();
        }

        if (hoveredBlockId && hoveredBlockData) {
            const { blockTrackList, centerTrack, cx, cy } = hoveredBlockData;
            const blockInfo = this.currentBlocks && this.currentBlocks[hoveredBlockId]
                ? this.currentBlocks[hoveredBlockId]
                : {};

            this.ctx.save();
            const blockColor = this.getBlockColor(hoveredBlockId);
            const edgePoints = this.renderer.trackHandler.getEdgePoints(halfTile);

            for (const track of blockTrackList) {
                const trackCx = this.gridToScreenX(track.x, track.y);
                const trackCy = this.gridToScreenY(track.x, track.y);

                this.ctx.save();
                this.ctx.translate(trackCx, trackCy);

                const trackBits = track.track_bits || 0;
                const segments = getActiveSegments(trackBits);

                this.ctx.strokeStyle = blockColor.replace('0.6', '0.9');
                this.ctx.lineWidth = 12 * this.scale;
                this.ctx.lineCap = 'butt';

                for (const [e1, e2] of segments) {
                    const start = edgePoints[e1];
                    const end = edgePoints[e2];
                    if (start && end) {
                        this.ctx.beginPath();
                        this.ctx.moveTo(start.x, start.y);
                        this.ctx.lineTo(end.x, end.y);
                        this.ctx.stroke();
                    }
                }

                this.ctx.restore();
            }

            this.ctx.restore();

            const blockName = blockInfo.name || '';
            const blockState = (blockInfo.state || 'FREE').toUpperCase();
            const stateColor = getBlockStateColor(blockState);

            const shortId = hoveredBlockId.length > 10 ? hoveredBlockId.slice(-6) : hoveredBlockId;
            const displayName = blockName || shortId;

            // Use dynamic spacing based on user preference (same as non-hovered)
            const spacingMultiplier = getLabelSpacingMultiplier();
            const offsetY = tileSize * spacingMultiplier;
            const offsetX = tileSize * (spacingMultiplier - 0.05);
            const neOffsetX = 10;
            const neOffsetY = -10;

            this.ctx.save();

            const fontSize = Math.max(5, 8 * this.scale);
            this.ctx.font = `${fontSize}px sans-serif`;
            this.ctx.textAlign = 'left';
            this.ctx.textBaseline = 'top';
            const lineHeight = fontSize + 2;
            const trackCount = blockTrackList.length;

            const line1 = displayName;
            const line2 = `${blockState} ${trackCount} tiles`;
            const lengthStr = blockInfo.length_cm ? `${blockInfo.length_cm}cm` : '-';
            const radiusStr = blockInfo.min_radius_cm ? `R${blockInfo.min_radius_cm}` : '-';
            const speedStr = blockInfo.speed_limit_kmh ? `${blockInfo.speed_limit_kmh}km/h` : '-';
            const line3 = `${lengthStr} ${radiusStr} ${speedStr}`;

            const maxWidth = Math.max(
                this.ctx.measureText(line1).width,
                this.ctx.measureText(line2).width,
                this.ctx.measureText(line3).width
            );

            const padding = 1;
            const boxX = cx + offsetX + neOffsetX - padding;
            const boxY = cy + offsetY + neOffsetY - padding;
            const boxWidth = maxWidth + padding * 2;
            const boxHeight = lineHeight * 3 + padding * 2;

            this.ctx.fillStyle = 'rgba(13, 16, 32, 1.0)';
            this.ctx.strokeStyle = 'rgba(100, 100, 150, 1.0)';
            this.ctx.lineWidth = 1;
            this.ctx.beginPath();
            this.ctx.roundRect(boxX, boxY, boxWidth, boxHeight, 3);
            this.ctx.fill();
            this.ctx.stroke();

            let yPos = cy + offsetY + neOffsetY;

            this.ctx.fillStyle = '#ddd';
            this.ctx.fillText(line1, cx + offsetX + neOffsetX, yPos);
            yPos += lineHeight;

            this.ctx.fillStyle = stateColor;
            this.ctx.fillText(line2, cx + offsetX + neOffsetX, yPos);
            yPos += lineHeight;

            this.ctx.fillStyle = '#999';
            this.ctx.fillText(line3, cx + offsetX + neOffsetX, yPos);

            this.ctx.restore();
        }
    }

    drawBlockPreviewPath(path) {
        if (!path || path.length === 0) return;

        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.8)';
        this.ctx.fillStyle = 'rgba(130, 87, 229, 0.3)';
        this.ctx.lineWidth = 4 * this.scale;
        this.ctx.setLineDash([6, 4]);

        const cellSize = this.gridSize * this.scale;
        const halfTile = cellSize / 2;

        for (const pos of path) {
            const cx = this.gridToScreenX(pos.x, pos.y);
            const cy = this.gridToScreenY(pos.x, pos.y);

            this.ctx.beginPath();
            this.ctx.moveTo(cx, cy - halfTile);
            this.ctx.lineTo(cx + halfTile, cy);
            this.ctx.lineTo(cx, cy + halfTile);
            this.ctx.lineTo(cx - halfTile, cy);
            this.ctx.closePath();
            this.ctx.fill();

            const boxSize = halfTile * 0.3;
            this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
            this.ctx.lineWidth = 2 * this.scale;
            this.ctx.setLineDash([]);
            this.ctx.strokeRect(cx - boxSize, cy - boxSize, boxSize * 2, boxSize * 2);
            this.ctx.setLineDash([6, 4]);
            this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.8)';
        }

        this.ctx.setLineDash([]);
        this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.6)';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.beginPath();
        for (let i = 0; i < path.length; i++) {
            const cx = this.gridToScreenX(path[i].x, path[i].y);
            const cy = this.gridToScreenY(path[i].x, path[i].y);
            if (i === 0) this.ctx.moveTo(cx, cy);
            else this.ctx.lineTo(cx, cy);
        }
        this.ctx.stroke();

        this.ctx.restore();
    }
}
