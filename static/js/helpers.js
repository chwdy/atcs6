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
// BLOCK-RELATIVE DIRECTION FORMATTING
// ============================================================================

/**
 * Format a block-relative direction ("forward" / "reverse" / "either") as a
 * decorated label that also surfaces the concrete map heading.
 *
 * `direction` values come from the backend per `design_new.md`:
 *   - "forward" / "reverse" — block-relative tokens, resolved via
 *     `Block.ordered_tiles` adjacency (`BlockManager.direction_info(...)`).
 *   - "either" — author-level preference; carries no concrete geometry.
 *
 * Forms produced (per the choice in this UI pass):
 *   - `compact` ("F·E", "R·N", "↔")        — for tight chips
 *   - `full`    ("F-(5,3)-E", "R-(2,0)-N") — for tooltips and detail rows
 *
 * When geometry isn't available (no block picked yet, "either" preference,
 * or backend couldn't resolve), the helper falls back to a single-letter
 * prefix ("F" / "R" / "↔") so the UI never shows a bare "forward".
 *
 * Inputs:
 *   - direction: "forward" | "reverse" | "either" | null
 *   - terminalTile: [x, y] | null         — last-traversed tile in that direction
 *   - terminalHeading: "N"|"E"|"S"|"W"|null — compass exit edge
 *   - opts:
 *       - form: "compact" | "full" (default: "compact")
 *       - emDash: string used when direction is null (default: "—")
 *
 * Examples:
 *   formatBlockDirection("forward", [5,3], "E")            -> "F·E"
 *   formatBlockDirection("forward", [5,3], "E", {form:"full"}) -> "F-(5,3)-E"
 *   formatBlockDirection("reverse", null, null)            -> "R"
 *   formatBlockDirection("either")                          -> "↔"
 *   formatBlockDirection(null)                              -> "—"
 */
function formatBlockDirection(direction, terminalTile, terminalHeading, opts) {
    const form = (opts && opts.form) || 'compact';
    const emDash = (opts && opts.emDash) || '—';

    if (!direction) return emDash;
    const lower = String(direction).toLowerCase();

    if (lower === 'either') return '↔';

    const prefix = lower === 'forward' ? 'F'
                 : lower === 'reverse' ? 'R'
                 : null;
    if (prefix === null) return String(direction);

    const hasGeom = Array.isArray(terminalTile)
        && terminalTile.length === 2
        && terminalHeading;
    if (!hasGeom) return prefix;

    const [tx, ty] = terminalTile;
    const heading = String(terminalHeading).toUpperCase();

    if (form === 'full') {
        return `${prefix}-(${tx},${ty})-${heading}`;
    }
    return `${prefix}·${heading}`;
}

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

    console.debug('[Editor] Edge tile:', {x: edgeX, y: edgeY, trackBits: edgeTrack.track_bits, exits});

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
        console.debug('[Editor] Single-tile station, chose direction:', direction, 'position:', {x: nextX, y: nextY});
    } else {
        [nextX, nextY] = availableNeighbors[0];
    }

    const trackBits = getTrackBitsForOrientation(orientation, nextX, nextY, edgeX, edgeY);
    console.debug('[Editor] Calculated next tile:', {x: nextX, y: nextY, trackBits});

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

        // Move footer inside form so submit button works
        form.appendChild(footer);

        body.appendChild(form);

        modal.appendChild(header);
        modal.appendChild(body);
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
// AVAILABILITY SEGMENT (Block / Switch / Platform / Station)
// ----------------------------------------------------------------------------
// Three-state segmented control per docs/DESIGN_LANGUAGE.md §3:
//   Available  — disabled=false, manual=false  (default)
//   Manual     — disabled=false, manual=true   (auto blocks; operator can use)
//   Disabled   — disabled=true                 (off-limits to everything)
//
// Returns the markup for the segmented control. The caller wires three
// buttons by ID and applies the chosen state back via the entity PATCH
// endpoint (`{disabled: ..., manual: ...}`).
//
// `entityKind` is used only for ID prefixing so multiple availability
// segments can coexist on one screen (e.g. block-editor + adjacent switch).
// ============================================================================

function deriveAvailabilityState(entity) {
    if (!entity) return 'available';
    if (entity.disabled) return 'disabled';
    if (entity.manual) return 'manual';
    return 'available';
}

