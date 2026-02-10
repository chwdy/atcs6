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
// INTERNAL HELPERS
// ============================================================================

/**
 * Generate HTML attributes string from object
 * @param {object} attrs - Key-value pairs for data attributes (e.g. {'id': '1'} -> data-id="1")
 * @returns {string} String of data attributes
 */
function renderAttributes(attrs) {
    if (!attrs) return '';
    return Object.entries(attrs)
        .map(([key, val]) => `data-${key}="${val}"`)
        .join(' ');
}

// ============================================================================
// TRACK & ORIENTATION LOGIC
// ============================================================================

function getTrackExits(trackBits) {
    const bitMap = [
        [TRACK_NS, ['N', 'S']], [TRACK_EW, ['E', 'W']],
        [TRACK_NE, ['N', 'E']], [TRACK_NW, ['N', 'W']],
        [TRACK_SE, ['S', 'E']], [TRACK_SW, ['S', 'W']]
    ];
    
    const exits = bitMap.flatMap(([bit, dirs]) => (trackBits & bit) ? dirs : []);
    return [...new Set(exits)];
}

function getNeighborPosition(x, y, direction) {
    const offsets = { 'N': [0, 1], 'E': [1, 0], 'S': [0, -1], 'W': [-1, 0] };
    const [dx, dy] = offsets[direction] || [0, 0];
    return [x + dx, y + dy];
}

function getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY) {
    if (orientation === 'NS') return TRACK_NS;
    if (orientation === 'EW') return TRACK_EW;
    
    const dx = nextX - edgeX;
    const dy = nextY - edgeY;
    
    if (orientation === 'NE_SW') {
        if (dx === 1 && dy === 0) return TRACK_SW;
        if (dx === 0 && dy === 1) return TRACK_NE;
        if (dx === -1 && dy === 0) return TRACK_NE;
        if (dx === 0 && dy === -1) return TRACK_SW;
    }
    
    if (orientation === 'NW_SE') {
        if (dx === 1 && dy === 0) return TRACK_SE;
        if (dx === 0 && dy === 1) return TRACK_SE;
        if (dx === -1 && dy === 0) return TRACK_NW;
        if (dx === 0 && dy === -1) return TRACK_NW;
    }

    throw new Error(`Cannot determine track bits for orientation ${orientation}`);
}

function detectOrientation(sortedPositions, tracks) {
    if (!sortedPositions?.length) throw new Error('Need at least 1 position to detect orientation');
    if (!Array.isArray(tracks)) throw new Error('Track data is required to detect orientation');

    if (sortedPositions.length === 1) {
        const [x, y] = sortedPositions[0];
        const track = tracks.find(t => t.x === x && t.y === y);
        if (!track) throw new Error(`No track found at position (${x}, ${y})`);

        if (track.track_bits & TRACK_NS) return 'NS';
        if (track.track_bits & TRACK_EW) return 'EW';
        if (track.track_bits & (TRACK_NE | TRACK_SW)) return 'NE_SW';
        if (track.track_bits & (TRACK_NW | TRACK_SE)) return 'NW_SE';

        throw new Error('Cannot determine orientation from track bits');
    }

    const allSameX = sortedPositions.every(([x]) => x === sortedPositions[0][0]);
    if (allSameX) return 'NS';

    const allSameY = sortedPositions.every(([_, y]) => y === sortedPositions[0][1]);
    if (allSameY) return 'EW';

    const sums = sortedPositions.map(([x, y]) => x + y);
    const diffs = sortedPositions.map(([x, y]) => x - y);

    // Check strict monotonicity
    const isMonotonic = arr => arr.slice(1).every((val, i) => val > arr[i]);
    
    if (isMonotonic(diffs)) return 'NE_SW';
    if (isMonotonic(sums)) return 'NW_SE';

    const sumVariance = Math.max(...sums) - Math.min(...sums);
    const diffVariance = Math.max(...diffs) - Math.min(...diffs);

    return diffVariance > sumVariance ? 'NE_SW' : 'NW_SE';
}

