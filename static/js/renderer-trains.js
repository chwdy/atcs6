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

    // ========================================================================
    // Main Entry Point
    // ========================================================================

    drawTrain(train) {
        // Check for new path-based position
        if (train.path_tiles && train.path_tiles.length > 0 && train.path_tile_info) {
            this.drawTrainFromPath(train);
            return;
        }
        
        // Fall back to legacy rendering
        this.drawTrainLegacy(train);
    }

    // ========================================================================
    // Legacy Rendering (for trains without path data)
    // ========================================================================

    drawTrainLegacy(train) {
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

        // Draw tail if TrainPosition end_b data is available
        if (train.position.end_b) {
            const tailEnd = train.position.leading_end === 'a' 
                ? train.position.end_b 
                : train.position.end_a;
            this.drawTrainTailLegacy(train, tailEnd, tracks, s);
        }
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

    /**
     * Draw tail indicator (square) for legacy rendering using TrainPosition end_b data.
     */
    drawTrainTailLegacy(train, tailEnd, tracks, s) {
        if (!tailEnd || !tailEnd.tile) return;

        const tailX = tailEnd.tile[0];
        const tailY = tailEnd.tile[1];
        const tailEntryEdge = tailEnd.entry_edge;

        // Get track at tail position
        const tailTrack = tracks.find(t => t.x === tailX && t.y === tailY);
        if (!tailTrack) return;

        // Get tile length from occupied_tiles_rich or fallback
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
        this.ctx.fillStyle = '#ffffff';
        this.ctx.fillRect(-sqSize / 2, -sqSize / 2, sqSize, sqSize);

        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.strokeRect(-sqSize / 2, -sqSize / 2, sqSize, sqSize);

        this.ctx.restore();
    }

    /**
     * Get edge points for isometric tile positioning.
     */
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
        
        const exits = [];
        
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
            return [exits[track.switch_state] || exits[0]];
        }
        
        return exits;
    }

    // ========================================================================
    // Path-Based Rendering Helper Methods
    // ========================================================================

    /**
     * Get edge center point in isometric screen coordinates relative to tile center.
     * @param {string} edge - 'N', 'E', 'S', 'W'
     * @returns {{x: number, y: number}} Offset from tile center in screen coords
     */
    getEdgeCenterOffset(edge) {
        const h = this.gridSize * this.scale / 2;
        // Isometric: N = up-right, E = down-right, S = down-left, W = up-left
        const edgeOffsets = {
            'N': { x: h / 2, y: -h / 2 },   // up-right
            'E': { x: h / 2, y: h / 2 },    // down-right  
            'S': { x: -h / 2, y: h / 2 },   // down-left
            'W': { x: -h / 2, y: -h / 2 }   // up-left
        };
        return edgeOffsets[edge] || { x: 0, y: 0 };
    }

    /**
     * Get exit edge from entry edge based on track bits.
     * @param {number} trackBits - 6-bit track configuration
     * @param {string} entryEdge - Entry edge ('N', 'E', 'S', 'W')
     * @returns {string|null} Exit edge or null if not found
     */
    getExitEdge(trackBits, entryEdge) {
        const BIT_TO_EDGES = [
            ['N', 'S'],  // bit 0: NS
            ['E', 'W'],  // bit 1: EW
            ['N', 'E'],  // bit 2: NE
            ['N', 'W'],  // bit 3: NW
            ['S', 'E'],  // bit 4: SE
            ['S', 'W'],  // bit 5: SW
        ];
        
        for (let bit = 0; bit < 6; bit++) {
            if (trackBits & (1 << bit)) {
                const [a, b] = BIT_TO_EDGES[bit];
                if (a === entryEdge) return b;
                if (b === entryEdge) return a;
            }
        }
        return null;
    }

    /**
     * Get the opposite edge.
     * @param {string} edge - Edge direction ('N', 'E', 'S', 'W')
     * @returns {string} Opposite edge
     */
    getOppositeEdge(edge) {
        const opposites = { 'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E' };
        return opposites[edge] || edge;
    }

    /**
     * Calculate screen position for a point along a tile's path.
     * @param {Array} tile - [x, y] tile coordinates
     * @param {string} entryEdge - Edge the train entered from
     * @param {number} distance_cm - Distance traveled from entry edge
     * @param {number} tile_length_cm - Total length of path through tile
     * @param {number} trackBits - 6-bit track configuration
     * @returns {{x: number, y: number, rotation: number}} Screen position and rotation
     */
    getTilePathPosition(tile, entryEdge, distance_cm, tile_length_cm, trackBits) {
        const x = tile[0];
        const y = tile[1];
        
        // Get tile center in screen coords
        const centerX = this.gridToScreenX(x, y);
        const centerY = this.gridToScreenY(x, y);
        
        // Get entry and exit edge offsets
        const entryOffset = this.getEdgeCenterOffset(entryEdge);
        const exitEdge = this.getExitEdge(trackBits, entryEdge);
        const exitOffset = exitEdge ? this.getEdgeCenterOffset(exitEdge) : entryOffset;
        
        // Calculate progress along path (0 = entry, 1 = exit)
        const progress = tile_length_cm > 0 ? Math.min(1.0, distance_cm / tile_length_cm) : 0;
        
        // Interpolate between entry and exit
        const offsetX = entryOffset.x + (exitOffset.x - entryOffset.x) * progress;
        const offsetY = entryOffset.y + (exitOffset.y - entryOffset.y) * progress;
        
        return {
            x: centerX + offsetX,
            y: centerY + offsetY,
            rotation: Math.atan2(exitOffset.y - entryOffset.y, exitOffset.x - entryOffset.x)
        };
    }

    // ========================================================================
    // Path-Based Rendering
    // ========================================================================

    /**
     * Draw a train using path-based position data.
     * @param {Object} train - Train object with path_tiles and path_tile_info
     */
    drawTrainFromPath(train) {
        const pathTiles = train.path_tiles;
        const pathInfo = train.path_tile_info || [];
        
        // Determine head and tail based on leading_end
        const headIndex = train.leading_end === 'a' ? train.end_a_index : train.end_b_index;
        const tailIndex = train.leading_end === 'a' ? train.end_b_index : train.end_a_index;
        const headDistanceCm = train.leading_end === 'a' ? train.end_a_distance_cm : train.end_b_distance_cm;
        const tailDistanceCm = train.leading_end === 'a' ? train.end_b_distance_cm : train.end_a_distance_cm;
        const headEntryEdge = train.leading_end === 'a' ? train.end_a_entry_edge : train.end_b_entry_edge;
        const tailEntryEdge = train.leading_end === 'a' ? train.end_b_entry_edge : train.end_a_entry_edge;
        
        // Validate indices - enhanced bounds checking
        if (headIndex === undefined || headIndex === null || 
            tailIndex === undefined || tailIndex === null) {
            console.warn('[RendererTrainHandler] Missing head/tail indices', {
                headIndex, tailIndex, trainId: train.id
            });
            return;
        }
        
        if (headIndex >= pathTiles.length || tailIndex >= pathTiles.length) {
            console.warn('[RendererTrainHandler] Index out of bounds', {
                headIndex, tailIndex, pathLength: pathTiles.length, trainId: train.id
            });
            return;
        }
        
        // Get track bits lookup
        const tracks = this.currentNetwork?.tracks || [];
        const getTrackBits = (tile) => {
            const t = tracks.find(tr => tr.x === tile[0] && tr.y === tile[1]);
            return t ? t.track_bits : 0;
        };
        
        const headTile = pathTiles[headIndex];
        const tailTile = pathTiles[tailIndex];
        const headInfo = pathInfo[headIndex] || { length_cm: 50 };
        const tailInfo = pathInfo[tailIndex] || { length_cm: 50 };
        
        // Calculate head and tail screen positions
        const headPos = this.getTilePathPosition(
            headTile, headEntryEdge, headDistanceCm, 
            headInfo.length_cm, getTrackBits(headTile)
        );
        
        const tailPos = this.getTilePathPosition(
            tailTile, tailEntryEdge, tailDistanceCm,
            tailInfo.length_cm, getTrackBits(tailTile)
        );
        
        const s = this.scale;
        const occupiedTileCount = Math.abs(headIndex - tailIndex) + 1;
        
        // === Draw train body (white stroke with dark border from tail to head) ===
        // Build the path once, then stroke it twice (dark border first, white fill second)
        const buildBodyPath = () => {
            this.ctx.beginPath();
            this.ctx.moveTo(tailPos.x, tailPos.y);
            
            if (occupiedTileCount === 1) {
                // Same tile - direct line
                this.ctx.lineTo(headPos.x, headPos.y);
            } else if (occupiedTileCount === 2) {
                // 2 tiles - go through boundary
                const tailCenterX = this.gridToScreenX(tailTile[0], tailTile[1]);
                const tailCenterY = this.gridToScreenY(tailTile[0], tailTile[1]);
                const tailEntryOffset = this.getEdgeCenterOffset(tailEntryEdge);
                this.ctx.lineTo(tailCenterX + tailEntryOffset.x, tailCenterY + tailEntryOffset.y);
                this.ctx.lineTo(headPos.x, headPos.y);
            } else {
                // 3+ tiles - draw through all occupied tiles
                const step = tailIndex <= headIndex ? 1 : -1;
                let currentEntryEdge = tailEntryEdge;
                
                for (let i = tailIndex; i !== headIndex; i += step) {
                    const currentTile = pathTiles[i];
                    const trackBits = getTrackBits(currentTile);
                    const exitEdge = this.getExitEdge(trackBits, currentEntryEdge);
                    
                    if (exitEdge) {
                        const centerX = this.gridToScreenX(currentTile[0], currentTile[1]);
                        const centerY = this.gridToScreenY(currentTile[0], currentTile[1]);
                        const exitOffset = this.getEdgeCenterOffset(exitEdge);
                        this.ctx.lineTo(centerX + exitOffset.x, centerY + exitOffset.y);
                        currentEntryEdge = this.getOppositeEdge(exitEdge);
                    }
                }
                this.ctx.lineTo(headPos.x, headPos.y);
            }
        };
        
        // Draw dark border (thicker stroke underneath)
        this.ctx.save();
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 5 * s;
        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
        buildBodyPath();
        this.ctx.stroke();
        this.ctx.restore();
        
        // Draw white fill on top
        this.ctx.save();
        this.ctx.strokeStyle = '#ffffff';
        this.ctx.lineWidth = 3 * s;
        this.ctx.lineCap = 'round';
        this.ctx.lineJoin = 'round';
        buildBodyPath();
        this.ctx.stroke();
        this.ctx.restore();
        
        // === Draw tail square ===
        this.drawTailMarker(tailPos.x, tailPos.y, tailPos.rotation);
        
        // === Draw head triangle ===
        this.drawHeadMarker(headPos.x, headPos.y, headPos.rotation);
        
        // === Draw label ===
        const headTrackBits = getTrackBits(headTile);
        this.drawTrainLabel(train, headPos.x, headPos.y, headTrackBits, train.direction, headTile[0], headTile[1], tracks);
    }

    /**
     * Draw head marker (white triangle pointing in direction of movement).
     */
    drawHeadMarker(x, y, rotation) {
        const s = this.scale;
        const triSize = 8 * s;
        
        this.ctx.save();
        this.ctx.translate(x, y);
        this.ctx.rotate(rotation);
        
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
    }

    /**
     * Draw tail marker (white square with dark border).
     * The square is offset backward along the track direction and rotated to align with the track.
     */
    drawTailMarker(x, y, rotation = 0) {
        const s = this.scale;
        const squareSize = 5 * s;

        // Calculate offset position - move backward along the path direction
        // rotation gives us the direction of the path, we offset in the opposite direction
        const offsetDistance = squareSize / 2;
        const offsetX = -Math.cos(rotation) * offsetDistance;
        const offsetY = -Math.sin(rotation) * offsetDistance;

        const drawX = x + offsetX;
        const drawY = y + offsetY;

        this.ctx.save();
        this.ctx.translate(drawX, drawY);
        this.ctx.rotate(rotation);  // Rotate square to align with track direction

        this.ctx.fillStyle = '#ffffff';
        this.ctx.fillRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.strokeRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

        this.ctx.restore();
    }

    /**
     * Draw train label (purple box with train ID).
     */
    drawTrainLabel(train, cx, cy, trackBits, direction, tileX, tileY, tracks) {
        const s = this.scale;
        const tileSize = this.gridSize * s;
        
        // Use smart positioning with tile coordinates and tracks array if available
        const quadrant = getBestLabelQuadrant(trackBits, direction || 'EAST', tileX, tileY, tracks);
        // Use dynamic spacing based on user preference
        const offset = getQuadrantOffsets()[quadrant];
        
        const labelX = cx + offset.dx * tileSize;
        const labelY = cy + offset.dy * tileSize;
        
        // Draw leader line
        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
        this.ctx.lineWidth = 1 * s;
        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(labelX, labelY);
        this.ctx.stroke();
        this.ctx.restore();
        
        // Draw label box
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