function availabilityStateToFlags(state) {
    switch (state) {
        case 'disabled':
            return { disabled: true, manual: false };
        case 'manual':
            return { disabled: false, manual: true };
        case 'available':
        default:
            return { disabled: false, manual: false };
    }
}

function renderAvailabilitySegment(entityKind, entity, opts = {}) {
    const state = deriveAvailabilityState(entity);
    const idBase = (opts.idBase || `${entityKind}-availability`).replace(/[^a-z0-9-]/gi, '-');
    const label = opts.label || 'Availability';
    const help = opts.help
        || 'Manual = hidden from automatic routing (operator can still override). Disabled = fully off-limits.';
    const dis = (s) => state === s ? 'active' : '';
    return `
        <div class="input-large-container mb-8" data-availability-segment="${entityKind}">
            <label class="input-large-label">${label}</label>
            <div class="switch-group" role="radiogroup" aria-label="${label}">
                <button type="button"
                    class="switch-btn ${dis('available')}"
                    id="${idBase}-available"
                    data-availability="available"
                    title="Available — normal operation">Available</button>
                <button type="button"
                    class="switch-btn ${dis('manual')}"
                    id="${idBase}-manual"
                    data-availability="manual"
                    title="Manual — auto-routing skips this; operator can override with manual_override=true">Manual</button>
                <button type="button"
                    class="switch-btn ${dis('disabled')}"
                    id="${idBase}-disabled"
                    data-availability="disabled"
                    title="Disabled — physically off-limits; no operator override">Disabled</button>
            </div>
            <div class="text-xxs text-muted mt-4">${help}</div>
        </div>
    `;
}

// Wire the three buttons of a `renderAvailabilitySegment` block. `onChange`
// ============================================================================
// PRIORITY INPUT VALIDATION
// ============================================================================
//
// Per design_new.md "RouteRun overrides" — priority is an integer in 0..9.
// The route editor and the per-RouteRun override editor both surface a free-
// text priority input; both call this helper so a single rule decides what
// "empty / valid / invalid" means and the user always sees a toast on invalid.
//
// Returns one of three shapes:
//   { kind: 'empty' }                 — input was blank or missing; caller skips the field.
//   { kind: 'ok', value: <int> }      — valid integer in 0..9.
//   { kind: 'error', message: <str> } — invalid; caller shows the message via showToast('error').
function validatePriorityInput(rawValue) {
    if (rawValue === undefined || rawValue === null) {
        return { kind: 'empty' };
    }
    const trimmed = String(rawValue).trim();
    if (trimmed === '') {
        return { kind: 'empty' };
    }
    const n = Number(trimmed);
    if (!Number.isInteger(n)) {
        return { kind: 'error', message: 'Priority must be a whole number between 0 and 9' };
    }
    if (n < 0 || n > 9) {
        return { kind: 'error', message: 'Priority must be between 0 and 9' };
    }
    return { kind: 'ok', value: n };
}

// receives `(state, flags)` where `flags = {disabled, manual}` ready to send
// to the entity PATCH endpoint.
function bindAvailabilitySegment(idBase, onChange) {
    const states = ['available', 'manual', 'disabled'];
    states.forEach((state) => {
        const btn = document.getElementById(`${idBase}-${state}`);
        if (!btn) return;
        btn.onclick = () => {
            states.forEach((s) => {
                const b = document.getElementById(`${idBase}-${s}`);
                if (b) b.classList.toggle('active', s === state);
            });
            try {
                onChange(state, availabilityStateToFlags(state));
            } catch (err) {
                console.error('[availability-segment] onChange failed', err);
            }
        };
    });
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

function getBlockStateColor(state, alpha = 1) {
    const tokenMap = {
        free: '--block-free',
        reserved: '--block-reserved',
        occupied: '--block-occupied',
        caution: '--block-caution',
        disabled: '--block-disabled',
        manual: '--block-manual'
    };
    const token = tokenMap[(state || '').toLowerCase()];
    const fallback = '#888';
    let hex = fallback;
    if (token) {
        const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
        if (value) hex = value;
    }
    if (alpha === 1) return hex;
    let h = hex.replace('#', '');
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
