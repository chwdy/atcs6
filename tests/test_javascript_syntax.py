"""
JavaScript Syntax Validation Tests

These tests ensure that all JavaScript files have valid syntax and can be loaded
without errors. This catches issues before they reach the browser.

Critical: These tests MUST pass before any commit.
"""

import os
import shutil
import subprocess
import pytest
from pathlib import Path


# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent
JS_DIR = PROJECT_ROOT / "static" / "js"

# Node.js major version required for ES2020+ syntax
_NODE_MAJOR = "22"


def _resolve_node_path() -> str:
    """Resolve a Node.js binary that satisfies the required major version.

    Resolution order:
    1. nvm: source nvm.sh, use 'nvm which <major>' to get the exact path.
       If the version is not installed, run 'nvm install <major>' first.
    2. System node (PATH): used as a fallback when nvm is not present.

    Returns:
        Absolute path to the node binary.

    Raises:
        RuntimeError: If no suitable Node.js binary can be found or installed.
    """
    nvm_sh = Path(os.path.expanduser("~/.nvm/nvm.sh"))

    if nvm_sh.exists():
        # Ask nvm for the path of the installed version (fast, no subshell overhead)
        result = subprocess.run(
            ["bash", "-c", f"source {nvm_sh} && nvm which {_NODE_MAJOR}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            path = result.stdout.strip()
            if path and Path(path).exists():
                return path

        # Not installed — install it now
        print(
            f"\n[test_javascript_syntax] Node {_NODE_MAJOR} not found via nvm, installing..."
        )
        install = subprocess.run(
            ["bash", "-c", f"source {nvm_sh} && nvm install {_NODE_MAJOR}"],
            capture_output=False,  # show output so the user can see progress
            text=True,
        )
        if install.returncode != 0:
            raise RuntimeError(
                f"nvm install {_NODE_MAJOR} failed (exit {install.returncode})"
            )

        # Try nvm which again after install
        result = subprocess.run(
            ["bash", "-c", f"source {nvm_sh} && nvm which {_NODE_MAJOR}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            path = result.stdout.strip()
            if path and Path(path).exists():
                return path

        raise RuntimeError(
            f"nvm install {_NODE_MAJOR} succeeded but 'nvm which {_NODE_MAJOR}' still failed"
        )

    # nvm not available — fall back to whatever node is on PATH
    system_node = shutil.which("node")
    if system_node:
        return system_node

    raise RuntimeError(
        f"Node.js {_NODE_MAJOR} not found. "
        "Install nvm (https://github.com/nvm-sh/nvm) or install Node.js manually."
    )


# Resolved once at import time so pytest parametrize runs cleanly
NODE_PATH = _resolve_node_path()


def get_all_js_files():
    """Get all JavaScript files in the static/js directory"""
    return list(JS_DIR.glob("*.js"))


class TestJavaScriptSyntax:
    """Test JavaScript files for syntax errors"""

    @pytest.mark.parametrize("js_file", get_all_js_files(), ids=lambda p: p.name)
    def test_javascript_syntax_valid(self, js_file):
        """Test that JavaScript file has valid syntax using Node.js"""
        result = subprocess.run(
            [NODE_PATH, "--check", str(js_file)], capture_output=True, text=True
        )

        assert result.returncode == 0, (
            f"Syntax error in {js_file.name}:\n{result.stderr}"
        )

    def test_all_required_js_files_exist(self):
        """Test that all required JavaScript files exist"""
        required_files = ["editor.js", "app.js"]

        for filename in required_files:
            file_path = JS_DIR / filename
            assert file_path.exists(), f"Required file {filename} is missing"

    def test_api_js_has_api_class(self):
        """Test that the migrated api module defines the API class"""
        api_file = PROJECT_ROOT / "frontend" / "src" / "lib" / "api.ts"
        content = api_file.read_text()

        assert "class API" in content, "api.ts must define API class"
        assert "window.api = new API()" in content, "api.ts must export API instance"

    def test_editor_js_has_editor_class(self):
        """Test that editor.js defines the Editor class"""
        editor_file = JS_DIR / "editor.js"
        content = editor_file.read_text()

        assert "class Editor" in content, "editor.js must define Editor class"

    def test_editor_js_has_helper_class(self):
        """Test that the migrated helpers module defines the required helper functions"""
        helpers_file = PROJECT_ROOT / "frontend" / "src" / "lib" / "helpers.ts"
        content = helpers_file.read_text()

        # Check for key helper functions
        required_functions = [
            "function renderHeaderId",
            "function renderPropertyItem",
            "function renderBitToggle",
        ]

        for func in required_functions:
            assert func in content, f"helpers.js must define {func} function"

    def test_renderer_js_has_renderer_class(self):
        """Test that the migrated renderer module defines the Renderer class"""
        renderer_file = PROJECT_ROOT / "frontend" / "src" / "renderer" / "renderer.ts"
        content = renderer_file.read_text()

        assert "class Renderer" in content, "renderer.ts must define Renderer class"

    def test_app_js_has_app_class(self):
        """Test that app.js defines the App class"""
        app_file = JS_DIR / "app.js"
        content = app_file.read_text()

        assert "class App" in content, "app.js must define App class"

    def test_editor_trains_waits_for_ato_stop(self):
        """ATO stop should wait for train to fully stop before refresh."""
        editor_trains_file = JS_DIR / "editor-trains.js"
        content = editor_trains_file.read_text()

        assert "waitForTrainStopped" in content, (
            "editor-trains.js must define waitForTrainStopped helper"
        )
        assert "await waitForTrainStopped()" in content, (
            "ATO Stop should wait for the train to stop before refresh"
        )

    def test_editor_trains_updates_cab_buttons_on_live_update(self):
        """Train control panel should update cab buttons during live updates."""
        editor_trains_file = JS_DIR / "editor-trains.js"
        content = editor_trains_file.read_text()

        assert "updateControlPanelValuesFromData" in content, (
            "editor-trains.js must define updateControlPanelValuesFromData"
        )
        assert "updateCabButtons" in content, (
            "Train control live update should include updateCabButtons helper"
        )

    def test_no_duplicate_code_blocks(self):
        """Test that JavaScript files don't have obvious duplicate code blocks"""
        for js_file in get_all_js_files():
            content = js_file.read_text()
            lines = content.split("\n")

            # Check for duplicate "return result;" followed by "} catch (e) {" patterns
            # This was the exact bug that broke the app
            for i in range(len(lines) - 3):
                if (
                    lines[i].strip() == "return result;"
                    and lines[i + 1].strip() == "} catch (e) {"
                    and i > 0
                ):
                    # Check if there's another "return result;" within 20 lines before
                    window_start = max(0, i - 20)
                    window = lines[window_start:i]
                    if any("return result;" in line for line in window):
                        # Found potential duplicate - check if it's in a complete try-catch
                        prev_content = "\n".join(lines[window_start:i])
                        # If we see "} catch (e) {" without a matching try, it's likely duplicate
                        if prev_content.count("} catch") > prev_content.count("try {"):
                            pytest.fail(
                                f"Potential duplicate code block in {js_file.name} "
                                f"around line {i + 1}"
                            )
