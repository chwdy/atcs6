/**
 * helpers.js - Reusable UI Components and Helper Functions
 * 
 * This file contains standalone helper functions for generating consistent HTML components
 * across the editor panels (Station, Block, Signal, Train editors).
 * 
 * All functions follow the project's CSS utility class system (see STYLE_GUIDE.md).
 * Use CSS classes instead of inline styles whenever possible.
 */

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
        const assignedBlock = blockAssignmentsSorted?.[index] || null;
        
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
    return colors[state?.toLowerCase()] || '#888';
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
    return colors[state?.toLowerCase()] || '#888';
}
