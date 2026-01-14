class RendererDebugHandler {
    constructor(renderer) {
        this.renderer = renderer;
    }

    get ctx() { return this.renderer.ctx; }
    get scale() { return this.renderer.scale; }
    get gridSize() { return this.renderer.gridSize; }

    gridToScreenX(x, y) { return this.renderer.gridToScreenX(x, y); }
    gridToScreenY(x, y) { return this.renderer.gridToScreenY(x, y); }

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
        
        if (track.is_switch && track.switch_state !== undefined && exits.length > 1) {
            return [exits[track.switch_state] || exits[0]];
        }
        
        return exits;
    }

    drawSelection(gridX, gridY) {
        this.ctx.save();

        const tileSize = this.gridSize * this.scale;
        const cx = this.gridToScreenX(gridX, gridY);
        const cy = this.gridToScreenY(gridX, gridY);
        const halfTile = tileSize / 2;

        this.ctx.strokeStyle = '#8257e5';
        this.ctx.lineWidth = 2 * this.scale;
        this.ctx.setLineDash([4, 4]);

        this.ctx.beginPath();
        this.ctx.moveTo(cx, cy - halfTile);
        this.ctx.lineTo(cx + halfTile, cy);
        this.ctx.lineTo(cx, cy + halfTile);
        this.ctx.lineTo(cx - halfTile, cy);
        this.ctx.closePath();
        this.ctx.stroke();

        this.ctx.restore();
    }

    drawTrackBitsIndicator(x, y, trackBits) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const cellSize = this.gridSize * this.scale;

        this.ctx.save();
        this.ctx.font = `${10 * this.scale}px monospace`;
        this.ctx.fillStyle = '#888';
        this.ctx.textAlign = 'right';
        this.ctx.textBaseline = 'bottom';

        this.ctx.fillText(
            trackBits.toString(),
            screenX + cellSize * 0.45,
            screenY + cellSize * 0.45
        );

        this.ctx.restore();
    }

    drawBitVisualization(x, y, trackBits) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const cellSize = this.gridSize * this.scale;

        const bitPositions = {
            'NS': { x: 0.1, y: 0.1, bit: 1 },
            'EW': { x: 0.3, y: 0.1, bit: 2 },
            'NE': { x: 0.5, y: 0.1, bit: 4 },
            'NW': { x: 0.7, y: 0.1, bit: 8 },
            'SE': { x: 0.1, y: 0.3, bit: 16 },
            'SW': { x: 0.3, y: 0.3, bit: 32 }
        };

        this.ctx.save();
        for (const pos of Object.values(bitPositions)) {
            const isActive = (trackBits & pos.bit) !== 0;

            this.ctx.fillStyle = isActive ? '#00ff88' : '#333';
            this.ctx.beginPath();
            this.ctx.arc(
                screenX - cellSize * 0.5 + cellSize * pos.x,
                screenY - cellSize * 0.5 + cellSize * pos.y,
                3 * this.scale,
                0, Math.PI * 2
            );
            this.ctx.fill();
        }
        this.ctx.restore();
    }

    drawCoordinateLabel(x, y) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const cellSize = this.gridSize * this.scale;

        this.ctx.save();
        this.ctx.font = `${8 * this.scale}px monospace`;
        this.ctx.fillStyle = '#666';
        this.ctx.textAlign = 'left';
        this.ctx.textBaseline = 'top';
        this.ctx.fillText(
            `(${x},${y})`,
            screenX - cellSize * 0.45,
            screenY - cellSize * 0.45
        );
        this.ctx.restore();
    }

    highlightDeadEnds(x, y, deadEnds, trackBits = 0) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const tileSize = this.gridSize * this.scale;
        const quarterTile = tileSize / 4;
        const markerLen = 6 * this.scale;

        const TRACK_NS = 1, TRACK_EW = 2, TRACK_NE = 4, TRACK_NW = 8, TRACK_SE = 16, TRACK_SW = 32;

        const portToPaths = {
            'N': [TRACK_NS, TRACK_NE, TRACK_NW],
            'S': [TRACK_NS, TRACK_SE, TRACK_SW],
            'E': [TRACK_EW, TRACK_NE, TRACK_SE],
            'W': [TRACK_EW, TRACK_NW, TRACK_SW]
        };

        const pathPerpendicular = {
            [TRACK_NS]: { dx: 1, dy: 1 },
            [TRACK_EW]: { dx: 1, dy: -1 },
            [TRACK_NE]: { dx: 1, dy: 0 },
            [TRACK_NW]: { dx: 0, dy: 1 },
            [TRACK_SE]: { dx: 0, dy: 1 },
            [TRACK_SW]: { dx: 1, dy: 0 }
        };

        const edgePositions = {
            'N': { x: quarterTile, y: -quarterTile },
            'E': { x: quarterTile, y: quarterTile },
            'S': { x: -quarterTile, y: quarterTile },
            'W': { x: -quarterTile, y: -quarterTile }
        };

        this.ctx.save();
        this.ctx.strokeStyle = '#ff9900';
        this.ctx.lineWidth = 3 * this.scale;

        for (const edge of deadEnds) {
            const port = edge.length > 1 ? edge[0] : edge;
            const paths = portToPaths[port];
            if (!paths) continue;

            let activePath = null;
            for (const pathBit of paths) {
                if (trackBits & pathBit) {
                    activePath = pathBit;
                    if (pathBit === TRACK_NS || pathBit === TRACK_EW) break;
                }
            }

            let dx, dy;
            if (activePath && pathPerpendicular[activePath]) {
                dx = pathPerpendicular[activePath].dx;
                dy = pathPerpendicular[activePath].dy;
            } else {
                dx = (port === 'N' || port === 'S') ? 1 : -1;
                dy = 1;
            }

            const len = Math.sqrt(dx * dx + dy * dy);
            dx = (dx / len) * markerLen;
            dy = (dy / len) * markerLen;

            const pos = edgePositions[port];
            const cx = screenX + pos.x;
            const cy = screenY + pos.y;

            this.ctx.beginPath();
            this.ctx.moveTo(cx - dx, cy - dy);
            this.ctx.lineTo(cx + dx, cy + dy);
            this.ctx.stroke();
        }
        this.ctx.restore();
    }

    drawSwitchStateOverlay(track) {
        const screenX = this.gridToScreenX(track.x, track.y);
        const screenY = this.gridToScreenY(track.x, track.y);

        this.ctx.save();

        const stateText = track.switch_state === 0 ? 'MAIN' : 'DIV';
        const isDiagonalOnly = track.is_diagonal_only_switch;

        this.ctx.font = `bold ${11 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';

        const bgColor = track.switch_state === 0 ? '#1a5a2a' : '#5a4a1a';
        const textColor = track.switch_state === 0 ? '#04d361' : '#ffd93d';

        this.ctx.fillStyle = bgColor;
        this.ctx.globalAlpha = 0.9;
        this.ctx.beginPath();
        this.ctx.roundRect(screenX - 25 * this.scale, screenY - 18 * this.scale, 50 * this.scale, 16 * this.scale, 3);
        this.ctx.fill();
        this.ctx.globalAlpha = 1;

        this.ctx.fillStyle = textColor;
        this.ctx.fillText(stateText, screenX, screenY - 10 * this.scale);

        if (isDiagonalOnly) {
            this.ctx.font = `${8 * this.scale}px sans-serif`;
            this.ctx.fillStyle = '#ff9900';
            this.ctx.fillText('⟳clockwise', screenX, screenY + 6 * this.scale);
        }

        this.ctx.restore();
    }

    drawEdgeLabels(x, y) {
        const screenX = this.gridToScreenX(x, y);
        const screenY = this.gridToScreenY(x, y);
        const tileSize = this.gridSize * this.scale;
        const quarterTile = tileSize / 4;

        this.ctx.save();
        this.ctx.font = `bold ${9 * this.scale}px sans-serif`;
        this.ctx.textAlign = 'center';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillStyle = '#00ffff';

        this.ctx.fillText('N', screenX + quarterTile + 8, screenY - quarterTile - 8);
        this.ctx.fillText('E', screenX + quarterTile + 8, screenY + quarterTile + 8);
        this.ctx.fillText('S', screenX - quarterTile - 8, screenY + quarterTile + 8);
        this.ctx.fillText('W', screenX - quarterTile - 8, screenY - quarterTile - 8);

        this.ctx.restore();
    }

    drawTrainPathDebug(trains) {
        const showTrainPathElement = document.getElementById('debug-show-train-path');
        if (!showTrainPathElement || !showTrainPathElement.checked) return;

        for (const train of trains) {
            // Use new TrainPosition format
            if (!train.position || !train.position.end_a) continue;

            const headEnd = train.position.leading_end === 'a' ? train.position.end_a : train.position.end_b;
            const x = headEnd.tile[0];
            const y = headEnd.tile[1];
            const entry_edge = headEnd.entry_edge;
            if (!entry_edge) continue;

            // Get track to find exit edge
            const tracks = this.currentNetwork?.tracks || [];
            const track = tracks.find(t => t.x === x && t.y === y);
            if (!track) continue;
            
            const exits = this.getTrackExits(track, entry_edge);
            const exit_edge = exits && exits.length > 0 ? exits[0] : null;
            if (!exit_edge) continue;
            
            const tileLengthCm = track?.tile_length_cm || 250.0;
            const distance = Math.min(1.0, Math.max(0.0, headEnd.distance_cm / tileLengthCm));

            const screenX = this.gridToScreenX(x, y);
            const screenY = this.gridToScreenY(x, y);
            const tileSize = this.gridSize * this.scale;
            const half = tileSize / 2;

            const edgePoints = {
                'N': { x: half / 2, y: -half / 2 },
                'E': { x: half / 2, y: half / 2 },
                'S': { x: -half / 2, y: half / 2 },
                'W': { x: -half / 2, y: -half / 2 }
            };

            const startPt = edgePoints[entry_edge];
            const endPt = edgePoints[exit_edge];

            if (startPt && endPt) {
                this.ctx.save();
                this.ctx.strokeStyle = '#ff00ff';
                this.ctx.lineWidth = 2 * this.scale;
                this.ctx.setLineDash([3, 3]);
                this.ctx.beginPath();
                this.ctx.moveTo(screenX + startPt.x, screenY + startPt.y);
                this.ctx.lineTo(screenX + endPt.x, screenY + endPt.y);
                this.ctx.stroke();

                this.ctx.fillStyle = '#00ff00';
                this.ctx.beginPath();
                this.ctx.arc(screenX + startPt.x, screenY + startPt.y, 4 * this.scale, 0, Math.PI * 2);
                this.ctx.fill();

                this.ctx.fillStyle = '#ff0000';
                this.ctx.beginPath();
                this.ctx.arc(screenX + endPt.x, screenY + endPt.y, 4 * this.scale, 0, Math.PI * 2);
                this.ctx.fill();

                this.ctx.restore();

                this.ctx.save();
                this.ctx.font = `${8 * this.scale}px monospace`;
                this.ctx.fillStyle = '#ffffff';
                this.ctx.textAlign = 'left';
                this.ctx.textBaseline = 'top';
                const debugText = `${entry_edge}→${exit_edge} d:${(distance * 100).toFixed(0)}%`;
                this.ctx.fillText(debugText, screenX + half * 0.6, screenY - half * 0.8);
                this.ctx.restore();
            }
        }
    }

    drawDebugOverlays(tracks) {
        const showBitsElement = document.getElementById('debug-show-bits');
        const showCoordsElement = document.getElementById('debug-show-coords');
        const showSwitchesElement = document.getElementById('debug-show-switches');
        const showEdgesElement = document.getElementById('debug-show-edges');
        const showBits = showBitsElement ? showBitsElement.checked : false;
        const showCoords = showCoordsElement ? showCoordsElement.checked : false;
        const showSwitches = showSwitchesElement ? showSwitchesElement.checked : false;
        const showEdges = showEdgesElement ? showEdgesElement.checked : false;

        for (const track of tracks) {
            if (showBits && track.track_bits !== undefined) {
                this.drawBitVisualization(track.x, track.y, track.track_bits);
                this.drawTrackBitsIndicator(track.x, track.y, track.track_bits);
            }

            if (showCoords) {
                this.drawCoordinateLabel(track.x, track.y);
            }

            if (showSwitches && track.is_switch) {
                this.drawSwitchStateOverlay(track);
            }

            if (showEdges) {
                this.drawEdgeLabels(track.x, track.y);
            }
        }
    }
}
