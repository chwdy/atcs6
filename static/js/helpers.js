/**
 * helpers.js - Reusable UI Components and Helper Functions
 * 
 * This file contains standalone helper functions for generating consistent HTML components
 * across the editor panels (Station, Block, Signal, Train editors).
 * 
 * All functions follow the project's CSS utility class system (see STYLE_GUIDE.md).
 * Use CSS classes instead of inline styles whenever possible.
 */

function getTrackExits(trackBits) {
    const exits = [];

    if (trackBits & TRACK_NS) { exits.push('N', 'S'); }
    if (trackBits & TRACK_EW) { exits.push('E', 'W'); }
    if (trackBits & TRACK_NE) { exits.push('N', 'E'); }
    if (trackBits & TRACK_NW) { exits.push('N', 'W'); }
    if (trackBits & TRACK_SE) { exits.push('S', 'E'); }
    if (trackBits & TRACK_SW) { exits.push('S', 'W'); }

    return [...new Set(exits)];
}

function getNeighborPosition(x, y, direction) {
    const offsets = {
        'N': [0, 1],
        'E': [1, 0],
        'S': [0, -1],
        'W': [-1, 0]
    };
    const [dx, dy] = offsets[direction];
    return [x + dx, y + dy];
}

function getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY) {
    if (orientation === 'NS') {
        return TRACK_NS;
    }
    if (orientation === 'EW') {
        return TRACK_EW;
    }
    if (orientation === 'NE_SW') {
        const dx = nextX - edgeX;
        const dy = nextY - edgeY;
        if (dx === 1 && dy === 0) return TRACK_SW;
        if (dx === 0 && dy === 1) return TRACK_NE;
        if (dx === -1 && dy === 0) return TRACK_NE;
        if (dx === 0 && dy === -1) return TRACK_SW;
    }
    if (orientation === 'NW_SE') {
        const dx = nextX - edgeX;
        const dy = nextY - edgeY;
        if (dx === 1 && dy === 0) return TRACK_SE;
        if (dx === 0 && dy === 1) return TRACK_SE;
        if (dx === -1 && dy === 0) return TRACK_NW;
        if (dx === 0 && dy === -1) return TRACK_NW;
    }

    throw new Error(`Cannot determine track bits for orientation ${orientation}`);
}

function detectOrientation(sortedPositions, tracks) {
    if (!sortedPositions || sortedPositions.length < 1) {
        throw new Error('Need at least 1 position to detect orientation');
    }

    if (!Array.isArray(tracks)) {
        throw new Error('Track data is required to detect orientation');
    }

    if (sortedPositions.length === 1) {
        const [x, y] = sortedPositions[0];
        const track = tracks.find(t => t.x === x && t.y === y);
        if (!track) {
            throw new Error(`No track found at position (${x}, ${y})`);
        }

        if (track.track_bits & TRACK_NS) return 'NS';
        if (track.track_bits & TRACK_EW) return 'EW';
        if (track.track_bits & (TRACK_NE | TRACK_SW)) return 'NE_SW';
        if (track.track_bits & (TRACK_NW | TRACK_SE)) return 'NW_SE';

        throw new Error('Cannot determine orientation from track bits');
    }

    const allSameX = sortedPositions.every(([x, _]) => x === sortedPositions[0][0]);
    const allSameY = sortedPositions.every(([_, y]) => y === sortedPositions[0][1]);

    if (allSameX) return 'NS';
    if (allSameY) return 'EW';

    const sums = sortedPositions.map(([x, y]) => x + y);
    const diffs = sortedPositions.map(([x, y]) => x - y);

    let diffsMonotonic = true;
    for (let i = 1; i < diffs.length; i++) {
        if (diffs[i] <= diffs[i - 1]) {
            diffsMonotonic = false;
            break;
        }
    }

    let sumsMonotonic = true;
    for (let i = 1; i < sums.length; i++) {
        if (sums[i] <= sums[i - 1]) {
            sumsMonotonic = false;
            break;
        }
    }

    if (diffsMonotonic) {
        return 'NE_SW';
    }
    if (sumsMonotonic) {
        return 'NW_SE';
    }

    const sumVariance = Math.max(...sums) - Math.min(...sums);
    const diffVariance = Math.max(...diffs) - Math.min(...diffs);

    if (diffVariance > sumVariance) {
        return 'NE_SW';
    }
    return 'NW_SE';
}

