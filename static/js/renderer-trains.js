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
        // Read from the per-frame snapshot built by Renderer._flushRedraw
        // (static/js/render-snapshot.js). Falls back to stateManager only
        // when no snapshot is available — e.g., the public drawNetwork()
        // entry called outside the rAF flush path.
        const trainSnap = this.renderer.currentSnapshot?.trains?.[train.id];
        if (trainSnap) {
            this.drawTrainFromSnapshot(train, trainSnap);
            return;
        }
        const controller = window.stateManager?.getController(train.id);
        if (!controller?.span?.occupied_tiles?.length) return;
        // Adapt the controller into a snapshot-shaped object so the
        // downstream renderer code only has one shape to consume.
        this.drawTrainFromSnapshot(train, {
            id: train.id,
            cab: controller.cab ?? null,
            span: {
                occupiedTiles: controller.span.occupied_tiles,
                leadingEnd: controller.span.leading_end,
                endA: controller.span.end_a,
                endB: controller.span.end_b,
            },
        });
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
     * Draw a train from a snapshot entry (see render-snapshot.js).
     *
     * The snapshot's span shape mirrors the controller's: occupied_tiles
     * ordered end_a → end_b, plus end_a / end_b geometry, plus
     * leading_end ∈ {'a','b'}. Per-tile length is not in the network
     * payload for v1; we use a 50cm-per-tile constant for body
     * interpolation (visually approximate but acceptable for v1).
     */
    drawTrainFromSnapshot(train, trainSnap) {
        const span = trainSnap.span;
        const occupiedTiles = span.occupiedTiles || [];
        if (occupiedTiles.length === 0) return;

        // cab is null when the train has no cab selected; we still draw
        // both endpoints, but as squares rather than head triangle.
        const hasLeadingEnd = trainSnap.cab === 'A' || trainSnap.cab === 'B';

        // Resolve which end is head vs tail. occupiedTiles is ordered
        // end_a → end_b (see network.py). leadingEnd ('a' or 'b') tells us
        // which endpoint is the head.
        const leadingEnd = span.leadingEnd;
        const headFromA = leadingEnd === 'a';
        const headEndpoint = headFromA ? span.endA : span.endB;
        const tailEndpoint = headFromA ? span.endB : span.endA;
        const headTile = headEndpoint.tile;
        const tailTile = tailEndpoint.tile;
        const headDistanceCm = headEndpoint.distance_cm;
        const tailDistanceCm = tailEndpoint.distance_cm;
        const headEntryEdge = headEndpoint.entry_edge;
        const tailEntryEdge = tailEndpoint.entry_edge;

        // Per-tile length is `block.length_cm / number_of_tiles_in_block`
        // (matching `RailwayNetwork._get_tile_length_cm` on the backend).
        // Switches have length 0 — the head transits them instantly, and
        // interpolation on a 0-length tile is meaningless, so the renderer
        // pins it at the entry edge (handled inside getTilePathPosition).
        const tracks = this.currentNetwork?.tracks || [];
        const blocks = this.currentNetwork?.blocks || {};
        const DEFAULT_TILE_LENGTH_CM = 50;

        const tileCountByBlock = new Map();
        for (const tr of tracks) {
            if (!tr.block_section_id) continue;
            tileCountByBlock.set(
                tr.block_section_id,
                (tileCountByBlock.get(tr.block_section_id) || 0) + 1
            );
        }

        const getTrack = (tile) => tracks.find(tr => tr.x === tile[0] && tr.y === tile[1]);
        const getTrackBits = (tile) => getTrack(tile)?.track_bits || 0;
        const getTileLengthCm = (tile) => {
            const t = getTrack(tile);
            if (!t) return DEFAULT_TILE_LENGTH_CM;
            if (t.is_switch) return 0;
            if (!t.block_section_id) return DEFAULT_TILE_LENGTH_CM;
            const block = blocks[t.block_section_id];
            const blockLen = block?.length_cm;
            if (!blockLen || blockLen <= 0) return DEFAULT_TILE_LENGTH_CM;
            const tileCount = tileCountByBlock.get(t.block_section_id) || 1;
            return blockLen / tileCount;
        };

        // Calculate head and tail screen positions
        const headPos = this.getTilePathPosition(
            headTile, headEntryEdge, headDistanceCm,
            getTileLengthCm(headTile), getTrackBits(headTile)
        );

        const tailPos = this.getTilePathPosition(
            tailTile, tailEntryEdge, tailDistanceCm,
            getTileLengthCm(tailTile), getTrackBits(tailTile)
        );

        const s = this.scale;
        const occupiedTileCount = occupiedTiles.length;

        // === Draw train body (white stroke with dark border from tail to head) ===
        // Walk occupied_tiles tail→head to build a polyline.
        // occupied_tiles is ordered end_a → end_b; if the head is end_a, we
        // need to reverse so the walk goes tail → head.
        const tilesTailToHead = headFromA
            ? [...occupiedTiles].reverse()
            : occupiedTiles;

        const pathPoints = [];
        pathPoints.push({ x: tailPos.x, y: tailPos.y });

        // Exit edge from one occupied tile to the next is fixed by their
        // grid positions — derive it directly. Using `getTrackExits()` with
        // the live `switch_state` would pick the wrong branch any time the
        // switch state differs from what the train actually traversed.
        const edgeFromDelta = (curr, next) => {
            const dx = next[0] - curr[0];
            const dy = next[1] - curr[1];
            if (dy > 0) return 'N';
            if (dy < 0) return 'S';
            if (dx > 0) return 'E';
            if (dx < 0) return 'W';
            return null;
        };

        if (!hasLeadingEnd) {
            // No cab active: simple direct line between ends regardless of track path
            pathPoints.push({ x: headPos.x, y: headPos.y });
        } else if (occupiedTileCount === 1) {
            // Same tile - direct line
            pathPoints.push({ x: headPos.x, y: headPos.y });
        } else if (occupiedTileCount === 2) {
            // 2 tiles - go through the shared boundary derived from grid positions.
            const tailExitEdge = edgeFromDelta(tailTile, headTile);
            const headEntryEdgeCalc = tailExitEdge ? this.getOppositeEdge(tailExitEdge) : null;

            if (tailExitEdge) {
                const tailCenterX = this.gridToScreenX(tailTile[0], tailTile[1]);
                const tailCenterY = this.gridToScreenY(tailTile[0], tailTile[1]);
                const tailExitOffset = this.getEdgeCenterOffset(tailExitEdge);
                pathPoints.push({ x: tailCenterX + tailExitOffset.x, y: tailCenterY + tailExitOffset.y });
            }

            if (headEntryEdgeCalc) {
                const headCenterX = this.gridToScreenX(headTile[0], headTile[1]);
                const headCenterY = this.gridToScreenY(headTile[0], headTile[1]);
                const headEntryOffset = this.getEdgeCenterOffset(headEntryEdgeCalc);
                pathPoints.push({ x: headCenterX + headEntryOffset.x, y: headCenterY + headEntryOffset.y });
            }

            pathPoints.push({ x: headPos.x, y: headPos.y });
        } else {
            // 3+ tiles - walk all occupied tiles tail → head, using each
            // tile-to-next-tile delta to pick the shared edge.
            for (let i = 0; i < tilesTailToHead.length - 1; i++) {
                const currentTile = tilesTailToHead[i];
                const nextTile = tilesTailToHead[i + 1];
                const exitEdge = edgeFromDelta(currentTile, nextTile);
                if (!exitEdge) continue;

                const centerX = this.gridToScreenX(currentTile[0], currentTile[1]);
                const centerY = this.gridToScreenY(currentTile[0], currentTile[1]);
                const exitOffset = this.getEdgeCenterOffset(exitEdge);
                pathPoints.push({ x: centerX + exitOffset.x, y: centerY + exitOffset.y });

                const entryEdge = this.getOppositeEdge(exitEdge);
                const nextCenterX = this.gridToScreenX(nextTile[0], nextTile[1]);
                const nextCenterY = this.gridToScreenY(nextTile[0], nextTile[1]);
                const entryOffset = this.getEdgeCenterOffset(entryEdge);
                pathPoints.push({ x: nextCenterX + entryOffset.x, y: nextCenterY + entryOffset.y });
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
        
        // === Draw end markers ===
        if (hasLeadingEnd) {
            // Cab is active: draw tail square and head triangle
            this.drawTailMarker(tailPos.x, tailPos.y, tailPos.rotation, train);
            this.drawHeadMarker(headPos.x, headPos.y, headPos.rotation);
        } else {
            // No cab active: draw square markers at both ends with end identifiers
            // Calculate rotation pointing from tail to head for consistent body direction
            const dx = headPos.x - tailPos.x;
            const dy = headPos.y - tailPos.y;
            const rotationToHead = Math.atan2(dy, dx);
            const rotationToTail = rotationToHead + Math.PI;
            this.drawEndMarker(tailPos.x, tailPos.y, rotationToTail, train, 'B');
            this.drawEndMarker(headPos.x, headPos.y, rotationToHead, train, 'A');
        }

        // === Draw label ===
        const headTrackBits = getTrackBits(headTile);
        this.drawTrainLabel(train, headPos.x, headPos.y, headTrackBits, train.facing_direction, headTile[0], headTile[1], tracks);
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
     * Draw end marker (white square with dark border and train ID + end identifier).
     * Used when no cab is active to label both ends (e.g., "2A", "3B").
     * @param {number} x - Screen X position
     * @param {number} y - Screen Y position
     * @param {number} rotation - Rotation angle in radians
     * @param {Object} train - Train object
     * @param {string} endId - End identifier ('A' or 'B')
     */
    drawEndMarker(x, y, rotation = 0, train = null, endId = 'A') {
        const s = this.scale;
        const squareSize = 10 * s;

        // Offset forward so the back edge aligns with the position
        const offsetDistance = squareSize / 2;
        const offsetX = Math.cos(rotation) * offsetDistance;
        const offsetY = Math.sin(rotation) * offsetDistance;

        const drawX = x + offsetX;
        const drawY = y + offsetY;

        this.ctx.save();
        this.ctx.translate(drawX, drawY);
        this.ctx.rotate(rotation);

        // Draw white background square
        this.ctx.fillStyle = '#ffffff';
        this.ctx.fillRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

        // Draw dark border
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1 * s;
        this.ctx.strokeRect(-squareSize / 2, -squareSize / 2, squareSize, squareSize);

        // Draw car number + end identifier (e.g., "2A", "3B")
        if (train && train.num_cars) {
            this.ctx.save();
            // Counter-rotate to keep text upright
            this.ctx.rotate(-rotation);
            this.ctx.fillStyle = '#333';
            this.ctx.font = `bold ${5 * s}px sans-serif`;
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'middle';
            this.ctx.fillText(`${train.num_cars}${endId}`, 0, 0);
            this.ctx.restore();
        }

        this.ctx.restore();
    }

    /**
     * Draw train label (purple box with train ID).
     * Picks one of 4 quadrants around the train, biased by heading direction
     * and scored against nearby track segments so the label dodges tracks.
     */
    drawTrainLabel(train, cx, cy, trackBits, direction, tileX, tileY, tracks) {
        const s = this.scale;
        const tileSize = this.gridSize * s;

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

        const spacingMultiplier = getLabelSpacingMultiplier();
        const textDependentOffset = Math.SQRT2 * boxWidth;

        const { labelX, labelY } = this._pickLabelPosition(
            train, cx, cy, direction, boxWidth, boxHeight,
            tileSize, spacingMultiplier, textDependentOffset, tracks,
        );
        
        // Draw leader line
        this.ctx.save();
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
        this.ctx.lineWidth = 1 * s;
        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy);
        this.ctx.lineTo(labelX, labelY);
        this.ctx.stroke();
        this.ctx.restore();
        
        // Draw label box — trains are live identity, so they sit one tier
        // above the station label vocabulary: same dark base, but a dim
        // purple fill (~18%) + purple border carry the categorical color
        // without saturating into an alert pill. The motion of the train
        // chassis below is what should read as "live," not the label.
        this.ctx.save();
        this.ctx.font = `bold ${9 * s}px sans-serif`;

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

        // Opaque dark base mixed with ~15% purple — the categorical cast is
        // perceptible without saturating into an alert pill, and the label
        // stays legible when it overlaps a station/platform behind it.
        this.ctx.fillStyle = 'rgba(32, 26, 50, 0.92)';
        this.ctx.fill();

        this.ctx.strokeStyle = 'rgba(130, 87, 229, 0.9)';
        // Cap stroke at ~2px screen width — same screen-pixel cap as platform
        // highlights so canvas-label borders stay hairline at any zoom.
        this.ctx.lineWidth = Math.min(Math.max(1.0, s * 0.35), 2.0);
        this.ctx.stroke();

        this.ctx.fillStyle = '#ffffff';
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(labelText, labelX, labelY);
        this.ctx.restore();
    }

    _pickLabelPosition(train, cx, cy, direction, boxW, boxH, tileSize, spacing, textOffset, tracks) {
        const dir = direction || 'EAST';
        const preferred = (typeof DIRECTION_PREFERRED_QUADRANTS !== 'undefined'
            && DIRECTION_PREFERRED_QUADRANTS[dir])
            || ['SE', 'SW', 'NE', 'NW'];
        const quadOffsets = {
            'NW': { ox: -0.5, oy: -0.5 },
            'NE': { ox:  0.5, oy: -0.5 },
            'SW': { ox: -0.5, oy:  0.5 },
            'SE': { ox:  0.5, oy:  0.5 },
        };

        const segments = this.renderer.getNearbyTrackSegments(cx, cy, tileSize * 3);
        const platformSegs = this.renderer.platformHandler?.getPlatformBodySegments?.() || [];
        const obstacles = segments.concat(platformSegs);

        let bestLX = cx, bestLY = cy, bestScore = -Infinity;
        for (let i = 0; i < preferred.length; i++) {
            const { ox, oy } = quadOffsets[preferred[i]] || quadOffsets['SE'];
            const lx = cx + ox * tileSize * spacing + ox * textOffset;
            const ly = cy + oy * tileSize * spacing + oy * textOffset;
            const bx = lx - boxW / 2;
            const by = ly - boxH / 2;

            const dist = this.renderer.distRectToSegments(bx, by, boxW, boxH, obstacles);
            // Saturate so far-from-tracks options become equivalent and the
            // direction preference wins as a tiebreaker.
            const distScore = Math.min(dist, 30);
            const preferenceBonus = (preferred.length - i) * 2;
            const score = distScore + preferenceBonus;

            if (score > bestScore) {
                bestScore = score;
                bestLX = lx;
                bestLY = ly;
            }
        }

        // Dodge the cursor with hysteresis: only re-evaluate when the mouse
        // actually moves. Otherwise zoom/pan would slide the natural label
        // position under a stationary cursor and flip-flop the dodge state.
        if (!this._labelDodge) this._labelDodge = new Map();
        const mx = this.renderer.mouseX;
        const my = this.renderer.mouseY;
        const prev = this._labelDodge.get(train.id);
        let dodged = prev?.dodged || false;
        if (!prev || prev.mx !== mx || prev.my !== my) {
            if (mx < 0 || my < 0) {
                dodged = false;
            } else {
                const halfW = boxW / 2;
                const halfH = boxH / 2;
                dodged = mx >= bestLX - halfW && mx <= bestLX + halfW
                      && my >= bestLY - halfH && my <= bestLY + halfH;
            }
        }
        this._labelDodge.set(train.id, { dodged, mx, my });

        return dodged
            ? { labelX: 2 * cx - bestLX, labelY: 2 * cy - bestLY }
            : { labelX: bestLX, labelY: bestLY };
    }
}
