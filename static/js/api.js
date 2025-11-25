class API {
    constructor(baseUrl = 'http://localhost:5001/api') {
        this.baseUrl = baseUrl;
    }

    async getNetwork() {
        const response = await fetch(`${this.baseUrl}/network`);
        return await response.json();
    }

    async updateNetwork(networkData) {
        const response = await fetch(`${this.baseUrl}/network/update`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(networkData)
        });
        return await response.json();
    }

    async createTrain(trainData) {
        const response = await fetch(`${this.baseUrl}/trains`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(trainData)
        });
        return await response.json();
    }

    async controlSimulation(action, speed = null) {
        const data = { action };
        if (speed !== null) {
            data.speed = speed;
        }

        const response = await fetch(`${this.baseUrl}/simulation/control`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(data)
        });
        return await response.json();
    }

    async getSimulationState() {
        const response = await fetch(`${this.baseUrl}/simulation/state`);
        return await response.json();
    }
}

// Export as global
window.api = new API();