function calculateNextTile(sortedPositions, orientation, direction, tracks) {
    if (!sortedPositions?.length) throw new Error('No positions to extend from');
    if (!Array.isArray(tracks)) throw new Error('Track data is required to extend');

    const edgeTile = direction === 'start' ? sortedPositions[0] : sortedPositions[sortedPositions.length - 1];
    const [edgeX, edgeY] = edgeTile;

    const edgeTrack = tracks.find(t => t.x === edgeX && t.y === edgeY);
    if (!edgeTrack) throw new Error(`No track found at edge position (${edgeX}, ${edgeY})`);

    const exits = getTrackExits(edgeTrack.track_bits);
    if (exits.length !== 2) throw new Error(`Edge tile must have exactly 2 exits, found ${exits.length}`);

    console.log('[Editor] Edge tile:', {x: edgeX, y: edgeY, trackBits: edgeTrack.track_bits, exits});

    const neighbors = exits.map(exit => getNeighborPosition(edgeX, edgeY, exit));
    const positionSet = new Set(sortedPositions.map(([x, y]) => `${x},${y}`));
    const availableNeighbors = neighbors.filter(([nx, ny]) => !positionSet.has(`${nx},${ny}`));

    if (availableNeighbors.length === 0) throw new Error('Both neighbors are already in the platform - cannot extend');

    let nextX, nextY;
    if (availableNeighbors.length > 1) {
        const [n1x, n1y] = availableNeighbors[0];
        const [n2x, n2y] = availableNeighbors[1];

        // Logic to pick correct neighbor based on orientation flow
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
 */
function renderTextInput(id, label, value, options = {}) {
    const { placeholder = '', dataAttrs = {} } = options;
    return renderPropertyItem(label, `
        <input type="text" id="${id}" value="${value}" placeholder="${placeholder}" class="property-input" ${renderAttributes(dataAttrs)}>
    `);
}

/**
 * Render number input field
 */
function renderNumberInput(id, label, value, options = {}) {
    const { min = 0, max = '', step = 1, placeholder = '' } = options;
    const maxAttr = max !== '' ? `max="${max}"` : '';
    const placeholderAttr = placeholder ? `placeholder="${placeholder}"` : '';
    
    return renderPropertyItem(label, `
        <input type="number" id="${id}" value="${value}" min="${min}" ${maxAttr} step="${step}" ${placeholderAttr} class="property-input">
    `);
}

/**
 * Render dropdown select field
 */
function renderDropdown(id, label, value, options, dataAttrs = {}) {
    const optionsHtml = options.map(({value: v, label: l}) => 
        `<option value="${v}" ${v === value ? 'selected' : ''}>${l}</option>`
    ).join('');
    
    return renderPropertyItem(label, `
        <select id="${id}" class="property-input" ${renderAttributes(dataAttrs)}>
            ${optionsHtml}
        </select>
    `);
}

/**
 * Render status badge with color
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
 */
function renderActionButtons(saveBtnId, deleteBtnId, saveText = 'Save Changes', deleteText = 'Delete') {
    return `
        <div class="property-item-row">
            <button id="${saveBtnId}" class="btn btn-primary text-lg font-bold" style="flex: 1;">${saveText}</button>
            <button id="${deleteBtnId}" class="btn btn-danger text-lg font-bold" style="flex: 1;">${deleteText}</button>
        </div>
    `;
}

/**
 * Render conditional content
 */
function renderConditional(condition, html) {
    return condition ? html : '';
}

/**
 * Reusable confirmation modal for destructive or important actions.
 * Returns a promise that resolves true on confirm, false on cancel.
 */
function showConfirmModal(options = {}) {
    const {
        title = 'Confirm Action',
        message = 'Are you sure?',
        detail = '',
        confirmText = 'Confirm',
        cancelText = 'Cancel',
        confirmVariant = 'danger'
    } = options;

    const variantClassMap = {
        danger: 'btn-danger',
        success: 'btn-success',
        primary: 'btn-primary',
        secondary: 'btn-secondary'
    };
    const confirmClass = variantClassMap[confirmVariant] || 'btn-danger';

    return new Promise((resolve) => {
        const existing = document.querySelector('.app-modal-overlay');
        if (existing) {
            existing.remove();
        }

        const overlay = document.createElement('div');
        overlay.className = 'app-modal-overlay';

        const modal = document.createElement('div');
        modal.className = 'app-modal';

        const header = document.createElement('div');
        header.className = 'app-modal-header';
        header.textContent = title;

        const body = document.createElement('div');
        body.className = 'app-modal-body';

        const messageEl = document.createElement('div');
        messageEl.className = 'app-modal-message';
        messageEl.textContent = message;
        body.appendChild(messageEl);

        if (detail) {
            const detailEl = document.createElement('div');
            detailEl.className = 'app-modal-detail';
            detailEl.textContent = detail;
            body.appendChild(detailEl);
        }

        const footer = document.createElement('div');
        footer.className = 'app-modal-footer';

        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn btn-secondary btn-sm';
        cancelBtn.textContent = cancelText;

        const confirmBtn = document.createElement('button');
        confirmBtn.className = `btn ${confirmClass} btn-sm`;
        confirmBtn.textContent = confirmText;

        footer.appendChild(cancelBtn);
        footer.appendChild(confirmBtn);

        modal.appendChild(header);
        modal.appendChild(body);
        modal.appendChild(footer);
        overlay.appendChild(modal);

        const cleanup = () => {
            document.removeEventListener('keydown', onKeyDown);
            overlay.remove();
        };

        const handleCancel = () => {
            cleanup();
            resolve(false);
        };

        const handleConfirm = () => {
            cleanup();
            resolve(true);
        };

        const onKeyDown = (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                handleCancel();
                return;
            }

            if (event.key === 'Enter') {
                event.preventDefault();
                handleConfirm();
            }
        };

        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) {
                handleCancel();
            }
        });
        cancelBtn.addEventListener('click', handleCancel);
        confirmBtn.addEventListener('click', handleConfirm);
        document.addEventListener('keydown', onKeyDown);

        document.body.appendChild(overlay);
        cancelBtn.focus();
    });
}

