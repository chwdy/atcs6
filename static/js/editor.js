// ============================================================================
// editor.js - Railway Track Editor
// ============================================================================
// Requires: constants.js, api.js, helpers.js, pathfinders.js, renderer.js
// ============================================================================

class Editor {
    constructor(renderer, api) {
        this.renderer = renderer;
        this.api = api;
        this.selectedTool = 'select';
        this.isPanning = false;
        this.isDraggingBuild = false;
        this.dragStart = null;
        this.dragEnd = null;
        this.previewPath = null;
        this.lastValidPreviewPath = null;
        this.lastValidPreviewTracks = null;
        this.lastMouseX = 0;
        this.lastMouseY = 0;
        this.selectedElement = null;
        this.cachedNetwork = null;
        this.cachedBlocks = [];  // Block Manager cache
        this.selectedBlockId = null;  // Currently selected block in Block Manager

        // Hover preview for signal/platform tools
        this.hoverPosition = null;
        this.hoverValid = false;
        this.previewSignalDirection = null;  // Current preview direction for signal tool (cycles on right-click)
        this.previewSignalDirectionIndex = 0;  // Index into valid directions array
        
        // Shift-key avoid-connect mode: while held, the track pathfinder
        // prefers crossovers over creating switches on existing-track tiles.
        this.avoidConnectMode = false;

        // R/T-key forced-segment override for track drag: when set, the
        // start (R) or end (T) tile is rendered with the chosen segment
        // instead of being inferred from the path. Cycles through
        // null -> NS -> EW -> NW -> SW -> SE -> NE -> null. Resets to null
        // when a drag commits or is cancelled.
        this.forcedStartSegment = null;
        this.forcedEndSegment = null;


        // Route tool state
        this.pathToolState = {
            active: false,
            startBlockId: null,
            startPosition: null,           // {x, y} of clicked tile
            reachableEndpoints: [],        // Block IDs from API
            reachablePositions: new Set(), // Tile positions for quick lookup
            priority: 5,                   // Default priority (1-10)
            trainId: null                  // Auto-detected or user-selected
        };
        this.cachedPaths = [];            // List of active routes

        // Box-select tool state. selectedKeys accumulates "x,y" strings across
        // successive rectangle drags; cleared by Delete (after success), Esc
        // (when non-empty), Space, or mode exit. Drag coords are screen-space
        // so the rubber-band stays pixel-accurate during zooming.
        //
        // The lasso-select and paint-select tools share the same selectedKeys
        // set so Delete / Esc / Space semantics apply uniformly; only the
        // in-progress drag shape differs (rectangle, free-drawn polyline, or
        // per-tile brush). `shiftRemoving` is captured at mousedown so the
        // add/remove choice is locked for the duration of the drag even if
        // the user releases Shift partway through.
        this.boxSelectState = {
            selectedKeys: new Set(),
            dragScreenStart: null,
            dragScreenEnd: null,
            isDragging: false,
            shiftRemoving: false,
        };
        this.lassoSelectState = {
            screenPoints: [],
            isDrawing: false,
            shiftRemoving: false,
        };
        this.paintSelectState = {
            isPainting: false,
            shiftRemoving: false,
            lastKey: null,  // last "x,y" applied this stroke, to avoid redundant work
        };

        // === Active editor mode ===
        //
        // An EditorMode is the unified abstraction for any multi-step
        // interaction on the layout (drawing a route, picking a tile,
        // extending a structure, etc.). Shape:
        //
        //   {
        //     name:           string,                       // log + default ESC toast
        //     toolName?:      string,                       // toolbar tool this mode owns
        //     cancelMessage?: string,                       // override for the ESC toast
        //     onEnter?:       () => void | Promise<void>,   // setup hook
        //     onClick:        ({gridPos, screenX, screenY, event}) => bool | Promise<bool>,
        //     onExit:         () => void | Promise<void>    // teardown hook
        //   }
        //
        // Invariants:
        //   - At most one activeMode at a time. enterMode exits the prior.
        //   - When activeMode.toolName is set, selectedTool === activeMode.toolName.
        //     enterMode sets the tool; exitMode resets it to 'select'.
        //   - When activeMode.toolName is null/undefined (sidebar/panel-driven),
        //     selectedTool is forced to 'select' on enter and untouched on exit.
        this.activeMode = null;

        this.pathfinder = new ConstructionPathfinder();
        this.blockHandler = new BlockEditorHandler(this);
        this.signalHandler = new SignalEditorHandler(this);
        this.platformHandler = new PlatformEditorHandler(this);
        this.stationGroupHandler = new StationGroupEditorHandler(this);
        this.trainHandler = new TrainEditorHandler(this);
        this.pathHandler = new PathEditorHandler(this);
        this.routeHandler = new RouteEditorHandler(this);
        this.routeRunHandler = new RouteRunEditorHandler(this);
        this.switchHandler = new SwitchEditorHandler(this);
        this.uiHandler = new EditorUIHandler(this);
        this.multiSelectHandler = new MultiSelectActionsHandler(this);
        
        this.setupEventListeners();
        this.setupBlockManager();
        this._currentEditorType = null;  // 'block', 'signal', or 'platform'
        this._currentEditorId = null;    // The ID of the item being edited

        this.setupSecondarySidebar();
    }

    // === Secondary Sidebar Management ===

    setupSecondarySidebar() {
        return this.uiHandler.setupSecondarySidebar();
    }

    // === Collapsible Tool Categories ===
    
    setupCollapsibleCategories() {
        return this.uiHandler.setupCollapsibleCategories();
    }

    setupCollapsibleSidebarSections() {
        return this.uiHandler.setupCollapsibleSidebarSections();
    }
    
    toggleCategory(category) {
        return this.uiHandler.toggleCategory(category);
    }
    
    expandCategoryForTool(toolName) {
        return this.uiHandler.expandCategoryForTool(toolName);
    }
    
    saveCategoryStates() {
        return this.uiHandler.saveCategoryStates();
    }
    
    loadCategoryStates() {
        return this.uiHandler.loadCategoryStates();
    }

    showSecondarySidebar(title, content) {
        // Any editor opening clears a stale block-direction overlay from a
        // previously-open block editor. The block editor re-pushes its own
        // overlay right after this call returns (see editor-blocks showEditor).
        if (this.renderer?.clearBlockDirectionOverlay) {
            this.renderer.clearBlockDirectionOverlay();
        }
        return this.uiHandler.showSecondarySidebar(title, content);
    }

    hideSecondarySidebar(options = {}) {
        return this.uiHandler.hideSecondarySidebar(options);
    }

    goBackSecondaryPanel() {
        return this.uiHandler.goBackSecondaryPanel();
    }

    goForwardSecondaryPanel() {
        return this.uiHandler.goForwardSecondaryPanel();
    }

    closeActiveSecondaryTab() {
        return this.uiHandler.closeActiveSecondaryTab();
    }

    async restoreSecondaryPanel(snapshot) {
        const type = snapshot?.editorType;
        const id = snapshot?.editorId;

        switch (type) {
            case 'block':
                if (!id) return false;
                await this.showBlockEditor(id);
                return true;
            case 'signal':
                if (!id) return false;
                await this.showSignalEditor(id);
                return true;
            case 'platform':
                if (!id) return false;
                await this.showPlatformEditor(id);
                return true;
            case 'switch':
                if (!id) return false;
                await this.showSwitchEditor(id);
                return true;
            case 'train-control':
                if (!id) return false;
                await this.showTrainControlPanel(id);
                return true;
            case 'trains-list':
                await this.showTrainsPanel();
                return true;
            case 'schedules-list':
                await this.showSchedulesPanel();
                return true;
            case 'paths-list':
                await this.showPathsPanel();
                return true;
            case 'path':
                if (!id) return false;
                await this.showPathEditor(id);
                return true;
            case 'path-setup':
                this.showRoutePrioritySelector();
                return true;
            case 'extend-path-setup':
                this.showExtendRoutePanel();
                return true;
            case 'station-group':
            case 'station-group-list':
                await this.showStationsPanel();
                return true;
            case 'routes-list':
                await this.showRoutesPanel();
                return true;
            case 'route-editor':
                if (!id) return false;
                await this.routeHandler.showRouteEditor(id);
                return true;
            case 'route-runs-list':
                await this.routeRunHandler.showRouteRunsPanel();
                return true;
            case 'route-run-editor':
                if (!id) return false;
                await this.routeRunHandler.showRouteRunEditor(parseInt(id, 10));
                return true;
            default:
                return false;
        }
    }

    getBreadcrumbForEntityView(entityView) {
        const type = entityView?.editorType || entityView?.type || null;
        const id = entityView?.editorId || entityView?.id || null;

        switch (type) {
            case 'block':
                return [
                    { label: 'Blocks', editorType: null, editorId: null },
                    { label: id || 'Block', editorType: 'block', editorId: id },
                ];
            case 'signal':
                return [
                    { label: 'Signals', editorType: null, editorId: null },
                    { label: id || 'Signal', editorType: 'signal', editorId: id },
                ];
            case 'platform':
                return [
                    { label: 'Platforms', editorType: null, editorId: null },
                    { label: id || 'Platform', editorType: 'platform', editorId: id },
                ];
            case 'switch':
                return [
                    { label: 'Switches', editorType: null, editorId: null },
                    { label: id || 'Switch', editorType: 'switch', editorId: id },
                ];
            case 'trains-list':
                return [{ label: 'Trains', editorType: 'trains-list', editorId: null }];
            case 'train-control':
                return [
                    { label: 'Trains', editorType: 'trains-list', editorId: null },
                    { label: id || 'Train', editorType: 'train-control', editorId: id },
                ];
            case 'schedules-list':
                return [{ label: 'Schedules', editorType: 'schedules-list', editorId: null }];
            case 'paths-list':
                return [{ label: 'Paths', editorType: 'paths-list', editorId: null }];
            case 'path':
                return [
                    { label: 'Paths', editorType: 'paths-list', editorId: null },
                    { label: id || 'Path', editorType: 'path', editorId: id },
                ];
            case 'path-setup':
                return [
                    { label: 'Paths', editorType: 'paths-list', editorId: null },
                    { label: 'Create Path', editorType: 'path-setup', editorId: null },
                ];
            case 'extend-path-setup':
                return [
                    { label: 'Paths', editorType: 'paths-list', editorId: null },
                    { label: 'Extend Path', editorType: 'extend-path-setup', editorId: id || null },
                ];
            case 'routes-list':
                return [{ label: 'Routes', editorType: 'routes-list', editorId: null }];
            case 'route-editor':
                return [
                    { label: 'Routes', editorType: 'routes-list', editorId: null },
                    { label: id || 'Route', editorType: 'route-editor', editorId: id },
                ];
            case 'route-runs-list':
                return [{ label: 'Route Runs', editorType: 'route-runs-list', editorId: null }];
            case 'route-run-editor':
                return [
                    { label: 'Route Runs', editorType: 'route-runs-list', editorId: null },
                    { label: id ? `#${id}` : 'Run', editorType: 'route-run-editor', editorId: id },
                ];
            case 'station-group-list':
            case 'station-group':
                return [{ label: 'Stations', editorType: 'station-group-list', editorId: null }];
            default:
                return [{ label: entityView?.title || 'Editor', editorType: type, editorId: id }];
        }
    }

    showFullJSONViewer(data, title = 'JSON Viewer') {
        return this.uiHandler.showFullJSONViewer(data, title);
    }

    syntaxHighlightJSON(jsonString) {
        return this.uiHandler.syntaxHighlightJSON(jsonString);
    }

    updateSecondaryEditorForTile(track) {
        return this.uiHandler.updateSecondaryEditorForTile(track);
    }

    async showSignalsEditorForTile(track) {
        return this.signalHandler.showSignalsEditorForTile(track);
    }

