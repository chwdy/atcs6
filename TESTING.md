# Testing Documentation

## Overview

The Railway Management System includes comprehensive automated testing to prevent regressions and ensure code quality. Tests run automatically when starting the demo or server.

## Test Suites

### 1. Python Backend Tests (`tests/test_backend.py`)

**Coverage:**
- Track geometry and validation (42 valid track_bits configurations)
- Block state management and transitions
- Signal state calculation and placement validation
- Station orientation detection (NS, EW, NW_SE, NE_SW)
- Pathfinding algorithms
- Dispatcher routing
- Frontend-backend parity

**Run manually:**
```bash
pip install pytest
pytest tests/test_backend.py -v
```

### 2. JavaScript API Tests (`tests/test_api.js`)

**Coverage:**
- All 42 REST API endpoints
- Request/response validation
- Error handling
- State management via API
- Frontend-backend parity via HTTP

**Run manually:**
```bash
# Start server first
python3 server.py

# In another terminal
node tests/test_api.js
```

## Automated Testing

### When Starting the Demo

```bash
python3 demo_main.py
```

**What happens:**
1. ✅ Runs Python backend tests (if pytest installed)
2. ✅ Shows test results
3. ✅ Starts demo if tests pass
4. 🛑 Aborts if tests fail

### When Starting the Server

```bash
python3 server.py
```

**What happens:**
1. ✅ Runs block validation tests (always - no dependencies)
2. ✅ Runs full backend test suite (if pytest installed)
3. ✅ Initializes demo network if tests pass
4. ✅ Starts Flask server on port 5001
5. 🛑 Aborts if any tests fail

**Critical:** The server will NOT start if tests fail. This ensures you never deploy broken code!

## Running All Tests

### Option 1: Bash Script

```bash
./run_all_tests.sh
```

### Option 2: Python Script

```bash
python3 run_all_tests.py
```

Both scripts will:
1. Run Python backend tests
2. Check if server is running
3. Run JavaScript API tests (if server is running)
4. Report overall results

## Test Organization

```
tests/
├── test_backend.py    # Python backend unit tests
└── test_api.js        # JavaScript API integration tests
```

## Continuous Integration

For CI/CD pipelines, use:

```bash
# Install dependencies
pip install pytest

# Run backend tests only (no server required)
pytest tests/test_backend.py -v

# For full test suite (requires server in separate process)
python3 run_all_tests.py
```

## Test Philosophy

**Pre-flight Testing:**
- Tests run BEFORE starting the application
- Failures prevent startup
- Ensures no broken code reaches users

**Coverage:**
- Unit tests for core logic
- Integration tests for API endpoints
- Parity tests between frontend and backend

**No Regressions:**
- Every feature has tests
- Tests must pass to start server
- Broken code cannot be deployed

## Adding New Tests

### Python Backend Test

Add to `tests/test_backend.py`:

```python
class TestMyFeature:
    def test_my_functionality(self):
        # Arrange
        network = RailwayNetwork()

        # Act
        result = network.my_function()

        # Assert
        assert result == expected_value
```

### JavaScript API Test

Add to `tests/test_api.js`:

```javascript
async function test_my_endpoint() {
    await resetNetwork();

    const result = await fetchJSON('/my-endpoint', {
        method: 'POST',
        body: JSON.stringify({ data: 'test' })
    });

    assert(result.ok, 'Endpoint should succeed');
    assert(result.data.field === 'expected', 'Field should match');
}

// Add to runAllTests()
await runTest('My endpoint test', test_my_endpoint);
```

## Troubleshooting

### "pytest not found"

```bash
pip install pytest
```

### "Server not running" (for JavaScript tests)

```bash
# Terminal 1: Start server
python3 server.py

# Terminal 2: Run tests
node tests/test_api.js
```

### Tests timeout

Increase timeout in test configuration:
- Python: `timeout=120` in subprocess.run()
- JavaScript: Default is 120 seconds

### Tests fail on startup

This is intentional! Fix the failing tests before proceeding. The system is protecting you from deploying broken code.

## Benefits

✅ **No Regressions:** Tests catch breaking changes immediately
✅ **Quality Assurance:** Server won't start with broken code
✅ **Developer Confidence:** Make changes knowing tests will catch issues
✅ **Documentation:** Tests serve as examples of how to use the API
✅ **CI/CD Ready:** Automated testing suitable for continuous integration

## Summary

The Railway Management System uses comprehensive automated testing to ensure quality and prevent regressions. Tests run automatically on startup, preventing broken code from reaching production. This "fail-fast" approach saves time and maintains code quality.
