class Editor {
    constructor(renderer, api) {
        this.renderer = renderer;
        this.api = api;
        this.selectedTool = 'select';
        this.isDragging = false;
        this.lastMouseX = 0;
        this.lastMouseY = 0;

        this.setupEventListeners();
    }

    setupEventListeners() {
        // Tool selection
        document.querySelectorAll('.tool-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
                e.currentTarget.classList.add('active');
                this.selectedTool = e.currentTarget.dataset.tool;
                console.log('Selected tool:', this.selectedTool);
            });
        });

        // Canvas interactions
        const canvas = this.renderer.canvas;

        canvas.addEventListener('mousedown', (e) => {
            if (e.button === 1 || (e.button === 0 && this.selectedTool === 'select')) {
                this.isDragging = true;
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            } else if (e.button === 0) {
                this.handleCanvasClick(e);
            }
        });

        canvas.addEventListener('mousemove', (e) => {
            if (this.isDragging) {
                const dx = e.clientX - this.lastMouseX;
                const dy = e.clientY - this.lastMouseY;
                this.renderer.offsetX += dx;
                this.renderer.offsetY += dy;
                this.renderer.draw();
                this.lastMouseX = e.clientX;
                this.lastMouseY = e.clientY;
            } else {
                // Highlight grid cell?
                const rect = canvas.getBoundingClientRect();
                const x = e.clientX - rect.left;
                const y = e.clientY - rect.top;
                const gridPos = this.renderer.screenToGrid(x, y);
                document.getElementById('status-coords').textContent = `${gridPos.x}, ${gridPos.y}`;
            }
        });

        canvas.addEventListener('mouseup', () => {
            this.isDragging = false;
        });

        canvas.addEventListener('wheel', (e) => {
            e.preventDefault();
            const zoomIntensity = 0.1;
            const delta = e.deltaY < 0 ? 1 : -1;
            const newScale = this.renderer.scale + (delta * zoomIntensity);

            if (newScale > 0.1 && newScale < 5) {
                this.renderer.scale = newScale;
                this.renderer.draw();
                document.getElementById('status-zoom').textContent = `${Math.round(newScale * 100)}%`;
            }
        });
    }

    async handleCanvasClick(e) {
        const rect = this.renderer.canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const gridPos = this.renderer.screenToGrid(x, y);

        console.log('Click at grid:', gridPos);

        // Fetch current network to modify it
        // In a real app, we'd maintain local state and sync
        const network = await this.api.getNetwork();

        if (this.selectedTool === 'eraser') {
            // Remove track at pos
            network.tracks = network.tracks.filter(t => t.x !== gridPos.x || t.y !== gridPos.y);
        } else if (this.selectedTool === 'track') {
            // Add straight track
            // Check if exists
            const exists = network.tracks.find(t => t.x === gridPos.x && t.y === gridPos.y);
            if (!exists) {
                network.tracks.push({
                    x: gridPos.x,
                    y: gridPos.y,
                    type: 'STRAIGHT',
                    connections: {
                        'WEST': ['EAST'],
                        'EAST': ['WEST']
                    }
                });
            }
        } else if (this.selectedTool === 'curve') {
            const exists = network.tracks.find(t => t.x === gridPos.x && t.y === gridPos.y);
            if (!exists) {
                network.tracks.push({
                    x: gridPos.x,
                    y: gridPos.y,
                    type: 'CURVE',
                    connections: {
                        'SOUTH': ['EAST'],
                        'EAST': ['SOUTH']
                    }
                });
            }
        } else if (this.selectedTool === 'switch') {
            const exists = network.tracks.find(t => t.x === gridPos.x && t.y === gridPos.y);
            if (!exists) {
                network.tracks.push({
                    x: gridPos.x,
                    y: gridPos.y,
                    type: 'SWITCH',
                    switch_position: 'MAIN',
                    connections: {
                        'WEST': ['EAST', 'SOUTH'],
                        'EAST': ['WEST'],
                        'SOUTH': ['WEST']
                    }
                });
            }
        } else if (this.selectedTool === 'signal') {
            const exists = network.tracks.find(t => t.x === gridPos.x && t.y === gridPos.y);
            if (!exists) {
                network.tracks.push({
                    x: gridPos.x,
                    y: gridPos.y,
                    type: 'SIGNAL',
                    signal_state: 'RED',
                    signal_direction: 'EAST',
                    connections: {
                        'WEST': ['EAST'],
                        'EAST': ['WEST']
                    }
                });
            }
        } else if (this.selectedTool === 'station') {
            const exists = network.tracks.find(t => t.x === gridPos.x && t.y === gridPos.y);
            if (!exists) {
                network.tracks.push({
                    x: gridPos.x,
                    y: gridPos.y,
                    type: 'STATION',
                    station_name: `Station ${gridPos.x}`,
                    connections: {
                        'WEST': ['EAST'],
                        'EAST': ['WEST']
                    }
                });
            }
        }

        // Update backend
        await this.api.updateNetwork(network);

        // Refresh view
        window.app.refreshNetwork();
    }
}