/**
 * Reusable form modal for collecting one or more input values.
 * Returns an object of field values, or null when cancelled.
 */
function showFormModal(options = {}) {
    const {
        title = 'Input Required',
        message = '',
        detail = '',
        fields = [],
        confirmText = 'Save',
        cancelText = 'Cancel',
        confirmVariant = 'primary'
    } = options;

    const variantClassMap = {
        danger: 'btn-danger',
        success: 'btn-success',
        primary: 'btn-primary',
        secondary: 'btn-secondary'
    };
    const confirmClass = variantClassMap[confirmVariant] || 'btn-primary';

    return new Promise((resolve) => {
        const existing = document.querySelector('.app-modal-overlay');
        if (existing) {
            existing.remove();
        }

        const overlay = document.createElement('div');
        overlay.className = 'app-modal-overlay';

        const modal = document.createElement('div');
        modal.className = 'app-modal';

        const header = document.createElement('div');
        header.className = 'app-modal-header';
        header.textContent = title;

        const body = document.createElement('div');
        body.className = 'app-modal-body';

        if (message) {
            const messageEl = document.createElement('div');
            messageEl.className = 'app-modal-message';
            messageEl.textContent = message;
            body.appendChild(messageEl);
        }

        if (detail) {
            const detailEl = document.createElement('div');
            detailEl.className = 'app-modal-detail';
            detailEl.textContent = detail;
            body.appendChild(detailEl);
        }

        const form = document.createElement('form');
        form.className = 'app-modal-form';
        form.noValidate = true;

        fields.forEach((field) => {
            const wrapper = document.createElement('div');
            wrapper.className = 'app-modal-field';

            const label = document.createElement('label');
            label.textContent = field.label || field.name;
            label.htmlFor = `app-modal-${field.name}`;
            wrapper.appendChild(label);

            let inputEl;
            if (field.type === 'select') {
                inputEl = document.createElement('select');
                const optionsList = Array.isArray(field.options) ? field.options : [];
                optionsList.forEach((opt) => {
                    const optionEl = document.createElement('option');
                    optionEl.value = opt.value;
                    optionEl.textContent = opt.label;
                    inputEl.appendChild(optionEl);
                });
            } else if (field.type === 'textarea') {
                inputEl = document.createElement('textarea');
                inputEl.rows = field.rows || 3;
            } else {
                inputEl = document.createElement('input');
                inputEl.type = field.type || 'text';
            }

            inputEl.id = `app-modal-${field.name}`;
            inputEl.name = field.name;
            inputEl.className = 'property-input';
            if (field.placeholder) inputEl.placeholder = field.placeholder;
            if (field.required) inputEl.required = true;
            if (typeof field.min !== 'undefined') inputEl.min = String(field.min);
            if (typeof field.max !== 'undefined') inputEl.max = String(field.max);
            if (typeof field.step !== 'undefined') inputEl.step = String(field.step);
            if (field.defaultValue !== undefined && field.defaultValue !== null) {
                inputEl.value = String(field.defaultValue);
            }
            wrapper.appendChild(inputEl);

            if (field.hint) {
                const hint = document.createElement('div');
                hint.className = 'app-modal-hint';
                hint.textContent = field.hint;
                wrapper.appendChild(hint);
            }

            form.appendChild(wrapper);
        });

        const errorEl = document.createElement('div');
        errorEl.className = 'app-modal-error hidden';
        form.appendChild(errorEl);

        body.appendChild(form);

        const footer = document.createElement('div');
        footer.className = 'app-modal-footer';

        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'btn btn-secondary btn-sm';
        cancelBtn.textContent = cancelText;

        const confirmBtn = document.createElement('button');
        confirmBtn.type = 'submit';
        confirmBtn.className = `btn ${confirmClass} btn-sm`;
        confirmBtn.textContent = confirmText;

        footer.appendChild(cancelBtn);
        footer.appendChild(confirmBtn);

        modal.appendChild(header);
        modal.appendChild(body);
        modal.appendChild(footer);
        overlay.appendChild(modal);

        const showError = (messageText) => {
            errorEl.textContent = messageText;
            errorEl.classList.remove('hidden');
        };

        const clearError = () => {
            errorEl.textContent = '';
            errorEl.classList.add('hidden');
        };

        const collectValues = () => {
            const values = {};
            for (const field of fields) {
                const el = form.elements.namedItem(field.name);
                if (!el) continue;

                let value = el.value;
                if (field.type === 'number') {
                    if (value === '') {
                        value = '';
                    } else {
                        const parsed = Number(value);
                        if (!Number.isFinite(parsed)) {
                            return { ok: false, error: `${field.label || field.name} must be a valid number.` };
                        }
                        value = parsed;
                    }
                }

                if (field.required && (value === '' || value === null || value === undefined)) {
                    return { ok: false, error: `${field.label || field.name} is required.` };
                }

                if (typeof field.validate === 'function') {
                    const validationResult = field.validate(value, values);
                    if (typeof validationResult === 'string' && validationResult) {
                        return { ok: false, error: validationResult };
                    }
                }

                values[field.name] = value;
            }
            return { ok: true, values };
        };

        const cleanup = () => {
            document.removeEventListener('keydown', onKeyDown);
            overlay.remove();
        };

        const handleCancel = () => {
            cleanup();
            resolve(null);
        };

        const handleSubmit = (event) => {
            event.preventDefault();
            clearError();
            const collected = collectValues();
            if (!collected.ok) {
                showError(collected.error || 'Please correct the highlighted fields.');
                return;
            }
            cleanup();
            resolve(collected.values);
        };

        const onKeyDown = (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                handleCancel();
            }
        };

        form.addEventListener('submit', handleSubmit);
        cancelBtn.addEventListener('click', handleCancel);
        overlay.addEventListener('click', (event) => {
            if (event.target === overlay) {
                handleCancel();
            }
        });
        document.addEventListener('keydown', onKeyDown);

        document.body.appendChild(overlay);
        const firstInput = form.querySelector('input, select, textarea');
        if (firstInput) {
            firstInput.focus();
            if (firstInput.select) firstInput.select();
        }
    });
}

