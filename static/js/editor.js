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
        
        // Diagonal track building mode (shift key)
        this.shiftKeyHeld = false;
        this.diagonalBuildMode = false;  // True when building diagonal/short-path tracks

        // Platform extend mode
        this.stationExtendMode = { active: false };
        
        // Block extend mode
        this.blockExtendMode = { active: false };

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

        this.pathfinder = new ConstructionPathfinder();
        this.existingOnlyPathfinder = new ExistingTrackPathfinder();
        this.blockHandler = new BlockEditorHandler(this);
        this.signalHandler = new SignalEditorHandler(this);
        this.platformHandler = new PlatformEditorHandler(this);
        this.stationGroupHandler = new StationGroupEditorHandler(this);
        this.trainHandler = new TrainEditorHandler(this);
        this.pathHandler = new PathEditorHandler(this);
        this.routeHandler = new RouteEditorHandler(this);
        this.switchHandler = new SwitchEditorHandler(this);
        this.uiHandler = new EditorUIHandler(this);
        
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
                // List and transient panels don't need IDs - always valid
                itemExists = true;
                break;
            case 'path':
            case 'route-editor':
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

    /**
     * Refresh the main properties panel if the selected element is at the given position.
     * Used after API calls that modify track data.
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     */
    async refreshPropertiesPanelIfNeeded(x, y) {
        if (this.selectedElement && this.selectedElement.x === x && this.selectedElement.y === y) {
            await this.selectElement({ x, y });
        }
    }

    async showBlockEditor(blockId) {
        return this.blockHandler.showEditor(blockId);
    }

    async initBlockEditorHandlers(blockId) {
        return this.blockHandler.initEditorHandlers(blockId);
    }

    showPlatformEditor(platformId) {
        return this.platformHandler.showEditor(platformId);
    }

    async showSwitchEditor(switchId) {
        return this.switchHandler.showEditor(switchId);
    }

    async initPlatformEditorHandlers(platformId) {
        return this.platformHandler.initEditorHandlers(platformId);
    }

    async showBlockAssignmentDialog(platformId, x, y, tileIndex) {
        return this.platformHandler.showBlockAssignmentDialog(platformId, x, y, tileIndex);
    }


    async enterPlatformExtendMode(platformId, direction) {
        return this.platformHandler.enterExtendMode(platformId, direction);
    }

    exitPlatformExtendMode() {
        return this.platformHandler.exitExtendMode();
    }

    async handlePlatformExtendClick(x, y) {
        return this.platformHandler.handleExtendClick(x, y);
    }

    async showStationsPanel() {
        return this.stationGroupHandler.showPanel();
    }

    async enterBlockExtendMode(blockId, direction) {
        return this.blockHandler.enterExtendMode(blockId, direction);
    }

    exitBlockExtendMode() {
        return this.blockHandler.exitExtendMode();
    }

    async handleBlockExtendClick(x, y) {
        return this.blockHandler.handleExtendClick(x, y);
    }

    async showTrainEditor(trainId) {
        return this.trainHandler.showTrainControlPanel(trainId);
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

    setupEventListeners() {
        // Tool selection
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
                e.currentTarget.classList.add('active');

                const newTool = e.currentTarget.dataset.tool;
                const oldTool = this.selectedTool;

                // Auto-toggle block debug when entering/leaving block tools
                const blockDebugCheckbox = document.getElementById('debug-show-blocks');
                const isBlockTool = ['block', 'extend-block'].includes(newTool);
                const wasBlockTool = ['block', 'extend-block'].includes(oldTool);

                if (isBlockTool && !wasBlockTool) {
                    // Entering block tool mode - enable block colors only (no debug info)
                    this._savedBlockDebugState = blockDebugCheckbox?.checked;
                    this.renderer.showBlockColorsOnly = true;
                    window.app.refreshNetwork(); // Refresh to show block colors
                } else if (!isBlockTool && wasBlockTool) {
                    // Leaving block tool mode - restore normal rendering
                    this.renderer.showBlockColorsOnly = false;
                    window.app.refreshNetwork(); // Refresh to update display
                }

                // Handle route tool special actions
                if (newTool === 'path') {
                    this.enterRouteToolMode();
                } else if (newTool === 'extend-path') {
                    this.enterExtendRouteMode();
                } else if (newTool === 'cancel-path') {
                    this.enterCancelRouteMode();
                } else if (newTool === 'paths-panel') {
                    this.showPathsPanel();
                    // Don't change selected tool - keep current tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'add-train') {
                    this.showAddTrainDialog();
                    // Don't change selected tool - keep select tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'train-panel') {
                    this.showTrainsPanel();
                    // Don't change selected tool - keep current tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'schedules-panel') {
                    this.showSchedulesPanel();
                    // Don't change selected tool - keep current tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'routes-panel') {
                    this.showRoutesPanel();
                    // Don't change selected tool - keep current tool active
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (newTool === 'stations-panel') {
                    this.showStationsPanel();
                    e.currentTarget.classList.remove('active');
                    document.querySelector(`.tool-btn[data-tool="${oldTool}"]`)?.classList.add('active');
                    return;
                } else if (oldTool === 'path') {
                    // Leaving path tool mode
                    this.exitRouteToolMode();
                } else if (oldTool === 'extend-path') {
                    // Leaving extend-path tool mode
                    this.exitExtendRouteMode();
                } else if (oldTool === 'cancel-path') {
                    // Leaving cancel-path tool mode
                    this.exitCancelRouteMode();
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
            // Left click with signal/platform = single click placement
            else if (e.button === 0 && ['signal', 'platform'].includes(this.selectedTool)) {
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
                
                this.renderer.panX += dx;
                this.renderer.panY += dy;
                // Redraw the full network, not just the grid
                if (this.renderer.currentNetwork) {
                    this.renderer.drawNetwork(this.renderer.currentNetwork);
                } else {
                    this.renderer.draw();
                }
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            }
            else if (this.isDraggingBuild && this.dragStart) {
                this.dragEnd = gridPos;
                this.updatePreviewPath();
            }
            // Hover preview for signal/platform tools
            else if (['signal', 'platform'].includes(this.selectedTool)) {
                this.updateHoverPreview(gridPos);
            }
        });

        // Mouse up - commit drag build
        canvas.addEventListener('mouseup', async (e) => {
            if (this.isPanning) {
                this.isPanning = false;
            }
            else if (this.isDraggingBuild && this.dragStart && this.dragEnd) {
                // Stop dragging state immediately to prevent updates during async operations
                this.isDraggingBuild = false;

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

                this.dragStart = null;
                this.dragEnd = null;
                this.previewPath = null;
            }
        });

        // Right click - context actions (rotate signal, toggle switch)
        canvas.addEventListener('contextmenu', async (e) => {
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const gridPos = this.renderer.screenToGrid(x, y);

            await this.handleRightClick(gridPos);
        });

        // Zoom with mouse-centered zooming and pan compensation
        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();

            // Get mouse position in screen coordinates
            const rect = canvas.getBoundingClientRect();
            const mouseScreenX = e.clientX - rect.left;
            const mouseScreenY = e.clientY - rect.top;

            // Calculate mouse position in world coordinates BEFORE zoom
            const worldPosBeforeZoom = this.renderer.screenToGrid(mouseScreenX, mouseScreenY);

            // Apply zoom
            const zoomIntensity = 0.1;
            const delta = e.deltaY < 0 ? 1 : -1;
            const oldScale = this.renderer.scale;
            const newScale = oldScale + (delta * zoomIntensity);

            // Clamp zoom level
            if (newScale <= 0.1 || newScale >= 5) return;

            // Update scale
            this.renderer.scale = newScale;

            // Calculate where the mouse world position would appear on screen with new scale
            // (without adjusting pan)
            const newTileSize = this.renderer.gridSize * newScale;
            const screenXWithoutPanAdjust = this.renderer.centerX + this.renderer.panX +
                ((worldPosBeforeZoom.x + worldPosBeforeZoom.y + 1) * newTileSize / 2);
            const screenYWithoutPanAdjust = this.renderer.centerY + this.renderer.panY +
                ((worldPosBeforeZoom.x - worldPosBeforeZoom.y) * newTileSize / 2);

            // Calculate pan adjustment to keep mouse world position under cursor
            const panAdjustX = mouseScreenX - screenXWithoutPanAdjust;
            const panAdjustY = mouseScreenY - screenYWithoutPanAdjust;

            // Apply pan adjustment
            this.renderer.panX += panAdjustX;
            this.renderer.panY += panAdjustY;

            // Redraw with cached network
            if (this.cachedNetwork) {
                this.renderer.drawNetwork(this.cachedNetwork);
            } else {
                this.renderer.draw();
            }
            document.getElementById('status-zoom').textContent = `${Math.round(newScale * 100)}%`;
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
            const gridPos = this.renderer.screenToGrid(x, y);
            
            // Check if in station extend mode
            if (this.stationExtendMode?.active) {
                const handled = await this.handlePlatformExtendClick(gridPos.x, gridPos.y);
                if (handled) return;
            }
            
            // Check if in block extend mode
            if (this.blockExtendMode?.active) {
                const handled = await this.handleBlockExtendClick(gridPos.x, gridPos.y);
                if (handled) return;
            }
            
            // Check if in path tool mode
            if (this.selectedTool === 'path') {
                await this.handleRouteToolClick(gridPos);
                return;
            }
            
            // Check if in extend-path tool mode
            if (this.selectedTool === 'extend-path') {
                await this.handleExtendRouteToolClick(gridPos);
                return;
            }
            
            // Check if in cancel-path tool mode
            if (this.selectedTool === 'cancel-path') {
                await this.handleCancelRouteToolClick(gridPos);
                return;
            }
            
            // Check if route editor is in waypoint click mode
            if (this.routeHandler.waypointClickMode) {
                const handled = await this.routeHandler.handleWaypointClick(gridPos);
                if (handled) return;
            }
            
            // Only handle selection with select tool
            if (this.selectedTool !== 'select') return;
            
            this.selectElement(gridPos);
        });

        // Global keyboard events for shift key (diagonal build mode)
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Shift' && !this.shiftKeyHeld) {
                this.shiftKeyHeld = true;
                this.diagonalBuildMode = true;
                // Update preview if currently dragging
                if (this.isDraggingBuild) {
                    this.updatePreviewPath();
                }
            }
        });

        document.addEventListener('keyup', (e) => {
            if (e.key === 'Shift') {
                this.shiftKeyHeld = false;
                this.diagonalBuildMode = false;
                // Update preview if currently dragging
                if (this.isDraggingBuild) {
                    this.updatePreviewPath();
                }
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
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                // Check if we're in a modal or input field
                const inModal = document.querySelector('.block-modal-overlay, .modal-overlay') !== null;
                const inInput = document.activeElement.tagName === 'INPUT' ||
                               document.activeElement.tagName === 'TEXTAREA' ||
                               document.activeElement.tagName === 'SELECT';

                if (inModal || inInput) {
                    return; // Let modal/input handlers deal with it
                }

                // Exit station extend mode if active
                if (this.stationExtendMode?.active) {
                    this.exitPlatformExtendMode();
                    window.showToast('Extend mode cancelled', 'info', 3000);
                    return;
                }

                // Exit block extend mode if active
                if (this.blockExtendMode?.active) {
                    this.exitBlockExtendMode();
                    window.showToast('Extend mode cancelled', 'info', 3000);
                    return;
                }

                // Exit path tool mode if active
                if (this.pathToolState?.active) {
                    this.exitRouteToolMode();
                    window.showToast('Route creation cancelled', 'info', 3000);
                    return;
                }

                // Exit route editor waypoint click mode if active
                if (this.routeHandler.waypointClickMode) {
                    this.routeHandler.exitWaypointClickMode();
                    window.showToast('Waypoint selection cancelled', 'info', 3000);
                    return;
                }

                // Switch to select tool
                if (this.selectedTool !== 'select') {
                    const selectBtn = document.querySelector('.tool-btn[data-tool="select"]');
                    if (selectBtn) {
                        selectBtn.click(); // This triggers the existing tool switch logic
                    }
                }
            }
        });
    }

    setupBlockManager() {
        this.blockHandler.setup();
    }

    async refreshBlocksList() {
        return this.blockHandler.refreshList();
    }

    selectBlock(blockId) {
        return this.blockHandler.select(blockId);
    }

    async editBlockDialog(blockId) {
        return this.blockHandler.editDialog(blockId);
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

    showBlockPropertiesModal(options) {
        return this.blockHandler.showPropertiesModal(options);
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

        if (this.selectedTool === 'track') {
            // Use pathfinder for track placement only
            const existingTracks = this.cachedNetwork ? this.cachedNetwork.tracks : [];
            const foundPath = this.pathfinder.findPath(
                this.dragStart.x, this.dragStart.y,
                this.dragEnd.x, this.dragEnd.y,
                existingTracks
            );

            // If path found, use it and cache it; otherwise use last valid path
            if (foundPath) {
                this.previewPath = foundPath;
                // Use diagonal tracks if shift key is held
                this.previewTracks = this.diagonalBuildMode
                    ? pathToDiagonalTracks(this.previewPath, this.cachedNetwork)
                    : pathToTracks(this.previewPath, this.cachedNetwork);
                // Cache this valid path
                this.lastValidPreviewPath = this.previewPath;
                this.lastValidPreviewTracks = this.previewTracks;
            } else {
                // Destination blocked - use last valid path
                this.previewPath = this.lastValidPreviewPath;
                this.previewTracks = this.lastValidPreviewTracks;
            }
        }
        else if (['block', 'extend-block', 'extend-platform', 'eraser', 'remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool)) {
            // Use existing-only pathfinder for block assignment, extend-block, extend-platform, eraser, and removal tools
            const existingTracks = this.cachedNetwork ? this.cachedNetwork.tracks : [];
            const foundPath = this.existingOnlyPathfinder.findPathOnExistingTracks(
                this.dragStart.x, this.dragStart.y,
                this.dragEnd.x, this.dragEnd.y,
                existingTracks
            );

            if (foundPath) {
                this.previewPath = foundPath;
                this.lastValidPreviewPath = foundPath;
            } else {
                this.previewPath = this.lastValidPreviewPath;
            }
            this.previewTracks = null;
        }
        else {
            // Single placement for other tools
            this.previewPath = [this.dragEnd];
            this.previewTracks = null;
        }

        // Redraw with preview
        if (this.cachedNetwork) {
            this.renderer.drawNetwork(this.cachedNetwork);
            // Draw actual track geometry preview if available
            if (this.previewTracks && this.previewTracks.length > 0) {
                this.renderer.trackHandler.drawPreviewTracks(this.previewTracks);
            }
            // Draw eraser and removal tools preview in red
            else if (['eraser', 'remove-platform-tile', 'remove-block-tile'].includes(this.selectedTool) && this.previewPath) {
                this.renderer.trackHandler.drawEraserPreviewPath(this.previewPath);
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
                // Use diagonal tracks if shift key is held
                const tracks = this.diagonalBuildMode
                    ? pathToDiagonalTracks(this.previewPath, this.cachedNetwork)
                    : pathToTracks(this.previewPath, this.cachedNetwork);
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
                            max_speed_kmh: result.maxSpeed
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
                // Batch delete all tracks in the path
                if (!this.previewPath || this.previewPath.length === 0) return;

                const positions = this.previewPath.map(p => [p.x, p.y]);
                await this.api.deleteTracksBulk(positions);
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

    // Single click placement for signal/platform tools
    async handleSingleClickPlace(gridPos) {
        const existingTrack = this.cachedNetwork?.tracks?.find(
            t => t.x === gridPos.x && t.y === gridPos.y
        );

        if (!existingTrack) {
            window.showToast(`Cannot place ${this.selectedTool}: No track at this position`, 'error', 5000);
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
        this.renderer.drawNetwork(this.cachedNetwork);

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
        // Check if we're in path tool mode - cancel path creation
        if (this.selectedTool === 'path' && this.pathToolState?.active) {
            this.resetRouteToolState();
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
            else if (track.block_section_id) {
                // Right-click cycles block state: FREE → RESERVED → OCCUPIED → CAUTION → FREE
                // Backend validates route reservations and rejects if block is locked by a route
                try {
                    await this.api.cycleBlockState(track.block_section_id);
                } catch (blockErr) {
                    // Parse error message from API response
                    const errMsg = this._parseApiError(blockErr, 'Cannot change block state');
                    window.showToast(errMsg, 'error', 5000);
                }
                await window.app.refreshNetwork();
                // Refresh properties panel to show updated block state
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
        
        // Check if forbidden
        const isForbidden = FORBIDDEN_TRACK_BITS.has(trackBits);
        
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
                const switchStateColor = track.switch_state === 0 ? '#04d361' : '#ffd93d';
                const switchStateText = track.switch_state === 0 ? 'MAIN' : 'DIVERGE';
                const isLocked = track.is_locked || track.locked_by_path;
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
                        signal.signal_state === 'green' || signal.signal_state === 'GREEN' ? '#04d361' :
                        (signal.signal_state === 'yellow' || signal.signal_state === 'YELLOW' ? '#ffd93d' : '#e83f5b')
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
                const stateColor = block && block.state === 'free' ? '#04d361' :
                                   block && block.state === 'reserved' ? '#ffd93d' :
                                   block && block.state === 'occupied' ? '#e83f5b' :
                                   block && block.state === 'caution' ? '#ff9500' : '#888';
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
                        // Check new TrainPosition format: occupied_tiles array
                        const pos = t.position;
                        if (pos && pos.occupied_tiles) {
                            return pos.occupied_tiles.some(tile => 
                                tile[0] === track.x && tile[1] === track.y
                            );
                        }
                        // Fallback: check head tile from end_a/end_b
                        if (pos && pos.end_a && pos.end_b) {
                            const headEnd = pos.leading_end === 'a' ? pos.end_a : pos.end_b;
                            if (headEnd.tile) {
                                return headEnd.tile[0] === track.x && headEnd.tile[1] === track.y;
                            }
                        }
                        return false;
                    })
                    : [];
                if (trainsAtTile.length === 0) return '';
                return `
                <div class="property-group-header">Train</div>
                ${trainsAtTile.map(train => `
                <div class="property-item-row">
                    <div class="train-summary-row">
                        <div class="train-status-dot" style="background-color: ${
                            train.state === 'running_moving' ? '#04d361' :
                            train.state === 'running_waiting' ? '#ffd93d' :
                            train.state === 'running_at_station' ? '#00bfff' :
                            train.state === 'manual' ? '#ff9500' :
                            train.state === 'idle' ? '#888' : '#e83f5b'
                        }"></div>
                        <div class="train-info">
                            <span class="train-id">${train.id}</span>
                            <span class="train-state">${train.state || 'unknown'}</span>
                        </div>
                        <button class="edit-btn edit-train-btn" data-train-id="${train.id}">Edit</button>
                    </div>
                </div>
                `).join('')}
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

            <!-- Combined Bit Editor (33%) and Visual Editor (67%) Side-by-Side -->
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
            
            <!-- View Full JSON Button -->
            <button id="view-full-json-btn" class="btn btn-primary btn-full text-sm mt-12">
                📄 View Full JSON
            </button>
        `;
        
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

    // Initialize train summary edit button handler
    initTrainEditor(track) {
        // Find all trains at this tile
        const trainsAtTile = this.cachedNetwork && this.cachedNetwork.trains
            ? this.cachedNetwork.trains.filter(t => {
                // Check new TrainPosition format: occupied_tiles array
                const pos = t.position;
                if (pos && pos.occupied_tiles) {
                    return pos.occupied_tiles.some(tile => 
                        tile[0] === track.x && tile[1] === track.y
                    );
                }
                // Fallback: check head tile from end_a/end_b
                if (pos && pos.end_a && pos.end_b) {
                    const headEnd = pos.leading_end === 'a' ? pos.end_a : pos.end_b;
                    if (headEnd.tile) {
                        return headEnd.tile[0] === track.x && headEnd.tile[1] === track.y;
                    }
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

    exitRouteToolMode() {
        return this.pathHandler.exitRouteToolMode();
    }

    resetRouteToolState() {
        return this.pathHandler.resetRouteToolState();
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

    exitExtendRouteMode() {
        return this.pathHandler.exitExtendRouteMode();
    }

    resetExtendRouteState() {
        return this.pathHandler.resetExtendRouteState();
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
}