function calculateNextTile(sortedPositions, orientation, direction, tracks) {
    if (!sortedPositions || sortedPositions.length === 0) {
        throw new Error('No positions to extend from');
    }

    if (!Array.isArray(tracks)) {
        throw new Error('Track data is required to extend');
    }

    const edgeTile = direction === 'start' ? sortedPositions[0] : sortedPositions[sortedPositions.length - 1];
    const [edgeX, edgeY] = edgeTile;

    const edgeTrack = tracks.find(t => t.x === edgeX && t.y === edgeY);
    if (!edgeTrack) {
        throw new Error(`No track found at edge position (${edgeX}, ${edgeY})`);
    }

    const exits = getTrackExits(edgeTrack.track_bits);
    if (exits.length !== 2) {
        throw new Error(`Edge tile must have exactly 2 exits, found ${exits.length}`);
    }

    console.log('[Editor] Edge tile:', {x: edgeX, y: edgeY, trackBits: edgeTrack.track_bits, exits});

    const neighbors = exits.map(exit => getNeighborPosition(edgeX, edgeY, exit));

    console.log('[Editor] Potential neighbors:', neighbors);

    const positionSet = new Set(sortedPositions.map(([x, y]) => `${x},${y}`));
    const availableNeighbors = neighbors.filter(([nx, ny]) => !positionSet.has(`${nx},${ny}`));

    if (availableNeighbors.length === 0) {
        throw new Error('Both neighbors are already in the platform - cannot extend');
    }

    let nextX, nextY;
    if (availableNeighbors.length > 1) {
        const [n1x, n1y] = availableNeighbors[0];
        const [n2x, n2y] = availableNeighbors[1];

        if (orientation === 'NS') {
            nextX = edgeX;
            nextY = direction === 'start' ? Math.max(n1y, n2y) : Math.min(n1y, n2y);
        } else if (orientation === 'EW') {
            nextX = direction === 'start' ? Math.min(n1x, n2x) : Math.max(n1x, n2x);
            nextY = edgeY;
        } else if (orientation === 'NE_SW') {
            const diff1 = n1x - n1y;
            const diff2 = n2x - n2y;
            [nextX, nextY] = direction === 'start' ?
                (diff1 > diff2 ? [n1x, n1y] : [n2x, n2y]) :
                (diff1 < diff2 ? [n1x, n1y] : [n2x, n2y]);
        } else if (orientation === 'NW_SE') {
            const sum1 = n1x + n1y;
            const sum2 = n2x + n2y;
            [nextX, nextY] = direction === 'start' ?
                (sum1 > sum2 ? [n1x, n1y] : [n2x, n2y]) :
                (sum1 < sum2 ? [n1x, n1y] : [n2x, n2y]);
        }

        console.log('[Editor] Single-tile station, chose direction:', direction, 'position:', {x: nextX, y: nextY});
    } else {
        [nextX, nextY] = availableNeighbors[0];
    }

    const trackBits = getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY);

    console.log('[Editor] Calculated next tile:', {x: nextX, y: nextY, trackBits});

    return { x: nextX, y: nextY, trackBits };
}

// ============================================================================
// PROPERTY RENDERING FUNCTIONS
// ============================================================================

/**
 * Render colored ID header box
 * @param {string} id - The ID to display
 * @param {string} borderColor - Hex color for border
 * @returns {string} HTML string
 */
function renderHeaderId(id, borderColor) {
    return `
        <div class="property-item">
            <div class="value-display value-display-mono font-bold text-white" style="border-color: ${borderColor};">${id}</div>
        </div>
    `;
}

/**
 * Render property item wrapper (label + content)
 * @param {string} label - Label text
 * @param {string} content - Inner HTML content
 * @returns {string} HTML string
 */
function renderPropertyItem(label, content) {
    return `
        <div class="property-item">
            <label class="property-label">${label}</label>
            ${content}
        </div>
    `;
}

/**
 * Render read-only value display
 * @param {string} label - Label text
 * @param {string} value - Value to display
 * @param {string} cssClass - Additional CSS class (e.g., 'text-track', 'text-info')
 * @param {boolean} mono - Use monospace font
 * @returns {string} HTML string
 */
function renderReadOnlyValue(label, value, cssClass = '', mono = false) {
    const monoClass = mono ? 'value-display-mono' : '';
    return renderPropertyItem(label, `
        <div class="value-display ${monoClass} ${cssClass}">
            ${value}
        </div>
    `);
}

/**
 * Render text input field
 * @param {string} id - Input element ID
 * @param {string} label - Label text
 * @param {string} value - Current value
 * @param {string} placeholder - Placeholder text
 * @param {object} dataAttrs - Data attributes as key-value pairs (e.g., {'signal-id': '123'})
 * @returns {string} HTML string
 */