/**
 * Reusable single-input prompt modal.
 * Returns the entered value, or null if cancelled.
 */
async function showPromptModal(options = {}) {
    const {
        title = 'Input Required',
        label = 'Value',
        placeholder = '',
        defaultValue = '',
        required = false,
        type = 'text',
        confirmText = 'OK',
        cancelText = 'Cancel',
        confirmVariant = 'primary',
        hint = '',
        validate = null
    } = options;

    const values = await showFormModal({
        title,
        fields: [{
            name: 'value',
            label,
            placeholder,
            defaultValue,
            required,
            type,
            hint,
            validate
        }],
        confirmText,
        cancelText,
        confirmVariant
    });

    if (!values) {
        return null;
    }
    return values.value;
}

// ============================================================================
// BIT TOGGLE COMPONENT
// ============================================================================

function renderBitToggle(bit, name, trackBits) {
    const isActive = trackBits & bit;
    const activeClass = isActive ? 'active' : 'inactive';
    return `
        <label class="bit-toggle-label ${activeClass} flex items-center p-4 rounded-sm text-xxs">
            <input type="checkbox" data-bit="${bit}" ${isActive ? 'checked' : ''} />
            <span class="bit-name ${activeClass}">${name} | ${bit}</span>
        </label>
    `;
}

// ============================================================================
// SIGNAL EDITOR COMPONENTS
// ============================================================================

function renderMultiSignalHeader(current, total, signalId, stateColor) {
    return `
        <div class="property-item">
            <div class="text-lg text-track font-bold">Signal ${current} of ${total}</div>
            <div class="value-display value-display-mono font-bold text-white" style="border-color: ${stateColor}; margin-top: 8px;">
                ${signalId}
            </div>
        </div>
    `;
}

function renderSignalStateDisplay(state, stateColor) {
    return renderPropertyItem('Current State', `
        <div class="value-display" style="display: flex; align-items: center; gap: 12px;">
            <span style="display: inline-block; width: 20px; height: 20px; border-radius: 50%; background: ${stateColor}; box-shadow: 0 0 8px ${stateColor};"></span>
            <span class="text-lg font-bold" style="color: ${stateColor};">${state}</span>
        </div>
    `);
}

