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
        if (!train.path_tiles || train.path_tiles.length === 0 || !train.path_tile_info) {
            return;
        }

        this.drawTrainFromPath(train);
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
        // When leading_end is null/undefined (no cab selected), treat train as symmetric
        // Use end_a as "head" position for rendering purposes
        const hasLeadingEnd = train.leading_end === 'a' || train.leading_end === 'b';
        const headIndex = train.leading_end === 'b' ? train.end_b_index : train.end_a_index;
        const tailIndex = train.leading_end === 'b' ? train.end_a_index : train.end_b_index;
        const headDistanceCm = train.leading_end === 'b' ? train.end_b_distance_cm : train.end_a_distance_cm;
        const tailDistanceCm = train.leading_end === 'b' ? train.end_a_distance_cm : train.end_b_distance_cm;
        const headEntryEdge = train.leading_end === 'b' ? train.end_b_entry_edge : train.end_a_entry_edge;
        const tailEntryEdge = train.leading_end === 'b' ? train.end_a_entry_edge : train.end_b_entry_edge;
        
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
        // Collect all path points
        const pathPoints = [];
        pathPoints.push({ x: tailPos.x, y: tailPos.y });
        
        if (occupiedTileCount === 1) {
            // Same tile - direct line
            pathPoints.push({ x: headPos.x, y: headPos.y });
        } else if (occupiedTileCount === 2) {
            // 2 tiles - go through boundary between tiles
            const tailTrack = tracks.find(t => t.x === tailTile[0] && t.y === tailTile[1]);
            if (!tailTrack) {
                console.warn('[RendererTrainHandler] Track not found for tail tile', {
                    tailTile, trainId: train.id
                });
                return;
            }
            const tailExits = this.getTrackExits(tailTrack, tailEntryEdge);
            const tailExitEdge = tailExits.length > 0 ? tailExits[0] : null;
            const headEntryEdgeCalc = tailExitEdge ? this.getOppositeEdge(tailExitEdge) : headEntryEdge;
            
            if (tailExitEdge) {
                const tailCenterX = this.gridToScreenX(tailTile[0], tailTile[1]);
                const tailCenterY = this.gridToScreenY(tailTile[0], tailTile[1]);
                const tailExitOffset = this.getEdgeCenterOffset(tailExitEdge);
                pathPoints.push({ x: tailCenterX + tailExitOffset.x, y: tailCenterY + tailExitOffset.y });
            }
            
            if (headEntryEdgeCalc && (headTile[0] !== tailTile[0] || headTile[1] !== tailTile[1])) {
                const headCenterX = this.gridToScreenX(headTile[0], headTile[1]);
                const headCenterY = this.gridToScreenY(headTile[0], headTile[1]);
                const headEntryOffset = this.getEdgeCenterOffset(headEntryEdgeCalc);
                pathPoints.push({ x: headCenterX + headEntryOffset.x, y: headCenterY + headEntryOffset.y });
            }
            
            pathPoints.push({ x: headPos.x, y: headPos.y });
        } else {
            // 3+ tiles - draw through all occupied tiles
            const step = tailIndex <= headIndex ? 1 : -1;
            let currentEntryEdge = tailEntryEdge;
            
            for (let i = tailIndex; i !== headIndex; i += step) {
                const currentTile = pathTiles[i];
                const track = tracks.find(t => t.x === currentTile[0] && t.y === currentTile[1]);
                if (!track) {
                    console.warn('[RendererTrainHandler] Track not found for intermediate tile', {
                        tile: currentTile, trainId: train.id
                    });
                    continue;
                }

                const exits = this.getTrackExits(track, currentEntryEdge);
                const exitEdge = exits.length > 0 ? exits[0] : null;
                
                if (exitEdge) {
                    const centerX = this.gridToScreenX(currentTile[0], currentTile[1]);
                    const centerY = this.gridToScreenY(currentTile[0], currentTile[1]);
                    const exitOffset = this.getEdgeCenterOffset(exitEdge);
                    pathPoints.push({ x: centerX + exitOffset.x, y: centerY + exitOffset.y });
                    currentEntryEdge = this.getOppositeEdge(exitEdge);
                }
            }
            pathPoints.push({ x: headPos.x, y: headPos.y });
        }
        
        // Draw body in segments: first (butt cap), middle (round caps), last (butt cap)
        const drawBodySegments = (strokeStyle, lineWidth) => {
            if (pathPoints.length < 2) return;
            
            // First segment (tail to first intermediate) - butt cap at tail
            this.ctx.save();
            this.ctx.strokeStyle = strokeStyle;
            this.ctx.lineWidth = lineWidth;
            this.ctx.lineCap = 'butt';
            this.ctx.lineJoin = 'round';
            this.ctx.beginPath();
            this.ctx.moveTo(pathPoints[0].x, pathPoints[0].y);
            this.ctx.lineTo(pathPoints[1].x, pathPoints[1].y);
            this.ctx.stroke();
            this.ctx.restore();
            
            // Middle segments - round caps for smooth joins
            if (pathPoints.length > 2) {
                this.ctx.save();
                this.ctx.strokeStyle = strokeStyle;
                this.ctx.lineWidth = lineWidth;
                this.ctx.lineCap = 'round';
                this.ctx.lineJoin = 'round';
                for (let i = 1; i < pathPoints.length - 1; i++) {
                    this.ctx.beginPath();
                    this.ctx.moveTo(pathPoints[i].x, pathPoints[i].y);
                    this.ctx.lineTo(pathPoints[i + 1].x, pathPoints[i + 1].y);
                    this.ctx.stroke();
                }
                this.ctx.restore();
            }
            
            // Last segment - butt cap at head (only if more than 2 points)
            if (pathPoints.length > 2) {
                this.ctx.save();
                this.ctx.strokeStyle = strokeStyle;
                this.ctx.lineWidth = lineWidth;
                this.ctx.lineCap = 'butt';
                this.ctx.lineJoin = 'round';
                this.ctx.beginPath();
                this.ctx.moveTo(pathPoints[pathPoints.length - 2].x, pathPoints[pathPoints.length - 2].y);
                this.ctx.lineTo(pathPoints[pathPoints.length - 1].x, pathPoints[pathPoints.length - 1].y);
                this.ctx.stroke();
                this.ctx.restore();
            }
        };
        
        // Draw dark border then white fill
        drawBodySegments('#333', 5 * s);
        drawBodySegments('#ffffff', 3 * s);
        
        // === Draw tail square (only when cab is active) ===
        if (hasLeadingEnd) {
            this.drawTailMarker(tailPos.x, tailPos.y, tailPos.rotation, train);
        }
        
        // === Draw head triangle (only when cab is active) ===
        if (hasLeadingEnd) {
            this.drawHeadMarker(headPos.x, headPos.y, headPos.rotation);
        }

        // === Draw label ===
        const headTrackBits = getTrackBits(headTile);
        this.drawTrainLabel(train, headPos.x, headPos.y, headTrackBits, train.direction, headTile[0], headTile[1], tracks);
    }

    /**
     * Draw head marker (white triangle pointing in direction of movement).
     * The triangle's flat (back) edge is aligned with the precise head position.
     */
    drawHeadMarker(x, y, rotation) {
        const s = this.scale;
        const triHeight = 9 * s;  // 1.5x of original 6s
        const triWidth = triHeight * 0.6;  // Width at base
        
        // Calculate offset to align flat back edge with (x, y)
        // Move forward along the rotation direction by half the triangle height
        const offsetX = Math.cos(rotation) * (triHeight / 2);
        const offsetY = Math.sin(rotation) * (triHeight / 2);
        
        const drawX = x + offsetX;
        const drawY = y + offsetY;
        
        this.ctx.save();
        this.ctx.translate(drawX, drawY);
        this.ctx.rotate(rotation);
        
        this.ctx.fillStyle = '#ffffff';
        this.ctx.beginPath();
        // Tip at front, flat back at -triHeight/2 (centered on x,y after offset)
        this.ctx.moveTo(triHeight / 2, 0);  // Tip
        this.ctx.lineTo(-triHeight / 2, -triWidth);  // Back left
        this.ctx.lineTo(-triHeight / 2, triWidth);   // Back right
        this.ctx.closePath();
        this.ctx.fill();
        
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.stroke();
        
        this.ctx.restore();
    }

     /**
      * Draw tail marker (white square with dark border and num_cars display).
      * Mirrors head marker: rotation points forward (toward head), offset forward,
      * so the square's back edge aligns with the tail position (x, y).
      * @param {number} x - Screen X position (tail position - trailing edge of train)
      * @param {number} y - Screen Y position (tail position - trailing edge of train)
      * @param {number} rotation - Rotation angle in radians (points toward head, entry→exit direction)
      * @param {Object} train - Train object (to access num_cars)
      */
     drawTailMarker(x, y, rotation = 0, train = null) {
        const s = this.scale;
        const squareSize = 10 * s;

        // Offset FORWARD (same as head marker) so after drawing centered square,
        // the back edge (-squareSize/2) ends up at the original tail position (x,y)
        const offsetDistance = squareSize / 2;
        const offsetX = Math.cos(rotation) * offsetDistance;
        const offsetY = Math.sin(rotation) * offsetDistance;

        const drawX = x + offsetX;
        const drawY = y + offsetY;

        this.ctx.save();
        this.ctx.translate(drawX, drawY);
        this.ctx.rotate(rotation);  // Rotate square to align with track direction

        // Draw white background square
        this.ctx.fillStyle = '#ffffff';
        this.ctx.fillRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

        // Draw dark border
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.strokeRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

        // Draw num_cars text in center (counter-rotate to keep upright for readability)
        if (train && train.num_cars) {
            this.ctx.save();
            // Counter-rotate both the track rotation AND the square's rotation to keep text upright
            this.ctx.rotate(-rotation);  // Cancel the square's rotation from line 621
            this.ctx.fillStyle = '#333';
            this.ctx.font = `bold ${6 * s}px sans-serif`;
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'middle';
            this.ctx.fillText(train.num_cars.toString(), 0, 0);
            this.ctx.restore();
        }

        this.ctx.restore();
    }

    /**
     * Draw train label (purple box with train ID).
     * Label is placed perpendicular to the train's heading direction.
     */
    drawTrainLabel(train, cx, cy, trackBits, direction, tileX, tileY, tracks) {
        const s = this.scale;
        const tileSize = this.gridSize * s;
        
        // Measure text first to calculate text-dependent offset
        const labelText = train.id;
        this.ctx.save();
        this.ctx.font = `bold ${9 * s}px sans-serif`;
        const textMetrics = this.ctx.measureText(labelText);
        const textWidth = textMetrics.width;
        const textHeight = 9 * s;
        const padding = 4 * s;
        const boxWidth = textWidth + padding * 2;
        const boxHeight = textHeight + padding * 1.5;
        this.ctx.restore();
        
        // Calculate offset perpendicular to train heading
        // If train is facing E/W, offset N/S; if facing N/S, offset E/W
        const dir = direction || 'EAST';
        let offsetX, offsetY;
        
        if (dir === 'EAST' || dir === 'WEST') {
            // Train facing horizontally, offset vertically (to the south in isometric view)
            offsetX = -0.5;  // South direction in isometric
            offsetY = 0.5;
        } else {
            // Train facing vertically (N/S), offset horizontally (to the east in isometric view)
            offsetX = 0.5;   // East direction in isometric
            offsetY = 0.5;
        }

        const spacingMultiplier = getLabelSpacingMultiplier();
        
        // Add text-dependent offset (sqrt(2) * textWidth for isometric space)
        const textDependentOffset = Math.SQRT2 * boxWidth;
        
        const labelX = cx + offsetX * tileSize * spacingMultiplier + offsetX * textDependentOffset;
        const labelY = cy + offsetY * tileSize * spacingMultiplier + offsetY * textDependentOffset;
        
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
        this.ctx.save();
        this.ctx.font = `bold ${9 * s}px sans-serif`;
        
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