function renderTextInput(id, label, value, placeholder = '', dataAttrs = {}) {
    const dataAttrStr = Object.entries(dataAttrs)
        .map(([key, val]) => `data-${key}="${val}"`)
        .join(' ');
    
    return renderPropertyItem(label, `
        <input type="text" id="${id}" value="${value}" placeholder="${placeholder}" class="property-input" ${dataAttrStr}>
    `);
}

/**
 * Render number input field
 * @param {string} id - Input element ID
 * @param {string} label - Label text
 * @param {number|string} value - Current value
 * @param {object} options - { min, max, step, placeholder }
 * @returns {string} HTML string
 */
function renderNumberInput(id, label, value, options = {}) {
    const { min = 0, max = '', step = 1, placeholder = '' } = options;
    const placeholderAttr = placeholder ? `placeholder="${placeholder}"` : '';
    const maxAttr = max !== '' ? `max="${max}"` : '';
    
    return renderPropertyItem(label, `
        <input type="number" id="${id}" value="${value}" min="${min}" ${maxAttr} step="${step}" ${placeholderAttr} class="property-input">
    `);
}

/**
 * Render dropdown select field
 * @param {string} id - Select element ID
 * @param {string} label - Label text
 * @param {string} value - Currently selected value
 * @param {Array<{value: string, label: string}>} options - Array of option objects
 * @param {object} dataAttrs - Data attributes as key-value pairs
 * @returns {string} HTML string
 */
function renderDropdown(id, label, value, options, dataAttrs = {}) {
    const dataAttrStr = Object.entries(dataAttrs)
        .map(([key, val]) => `data-${key}="${val}"`)
        .join(' ');
    
    const optionsHtml = options.map(opt => 
        `<option value="${opt.value}" ${opt.value === value ? 'selected' : ''}>${opt.label}</option>`
    ).join('');
    
    return renderPropertyItem(label, `
        <select id="${id}" class="property-input" ${dataAttrStr}>
            ${optionsHtml}
        </select>
    `);
}

/**
 * Render status badge with color
 * @param {string} label - Label text
 * @param {string} status - Status text to display
 * @param {string} cssClass - CSS class for styling (e.g., 'status-display-success')
 * @returns {string} HTML string
 */
function renderStatusBadge(label, status, cssClass) {
    return renderPropertyItem(label, `
        <div class="${cssClass}">
            ${status}
        </div>
    `);
}

/**
 * Render action buttons (Save/Delete)
 * @param {string} saveBtnId - ID for save button
 * @param {string} deleteBtnId - ID for delete button
 * @param {string} saveText - Text for save button (default: "Save Changes")
 * @param {string} deleteText - Text for delete button (default: "Delete")
 * @returns {string} HTML string
 */
function renderActionButtons(saveBtnId, deleteBtnId, saveText = 'Save Changes', deleteText = 'Delete') {
    return `
        <div class="property-item-row">
            <button id="${saveBtnId}" class="btn btn-primary text-lg font-bold" style="flex: 1;">
                ${saveText}
            </button>
            <button id="${deleteBtnId}" class="btn btn-danger text-lg font-bold" style="flex: 1;">
                ${deleteText}
            </button>
        </div>
    `;
}

/**
 * Render conditional content (only renders if condition is true)
 * @param {boolean} condition - Whether to render the content
 * @param {string} html - HTML to render if condition is true
 * @returns {string} HTML string or empty string
 */
function renderConditional(condition, html) {
    return condition ? html : '';
}

// ============================================================================
// BIT TOGGLE COMPONENT
// ============================================================================

/**
 * Render a bit toggle checkbox for track bit editor
 * @param {number} bit - Bit value (1, 2, 4, 8, 16, 32)
 * @param {string} name - Bit name (NS, EW, NE, NW, SE, SW)
 * @param {number} trackBits - Current track bits value
 * @returns {string} HTML string
 */
function renderBitToggle(bit, name, trackBits) {
    const isActive = trackBits & bit;
    return `
        <label class="bit-toggle-label ${isActive ? 'active' : 'inactive'} flex items-center p-4 rounded-sm text-xxs">
            <input type="checkbox" data-bit="${bit}" ${isActive ? 'checked' : ''} />
            <span class="bit-name ${isActive ? 'active' : 'inactive'}">${name} | ${bit}</span>
        </label>
    `;
}

// ============================================================================
// SIGNAL EDITOR COMPONENTS
// ============================================================================

/**
 * Render multi-signal header (for "Signal X of Y" display)
 * @param {number} current - Current signal index (1-based)
 * @param {number} total - Total number of signals
 * @param {string} signalId - Signal ID
 * @param {string} stateColor - Current state color
 * @returns {string} HTML string
 */