// ============================================================================
// ENTITY SUMMARY COMPONENTS
// ============================================================================

function renderEntitySummary({ type, id, state, borderColor, stateColor, editBtnId }) {
    return `
        <div class="property-item-row">
            <div class="${type}-summary-row" style="border-left: 3px solid ${borderColor}">
                <div class="${type}-info">
                    <span class="${type}-id">${id}</span>
                    <span class="${type}-state" style="color: ${stateColor}">${state}</span>
                </div>
                <button id="${editBtnId}" class="edit-btn btn-sm btn-secondary" data-${type}-id="${id}">Edit</button>
            </div>
        </div>
    `;
}

// ============================================================================
// BLOCK STATE DISPLAY
// ============================================================================

function renderBlockStateDisplay(state, cycleButtonId) {
    const stateColor = getBlockStateColor(state);

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

function renderTileItem(x, y, index, total, assignedBlock, blocks, removeBtnClass, assignBtnClass = null) {
    const isEdge = index === 0 || index === total - 1;
    let content = '';

    if (assignBtnClass) {
        // Render for multi-block station (with block assignment)
        let blockBadge = '';
        if (assignedBlock) {
            const block = blocks.find(b => b.block_id === assignedBlock);
            const blockColor = getBlockStateColor(block?.state);
            blockBadge = `<span class="tile-list-badge" style="background: ${blockColor}20; color: ${blockColor};">${assignedBlock}</span>`;
        }

        const assignButton = `
            <button class="${assignBtnClass} btn btn-xs btn-info" data-x="${x}" data-y="${y}" data-index="${index}" title="Assign a block section to this platform tile">
                ${assignedBlock ? 'Change' : 'Assign'}
            </button>
        `;

        const removeButton = isEdge ? 
            `<button class="${removeBtnClass} btn btn-xs btn-danger" data-x="${x}" data-y="${y}">Remove</button>` : 
            `<span class="tile-list-middle-label">middle</span>`;

        content = `
            <div class="flex items-center gap-8">
                <span class="tile-list-coords">(${x}, ${y})</span>
                ${blockBadge}
            </div>
            <div class="flex items-center gap-8">
                ${assignButton}
                ${removeButton}
            </div>
        `;
    } else {
        // Simple tile item
        const removeButton = isEdge ? 
            `<button class="${removeBtnClass} btn btn-xs btn-danger" data-x="${x}" data-y="${y}">Remove</button>` : 
            `<span class="tile-list-middle-label">middle</span>`;

        content = `
            <span class="tile-list-coords">(${x}, ${y})</span>
            ${removeButton}
        `;
    }

    return `
        <div class="tile-list-item" data-x="${x}" data-y="${y}">
            ${content}
        </div>
    `;
}

function renderTileList(sortedPositions, options) {
    const { 
        entityType, 
        extendStartBtnId, 
        extendEndBtnId, 
        removeBtnClass,
        // Optional extras for block assignment (stations)
        blockAssignments = null,
        blocks = null,
        assignBtnClass = null
    } = options;

    if (!sortedPositions?.length) return '';

    const tileItems = sortedPositions.map(([x, y], index) => {
        const assignedBlock = blockAssignments?.[index] || null;
        return renderTileItem(x, y, index, sortedPositions.length, assignedBlock, blocks, removeBtnClass, assignBtnClass);
    }).join('');

    return `
        <div class="tile-list">
            <div class="tile-list-header">${entityType === 'platform' ? 'Platform' : 'Block'} Tiles (${sortedPositions.length})</div>
            <button id="${extendStartBtnId}" class="btn btn-secondary btn-sm tile-list-extend-btn">➕ Extend Start</button>
            <div class="tile-list-container">
                ${tileItems}
            </div>
            <button id="${extendEndBtnId}" class="btn btn-secondary btn-sm tile-list-extend-btn">➕ Extend End</button>
            <div class="info-box-warning text-xxs text-warning mt-4 mb-0">Only edge tiles (start/end) can be removed</div>
        </div>
    `;
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

function getBlockStateColor(state) {
    const colors = {
        free: '#04d361',
        reserved: '#ffcd1e',
        occupied: '#e83f5b',
        caution: '#ff9500'
    };
    return colors[(state || '').toLowerCase()] || '#888';
}

function getSignalStateColor(state) {
    const colors = {
        green: '#04d361',
        yellow: '#ffd93d',
        red: '#e83f5b'
    };
    return colors[(state || '').toLowerCase()] || '#888';
}