    /**
     * Validate that the currently open editor still refers to a valid item.
     * If the item was deleted, close the editor. Called after network refresh.
     */
    validateCurrentEditor() {
        if (!this._currentEditorType) {
            return;
        }

        const id = this._currentEditorId;
        let itemExists = false;

        switch (this._currentEditorType) {
            case 'block':
                if (!id) return;
                itemExists = this.cachedNetwork?.blocks && (id in this.cachedNetwork.blocks);
                break;
            case 'signal':
                if (!id) return;
                itemExists = this.cachedNetwork?.signals && (id in this.cachedNetwork.signals);
                break;
            case 'platform':
                if (!id) return;
                itemExists = Array.isArray(this.cachedNetwork?.platforms) &&
                             this.cachedNetwork.platforms.some(p => p.platform_id === id);
                break;
            case 'station-group':
                if (!id) return;
                itemExists = Array.isArray(this.cachedNetwork?.station_groups) &&
                             this.cachedNetwork.station_groups.some(s => s.station_id === id);
                break;
            case 'train':
            case 'train-control':
                if (!id) return;
                itemExists = Array.isArray(this.cachedNetwork?.trains) &&
                             this.cachedNetwork.trains.some(t => t.id === id);
                break;
            case 'switch':
                if (!id) return;
                itemExists = this.cachedNetwork?.switches && (id in this.cachedNetwork.switches);
                break;
            case 'trains-list':
            case 'schedules-list':
            case 'path-setup':
            case 'extend-path-setup':
            case 'paths-list':
            case 'routes-list':
            case 'route-runs-list':
                // List and transient panels don't need IDs - always valid
                itemExists = true;
                break;
            case 'path':
            case 'route-editor':
            case 'route-run-editor':
                // These editor types need IDs but we'll mark as always valid for now
                // (proper validation would require checking if path/route still exists)
                if (!id) return;
                itemExists = true;
                break;
        }

        if (!itemExists) {
            // Item was deleted, close the editor
            this.hideSecondarySidebar();
        }
    }

    /**
     * Refresh the secondary editor if it matches the given type and id.
     * Used after API calls that modify specific items.
     * @param {string} type - 'block', 'signal', or 'platform'
     * @param {string} id - The ID of the item that was modified
     */
    refreshEditorIfNeeded(type, id) {
        if (this._currentEditorType !== type || this._currentEditorId !== id) {
            return;
        }

        // Don't refresh if user is actively editing an input field
        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }

        switch (type) {
            case 'block':
                this.showBlockEditor(id);
                break;
            case 'signal':
                this.showSignalEditor(id);
                break;
            case 'platform':
                this.showPlatformEditor(id);
                break;
        }
    }

    /**
     * Force refresh the currently open secondary editor.
     * Use sparingly - prefer refreshEditorIfNeeded for targeted updates.
     * Skips refresh if an input has focus to avoid interrupting user input.
     * During simulation, train-control updates are handled by app.js to avoid race conditions.
     */
    refreshCurrentEditor() {
        if (!this._currentEditorType || !this._currentEditorId) {
            return;
        }

        const activeElement = document.activeElement;
        if (activeElement && (activeElement.tagName === 'INPUT' || activeElement.tagName === 'TEXTAREA' || activeElement.tagName === 'SELECT')) {
            return;
        }

        // Phase 3-4: All panels now use subscriptions - no need to refresh
        if (this._currentEditorType === 'train-control') {
            if (window.app?.isSimulationRunning) return;
            this.trainHandler.updateTrainControlPanelValues(this._currentEditorId);
            return;
        }
        
        // All other panels use subscriptions
        if (this._currentEditorType === 'signal' || this._currentEditorType === 'switch' || 
            this._currentEditorType === 'block' || this._currentEditorType === 'platform' || this._currentEditorType === 'station-group') {
            return;
        }
    }

    async showBlockEditor(blockId) {
        return this.blockHandler.showEditor(blockId);
    }

    showPlatformEditor(platformId) {
        return this.platformHandler.showEditor(platformId);
    }

    async showSwitchEditor(switchId) {
        return this.switchHandler.showEditor(switchId);
    }

    async showStationsPanel() {
        return this.stationGroupHandler.showPanel();
    }

    async initTrainEditorHandlers(trainId) {
        return this.trainHandler.initTrainControlHandlers(trainId);
    }

    showAddTrainDialog() {
        return this.trainHandler.showAddTrainDialog();
    }

    async showTrainsPanel() {
        return this.trainHandler.showTrainsPanel();
    }

    async showSchedulesPanel() {
        return this.trainHandler.showSchedulesPanel();
    }

    async showTrainControlPanel(trainId) {
        return this.trainHandler.showTrainControlPanel(trainId);
    }

    // Tools that activate a multi-step EditorMode. The factory builds a fresh
    // EditorMode object on each entry.
    _modeFactoriesByTool() {
        return {
            'path':        () => this.pathHandler.buildRouteToolMode(),
            'extend-path': () => this.pathHandler.buildExtendRouteMode(),
            'cancel-path': () => this.pathHandler.buildCancelRouteMode(),
            'box-select':  () => this._buildBoxSelectMode(),
            'lasso-select':() => this._buildLassoSelectMode(),
            'paint-select':() => this._buildPaintSelectMode(),
        };
    }

    _buildBoxSelectMode() {
        const resetState = () => {
            this.boxSelectState.selectedKeys = new Set();
            this.boxSelectState.dragScreenStart = null;
            this.boxSelectState.dragScreenEnd = null;
            this.boxSelectState.isDragging = false;
            this.boxSelectState.shiftRemoving = false;
            this._applyBoxSelection(new Set());
            this.renderer.setBoxSelectRect(null);
        };
        return {
            name: 'Box select',
            toolName: 'box-select',
            cancelMessage: 'Box select cancelled',
            onEnter: () => {
                resetState();
                const status = document.getElementById('status-state');
                if (status) status.textContent = 'Box: drag to add, Shift+drag to remove, Space clears, Delete removes tracks';
            },
            // Mouse interactions are handled by the mousedown/move/up branches
            // below; the click hook is a no-op so clicks don't fall through to
            // selectElement on stray taps.
            onClick: () => true,
            onExit: () => {
                resetState();
                const status = document.getElementById('status-state');
                if (status) status.textContent = 'Ready';
                this.renderer.requestRedraw();
            },
        };
    }

    // Even-odd ray cast: returns true when (px, py) is inside the polygon
    // defined by `polygon` (array of {x, y} screen points). The polygon is
    // treated as closed (last vertex connects back to the first).
    _pointInPolygon(px, py, polygon) {
        let inside = false;
        for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
            const xi = polygon[i].x, yi = polygon[i].y;
            const xj = polygon[j].x, yj = polygon[j].y;
            const intersects = ((yi > py) !== (yj > py)) &&
                (px < (xj - xi) * (py - yi) / ((yj - yi) || 1e-9) + xi);
            if (intersects) inside = !inside;
        }
        return inside;
    }

    _buildLassoSelectMode() {
        const resetState = () => {
            this.boxSelectState.selectedKeys = new Set();
            this.lassoSelectState.screenPoints = [];
            this.lassoSelectState.isDrawing = false;
            this.lassoSelectState.shiftRemoving = false;
            this._applyBoxSelection(new Set());
            this.renderer.setLassoPath(null);
        };
        return {
            name: 'Lasso select',
            toolName: 'lasso-select',
            cancelMessage: 'Lasso select cancelled',
            onEnter: () => {
                resetState();
                const status = document.getElementById('status-state');
                if (status) status.textContent = 'Lasso: draw a shape to add, Shift to remove, Space clears, Delete removes tracks';
            },
            onClick: () => true,
            onExit: () => {
                resetState();
                const status = document.getElementById('status-state');
                if (status) status.textContent = 'Ready';
                this.renderer.requestRedraw();
            },
        };
    }

    // Brush-select: click adds the tile under the cursor; mouse-down + drag
    // paints every tile the cursor passes through into the same selection set
    // box and lasso write to. Shift inverts add → remove (locked at mousedown
    // so a release mid-stroke doesn't flip the operation).
    _buildPaintSelectMode() {
        const resetState = () => {
            this.boxSelectState.selectedKeys = new Set();
            this.paintSelectState.isPainting = false;
            this.paintSelectState.shiftRemoving = false;
            this.paintSelectState.lastKey = null;
            this._applyBoxSelection(new Set());
        };
        return {
            name: 'Brush select',
            toolName: 'paint-select',
            cancelMessage: 'Brush select cancelled',
            onEnter: () => {
                resetState();
                const status = document.getElementById('status-state');
                if (status) status.textContent = 'Brush: click or drag to add, Shift to remove, Space clears, Delete removes tracks';
            },
            onClick: () => true,
            onExit: () => {
                resetState();
                const status = document.getElementById('status-state');
                if (status) status.textContent = 'Ready';
                this.renderer.requestRedraw();
            },
        };
    }

    // Apply a single tile to the brush-select set. Returns true if selectedKeys
    // actually changed (so the caller can skip a redraw on a no-op).
    _applyPaintTile(gridPos, removing) {
        const tracks = this.cachedNetwork?.tracks || [];
        const track = tracks.find(t => t.x === gridPos.x && t.y === gridPos.y);
        if (!track) return false;
        const key = `${gridPos.x},${gridPos.y}`;
        if (removing) {
            if (!this.boxSelectState.selectedKeys.has(key)) return false;
            this.boxSelectState.selectedKeys.delete(key);
        } else {
            if (this.boxSelectState.selectedKeys.has(key)) return false;
            this.boxSelectState.selectedKeys.add(key);
        }
        return true;
    }

    // Tools that just open a panel or run a one-shot action. They never change
    // selectedTool — clicking one of these buttons re-activates the prior tool
    // button and leaves the editor in its previous state.
    _panelOnlyTools() {
        return {
            'paths-panel':     () => this.showPathsPanel(),
            'add-train':       () => this.showAddTrainDialog(),
            'train-panel':     () => this.showTrainsPanel(),
            'schedules-panel': () => this.showSchedulesPanel(),
            'routes-panel':    () => this.showRoutesPanel(),
            'route-runs-panel':() => this.routeRunHandler.showRouteRunsPanel(),
            'new-route':       () => this.routeHandler.createNewRoute(),
            'stations-panel':  () => this.showStationsPanel(),
        };
    }

    /**
     * Zoom toward a target absolute scale, keeping the world point under
     * (screenX, screenY) anchored under that same screen position. Shared
     * path used by the wheel handler and the +/- toolbar buttons.
     *
     * Math: gridToScreenX(g) = centerX + panX + (gx+gy+1) * tileSize/2 with
     * tileSize = gridSize * scale. Holding the world point at (sx, sy)
     * means re-solving panX, panY after scale changes so the same world
     * point lands at the same screen coordinate.
     */
    _zoomAt(screenX, screenY, targetScale) {
        // Math lives on Camera (frontend/src/lib/camera.ts) — see Camera.zoomAt for
        // the cursor-anchor derivation. Tests in frontend/src/lib/camera.test.ts.
        this.renderer.camera.zoomAt(screenX, screenY, targetScale);
        this.renderer.requestRedraw({ reason: 'zoom' });
        const zoomLabel = document.getElementById('status-zoom');
        if (zoomLabel) zoomLabel.textContent = `${Math.round(this.renderer.camera.scale * 100)}%`;
    }

    /**
     * Reset zoom to 1.0 and re-fit all tracks if available.
     */
    _resetZoom() {
        this.renderer.camera.scale = 1.0;
        if (this.cachedNetwork?.tracks?.length) {
            this.renderer.camera.centerOnContent(this.cachedNetwork.tracks);
        }
        this.renderer.requestRedraw({ reason: 'reset-zoom' });
        const zoomLabel = document.getElementById('status-zoom');
        if (zoomLabel) zoomLabel.textContent = '100%';
    }

    setupEventListeners() {
        const modeFactories = this._modeFactoriesByTool();
        const panelOnlyTools = this._panelOnlyTools();

        // Tool selection
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const newTool = e.currentTarget.dataset.tool;
                const oldTool = this.selectedTool;

                // Panel-only tools: run the action, leave selectedTool alone.
                // The .active class flip the browser does on click is reverted
                // explicitly here.
                if (newTool in panelOnlyTools) {
                    panelOnlyTools[newTool]();
                    return;
                }

                document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
                e.currentTarget.classList.add('active');

                // Exit any active mode before switching tools. enterMode below
                // also auto-exits, but we need this for tools that don't map
                // to a mode (block, station, signal, etc.).
                if (this.activeMode) {
                    await this.exitMode();
                }

                // Auto-toggle block debug when entering/leaving block tools
                const isBlockTool = ['block', 'extend-block'].includes(newTool);
                const wasBlockTool = ['block', 'extend-block'].includes(oldTool);
                if (isBlockTool !== wasBlockTool) {
                    this.renderer.showBlockColorsOnly = isBlockTool;
                    window.app.refreshNetwork();
                }

                if (newTool in modeFactories) {
                    await this.enterMode(modeFactories[newTool]());
                }

                this.selectedTool = newTool;
                this.previewPath = null;

                // Auto-expand the category containing the selected tool
                this.expandCategoryForTool(newTool);
            });
        });

        // Collapsible tool categories
        this.setupCollapsibleCategories();
        this.setupCollapsibleSidebarSections();

        const canvas = this.renderer.canvas;

        // Mouse down - start drag or pan
        canvas.addEventListener('mousedown', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);

            // Middle mouse, select tool, or route-related tools = pan
            const pathTools = ['path', 'extend-path', 'cancel-path'];
            if (e.button === 1 || (e.button === 0 && this.selectedTool === 'select') ||
                (e.button === 0 && pathTools.includes(this.selectedTool))) {
                this.isPanning = true;
                this.panStart = { x, y };
                this.hasPanned = false; // Track if actual panning occurred
                // Initialize last mouse position for delta calculation
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            }
            // Left mouse + path-based tools = start drag
            else if (e.button === 0 && ['track', 'block', 'eraser', 'extend-block', 'extend-platform', 'remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool)) {
                this.isDraggingBuild = true;
                this.dragStart = gridPos;
                this.dragEnd = gridPos;
                // Clear cached path for new drag operation
                this.lastValidPreviewPath = null;
                this.lastValidPreviewTracks = null;
            }
            // Left mouse + box-select = start screen-space rubber-band rectangle
            else if (e.button === 0 && this.selectedTool === 'box-select') {
                this.boxSelectState.isDragging = true;
                this.boxSelectState.shiftRemoving = e.shiftKey;
                this.boxSelectState.dragScreenStart = { x, y };
                this.boxSelectState.dragScreenEnd = { x, y };
                this.renderer.setBoxSelectRect({ startX: x, startY: y, endX: x, endY: y });
            }
            // Left mouse + lasso-select = start free-form polyline
            else if (e.button === 0 && this.selectedTool === 'lasso-select') {
                this.lassoSelectState.isDrawing = true;
                this.lassoSelectState.shiftRemoving = e.shiftKey;
                this.lassoSelectState.screenPoints = [{ x, y }];
                this.renderer.setLassoPath(this.lassoSelectState.screenPoints);
            }
            // Left mouse + paint-select = start tile-by-tile brush stroke,
            // applying the start tile immediately so a plain click also adds.
            else if (e.button === 0 && this.selectedTool === 'paint-select') {
                this.paintSelectState.isPainting = true;
                this.paintSelectState.shiftRemoving = e.shiftKey;
                this.paintSelectState.lastKey = null;
                const removing = this.paintSelectState.shiftRemoving;
                if (this._applyPaintTile(gridPos, removing)) {
                    this.paintSelectState.lastKey = `${gridPos.x},${gridPos.y}`;
                    this._applyBoxSelection(this.boxSelectState.selectedKeys);
                    this.renderer.requestRedraw();
                }
            }
            // Left click with signal/platform/tunnel = single click placement
            else if (e.button === 0 && ['signal', 'platform', 'tunnel'].includes(this.selectedTool)) {
                this.handleSingleClickPlace(gridPos);
            }
        });

        // Mouse move - update drag preview or pan
        canvas.addEventListener('mousemove', (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);

            document.getElementById('status-coords').textContent = `${gridPos.x}, ${gridPos.y}`;

            if (this.isPanning) {
                const dx = e.clientX - this.lastMouseX;
                const dy = e.clientY - this.lastMouseY;
                
                // If mouse moved more than a small threshold, mark as actual panning
                if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
                    this.hasPanned = true;
                }
                
                // Pan-drag fires per mousemove; rAF coalescing keeps this to
                // one paint per frame.
                this.renderer.camera.panBy(dx, dy);
                this.renderer.requestRedraw({ reason: 'pan' });
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            }
            else if (this.isDraggingBuild && this.dragStart) {
                this.dragEnd = gridPos;
                this.updatePreviewPath();
            }
            else if (this.boxSelectState.isDragging) {
                this.boxSelectState.dragScreenEnd = { x, y };
                this.renderer.setBoxSelectRect({
                    startX: this.boxSelectState.dragScreenStart.x,
                    startY: this.boxSelectState.dragScreenStart.y,
                    endX: x,
                    endY: y,
                });
                this.renderer.requestRedraw();
            }
            else if (this.lassoSelectState.isDrawing) {
                // Append a new vertex only if it moved at least 2px from the
                // previous one — keeps the polygon manageable on slow drags.
                const pts = this.lassoSelectState.screenPoints;
                const last = pts[pts.length - 1];
                if (!last || Math.abs(last.x - x) > 2 || Math.abs(last.y - y) > 2) {
                    pts.push({ x, y });
                    this.renderer.setLassoPath(pts);
                    this.renderer.requestRedraw();
                }
            }
            else if (this.paintSelectState.isPainting) {
                const key = `${gridPos.x},${gridPos.y}`;
                if (key !== this.paintSelectState.lastKey) {
                    const removing = this.paintSelectState.shiftRemoving;
                    if (this._applyPaintTile(gridPos, removing)) {
                        this._applyBoxSelection(this.boxSelectState.selectedKeys);
                        this.renderer.requestRedraw();
                    }
                    this.paintSelectState.lastKey = key;
                }
            }
            // Hover preview for signal/platform tools
            else if (['signal', 'platform'].includes(this.selectedTool)) {
                this.updateHoverPreview(gridPos);
            }
        });

        // Mouse up - commit drag build. Listen on document (not canvas) so a
        // release outside the canvas (e.g. over a side panel) still resets
        // the drag/pan state; otherwise isPanning/isDraggingBuild stays true
        // and the layout keeps panning when the cursor returns.
        document.addEventListener('mouseup', async (e) => {
            const onCanvas = (e.target === canvas);

            if (this.isPanning) {
                this.isPanning = false;
            }
            else if (this.boxSelectState.isDragging) {
                this.boxSelectState.isDragging = false;
                const start = this.boxSelectState.dragScreenStart;
                const end = this.boxSelectState.dragScreenEnd;
                const removing = this.boxSelectState.shiftRemoving;
                this.boxSelectState.dragScreenStart = null;
                this.boxSelectState.dragScreenEnd = null;
                this.boxSelectState.shiftRemoving = false;
                this.renderer.setBoxSelectRect(null);

                if (onCanvas && start && end) {
                    const tracks = this.cachedNetwork?.tracks || [];
                    // No-movement click (within 2px slop): add (or remove if Shift)
                    // the track at the click. Plain click always adds; redoing
                    // an add on an already-selected tile is a no-op.
                    const isClick = Math.abs(start.x - end.x) <= 2 && Math.abs(start.y - end.y) <= 2;
                    if (isClick) {
                        const gp = this.renderer.screenToGrid(start.x, start.y);
                        const track = tracks.find(t => t.x === gp.x && t.y === gp.y);
                        if (track) {
                            const key = `${gp.x},${gp.y}`;
                            if (removing) this.boxSelectState.selectedKeys.delete(key);
                            else this.boxSelectState.selectedKeys.add(key);
                        }
                    } else {
                        // Drag: add (or remove if Shift) any track whose screen
                        // center falls inside the rect
                        const minX = Math.min(start.x, end.x);
                        const maxX = Math.max(start.x, end.x);
                        const minY = Math.min(start.y, end.y);
                        const maxY = Math.max(start.y, end.y);
                        for (const t of tracks) {
                            const sx = this.renderer.gridToScreenX(t.x, t.y);
                            const sy = this.renderer.gridToScreenY(t.x, t.y);
                            if (sx >= minX && sx <= maxX && sy >= minY && sy <= maxY) {
                                const key = `${t.x},${t.y}`;
                                if (removing) this.boxSelectState.selectedKeys.delete(key);
                                else this.boxSelectState.selectedKeys.add(key);
                            }
                        }
                    }
                }

                this._applyBoxSelection(this.boxSelectState.selectedKeys);
                this.renderer.requestRedraw();
            }
            else if (this.lassoSelectState.isDrawing) {
                this.lassoSelectState.isDrawing = false;
                const pts = this.lassoSelectState.screenPoints;
                const removing = this.lassoSelectState.shiftRemoving;
                this.lassoSelectState.screenPoints = [];
                this.lassoSelectState.shiftRemoving = false;
                this.renderer.setLassoPath(null);

                if (onCanvas && pts.length >= 3) {
                    const tracks = this.cachedNetwork?.tracks || [];
                    for (const t of tracks) {
                        const sx = this.renderer.gridToScreenX(t.x, t.y);
                        const sy = this.renderer.gridToScreenY(t.x, t.y);
                        if (this._pointInPolygon(sx, sy, pts)) {
                            const key = `${t.x},${t.y}`;
                            if (removing) this.boxSelectState.selectedKeys.delete(key);
                            else this.boxSelectState.selectedKeys.add(key);
                        }
                    }
                }

                this._applyBoxSelection(this.boxSelectState.selectedKeys);
                this.renderer.requestRedraw();
            }
            else if (this.paintSelectState.isPainting) {
                // Tiles were applied incrementally during mousedown/mousemove;
                // mouseup just ends the stroke and clears per-stroke state.
                this.paintSelectState.isPainting = false;
                this.paintSelectState.shiftRemoving = false;
                this.paintSelectState.lastKey = null;
            }
            else if (this.isDraggingBuild && this.dragStart && this.dragEnd) {
                // Stop dragging state immediately to prevent updates during async operations
                this.isDraggingBuild = false;

                // Only commit if the release happened on the canvas; an
                // off-canvas release is treated as a cancel.
                if (onCanvas) {
                    // Check if this is a single click (no drag movement) for eraser
                    const isSingleClick = this.dragStart.x === this.dragEnd.x && this.dragStart.y === this.dragEnd.y;

                    if (this.selectedTool === 'eraser' && isSingleClick) {
                        // Single click eraser: two-stage deletion
                        await this.handleErase(this.dragStart);
                    } else if (['remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool) && isSingleClick) {
                        // Single click removal tools
                        await this.handleRemovalClick(this.dragStart);
                    } else if (['eraser', 'block', 'track', 'extend-block', 'extend-platform', 'remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool)) {
                        // Drag operation: commit the build/erase/block/removal path
                        await this.commitDragBuild();
                    }
                }

                this.dragStart = null;
                this.dragEnd = null;
                this.forcedStartSegment = null;
                this.forcedEndSegment = null;
                const hadPreview = this.previewPath !== null;
                this.previewPath = null;

                // Cancelled with a stale preview still on screen — redraw to clear it.
                if (!onCanvas && hadPreview && this.cachedNetwork) {
                    this.renderer.requestRedraw();
                }
            }
        });

        // Right click - cancel active drag, or context actions (rotate signal, toggle switch)
        canvas.addEventListener('contextmenu', async (e) => {
            e.preventDefault();

            // Cancel an in-progress drag-build without committing anything.
            if (this.isDraggingBuild) {
                this.isDraggingBuild = false;
                this.dragStart = null;
                this.dragEnd = null;
                this.forcedStartSegment = null;
                this.forcedEndSegment = null;
                this.previewPath = null;
                this.previewTracks = null;
                this.lastValidPreviewPath = null;
                this.lastValidPreviewTracks = null;
                if (this.cachedNetwork) {
                    this.renderer.requestRedraw();
                }
                return;
            }

            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);

            await this.handleRightClick(gridPos);
        });

        // Zoom with mouse-centered zooming and pan compensation.
        // Body is in _zoomAt so the +/- and 1:1 buttons share the same path.
        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const mouseScreenX = e.clientX - rect.left;
            const mouseScreenY = e.clientY - rect.top;
            const delta = e.deltaY < 0 ? 0.1 : -0.1;
            this._zoomAt(mouseScreenX, mouseScreenY, this.renderer.scale + delta);
        });

        // Click to select element for properties panel
        canvas.addEventListener('click', async (e) => {
            // Don't handle click if user was panning
            if (this.hasPanned) {
                this.hasPanned = false; // Reset for next interaction
                return;
            }

            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;

            // Station labels float above the grid — give them a chance to
            // capture the click before any tile/tool logic runs.
            const stationId = this.renderer.stationHandler?.stationAt?.(x, y);
            if (stationId) {
                await this.stationGroupHandler.showStationDetail(stationId);
                return;
            }

            const gridPos = this.renderer.screenToGrid(x, y);

            // Any active EditorMode gets the first shot at the click.
            // Returning true consumes the click; false lets it fall through.
            if (this.activeMode) {
                const handled = await this.activeMode.onClick({
                    gridPos,
                    screenX: e.clientX,
                    screenY: e.clientY,
                    event: e
                });
                if (handled) return;
            }

            // Only handle selection with select tool
            if (this.selectedTool !== 'select') return;
            
            this.selectElement(gridPos);
        });

        // Shift key toggles avoid-connect mode for the track pathfinder.
        // While held, dragging a new track over existing tiles prefers
        // crossovers (non-connecting segments) over switches.
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Shift' && !this.avoidConnectMode) {
                this.avoidConnectMode = true;
                if (this.isDraggingBuild) {
                    this.updatePreviewPath();
                }
            }
        });

        document.addEventListener('keyup', (e) => {
            if (e.key === 'Shift' && this.avoidConnectMode) {
                this.avoidConnectMode = false;
                if (this.isDraggingBuild) {
                    this.updatePreviewPath();
                }
            }
        });

        // R / T cycle the forced segment for the start (R) and end (T) tile
        // of an active track drag. Cycle: off -> NS -> EW -> NW -> SW -> SE
        // -> NE -> off. The override holds for the rest of the current drag
        // and is cleared when the drag commits, is right-click-cancelled, or
        // is released off-canvas.
        const SEGMENT_CYCLE = ['NS', 'EW', 'NW', 'SW', 'SE', 'NE'];
        const cycleSegment = (current) => {
            if (current === null) return SEGMENT_CYCLE[0];
            const i = SEGMENT_CYCLE.indexOf(current);
            return i === SEGMENT_CYCLE.length - 1 ? null : SEGMENT_CYCLE[i + 1];
        };
        document.addEventListener('keydown', (e) => {
            if (this.selectedTool !== 'track') return;
            const active = document.activeElement;
            if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT')) return;
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            const key = e.key.toLowerCase();
            if (key === 'r') {
                this.forcedStartSegment = cycleSegment(this.forcedStartSegment);
                window.showToast?.(`Start segment: ${this.forcedStartSegment || 'auto'}`, 'info', 800);
                if (this.isDraggingBuild) this.updatePreviewPath();
            } else if (key === 't') {
                this.forcedEndSegment = cycleSegment(this.forcedEndSegment);
                window.showToast?.(`End segment: ${this.forcedEndSegment || 'auto'}`, 'info', 800);
                if (this.isDraggingBuild) this.updatePreviewPath();
            }
        });

        // Inspector navigation shortcuts
        document.addEventListener('keydown', async (e) => {
            const activeElement = document.activeElement;
            const inInput = activeElement && (
                activeElement.tagName === 'INPUT' ||
                activeElement.tagName === 'TEXTAREA' ||
                activeElement.tagName === 'SELECT'
            );

            if (!inInput && e.altKey && !e.metaKey && !e.ctrlKey && e.key === 'ArrowLeft') {
                e.preventDefault();
                await this.goBackSecondaryPanel();
                return;
            }

            if (!inInput && e.altKey && !e.metaKey && !e.ctrlKey && e.key === 'ArrowRight') {
                e.preventDefault();
                await this.goForwardSecondaryPanel();
                return;
            }

            if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === 'w') {
                if (inInput) {
                    return;
                }
                e.preventDefault();
                await this.closeActiveSecondaryTab();
            }
        });

        // ESC key - exit extend mode or return to select tool
        document.addEventListener('keydown', async (e) => {
            if (e.key === 'Escape') {
                // Check if we're in a modal or input field
                const inModal = document.querySelector('.block-modal-overlay, .modal-overlay') !== null;
                const inInput = document.activeElement.tagName === 'INPUT' ||
                               document.activeElement.tagName === 'TEXTAREA' ||
                               document.activeElement.tagName === 'SELECT';

                if (inModal || inInput) {
                    return; // Let modal/input handlers deal with it
                }

                // Box/lasso/brush select: first Esc clears any selection;
                // second Esc (empty selection) falls through to exit the mode.
                const isMarkSelectTool = this.selectedTool === 'box-select'
                    || this.selectedTool === 'lasso-select'
                    || this.selectedTool === 'paint-select';
                if (isMarkSelectTool && this.boxSelectState.selectedKeys.size > 0) {
                    this.boxSelectState.selectedKeys = new Set();
                    this._applyBoxSelection(new Set());
                    this.renderer.requestRedraw();
                    return;
                }

                // Exit the active EditorMode if any, then revert to select.
                if (this.activeMode) {
                    const m = this.activeMode;
                    await this.exitMode();
                    if (this.selectedTool !== 'select') this._setSelectedTool('select');
                    window.showToast(m.cancelMessage ?? `${m.name} cancelled`, 'info', 3000);
                    return;
                }

                // Otherwise revert any non-mode tool (block, station, etc.)
                // back to select.
                if (this.selectedTool !== 'select') {
                    this._setSelectedTool('select');
                }
            }
        });

        // Delete / Backspace key - remove the box/lasso/brush-select selection.
        document.addEventListener('keydown', async (e) => {
            if (e.key !== 'Delete' && e.key !== 'Backspace') return;
            if (this.selectedTool !== 'box-select'
                && this.selectedTool !== 'lasso-select'
                && this.selectedTool !== 'paint-select') return;
            const inInput = document.activeElement && (
                document.activeElement.tagName === 'INPUT' ||
                document.activeElement.tagName === 'TEXTAREA' ||
                document.activeElement.tagName === 'SELECT'
            );
            const inModal = document.querySelector('.block-modal-overlay, .modal-overlay') !== null;
            if (inInput || inModal) return;
            e.preventDefault();

            if (this.boxSelectState.selectedKeys.size === 0) {
                window.showToast('Nothing selected', 'info', 2000);
                return;
            }

            const positions = [...this.boxSelectState.selectedKeys].map(k => {
                const [xs, ys] = k.split(',');
                return [Number(xs), Number(ys)];
            });

            try {
                const result = await this._bulkDeleteWithConfirm(positions);
                if (result) {
                    this.boxSelectState.selectedKeys = new Set();
                    this._applyBoxSelection(new Set());
                    await this.refreshAndSelect(null);
                }
            } catch (err) {
                console.error('[Editor] Box-select delete failed:', err);
                window.showToast('Failed to delete: ' + err.message, 'error', 5000);
            }
        });

        // Space key - clear the current selection without exiting the tool.
        // Distinct from Esc, which clears on first press and exits the mode on
        // the second.
        document.addEventListener('keydown', (e) => {
            if (e.key !== ' ' && e.code !== 'Space') return;
            if (this.selectedTool !== 'box-select'
                && this.selectedTool !== 'lasso-select'
                && this.selectedTool !== 'paint-select') return;
            const inInput = document.activeElement && (
                document.activeElement.tagName === 'INPUT' ||
                document.activeElement.tagName === 'TEXTAREA' ||
                document.activeElement.tagName === 'SELECT'
            );
            const inModal = document.querySelector('.block-modal-overlay, .modal-overlay') !== null;
            if (inInput || inModal) return;
            e.preventDefault();

            if (this.boxSelectState.selectedKeys.size === 0) return;
            this.boxSelectState.selectedKeys = new Set();
            this._applyBoxSelection(new Set());
            this.renderer.requestRedraw();
        });
    }

    // === ModeManager ===
    // See the EditorMode contract documented on the activeMode field in the
    // constructor. Use these helpers to enter/exit any drawing or picking
    // mode on the layout.

    async enterMode(mode) {
        if (this.activeMode) await this.exitMode();
        this._setSelectedTool(mode.toolName ?? 'select');
        this.activeMode = mode;
        if (mode.onEnter) {
            try { await mode.onEnter(); } catch (e) { console.error('[ModeManager] onEnter threw', e); }
        }
    }

    // Exits the active mode and runs its onExit. Does NOT change selectedTool —
    // the caller decides what tool to leave the editor in (ESC reverts to
    // 'select'; the tool-button handler sets the new tool itself).
    async exitMode() {
        if (!this.activeMode) return;
        const m = this.activeMode;
        this.activeMode = null;     // clear first so onExit can re-enter another mode
        try { await m.onExit(); } catch (e) { console.error('[ModeManager] onExit threw', e); }
    }

    // Pure DOM + state update. Does NOT call enter/exitMode (no recursion).
    _setSelectedTool(toolName) {
        this.selectedTool = toolName;
        document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
        document.querySelector(`.tool-btn[data-tool="${toolName}"]`)?.classList.add('active');
    }

    // Wrapper around renderer.setBoxSelection that also syncs the sidebar
    // multi-select panel. Every selection mutation site routes through this so
    // the panel summary stays in lock-step with the visual selection. Read the
    // source of truth from boxSelectState.selectedKeys (some sites pass a fresh
    // empty Set to the renderer while having just reassigned selectedKeys).
    _applyBoxSelection(keys) {
        this.renderer.setBoxSelection(keys);
        this.multiSelectHandler?.update(this.boxSelectState.selectedKeys);
    }

    // Clears box/lasso/brush selection from the sidebar Clear button or any
    // other programmatic caller. Keeps the tool active.
    clearMultiSelection() {
        this.boxSelectState.selectedKeys = new Set();
        this._applyBoxSelection(new Set());
        this.renderer.requestRedraw();
    }

    setupBlockManager() {
        this.blockHandler.setup();
    }

    async refreshBlocksList() {
        return this.blockHandler.refreshList();
    }

    async deleteBlock(blockId) {
        return this.blockHandler.delete(blockId);
    }

    getBlockColor(blockId) {
        return this.blockHandler.getColor(blockId);
    }

    showBlockModal(options) {
        return this.blockHandler.showModal(options);
    }

    showPlatformModal(options) {
        return this.platformHandler.showModal(options);
    }

    renderSignalList(signals) {
        return this.signalHandler.renderSignalList(signals);
    }

    async invalidateSignalsInPath(signalsInPath) {
        return this.signalHandler.invalidateSignalsInPath(signalsInPath);
    }

    // === End Block Manager Methods ===

    updatePreviewPath() {
        if (!this.dragStart || !this.dragEnd) return;

        // All drag tools share the construction pathfinder so the preview
        // shape is consistent across track / eraser / block / extend / remove
        // tools. The track tool layers its R/T/Shift overrides on top; other
        // tools accept the default path. Commit-time logic on non-track tools
        // already short-circuits cells without an existing track.
        const dragToolSet = [
            'track', 'block', 'extend-block', 'extend-platform',
            'eraser', 'remove-platform-tile', 'remove-block-tile'
        ];
        if (dragToolSet.includes(this.selectedTool)) {
            const existingTracks = this.cachedNetwork ? this.cachedNetwork.tracks : [];
            const isTrackTool = this.selectedTool === 'track';
            const foundPath = this.pathfinder.findPath(
                this.dragStart.x, this.dragStart.y,
                this.dragEnd.x, this.dragEnd.y,
                existingTracks,
                isTrackTool ? {
                    avoidConnect: this.avoidConnectMode,
                    forcedStartSegment: this.forcedStartSegment,
                    forcedEndSegment: this.forcedEndSegment,
                } : {}
            );

            if (foundPath) {
                this.previewPath = foundPath;
                this.previewTracks = isTrackTool
                    ? pathToTracks(this.previewPath, this.cachedNetwork, {
                        avoidConnect: this.avoidConnectMode,
                        forcedStartSegment: this.forcedStartSegment,
                        forcedEndSegment: this.forcedEndSegment,
                    })
                    : null;
                this.lastValidPreviewPath = this.previewPath;
                this.lastValidPreviewTracks = this.previewTracks;
            } else {
                this.previewPath = this.lastValidPreviewPath;
                this.previewTracks = isTrackTool ? this.lastValidPreviewTracks : null;
            }
        }
        else {
            // Single placement for other tools
            this.previewPath = [this.dragEnd];
            this.previewTracks = null;
        }

        // Redraw with preview
        if (this.cachedNetwork) {
            this.renderer.requestRedraw();
            // Draw actual track geometry preview if available
            if (this.previewTracks && this.previewTracks.length > 0) {
                this.renderer.trackHandler.drawPreviewTracks(this.previewTracks);
            }
            // Draw eraser and removal tools preview in red. Filter the
            // construction-pathfinder path to cells that actually have tracks
            // so the red diamonds don't leak into empty grid.
            else if (['eraser', 'remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool) && this.previewPath) {
                const tracks = this.cachedNetwork?.tracks || [];
                const trackKeys = new Set(tracks.map(t => `${t.x},${t.y}`));
                const filtered = this.previewPath.filter(p => trackKeys.has(`${p.x},${p.y}`));
                this.renderer.trackHandler.drawEraserPreviewPath(filtered);
            }
            // Draw block preview in purple (for block, extend-block, and extend-platform tools)
            else if (['block', 'extend-block', 'extend-platform'].includes(this.selectedTool) && this.previewPath) {
                this.renderer.blockHandler.drawBlockPreviewPath(this.previewPath);
            }
            else if (this.previewPath) {
                this.renderer.trackHandler.drawPreviewPath(this.previewPath);
            }
        }
    }

    async commitDragBuild() {
        if (!this.previewPath || this.previewPath.length === 0) {
            return;
        }

        try {
            if (this.selectedTool === 'track') {
                const tracks = pathToTracks(this.previewPath, this.cachedNetwork, {
                    avoidConnect: this.avoidConnectMode,
                    forcedStartSegment: this.forcedStartSegment,
                    forcedEndSegment: this.forcedEndSegment,
                });
                await this.api.addTracksBulk(tracks);
            }
            else if (this.selectedTool === 'block') {
                // Create new block - all tiles must have NO block assigned
                if (!this.previewPath || this.previewPath.length === 0) return;

                // Validate the path for create mode
                const tracks = this.cachedNetwork?.tracks || [];
                const validation = validateBlockPath(this.previewPath, tracks, 'create');

                if (!validation.valid) {
                    const errorMsg = 'Cannot create block:\n' + validation.errors.join('\n');
                    window.showToast(errorMsg, 'error', 5000);
                    console.error('[Editor] Block creation validation failed:', validation.errors);
                    return;
                }

                // Show warnings about signals but allow proceeding
                if (validation.warnings.length > 0) {
                    const proceed = await showConfirmModal({
                        title: 'Proceed with Warnings',
                        message: 'Block creation reported warnings.',
                        detail: `${validation.warnings.join('\n')}\n\nProceed anyway?`,
                        confirmText: 'Proceed',
                        confirmVariant: 'primary'
                    });
                    if (!proceed) return;
                }

                // Show modal dialog for block creation
                const result = await this.showBlockModal({
                    mode: 'create',
                    trackCount: this.previewPath.length
                });

                if (!result) return; // Cancelled

                try {
                    // Create the new block
                    await this.api.createBlock(result.blockId, result.name || '');

                    // Set max speed if provided
                    if (result.maxSpeed) {
                        await this.api.updateBlock(result.blockId, {
                            speed_limit_kmh: result.maxSpeed
                        });
                    }

                    // Assign tracks to block
                    const positions = this.previewPath.map(p => [p.x, p.y]);
                    await this.api.addTracksToBlock(result.blockId, positions);

                    // Refresh block list to show updated track count
                    await this.refreshBlocksList();
                } catch (err) {
                    console.error('[Editor] Failed to create block:', err);
                    window.showToast('Failed to create block: ' + err.message, 'error', 5000);
                    return;
                }
            }
            else if (this.selectedTool === 'extend-block') {
                // Extend existing block - must start on tile with block
                if (!this.previewPath || this.previewPath.length === 0) return;

                // Validate the path for extend mode
                const tracks = this.cachedNetwork?.tracks || [];
                const validation = validateBlockPath(this.previewPath, tracks, 'extend');

                if (!validation.valid) {
                    const errorMsg = 'Cannot extend block:\n' + validation.errors.join('\n');
                    window.showToast(errorMsg, 'error', 5000);
                    console.error('[Editor] Block extension validation failed:', validation.errors);
                    return;
                }

                // Check if there are signals in the path - these will be INVALIDATED
                const hasSignals = validation.signalsInPath.length > 0;
                if (hasSignals) {
                    const signalCount = validation.signalsInPath.length;
                    const proceed = await showConfirmModal({
                        title: 'Signals Will Be Invalidated',
                        message: `Path contains ${signalCount} signal(s).`,
                        detail: 'These signals will be invalidated and need reconfiguration.\n(They will show an orange warning triangle)\n\nProceed anyway?',
                        confirmText: 'Proceed',
                        confirmVariant: 'primary'
                    });
                    if (!proceed) return;
                }

                // Build a track map for looking up block assignments
                const trackMap = new Map();
                tracks.forEach(t => trackMap.set(`${t.x},${t.y}`, t));

                // Calculate actual new tiles (excluding tiles already in the target block)
                // The target block is the first block in the path (where extension starts)
                const targetBlockId = validation.blocksInPath.length > 0 ? validation.blocksInPath[0] : null;
                let newTileCount = this.previewPath.length;
                if (targetBlockId) {
                    newTileCount = this.previewPath.filter(pos => {
                        const track = trackMap.get(`${pos.x},${pos.y}`);
                        // Count as new if no track or track is not already in the target block
                        return !track || track.block_section_id !== targetBlockId;
                    }).length;
                }

                // Show modal dialog for block extension with correct new tile count
                const result = await this.showBlockModal({
                    mode: 'extend',
                    trackCount: newTileCount,
                    existingBlocks: validation.blocksInPath
                });

                if (!result) return; // Cancelled

                try {
                    // Use the extend API with force=true if there are signals
                    // Use forceOverwrite=true if extending into tracks belonging to other blocks
                    const positions = this.previewPath.map(p => [p.x, p.y]);
                    const hasOtherBlocks = validation.blocksInPath.length > 1 ||
                        (validation.blocksInPath.length === 1 && validation.blocksInPath[0] !== result.blockId);
                    const apiResult = await this.api.extendBlock(result.blockId, positions, hasSignals, hasOtherBlocks);

                    // Show info about invalidated signals if any
                    if (apiResult.invalidated_signals && apiResult.invalidated_signals.length > 0) {
                        const count = apiResult.invalidated_signals.length;
                        window.showToast(`Block extended. ${count} signal(s) need reconfiguration.`, 'warning');
                    }

                    // Refresh block list to show updated track count
                    await this.refreshBlocksList();
                } catch (err) {
                    console.error('[Editor] Failed to extend block:', err);
                    window.showToast('Failed to extend block: ' + err.message, 'error', 5000);
                    return;
                }
            }
            else if (this.selectedTool === 'extend-platform') {
                // Extend existing platform - must start on tile with platform
                if (!this.previewPath || this.previewPath.length === 0) return;

                // Find platforms that intersect with the path
                const tracks = this.cachedNetwork?.tracks || [];
                const platforms = this.cachedNetwork?.platforms || [];
                const platformsInPath = new Set();

                for (const pos of this.previewPath) {
                    const track = tracks.find(t => t.x === pos.x && t.y === pos.y);
                    if (track && track.station_id) {
                        platformsInPath.add(track.station_id);
                    }
                }

                if (platformsInPath.size === 0) {
                    window.showToast('No platform found in path. Start the drag on an existing platform.', 'error', 5000);
                    return;
                }

                // Get the platform ID to extend (use first platform found, which should be at drag start)
                let platformId;
                if (platformsInPath.size === 1) {
                    platformId = Array.from(platformsInPath)[0];
                } else {
                    // Multiple platforms - find the one at the drag start point
                    const startTrack = tracks.find(t => t.x === this.dragStart.x && t.y === this.dragStart.y);
                    if (startTrack && startTrack.station_id) {
                        platformId = startTrack.station_id;
                    } else {
                        platformId = Array.from(platformsInPath)[0];
                    }
                }

                try {
                    // Get positions that don't already belong to the platform
                    const platform = platforms.find(p => p.platform_id === platformId);
                    const platformPositions = new Set(
                        platform?.positions?.map(p => `${p[0]},${p[1]}`) || []
                    );

                    const newPositions = this.previewPath
                        .filter(p => !platformPositions.has(`${p.x},${p.y}`))
                        .map(p => [p.x, p.y]);

                    if (newPositions.length === 0) {
                        window.showToast('All tiles are already part of the platform.', 'error', 5000);
                        return;
                    }

                    await this.api.extendPlatform(platformId, newPositions);

                    window.showToast(`Platform extended with ${newPositions.length} tile(s)`, 'success');
                } catch (err) {
                    console.error('[Editor] Failed to extend platform:', err);
                    window.showToast('Failed to extend platform: ' + err.message, 'error', 5000);
                    return;
                }
            }
            else if (this.selectedTool === 'eraser') {
                // Batch delete tracks along the planned path. The construction
                // pathfinder may include cells without an existing track; filter
                // them so the confirmation count and modal reflect actual work.
                if (!this.previewPath || this.previewPath.length === 0) return;
                const tracks = this.cachedNetwork?.tracks || [];
                const trackKeys = new Set(tracks.map(t => `${t.x},${t.y}`));
                const positions = this.previewPath
                    .filter(p => trackKeys.has(`${p.x},${p.y}`))
                    .map(p => [p.x, p.y]);
                if (positions.length === 0) return;
                await this._bulkDeleteWithConfirm(positions);
            }
            else if (this.selectedTool === 'remove-platform-tile') {
                // Remove platform tiles along the path
                if (!this.previewPath || this.previewPath.length === 0) return;

                const tracks = this.cachedNetwork?.tracks || [];
                let successCount = 0;
                let errorCount = 0;
                let errors = [];

                for (const pos of this.previewPath) {
                    const track = tracks.find(t => t.x === pos.x && t.y === pos.y);
                    if (!track || !track.station_id) {
                        errors.push(`No platform at (${pos.x}, ${pos.y})`);
                        errorCount++;
                        continue;
                    }

                    try {
                        await this.api.removePlatformTile(track.station_id, pos.x, pos.y);
                        successCount++;
                    } catch (err) {
                        errors.push(`(${pos.x}, ${pos.y}): ${err.message}`);
                        errorCount++;
                    }
                }

                if (successCount > 0) {
                    window.showToast(`Removed ${successCount} platform tile(s)`, 'success');
                }
                if (errorCount > 0) {
                    window.showToast(`Failed to remove ${errorCount} tile(s):\n${errors.slice(0, 3).join('\n')}`, 'error', 5000);
                }
            }
            else if (this.selectedTool === 'remove-block-tile') {
                // Remove block tiles along the path
                if (!this.previewPath || this.previewPath.length === 0) return;

                const tracks = this.cachedNetwork?.tracks || [];
                
                // Group positions by block
                const blockGroups = new Map();
                for (const pos of this.previewPath) {
                    const track = tracks.find(t => t.x === pos.x && t.y === pos.y);
                    if (track && track.block_section_id) {
                        if (!blockGroups.has(track.block_section_id)) {
                            blockGroups.set(track.block_section_id, []);
                        }
                        blockGroups.get(track.block_section_id).push([pos.x, pos.y]);
                    }
                }

                if (blockGroups.size === 0) {
                    window.showToast('No block tiles in selected path', 'error', 3000);
                    return;
                }

                let totalSuccess = 0;
                let totalErrors = [];

                // Remove tiles from each block
                for (const [blockId, positions] of blockGroups.entries()) {
                    try {
                        const result = await this.api.removeBlockTiles(blockId, positions);
                        totalSuccess += result.removed || 0;
                        if (result.errors && result.errors.length > 0) {
                            totalErrors.push(...result.errors);
                        }
                    } catch (err) {
                        totalErrors.push(`Block ${blockId}: ${err.message}`);
                    }
                }

                if (totalSuccess > 0) {
                    window.showToast(`Removed ${totalSuccess} block tile(s)`, 'success');
                }
                if (totalErrors.length > 0) {
                    window.showToast(`Errors:\n${totalErrors.slice(0, 3).join('\n')}`, 'error', 5000);
                }
            }

            await this.refreshAndSelect(null);  // Refresh without selecting specific element
        } catch (err) {
            console.error('[Editor] Failed to commit build:', err);
        }
    }

    /**
     * Unified refresh helper - ensures single source of truth pattern:
     * 1. Await API refresh to get latest data
     * 2. Optionally refresh properties panel for a specific element
     * @param {Object|null} gridPos - Position to select and show in properties, or null to just refresh
     */
    async refreshAndSelect(gridPos) {
        await window.app.refreshNetwork();
        if (gridPos) {
            await this.selectElement(gridPos);
        }
    }

    // Wraps deleteTracksBulk with the server's impact pre-check / confirmation
    // flow. On HTTP 409 ({requires_confirmation, impact}), shows a modal listing
    // the orphans and retries with force=true. Returns the final API response,
    // or null if the user cancelled the confirmation.
    async _bulkDeleteWithConfirm(positions) {
        if (!positions || positions.length === 0) return null;
        try {
            return await this.api.deleteTracksBulk(positions, { force: false });
        } catch (err) {
            if (err.status !== 409 || !err.requires_confirmation) throw err;
            const impact = err.impact || {};
            const switches = (impact.removes_switches || []).length;
            const signals = (impact.removes_signals || []).length;
            const platforms = (impact.removes_platforms || []).length;
            const parts = [];
            if (switches)  parts.push(`${switches} switch${switches !== 1 ? 'es' : ''}`);
            if (signals)   parts.push(`${signals} signal${signals !== 1 ? 's' : ''}`);
            if (platforms) parts.push(`${platforms} platform${platforms !== 1 ? 's' : ''}`);
            const detail = parts.length
                ? `This will also remove ${parts.join(', ')}.`
                : '';
            const confirmed = await showConfirmModal({
                title: 'Confirm bulk delete',
                message: `Delete ${positions.length} track${positions.length !== 1 ? 's' : ''}?`,
                detail,
                confirmText: 'Delete',
                confirmVariant: 'danger',
            });
            if (!confirmed) return null;
            return await this.api.deleteTracksBulk(positions, { force: true });
        }
    }

    async handleErase(gridPos) {
        try {
            const existingTrack = this.cachedNetwork?.tracks?.find(
                t => t.x === gridPos.x && t.y === gridPos.y
            );

            if (!existingTrack) {
                return;
            }

            // Check if tile has objects (signal or platform)
            const hasSignal = existingTrack.signal_id || existingTrack.signal_state || existingTrack.type === 'signal';
            const hasPlatform = existingTrack.station_id || existingTrack.station_name || existingTrack.type === 'station';

            if (hasSignal || hasPlatform) {
                // First click: remove objects only, keep track

                // Build confirmation message
                let items = [];
                if (hasSignal) items.push(`Signal ${existingTrack.signal_id || 'at this location'}`);
                if (hasPlatform) items.push(`Platform ${existingTrack.station_name || existingTrack.station_id || 'at this location'}`);

                const confirmed = await showConfirmModal({
                    title: 'Delete Objects',
                    message: `Delete ${items.join(' and ')}?`,
                    detail: 'The track will remain in place.',
                    confirmText: 'Delete',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;

                if (hasSignal) {
                    // Use the signal-specific deletion API
                    if (existingTrack.signal_id) {
                        await this.api.deleteSignal(existingTrack.signal_id);
                    } else {
                        // Fallback to position-based deletion
                        await this.api.deleteSignalAt(gridPos.x, gridPos.y);
                    }
                }
                if (hasPlatform) {
                    // Use the platform-specific deletion API
                    if (existingTrack.station_id) {
                        await this.api.deletePlatform(existingTrack.station_id);
                    }
                }
            } else {
                // Second click (no objects): remove track entirely
                await this.api.deleteTrack(gridPos.x, gridPos.y);
            }

            await this.refreshAndSelect(null);  // Refresh without selecting (track may be gone)
        } catch (err) {
            console.error('[Editor] Failed to erase:', err);
        }
    }

    async handleRemovalClick(gridPos) {
        try {
            const existingTrack = this.cachedNetwork?.tracks?.find(
                t => t.x === gridPos.x && t.y === gridPos.y
            );

            if (!existingTrack) {
                window.showToast('No track at this position', 'error', 3000);
                return;
            }

            if (this.selectedTool === 'remove-platform-tile') {
                if (!existingTrack.station_id) {
                    window.showToast('No platform at this position', 'error', 3000);
                    return;
                }

                try {
                    await this.api.removePlatformTile(existingTrack.station_id, gridPos.x, gridPos.y);
                    window.showToast('Platform tile removed', 'success');
                } catch (err) {
                    window.showToast(`Failed to remove platform tile: ${err.message}`, 'error', 5000);
                }
            }
            else if (this.selectedTool === 'remove-block-tile') {
                if (!existingTrack.block_section_id) {
                    window.showToast('No block at this position', 'error', 3000);
                    return;
                }

                try {
                    const result = await this.api.removeBlockTiles(
                        existingTrack.block_section_id,
                        [[gridPos.x, gridPos.y]]
                    );
                    
                    if (result.removed > 0) {
                        window.showToast('Block tile removed', 'success');
                    } else if (result.errors && result.errors.length > 0) {
                        window.showToast(`Cannot remove: ${result.errors[0]}`, 'error', 5000);
                    }
                } catch (err) {
                    window.showToast(`Failed to remove block tile: ${err.message}`, 'error', 5000);
                }
            }

            await this.refreshAndSelect(null);
        } catch (err) {
            console.error('[Editor] Failed to handle removal click:', err);
        }
    }

    // Single click placement for signal/platform/tunnel tools
    async handleSingleClickPlace(gridPos) {
        const existingTrack = this.cachedNetwork?.tracks?.find(
            t => t.x === gridPos.x && t.y === gridPos.y
        );

        if (!existingTrack) {
            window.showToast(`Cannot place ${this.selectedTool}: No track at this position`, 'error', 5000);
            return;
        }

        if (this.selectedTool === 'tunnel') {
            await this.handleTunnelClick(gridPos, existingTrack);
            return;
        }

        if (this.selectedTool === 'signal') {
            // Check if already has a platform
            if (existingTrack.station_name) {
                window.showToast('Cannot place signal on a platform', 'error', 5000);
                return;
            }

            // First, validate signal placement via API (synchronous - always get fresh data)
            try {
                const validation = await this.api.validateSignalPlacement(gridPos.x, gridPos.y);

                if (!validation.can_place) {
                    window.showToast(validation.error || 'Cannot place signal at this position', 'error', 5000);
                    return;
                }

                // Only use cached preview direction if it's for the SAME position
                // This prevents race conditions where user clicks before validation updates
                // available_directions are detection directions (where trains travel TO)
                let detectionDirection;
                if (this.previewSignalDirection &&
                    this._lastValidatedPosition?.x === gridPos.x &&
                    this._lastValidatedPosition?.y === gridPos.y &&
                    validation.available_directions?.includes(this.previewSignalDirection)) {
                    detectionDirection = this.previewSignalDirection;
                } else {
                    // Use first available detection direction from fresh API validation
                    detectionDirection = validation.available_directions?.[0] || validation.valid_directions?.[0];
                }

                if (!detectionDirection) {
                    window.showToast('No valid detection direction available for signal placement', 'error', 5000);
                    return;
                }

                // Create signal via API with detection_direction
                await this.api.createSignal(
                    gridPos.x,
                    gridPos.y,
                    detectionDirection,  // detection_direction: where trains travel TO
                    'block',  // signal_type
                    null,     // name
                    true      // auto_connect
                );

                // Reset preview direction after placing signal
                this.previewSignalDirection = null;
                this.previewSignalDirectionIndex = 0;
                this._lastValidatedPosition = null;
            } catch (err) {
                console.error('[Editor] Signal creation failed:', err);
                window.showToast('Failed to create signal: ' + err.message, 'error', 5000);
                return;
            }
        } else if (this.selectedTool === 'platform') {
            // Validate platform placement via API
            try {
                const validation = await this.api.validatePlatformPlacement(gridPos.x, gridPos.y);

                if (!validation.can_place) {
                    window.showToast(validation.error || 'Cannot place platform at this position', 'error', 5000);
                    return;
                }

                // Show platform modal to get platform properties
                const platformData = await this.showPlatformModal({
                    mode: 'create',
                    x: gridPos.x,
                    y: gridPos.y,
                    existingBlock: validation.existing_block
                });

                if (!platformData) return; // User cancelled

                // Create platform via API
                await this.api.createPlatform({
                    x: gridPos.x,
                    y: gridPos.y,
                    name: platformData.name,
                    length_cm: platformData.lengthCm,
                    platform_side: platformData.platformSide,
                    top_speed_kmh: platformData.topSpeedKmh,
                    platform_id: platformData.platformId,
                });
            } catch (err) {
                console.error('[Editor] Platform creation failed:', err);
                window.showToast('Failed to create platform: ' + err.message, 'error', 5000);
                return;
            }
        }

        // Refresh and select the newly placed element
        await this.refreshAndSelect(gridPos);
        // Clear hover preview
        this.hoverPosition = null;
        this.hoverValid = false;
    }

    // Update hover preview for signal/platform tools
    // Uses async API validation for accurate signal placement checking
    updateHoverPreview(gridPos) {
        if (!['signal', 'platform'].includes(this.selectedTool)) {
            this.hoverPosition = null;
            this.hoverValid = false;
            return;
        }

        this.hoverPosition = gridPos;

        // Check if this tile has valid track for placement
        const existingTrack = this.cachedNetwork?.tracks?.find(
            t => t.x === gridPos.x && t.y === gridPos.y
        );

        // Quick local validation first
        if (!existingTrack) {
            this.hoverValid = false;
            this.previewSignalDirection = null;
            this.validSignalDirections = [];
            this.drawWithHoverPreview();
            return;
        }

        if (this.selectedTool === 'signal') {
            // Debounced API validation for accurate signal placement
            this.validateSignalPreviewAsync(gridPos, existingTrack);
        } else if (this.selectedTool === 'platform') {
            // Platform validation:
            // - Cannot be a switch
            // - Cannot already have signal
            this.hoverValid = !existingTrack.is_switch && !existingTrack.signal_state;
            this.previewSignalDirection = null;
            this.drawWithHoverPreview();
        }
    }

    // Async API validation for signal preview - debounced to avoid flooding server
    async validateSignalPreviewAsync(gridPos, existingTrack) {
        // Cancel any pending validation
        if (this.signalValidationTimeout) {
            clearTimeout(this.signalValidationTimeout);
        }

        // Show pending state with local validation first
        const trackBits = existingTrack.track_bits || 0;
        const localDirections = getValidSignalDirections(trackBits);
        const hasStation = !!existingTrack.station_name;

        if (localDirections.length === 0 || hasStation) {
            this.hoverValid = false;
            this.previewSignalDirection = null;
            this.validSignalDirections = [];
            this.drawWithHoverPreview();
            return;
        }

        // Immediately show local validation (prevents flickering)
        // Only update if this is a new position or we don't have directions yet
        if (this.validSignalDirections.length === 0 ||
            this._lastValidatedPosition?.x !== gridPos.x ||
            this._lastValidatedPosition?.y !== gridPos.y) {
            this.validSignalDirections = localDirections;
            this.hoverValid = localDirections.length > 0;
            if (this.hoverValid && !this.previewSignalDirection) {
                this.previewSignalDirectionIndex = 0;
                this.previewSignalDirection = localDirections[0];
            }
            this.drawWithHoverPreview();
        }

        // Debounce the API call (50ms)
        this.signalValidationTimeout = setTimeout(async () => {
            try {
                // Call API to get accurate validation including existing signal check
                const validation = await this.api.validateSignalPlacement(gridPos.x, gridPos.y);

                // Make sure we're still hovering on the same tile
                if (!this.hoverPosition || this.hoverPosition.x !== gridPos.x || this.hoverPosition.y !== gridPos.y) {
                    return;  // User moved away, ignore result
                }

                // Track that we've validated this position
                this._lastValidatedPosition = { x: gridPos.x, y: gridPos.y };

                if (!validation.can_place) {
                    // Signal cannot be placed (e.g., all directions already have signals)
                    this.hoverValid = false;
                    this.previewSignalDirection = null;
                    this.validSignalDirections = [];
                } else {
                    // Get available directions from API (directions without existing signals)
                    const availableDirections = validation.available_directions || [];

                    // Use available_directions to show which directions can be placed
                    this.validSignalDirections = availableDirections;
                    this.hoverValid = availableDirections.length > 0;

                    // Set initial preview direction
                    if (this.hoverValid && availableDirections.length > 0) {
                        if (!this.previewSignalDirection || !availableDirections.includes(this.previewSignalDirection)) {
                            this.previewSignalDirectionIndex = 0;
                            this.previewSignalDirection = availableDirections[0];
                        }
                    } else {
                        this.previewSignalDirection = null;
                    }
                }

                this.drawWithHoverPreview();
            } catch (err) {
                console.warn('[Editor] Signal validation API failed, using local validation:', err);
                // Fallback to local validation - already set above, no need to redraw
            }
        }, 50);
    }

    // Check if a tool is actively being used that needs hover preview preservation
    // Used by App to avoid flickering during network polling
    isToolActive() {
        // Tools that show hover previews
        const previewTools = ['signal', 'platform'];
        if (previewTools.includes(this.selectedTool) && this.hoverPosition) {
            return true;
        }
        // Also preserve during active dragging
        if (this.isDraggingBuild && this.previewPath && this.previewPath.length > 0) {
            return true;
        }
        return false;
    }

    // Redraw network with hover preview overlay
    drawWithHoverPreview() {
        if (!this.cachedNetwork) return;
        this.renderer.requestRedraw();

        if (this.hoverPosition) {
            // Get track bits for proper signal positioning in preview
            let trackBits = null;
            if (this.selectedTool === 'signal') {
                const existingTrack = this.cachedNetwork?.tracks?.find(
                    t => t.x === this.hoverPosition.x && t.y === this.hoverPosition.y
                );
                trackBits = existingTrack?.track_bits || null;
            }
            
            this.renderer.trackHandler.drawHoverPreview(
                this.hoverPosition,
                this.selectedTool,
                this.hoverValid,
                this.previewSignalDirection,
                trackBits
            );
        }
    }

    async handleRightClick(gridPos) {
        // Scrap an in-flight path selection but stay in the path tool so the
        // user can immediately start a new one.
        if (this.activeMode?.name === 'Route creation' && this.pathToolState?.active) {
            await this.resetRouteToolState();
            window.showToast('Route creation cancelled', 'info', 3000);
            return;
        }

        // Check if we're in signal tool mode - cycle preview direction
        if (this.selectedTool === 'signal' && this.hoverPosition &&
            this.hoverPosition.x === gridPos.x && this.hoverPosition.y === gridPos.y && this.hoverValid) {
            // Use cached valid directions from API validation
            const validDirections = this.validSignalDirections || [];
            if (validDirections.length > 1) {
                // Cycle to next direction
                this.previewSignalDirectionIndex = (this.previewSignalDirectionIndex + 1) % validDirections.length;
                this.previewSignalDirection = validDirections[this.previewSignalDirectionIndex];
                // Redraw with new preview direction
                this.drawWithHoverPreview();
                return;
            }
        }

        // Get track at position
        try {
            const track = await this.api.getTrack(gridPos.x, gridPos.y);
            if (track.error) return;

            // Check if track has switch points (uses is_switch from simplified global state system)
            if (track.is_switch) {
                // Toggle global switch state between MAIN (0) and DIVERGE (1)
                // Backend validates lock status and rejects if switch is locked by a route
                const newState = track.switch_state === 0 ? 1 : 0;
                try {
                    await this.api.updateTrack(gridPos.x, gridPos.y, {
                        switch_state: newState
                    });
                } catch (switchErr) {
                    // Parse error message from API response
                    const errMsg = this._parseApiError(switchErr, 'Cannot toggle switch');
                    window.showToast(errMsg, 'error', 5000);
                }
                
                await window.app.refreshNetwork();
                // Refresh properties panel to show updated switch state
                await this.selectElement(gridPos);
            }
        } catch (err) {
            console.error('Right-click action failed:', err);
        }
    }

    /**
     * Parse API error message from exception.
     * Extracts the 'error' field from JSON response if available.
     */
    _parseApiError(err, fallbackPrefix) {
        try {
            // Error message format: "HTTP 409: {"error": "...", ...}"
            const msg = err.message || String(err);
            const jsonMatch = msg.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
                const parsed = JSON.parse(jsonMatch[0]);
                if (parsed.error) {
                    return `${fallbackPrefix}: ${parsed.error}`;
                }
            }
            return `${fallbackPrefix}: ${msg}`;
        } catch {
            return `${fallbackPrefix}: ${err.message || String(err)}`;
        }
    }

    async selectElement(gridPos) {
        try {
            const track = await this.api.getTrack(gridPos.x, gridPos.y);
            if (!track.error) {
                // Handle signals - refresh from API when available
                if (track.signals && track.signals.length > 0) {
                     // The signals array contains {signal_id, direction, state, etc.}
                }

                // Refresh blocks list to ensure dropdowns are up-to-date
                if (track.signals && track.signals.length > 0) {
                    await this.refreshBlocksList();
                }

                this.selectedElement = track;
                this.updatePropertiesPanel(track);

                // Update secondary editor based on new tile's content
                if (!this.uiHandler.isSecondaryPanelPinned()) {
                    this.updateSecondaryEditorForTile(track);
                }
            } else {
                // Create virtual tile for empty position
                this.selectedElement = {
                    x: gridPos.x,
                    y: gridPos.y,
                    track_bits: 0,
                    directed_paths: [],
                    edges: [],
                    isVirtual: true  // Flag to indicate this tile doesn't exist yet
                };
                this.updatePropertiesPanel(this.selectedElement);

                // Close secondary editor for empty tile
                if (!this.uiHandler.isSecondaryPanelPinned()) {
                    this.updateSecondaryEditorForTile(this.selectedElement);
                }
            }
        } catch (err) {
            // On network error, also create virtual tile
            this.selectedElement = {
                x: gridPos.x,
                y: gridPos.y,
                track_bits: 0,
                directed_paths: [],
                edges: [],
                isVirtual: true
            };
            this.updatePropertiesPanel(this.selectedElement);

            // Close secondary editor for error/empty tile
            if (!this.uiHandler.isSecondaryPanelPinned()) {
                this.updateSecondaryEditorForTile(this.selectedElement);
            }
        }
    }

    updatePropertiesPanel(track) {
        const panel = document.querySelector('.properties-panel');
        if (!panel) return;
        
        // Get track_bits directly (6-bit system is the source of truth)
        const trackBits = track.track_bits ?? 0;
        const edges = track.edges || [];
        
        // Track bits segment names
        const SEGMENT_NAMES = {
            1: 'NS', 2: 'EW', 4: 'NE', 8: 'NW', 16: 'SE', 32: 'SW'
        };
        
        // Get active segments as readable list
        const activeSegments = [];
        for (const [bit, name] of Object.entries(SEGMENT_NAMES)) {
            if (trackBits & parseInt(bit)) {
                activeSegments.push(name);
            }
        }
        
        // Determine track shape description
        const numSegments = activeSegments.length;
        let shapeDesc = 'Empty';
        if (numSegments === 1) {
            if (trackBits === 1 || trackBits === 2) shapeDesc = 'Straight';
            else shapeDesc = 'Diagonal';
        } else if (numSegments === 2) {
            if (trackBits === 3) shapeDesc = 'Crossing (NS+EW)';
            else if (trackBits === 36) shapeDesc = 'Diamond (NE+SW)';
            else if (trackBits === 24) shapeDesc = 'Diamond (NW+SE)';
            else shapeDesc = 'Switch';
        } else if (numSegments >= 3) {
            shapeDesc = 'Complex Junction';
        }
        
        // Check if forbidden (explicit bad geometry) or a square-corner switch
        // (a branch 90° to a connecting segment, no 45° easing — see
        // isSquareCornerSwitch in constants.js).
        const isForbidden = FORBIDDEN_TRACK_BITS.has(trackBits) || isSquareCornerSwitch(trackBits);
        
        // Format binary with highlighted 1s
        const binaryStr = trackBits.toString(2).padStart(6, '0');
        const binaryHTML = binaryStr.split('').map(bit => 
            bit === '1' ? `<span class="text-track-bit-active">${bit}</span>` : bit
        ).join('');
        
        // Build the track bits editor canvas
        const editorId = `track-editor-${Date.now()}`;
        
        panel.innerHTML = `
            <h3>Properties</h3>
            
            <!-- Compact Header: Position and Type on one line with virtual badge -->
            <div class="compact-header info-display-medium">
                <span>(${track.x}, ${track.y}) - ${track.isVirtual ? 'Empty' : track.type}</span>
                ${track.isVirtual ? '<span class="badge badge-new">NEW</span>' : ''}
            </div>
            
            <!-- Track Bits Display (Primary) - Larger font, compact layout -->
            <div class="info-box-track info-display-large">
                <label class="text-track font-bold block mb-4">Track Bits</label>
                <div class="text-2xl font-bold mb-2 text-track font-mono">${trackBits} <span class="text-lg text-muted">(0b${binaryHTML})</span></div>
                <div class="text-muted text-base mb-8 info-meta">
                    ${shapeDesc} • ${numSegments} segment${numSegments !== 1 ? 's' : ''} • ${activeSegments.length > 0 ? activeSegments.join(', ') : 'No segments'} ${edges.length > 0 ? '• Edges: ' + edges.join(', ') : ''}
                </div>
                ${isForbidden ?
                    '<div class="text-xs text-danger mt-8 font-bold">⚠ FORBIDDEN CONFIGURATION</div>'
                    : ''}
            </div>

            <!-- Switch Summary (Compact view with Edit button) -->
            ${track.switch_points && Object.keys(track.switch_points).length > 0 ? (() => {
                const switchStateColor = track.switch_state === 0 ? 'var(--success-color)' : 'var(--warning-color)';
                const switchStateText = track.switch_state === 0 ? 'MAIN' : 'DIVERGE';
                // Lock ownership lives on the Switch entity's `locks` array
                // (design_new.md:651); resolve it from the cached network.
                const lockSwitchEntity = this.cachedNetwork?.switches?.[track.switch_id] || null;
                const isLocked = !!(lockSwitchEntity && Array.isArray(lockSwitchEntity.locks) && lockSwitchEntity.locks.length > 0);
                const hasSwitchId = track.switch_id && track.switch_id !== '';
                return `
                <div class="property-group-header">Switch</div>
                <div class="property-item-row">
                    <div class="switch-summary-row" style="border-left: 3px solid ${switchStateColor}">
                        <div class="switch-status-dot" style="background-color: ${switchStateColor};"></div>
                        <div class="switch-info">
                            <span class="switch-id">${track.switch_id || 'Switch'}</span>
                            <span class="switch-state" style="color: ${switchStateColor}">${switchStateText}${isLocked ? ' 🔒' : ''}</span>
                        </div>
                        ${hasSwitchId 
                            ? `<button class="edit-btn edit-switch-btn" data-switch-id="${track.switch_id}">Edit</button>`
                            : `<span class="text-muted text-xs">No switch entity</span>`
                        }
                    </div>
                </div>
                `;
            })() : ''}
            ${track.signals && track.signals.length > 0 ? `
            <div class="property-group-header">Signals</div>
            ${track.signals.map(signal => `
            <div class="property-item-row">
                <div class="signal-summary-row">
                    <div class="signal-status-dot" style="background-color: ${
                        signal.signal_state === 'green' || signal.signal_state === 'GREEN' ? 'var(--success-color)' :
                        (signal.signal_state === 'yellow' || signal.signal_state === 'YELLOW' ? 'var(--warning-color)' : 'var(--danger-color)')
                    }"></div>
                    <div class="signal-info">
                        <span class="signal-id">${signal.signal_id}</span>
                        <span class="signal-dir">→${signal.detection_direction}</span>
                    </div>
                    <button class="edit-btn edit-signal-btn" data-signal-id="${signal.signal_id}">Edit</button>
                </div>
            </div>
            `).join('')}
            ` : ''}

            <!-- Block Summary (Compact view with Edit button) -->
            ${track.block_section_id ? (() => {
                const block = this.cachedBlocks && this.cachedBlocks.find(b => b.block_id === track.block_section_id);
                const blockColor = this.getBlockColor(track.block_section_id);
                const stateColor = block ? getBlockStateColor(block.state) : 'var(--text-secondary)';
                return `
                <div class="property-group-header">Block</div>
                <div class="property-item-row">
                    <div class="block-summary-row" style="border-left: 3px solid ${blockColor}">
                        <div class="block-info">
                            <span class="block-id">${track.block_section_id}</span>
                            <span class="block-state" style="color: ${stateColor}">${block ? block.state : 'UNKNOWN'}</span>
                        </div>
                        <button class="edit-btn edit-block-btn" data-block-id="${track.block_section_id}">Edit</button>
                    </div>
                </div>
                ${block ? `
                <div class="property-item-row">
                    ${renderDirectionSegment('block', block, { idBase: 'prop-block-direction' })}
                </div>
                ` : ''}
                `;
            })() : ''}

            <!-- Platform Info Display -->
            ${track.station_id ? (() => {
                const platform = this.cachedNetwork && this.cachedNetwork.platforms 
                    ? this.cachedNetwork.platforms.find(p => p.platform_id === track.station_id) 
                    : null;
                return `
                <div class="property-group-header">Platform</div>
                <div class="property-item-row">
                    <div class="station-summary-row">
                        <div class="station-info">
                            <span class="station-id">${track.station_id}</span>
                            ${platform && platform.name ? `<span class="station-name">${platform.name}</span>` : ''}
                        </div>
                        <button class="edit-btn edit-platform-btn" data-platform-id="${track.station_id}">Edit</button>
                    </div>
                </div>
                `;
            })() : ''}
            
            <!-- Train Info Display (if train is at this tile) -->
            ${(() => {
                const trainsAtTile = this.cachedNetwork && this.cachedNetwork.trains
                    ? this.cachedNetwork.trains.filter(t => {
                        // Occupancy lives on the controller's span, not the train
                        // config object.
                        const span = window.stateManager.getController(t.id)?.span;
                        if (span && span.occupied_tiles) {
                            return span.occupied_tiles.some(tile =>
                                tile[0] === track.x && tile[1] === track.y
                            );
                        }
                        return false;
                    })
                    : [];
                if (trainsAtTile.length === 0) return '';
                return `
                <div class="property-group-header">Train</div>
                ${trainsAtTile.map(train => {
                    const controller = window.stateManager.getController(train.id);
                    const dotColor =
                        controller?.control_mode === 'manual' ? 'var(--warning-color)' :
                        controller?.state === 'running_moving' ? 'var(--success-color)' :
                        controller?.state === 'running_at_station' ? 'var(--info-color)' :
                        controller?.state === 'idle' ? 'var(--text-secondary)' : 'var(--danger-color)';
                    return `
                <div class="property-item-row">
                    <div class="train-summary-row">
                        <div class="train-status-dot" style="background-color: ${dotColor}"></div>
                        <div class="train-info">
                            <span class="train-id">${train.id}</span>
                            <span class="train-state">${controller?.state || 'unknown'}</span>
                        </div>
                        <button class="edit-btn edit-train-btn" data-train-id="${train.id}">Edit</button>
                    </div>
                </div>
                `;
                }).join('')}
                `;
            })()}
            ${track.occupied_by ? `
            <div class="property-item">
                <label>Occupied By:</label>
                <span class="text-danger">${track.occupied_by}</span>
            </div>
            ` : ''}
            ${track.dead_end_ports && track.dead_end_ports.length > 0 ? `
            <div class="property-item">
                <div class="info-display-row info-display-compact">
                    <span class="info-label">Dead Ends</span>
                    <span class="info-value text-warning">${track.dead_end_ports.join(', ')}</span>
                </div>
            </div>
            ` : ''}

            <!-- Bit Editor + Visual Editor live in the dedicated "Track Bits" sidebar section (see #track-bits-panel) -->

            <!-- View Full JSON Button -->
            <button id="view-full-json-btn" class="btn btn-primary btn-full text-sm mt-12">
                📄 View Full JSON
            </button>
        `;

        // Render bit editor into its dedicated sidebar section
        this.updateTrackBitsPanel(track, editorId, trackBits, binaryHTML, isForbidden);

        // Initialize track bits editor after panel is rendered
        setTimeout(() => this.initBitEditor(track), 0);
        setTimeout(() => this.initVisualTrackEditor(editorId, track), 0);
        setTimeout(() => this.initSignalEditor(track), 0);
        setTimeout(() => this.initBlockEditor(track), 0);
        setTimeout(() => this.initSwitchEditor(track), 0);
        setTimeout(() => this.initPlatformButtonHandlers(), 0);
        setTimeout(() => this.initTrainEditor(track), 0);
        setTimeout(() => this.initViewFullJSONButton(track), 0);
    }

    initViewFullJSONButton(track) {
        return this.uiHandler.initViewFullJSONButton(track);
    }

    initPlatformButtonHandlers() {
        return this.uiHandler.initPlatformButtonHandlers();
    }

    initSignalEditor(track) {
        return this.signalHandler.initSignalEditor(track);
    }

    async showSignalEditor(signalId) {
        return this.signalHandler.showSignalEditor(signalId);
    }

    // Initialize block summary edit button handler
    async initBlockEditor(track) {
        if (!track.block_section_id) return;

        // Find and attach click handler to Edit button
        const editBtn = document.querySelector(`.edit-block-btn[data-block-id="${track.block_section_id}"]`);
        if (editBtn) {
            editBtn.onclick = () => {
                this.showBlockEditor(track.block_section_id);
            };
        }

        // Allowed-travel-direction segment, shown only for a block selection.
        // Per docs/DESIGN_LANGUAGE.md §3 segmented control and design_new.md
        // `Block.allowed_travel_directions` canonical home. Auto-routing obeys
        // this; an operator override (manual_override) bypasses it.
        const blockId = track.block_section_id;
        bindDirectionSegment('prop-block-direction', async (_state, payload) => {
            try {
                const result = await this.api.updateBlock(blockId, payload);
                if (result && result.error) {
                    window.showToast(
                        `Cannot change travel direction: ${result.error}`,
                        'error',
                        4000
                    );
                    // Re-render to roll the active class back to the server's value.
                    this.updatePropertiesPanel(this.selectedElement);
                } else {
                    window.showToast(`Block ${blockId} travel direction updated`, 'info', 1500);
                    await window.app.refreshNetwork();
                }
            } catch (err) {
                console.error('Failed to update block travel direction:', err);
                window.showToast(
                    `Cannot change travel direction: ${err.message || err}`,
                    'error',
                    4000
                );
                this.updatePropertiesPanel(this.selectedElement);
            }
        });
    }

    // Initialize switch edit button handler
    initSwitchEditor(track) {
        if (!track.switch_id) return;

        const editBtn = document.querySelector(`.edit-switch-btn[data-switch-id="${track.switch_id}"]`);
        if (editBtn) {
            editBtn.onclick = () => {
                this.showSwitchEditor(track.switch_id);
            };
        }
    }

    // Populate the dedicated Track Bits sidebar section (lives in the fixed
    // tools area so it does NOT get pushed around when properties content
    // changes). Folded by default; preserves its expanded/collapsed state
    // across selections because only the body is re-rendered.
    updateTrackBitsPanel(track, editorId, trackBits, binaryHTML, isForbidden) {
        const body = document.getElementById('track-bits-panel-body');
        if (!body) return;

        body.innerHTML = `
            <div class="combined-editor">
                <!-- Bit Editor (Left - 33%) -->
                <div id="bit-editor" class="bg-section p-8 rounded">
                    <h4 class="text-track text-xs mb-8">Bit Editor</h4>
                    <div class="flex flex-col gap-4">
                        ${renderBitToggle(1, 'NS', trackBits)}
                        ${renderBitToggle(2, 'EW', trackBits)}
                        ${renderBitToggle(4, 'NE', trackBits)}
                        ${renderBitToggle(8, 'NW', trackBits)}
                        ${renderBitToggle(16, 'SE', trackBits)}
                        ${renderBitToggle(32, 'SW', trackBits)}
                    </div>
                    <div class="bit-info bg-hover rounded-sm text-tiny">
                        <p><span id="bit-total" class="font-bold text-track">${trackBits}</span> <span class="text-muted">(0b${binaryHTML})</span></p>
                        <p class="bit-warning font-bold text-danger mt-2" style="display: ${isForbidden ? 'block' : 'none'};">
                            ⚠ Forbidden
                        </p>
                    </div>
                    <button id="apply-bits" class="btn btn-primary btn-full text-xxs mt-6">Apply</button>
                </div>

                <!-- Visual Editor (Right - 67%) -->
                <div class="visual-editor bg-section p-8 rounded">
                    <label class="text-track font-bold text-xs mb-6 block">Visual Editor</label>
                    <canvas id="${editorId}" width="200" height="200" class="block w-full rounded"></canvas>
                    <div class="helper-text text-center">Click segments to toggle</div>
                </div>
            </div>
        `;
    }

    // Reset the Track Bits sidebar section to the empty placeholder.
    // Called when no track is selected.
    clearTrackBitsPanel() {
        const body = document.getElementById('track-bits-panel-body');
        if (body) {
            body.innerHTML = `<p class="properties-empty">Select a track to edit bits</p>`;
        }
    }

    // Initialize train summary edit button handler
    initTrainEditor(track) {
        // Find all trains at this tile
        const trainsAtTile = this.cachedNetwork && this.cachedNetwork.trains
            ? this.cachedNetwork.trains.filter(t => {
                // Occupancy lives on the controller's span, not the train
                // config object.
                const span = window.stateManager.getController(t.id)?.span;
                if (span && span.occupied_tiles) {
                    return span.occupied_tiles.some(tile =>
                        tile[0] === track.x && tile[1] === track.y
                    );
                }
                return false;
            })
            : [];
        
        if (trainsAtTile.length === 0) return;
        
        // Attach click handlers to all train Edit buttons
        trainsAtTile.forEach(train => {
            const editBtn = document.querySelector(`.edit-train-btn[data-train-id="${train.id}"]`);
            if (editBtn) {
                editBtn.onclick = () => {
                    // Use app.selectTrain() to ensure selectedTrainId is set
                    // This enables auto-updates during simulation
                    window.app.selectTrain(train.id);
                };
            }
        });
    }

    initBitEditor(track) {
        return this.uiHandler.initBitEditor(track);
    }

    initVisualTrackEditor(canvasId, track) {
        return this.uiHandler.initVisualTrackEditor(canvasId, track);
    }

    pointToSegmentDistance(px, py, x1, y1, x2, y2) {
        return this.uiHandler.pointToSegmentDistance(px, py, x1, y1, x2, y2);
    }

    clearPropertiesPanel() {
        return this.uiHandler.clearPropertiesPanel();
    }

    setCachedNetwork(network) {
        this.cachedNetwork = network;

        // Re-validate hover state if we have an active hover position
        // This ensures the hover preview reflects the updated network state
        if (this.hoverPosition && ['signal', 'platform'].includes(this.selectedTool)) {
            // Re-run validation with the new network data
            this.updateHoverPreview(this.hoverPosition);
        }
    }

    enterRouteToolMode() {
        return this.pathHandler.enterRouteToolMode();
    }

    async exitRouteToolMode() {
        return await this.pathHandler.exitRouteToolMode();
    }

    async resetRouteToolState() {
        return await this.pathHandler.resetRouteToolState();
    }

    async handleRouteToolClick(gridPos) {
        return this.pathHandler.handleRouteToolClick(gridPos);
    }

    async handleRouteFirstClick(gridPos, track) {
        return this.pathHandler.handleRouteFirstClick(gridPos, track);
    }

    buildReachablePositionsSet() {
        return this.pathHandler.buildReachablePositionsSet();
    }

    async handleRouteSecondClick(gridPos, track) {
        return this.pathHandler.handleRouteSecondClick(gridPos, track);
    }

    showRoutePrioritySelector() {
        return this.pathHandler.showRoutePrioritySelector();
    }

    async showPathsPanel() {
        return this.pathHandler.showPathsPanel();
    }

    renderPathListItem(path) {
        return this.pathHandler.renderPathListItem(path);
    }

    getPathStatusColor(status) {
        return this.pathHandler.getPathStatusColor(status);
    }

    initRoutesPanelHandlers() {
        return this.pathHandler.initRoutesPanelHandlers();
    }

    async showPathEditor(pathId) {
        return this.pathHandler.showPathEditor(pathId);
    }

    initPathEditorHandlers(pathId, path) {
        return this.pathHandler.initPathEditorHandlers(pathId, path);
    }

    enterExtendRouteMode() {
        return this.pathHandler.enterExtendRouteMode();
    }

    async exitExtendRouteMode() {
        return await this.pathHandler.exitExtendRouteMode();
    }

    async resetExtendRouteState() {
        return await this.pathHandler.resetExtendRouteState();
    }

    async handleExtendRouteToolClick(gridPos) {
        return this.pathHandler.handleExtendRouteToolClick(gridPos);
    }

    async handleExtendRouteFirstClick(gridPos, track) {
        return this.pathHandler.handleExtendRouteFirstClick(gridPos, track);
    }

    buildExtendRoutePositionsSet() {
        return this.pathHandler.buildExtendRoutePositionsSet();
    }

    async handleExtendRouteSecondClick(gridPos, track) {
        return this.pathHandler.handleExtendRouteSecondClick(gridPos, track);
    }

    showExtendRoutePanel() {
        return this.pathHandler.showExtendRoutePanel();
    }

    getDirectionName(dir) {
        return this.pathHandler.getDirectionName(dir);
    }

    enterCancelRouteMode() {
        return this.pathHandler.enterCancelRouteMode();
    }

    exitCancelRouteMode() {
        return this.pathHandler.exitCancelRouteMode();
    }

    async handleCancelRouteToolClick(gridPos) {
        return this.pathHandler.handleCancelRouteToolClick(gridPos);
    }

    // === Train Routes Panel Methods ===

    async showRoutesPanel() {
        return this.routeHandler.showRoutesPanel();
    }

    // === Tunnel Tool ===

    async handleTunnelClick(gridPos, existingTrack) {
        const deadEndPorts = Array.isArray(existingTrack.dead_end_ports)
            ? existingTrack.dead_end_ports.map(p => String(p).toUpperCase())
            : [];
        if (deadEndPorts.length === 0) {
            window.showToast(
                'Tunnel: clicked tile has no dead-end edge. Tunnels can only pair dead-ends.',
                'error',
                5000
            );
            return;
        }

        // v1 picks the first dead-end edge if multiple exist on the same tile.
        // Sorted so the choice is deterministic and obvious.
        const edge = deadEndPorts.slice().sort()[0];
        const endpoint = { x: gridPos.x, y: gridPos.y, edge };

        if (!this._pendingTunnelEndpoint) {
            this._pendingTunnelEndpoint = endpoint;
            this.renderer.setPendingTunnelEndpoint(endpoint);
            window.showToast(
                `Tunnel: first endpoint (${endpoint.x}, ${endpoint.y}).${endpoint.edge} selected. Click another dead-end to pair.`,
                'info',
                4000
            );
            return;
        }

        const first = this._pendingTunnelEndpoint;
        this._pendingTunnelEndpoint = null;
        this.renderer.setPendingTunnelEndpoint(null);
        try {
            await this.api.createTunnel(first, endpoint);
            window.showToast(
                `Tunnel created: (${first.x}, ${first.y}).${first.edge} ↔ (${endpoint.x}, ${endpoint.y}).${endpoint.edge}`,
                'success',
                4000
            );
            await this.refreshAndSelect(null);
        } catch (err) {
            window.showToast(`Failed to create tunnel: ${err.message}`, 'error', 6000);
        }
    }
}