function renderMultiSignalHeader(current, total, signalId, stateColor) {
    return `
        <div class="property-item">
            <div class="text-lg text-track font-bold">
                Signal ${current} of ${total}
            </div>
            <div class="value-display value-display-mono font-bold text-white" style="border-color: ${stateColor}; margin-top: 8px;">
                ${signalId}
            </div>
        </div>
    `;
}

/**
 * Render signal state display with colored indicator dot
 * @param {string} state - Signal state (GREEN/YELLOW/RED)
 * @param {string} stateColor - Hex color for state
 * @returns {string} HTML string
 */
function renderSignalStateDisplay(state, stateColor) {
    return renderPropertyItem('Current State', `
        <div class="value-display" style="display: flex; align-items: center; gap: 12px;">
            <span style="display: inline-block; width: 20px; height: 20px; border-radius: 50%; background: ${stateColor}; box-shadow: 0 0 8px ${stateColor};"></span>
            <span class="text-lg font-bold" style="color: ${stateColor};">
                ${state}
            </span>
        </div>
    `);
}

// ============================================================================
// ENTITY SUMMARY COMPONENTS
// ============================================================================

/**
 * Render entity summary row (for blocks, signals, stations)
 * @param {object} options - { type, id, state, borderColor, stateColor, editBtnId }
 * @returns {string} HTML string
 */
function renderEntitySummary(options) {
    const { type, id, state, borderColor, stateColor, editBtnId } = options;
    return `
        <div class="property-item-row">
            <div class="${type}-summary-row" style="border-left: 3px solid ${borderColor}">
                <div class="${type}-info">
                    <span class="${type}-id">${id}</span>
                    <span class="${type}-state" style="color: ${stateColor}">${state}</span>
                </div>
                <button class="edit-btn ${editBtnId}" data-${type}-id="${id}">Edit</button>
            </div>
        </div>
    `;
}

// ============================================================================
// BLOCK STATE DISPLAY
// ============================================================================

/**
 * Render block state display with cycle button
 * @param {string} state - Current block state
 * @param {string} cycleButtonId - ID for the cycle button
 * @returns {string} HTML string
 */
function renderBlockStateDisplay(state, cycleButtonId) {
    const stateColor = {
        'free': '#04d361',
        'reserved': '#ffd93d',
        'occupied': '#e83f5b',
        'caution': '#ff9500'
    }[state] || '#888';

    return renderPropertyItem('Current State', `
        <div class="flex items-center gap-12 bg-input p-10 rounded-lg">
            <span class="inline-block rounded" style="width: 16px; height: 16px; border-radius: 50%; background: ${stateColor}; box-shadow: 0 0 8px ${stateColor};"></span>
            <span id="block-state-display" class="text-lg font-bold" style="color: ${stateColor}; text-transform: uppercase;">
                ${state || 'FREE'}
            </span>
            <button id="${cycleButtonId}" class="text-xs bg-hover rounded text-muted" style="margin-left: auto; padding: 6px 12px; border: 1px solid #4a4a6e; cursor: pointer;">
                Cycle State
            </button>
        </div>
    `);
}

// ============================================================================
// TILE LIST RENDERING
// ============================================================================

/**
 * Render tile list with extend/remove buttons for stations or blocks
 * @param {Array<{x: number, y: number}>} sortedPositions - Array of tile positions
 * @param {object} options - Configuration options
 * @returns {string} HTML string
 */
function renderTileList(sortedPositions, options) {
    const { entityType, extendStartBtnId, extendEndBtnId, removeBtnClass } = options;
    
    if (!sortedPositions || sortedPositions.length === 0) {
        return '';
    }

    const tileItems = sortedPositions.map(([x, y], index) => {
        const isFirst = index === 0;
        const isLast = index === sortedPositions.length - 1;
        const isEdge = isFirst || isLast;
        
        return `
            <div class="station-tile-item" data-x="${x}" data-y="${y}">
                <span class="text-info text-sm value-display-mono">(${x}, ${y})</span>
                ${isEdge ? `
                <button class="${removeBtnClass} btn btn-sm btn-danger" data-x="${x}" data-y="${y}">
                    Remove
                </button>
                ` : `
                <span class="text-muted text-xxs">middle</span>
                `}
            </div>
        `;
    }).join('');

    return `
        <div class="property-item">
            <label class="property-label">${entityType === 'station' ? 'Station' : 'Block'} Tiles (${sortedPositions.length})</label>
            
            <!-- Extend Start Button -->
            <button id="${extendStartBtnId}" class="btn btn-secondary btn-full text-sm mb-8">
                ➕ Extend Start
            </button>
            
            <!-- Sorted Tile List -->
            <div class="bg-input rounded-lg" style="max-height: 200px; overflow-y: auto; border: 1px solid #3a3a4e;">
                ${tileItems}
            </div>
            
            <!-- Extend End Button -->
            <button id="${extendEndBtnId}" class="btn btn-secondary btn-full text-sm mt-8">
                ➕ Extend End
            </button>
            
            <div class="info-box-warning text-xxs text-warning mt-4">
                Only edge tiles (start/end) can be removed
            </div>
        </div>
    `;
}

