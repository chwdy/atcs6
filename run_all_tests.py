#!/usr/bin/env python3
"""
Run all test suites for the Railway Management System
This script runs both Python backend tests and JavaScript API tests
"""

import subprocess
import sys
import requests
from typing import Tuple


def print_header(text: str, char: str = "="):
    """Print a formatted header"""
    line = char * 72
    print(f"\n{line}")
    print(text)
    print(f"{line}\n")


def check_server_running(url: str = "http://localhost:5001/api/network") -> bool:
    """Check if the server is running"""
    try:
        response = requests.get(url, timeout=2)
        return response.status_code == 200
    except:
        return False


def run_python_tests() -> Tuple[bool, str]:
    """Run Python backend tests using pytest"""
    print_header("Part 1: Python Backend Tests")

    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/test_backend.py", "-v"],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            print("✅ Python tests PASSED")
            return True, result.stdout
        else:
            print("❌ Python tests FAILED")
            return False, result.stdout + "\n" + result.stderr

    except subprocess.TimeoutExpired:
        msg = "❌ Python tests TIMEOUT (exceeded 120 seconds)"
        print(msg)
        return False, msg

    except FileNotFoundError:
        msg = "⚠️  pytest not found. Install with: pip install pytest"
        print(msg)
        return False, msg

    except Exception as e:
        msg = f"⚠️  Error running Python tests: {e}"
        print(msg)
        return False, msg


def run_javascript_tests() -> Tuple[bool, str]:
    """Run JavaScript API tests using node"""
    print_header("Part 2: JavaScript API Tests")

    # Check if server is running
    if not check_server_running():
        msg = """⚠️  Server not running at localhost:5001

JavaScript API tests require the server to be running.

To run JavaScript tests:
  1. Start server: python3 server.py
  2. In another terminal: node tests/test_api.js

Skipping JavaScript tests..."""
        print(msg)
        return None, msg  # None indicates skipped, not failed

    print("✓ Server is running at localhost:5001")
    print("Running JavaScript API tests...\n")

    try:
        result = subprocess.run(
            ["node", "tests/test_api.js"],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            print("✅ JavaScript API tests PASSED")
            return True, result.stdout
        else:
            print("❌ JavaScript API tests FAILED")
            return False, result.stdout + "\n" + result.stderr

    except subprocess.TimeoutExpired:
        msg = "❌ JavaScript tests TIMEOUT (exceeded 120 seconds)"
        print(msg)
        return False, msg

    except FileNotFoundError:
        msg = "⚠️  node not found. Install Node.js to run JavaScript tests."
        print(msg)
        return False, msg

    except Exception as e:
        msg = f"⚠️  Error running JavaScript tests: {e}"
        print(msg)
        return False, msg


def main():
    """Run all test suites"""
    print_header("Railway Management System - Full Test Suite")

    # Run Python tests
    python_passed, python_output = run_python_tests()

    # Run JavaScript tests
    js_result, js_output = run_javascript_tests()

    # Print summary
    print_header("Test Suite Summary")

    results = []
    if python_passed:
        results.append("✅ Python tests: PASSED")
    else:
        results.append("❌ Python tests: FAILED")

    if js_result is None:
        results.append("⏭️  JavaScript tests: SKIPPED (server not running)")
    elif js_result:
        results.append("✅ JavaScript tests: PASSED")
    else:
        results.append("❌ JavaScript tests: FAILED")

    for result in results:
        print(result)

    # Determine overall status
    # Only fail if tests that ran actually failed (not skipped)
    overall_pass = python_passed and (js_result is None or js_result)

    print()
    if overall_pass:
        print("✅ ALL TESTS PASSED (or skipped)")
        return 0
    else:
        print("❌ SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Test run interrupted by user")
        sys.exit(130)
