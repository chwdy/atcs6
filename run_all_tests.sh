#!/bin/bash
# Run all test suites for the Railway Management System
# This script runs both Python backend tests and JavaScript API tests

set -e  # Exit on first error

echo "========================================================================"
echo "Railway Management System - Full Test Suite"
echo "========================================================================"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track overall status
OVERALL_STATUS=0

# ============================================================================
# Part 1: Python Backend Tests
# ============================================================================
echo "========================================================================"
echo "Part 1: Python Backend Tests"
echo "========================================================================"
echo ""

if command -v pytest &> /dev/null; then
    echo "Running pytest..."
    if pytest tests/test_backend.py -v; then
        echo -e "${GREEN}✅ Python tests PASSED${NC}"
    else
        echo -e "${RED}❌ Python tests FAILED${NC}"
        OVERALL_STATUS=1
    fi
else
    echo -e "${YELLOW}⚠️  pytest not found. Trying python3 -m pytest...${NC}"
    if python3 -m pytest tests/test_backend.py -v 2>/dev/null; then
        echo -e "${GREEN}✅ Python tests PASSED${NC}"
    else
        echo -e "${RED}❌ pytest not installed or tests failed${NC}"
        echo "Install with: pip install pytest"
        OVERALL_STATUS=1
    fi
fi

echo ""

# ============================================================================
# Part 2: JavaScript API Tests (requires server)
# ============================================================================
echo "========================================================================"
echo "Part 2: JavaScript API Tests"
echo "========================================================================"
echo ""

# Check if server is running
if curl -s http://localhost:5001/api/network > /dev/null 2>&1; then
    echo "✓ Server is running at localhost:5001"
    echo "Running JavaScript API tests..."
    echo ""

    if node tests/test_api.js; then
        echo -e "${GREEN}✅ JavaScript API tests PASSED${NC}"
    else
        echo -e "${RED}❌ JavaScript API tests FAILED${NC}"
        OVERALL_STATUS=1
    fi
else
    echo -e "${YELLOW}⚠️  Server not running at localhost:5001${NC}"
    echo "JavaScript API tests require the server to be running."
    echo ""
    echo "To run JavaScript tests:"
    echo "  1. Start server: python3 server.py"
    echo "  2. In another terminal: node tests/test_api.js"
    echo ""
    echo "Skipping JavaScript tests..."
fi

echo ""

# ============================================================================
# Summary
# ============================================================================
echo "========================================================================"
echo "Test Suite Summary"
echo "========================================================================"
if [ $OVERALL_STATUS -eq 0 ]; then
    echo -e "${GREEN}✅ ALL TESTS PASSED${NC}"
else
    echo -e "${RED}❌ SOME TESTS FAILED${NC}"
fi
echo ""

exit $OVERALL_STATUS