/**
 * Render tile list with block assignments for multi-block stations
 * @param {Array<[number, number]>} sortedPositions - Array of [x, y] coordinates
 * @param {object} blockAssignmentsSorted - Map of index -> block_id
 * @param {Array} blocks - Array of all blocks in network
 * @param {object} options - Configuration options
 * @returns {string} HTML string
 */
function renderTileListWithBlocks(sortedPositions, blockAssignmentsSorted, blocks, options) {
    const { entityType, extendStartBtnId, extendEndBtnId, removeBtnClass, assignBtnClass } = options;
    
    if (!sortedPositions || sortedPositions.length === 0) {
        return '';
    }

    const tileItems = sortedPositions.map(([x, y], index) => {
        const isFirst = index === 0;
        const isLast = index === sortedPositions.length - 1;
        const isEdge = isFirst || isLast;
        const assignedBlock = blockAssignmentsSorted && blockAssignmentsSorted[index]
            ? blockAssignmentsSorted[index]
            : null;
        
        // Get block color if assigned
        let blockBadge = '';
        if (assignedBlock) {
            const block = blocks.find(b => b.block_id === assignedBlock);
            const blockColor = block ? getBlockStateColor(block.state) : '#888';
            blockBadge = `<div class="text-xxs" style="color: ${blockColor}; font-weight: bold; margin-top: 4px;">● ${assignedBlock}</div>`;
        }
        
        return `
            <div class="station-tile-item" data-x="${x}" data-y="${y}" style="border-bottom: 1px solid #2a2a3e; padding: 8px 0;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <span class="text-info text-sm value-display-mono">(${x}, ${y})</span>
                        ${blockBadge}
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <button class="${assignBtnClass} btn btn-sm btn-info" data-x="${x}" data-y="${y}" data-index="${index}" title="Assign a block section to this station tile">
                            ${assignedBlock ? 'Change Block' : 'Assign Block'}
                        </button>
                        ${isEdge ? `
                        <button class="${removeBtnClass} btn btn-sm btn-danger" data-x="${x}" data-y="${y}">
                            Remove
                        </button>
                        ` : `
                        <span class="text-muted text-xxs">middle</span>
                        `}
                    </div>
                </div>
            </div>
        `;
    }).join('');

    return `
        <div class="property-item">
            <label class="property-label">${entityType === 'station' ? 'Station' : 'Block'} Tiles (${sortedPositions.length})</label>
            
            <!-- Extend Start Button -->
            <button id="${extendStartBtnId}" class="btn btn-secondary btn-full text-sm mb-8">
                ➕ Extend Start
            </button>
            
            <!-- Sorted Tile List with Block Assignments -->
            <div class="bg-input rounded-lg" style="max-height: 300px; overflow-y: auto; border: 1px solid #3a3a4e; padding: 8px;">
                ${tileItems}
            </div>
            
            <!-- Extend End Button -->
            <button id="${extendEndBtnId}" class="btn btn-secondary btn-full text-sm mt-8">
                ➕ Extend End
            </button>
            
            <div class="info-box-warning text-xxs text-warning mt-4">
                Only edge tiles (start/end) can be removed
            </div>
        </div>
    `;
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Get color for block state
 * @param {string} state - Block state (free, reserved, occupied, caution)
 * @returns {string} Hex color
 */
function getBlockStateColor(state) {
    const colors = {
        'free': '#04d361',
        'reserved': '#ffcd1e',
        'occupied': '#e83f5b',
        'caution': '#ff9500'
    };
    if (!state) {
        return '#888';
    }
    const normalizedState = String(state).toLowerCase();
    return colors[normalizedState] || '#888';
}

/**
 * Get color for signal state
 * @param {string} state - Signal state (green, yellow, red)
 * @returns {string} Hex color
 */
function getSignalStateColor(state) {
    const colors = {
        'green': '#04d361',
        'yellow': '#ffd93d',
        'red': '#e83f5b'
    };
    if (!state) {
        return '#888';
    }
    const normalizedState = String(state).toLowerCase();
    return colors[normalizedState] || '#888';
}
