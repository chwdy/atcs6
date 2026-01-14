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

    getEdgePoints(half) {
        const h = half;
        return {
            'N': { x: h / 2, y: -h / 2 },
            'E': { x: h / 2, y: h / 2 },
            'S': { x: -h / 2, y: h / 2 },
            'W': { x: -h / 2, y: -h / 2 }
        };
    }

    /**
     * Get possible exit edges for a track given an entry edge.
     * Mirrors the backend TrackSegment.get_exits() logic.
     */
    getTrackExits(track, entryEdge) {
        if (!track || !entryEdge) return [];
        
        const bits = track.track_bits || 0;
        const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;
        const OPPOSITE = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' };
        
        // Get all possible exits from this entry
        const exits = [];
        
        // For each track type, if entry is valid, add the exit
        if (bits & TRACK_NS) {
            if (entryEdge === 'N') exits.push('S');
            if (entryEdge === 'S') exits.push('N');
        }
        if (bits & TRACK_EW) {
            if (entryEdge === 'E') exits.push('W');
            if (entryEdge === 'W') exits.push('E');
        }
        if (bits & TRACK_NE) {
            if (entryEdge === 'N') exits.push('E');
            if (entryEdge === 'E') exits.push('N');
        }
        if (bits & TRACK_NW) {
            if (entryEdge === 'N') exits.push('W');
            if (entryEdge === 'W') exits.push('N');
        }
        if (bits & TRACK_SE) {
            if (entryEdge === 'S') exits.push('E');
            if (entryEdge === 'E') exits.push('S');
        }
        if (bits & TRACK_SW) {
            if (entryEdge === 'S') exits.push('W');
            if (entryEdge === 'W') exits.push('S');
        }
        
        // For switches, apply switch state to get the active exit
        if (track.is_switch && track.switch_state !== undefined && exits.length > 1) {
            // switch_state: 0 = first option, 1 = second option
            return [exits[track.switch_state] || exits[0]];
        }
        
        return exits;
    }

    drawTrain(train) {
        // Use new TrainPosition format with end_a/end_b
        const position = train.position;
        if (!position || !position.end_a) return;

        // Determine head position based on leading_end
        const headEnd = position.leading_end === 'a' ? position.end_a : position.end_b;
        const tailEnd = position.leading_end === 'a' ? position.end_b : position.end_a;
        
        const baseX = headEnd.tile[0];
        const baseY = headEnd.tile[1];
        const entryEdge = headEnd.entry_edge;
        const direction = position.facing_direction || train.direction || 'EAST';
        
        // Get track to determine exit edge
        const tracks = this.currentNetwork && this.currentNetwork.tracks
            ? this.currentNetwork.tracks
            : [];
        const track = tracks.find(t => t.x === baseX && t.y === baseY);
        
        // Get tile length from occupied_tiles_rich (more accurate) or fallback
        const occupiedRich = train.occupied_tiles_rich || [];
        const headTileRich = occupiedRich.find(t => 
            t.tile[0] === baseX && t.tile[1] === baseY
        );
        const tileLengthCm = headTileRich?.tile_length_cm || 50.0;
        const distance = Math.min(1.0, Math.max(0.0, headEnd.distance_cm / tileLengthCm));
        
        // Determine exit edge from track geometry
        let exitEdge = null;
        if (track && entryEdge) {
            const exits = this.getTrackExits(track, entryEdge);
            if (exits && exits.length > 0) {
                exitEdge = exits[0];
            }
        }
        
        const cx = this.gridToScreenX(baseX, baseY);
        const cy = this.gridToScreenY(baseX, baseY);
        const s = this.scale;
        const tileSize = this.gridSize * s;
        const half = tileSize / 2;

        let trainScreenX = cx;
        let trainScreenY = cy;
        let rotation = 0;

        if (entryEdge && exitEdge) {
            const edgePoints = this.getEdgePoints(half);
            const startPt = edgePoints[entryEdge];
            const endPt = edgePoints[exitEdge];
            
            if (startPt && endPt) {
                trainScreenX = cx + startPt.x + (endPt.x - startPt.x) * distance;
                trainScreenY = cy + startPt.y + (endPt.y - startPt.y) * distance;
                
                const dx = endPt.x - startPt.x;
                const dy = endPt.y - startPt.y;
                rotation = Math.atan2(dy, dx);
            }
        } else {
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
            
            trainScreenX = this.gridToScreenX(interpX, interpY);
            trainScreenY = this.gridToScreenY(interpX, interpY);
            
            if (direction === 'NORTH') rotation = -Math.PI / 4;
            else if (direction === 'EAST') rotation = Math.PI / 4;
            else if (direction === 'SOUTH') rotation = Math.PI * 3 / 4;
            else if (direction === 'WEST') rotation = -Math.PI * 3 / 4;
        }

        this.ctx.save();
        this.ctx.translate(trainScreenX, trainScreenY);
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

        // Draw tail indicator (square) at train's rear end
        this.drawTrainTail(train, tailEnd, tracks, s);

        const trackBits = track ? track.track_bits : 0;
        const quadrant = getBestLabelQuadrant(trackBits, train.direction || 'EAST');
        const offset = QUADRANT_OFFSETS[quadrant];

        const labelX = trainScreenX + offset.dx * tileSize;
        const labelY = trainScreenY + offset.dy * tileSize;

        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
        this.ctx.lineWidth = 1 * s;
        this.ctx.beginPath();
        this.ctx.moveTo(trainScreenX, trainScreenY);
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

    /**
     * Draw tail indicator (square) at train's rear end.
     */
    drawTrainTail(train, tailEnd, tracks, s) {
        if (!tailEnd || !tailEnd.tile) return;

        const tailX = tailEnd.tile[0];
        const tailY = tailEnd.tile[1];
        const tailEntryEdge = tailEnd.entry_edge;

        // Get track at tail position
        const tailTrack = tracks.find(t => t.x === tailX && t.y === tailY);
        if (!tailTrack) return;

        // Get tile length from occupied_tiles_rich (more accurate) or fallback
        const occupiedRich = train.occupied_tiles_rich || [];
        const tailTileRich = occupiedRich.find(t => 
            t.tile[0] === tailX && t.tile[1] === tailY
        );
        const tileLengthCm = tailTileRich?.tile_length_cm || 50.0;
        const tailDistance = Math.min(1.0, Math.max(0.0, tailEnd.distance_cm / tileLengthCm));

        // Get exit edge for tail
        let tailExitEdge = null;
        if (tailEntryEdge) {
            const exits = this.getTrackExits(tailTrack, tailEntryEdge);
            if (exits && exits.length > 0) {
                tailExitEdge = exits[0];
            }
        }

        const cx = this.gridToScreenX(tailX, tailY);
        const cy = this.gridToScreenY(tailX, tailY);
        const tileSize = this.gridSize * s;
        const half = tileSize / 2;

        let tailScreenX = cx;
        let tailScreenY = cy;
        let rotation = 0;

        if (tailEntryEdge && tailExitEdge) {
            const edgePoints = this.getEdgePoints(half);
            const startPt = edgePoints[tailEntryEdge];
            const endPt = edgePoints[tailExitEdge];

            if (startPt && endPt) {
                tailScreenX = cx + startPt.x + (endPt.x - startPt.x) * tailDistance;
                tailScreenY = cy + startPt.y + (endPt.y - startPt.y) * tailDistance;

                const dx = endPt.x - startPt.x;
                const dy = endPt.y - startPt.y;
                rotation = Math.atan2(dy, dx);
            }
        } else {
            // Fallback: use direction-based positioning
            const direction = train.position?.facing_direction || train.direction || 'EAST';
            const positionOffset = tailDistance - 0.5;
            let interpX = tailX;
            let interpY = tailY;

            if (direction === 'NORTH') {
                interpY = tailY + positionOffset;
                rotation = -Math.PI / 4;
            } else if (direction === 'SOUTH') {
                interpY = tailY - positionOffset;
                rotation = Math.PI * 3 / 4;
            } else if (direction === 'EAST') {
                interpX = tailX + positionOffset;
                rotation = Math.PI / 4;
            } else if (direction === 'WEST') {
                interpX = tailX - positionOffset;
                rotation = -Math.PI * 3 / 4;
            }

            tailScreenX = this.gridToScreenX(interpX, interpY);
            tailScreenY = this.gridToScreenY(interpX, interpY);
        }

        // Draw tail square
        this.ctx.save();
        this.ctx.translate(tailScreenX, tailScreenY);
        this.ctx.rotate(rotation);

        const sqSize = 5 * s;
        this.ctx.fillStyle = '#aaaaaa';
        this.ctx.fillRect(-sqSize / 2, -sqSize / 2, sqSize, sqSize);

        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.strokeRect(-sqSize / 2, -sqSize / 2, sqSize, sqSize);

        this.ctx.restore();
    }
}
