class App {
    constructor() {
        this.renderer = new Renderer('rail-canvas');
        this.editor = new Editor(this.renderer, window.api);
        this.simulationInterval = null;

        this.setupControls();
        this.refreshNetwork();

        // Start polling loop
        this.startPolling();
    }

    setupControls() {
        document.getElementById('btn-play').addEventListener('click', () => {
            window.api.controlSimulation('start');
        });

        document.getElementById('btn-pause').addEventListener('click', () => {
            window.api.controlSimulation('stop');
        });

        document.getElementById('btn-reset').addEventListener('click', () => {
            window.api.controlSimulation('reset');
            this.refreshNetwork();
        });

        document.getElementById('speed-slider').addEventListener('input', (e) => {
            const speed = parseFloat(e.target.value);
            document.getElementById('speed-value').textContent = `${speed}x`;
            window.api.controlSimulation('set_speed', speed);
        });

        document.getElementById('mode-edit').addEventListener('click', () => {
            document.getElementById('mode-edit').classList.add('active');
            document.getElementById('mode-sim').classList.remove('active');
            // Enable editor tools
        });

        document.getElementById('mode-sim').addEventListener('click', () => {
            document.getElementById('mode-sim').classList.add('active');
            document.getElementById('mode-edit').classList.remove('active');
            // Disable editor tools
        });
    }

    async refreshNetwork() {
        try {
            const network = await window.api.getNetwork();
            this.renderer.drawNetwork(network);
        } catch (e) {
            console.error('Failed to refresh network:', e);
        }
    }

    startPolling() {
        setInterval(async () => {
            await this.refreshNetwork();
        }, 500); // Poll every 500ms
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
