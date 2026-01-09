"""
JavaScript Syntax Validation Tests

These tests ensure that all JavaScript files have valid syntax and can be loaded
without errors. This catches issues before they reach the browser.

Critical: These tests MUST pass before any commit.
"""

import subprocess
import pytest
from pathlib import Path


# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent
JS_DIR = PROJECT_ROOT / "static" / "js"


def get_all_js_files():
    """Get all JavaScript files in the static/js directory"""
    return list(JS_DIR.glob("*.js"))


class TestJavaScriptSyntax:
    """Test JavaScript files for syntax errors"""

    @pytest.mark.parametrize("js_file", get_all_js_files(), ids=lambda p: p.name)
    def test_javascript_syntax_valid(self, js_file):
        """Test that JavaScript file has valid syntax using Node.js"""
        result = subprocess.run(
            ["node", "--check", str(js_file)], capture_output=True, text=True
        )

        assert result.returncode == 0, (
            f"Syntax error in {js_file.name}:\n{result.stderr}"
        )

    def test_all_required_js_files_exist(self):
        """Test that all required JavaScript files exist"""
        required_files = ["api.js", "renderer.js", "editor.js", "app.js"]

        for filename in required_files:
            file_path = JS_DIR / filename
            assert file_path.exists(), f"Required file {filename} is missing"

    def test_api_js_has_api_class(self):
        """Test that api.js defines the API class"""
        api_file = JS_DIR / "api.js"
        content = api_file.read_text()

        assert "class API" in content, "api.js must define API class"
        assert "window.api = new API()" in content, "api.js must export API instance"

    def test_editor_js_has_editor_class(self):
        """Test that editor.js defines the Editor class"""
        editor_file = JS_DIR / "editor.js"
        content = editor_file.read_text()

        assert "class Editor" in content, "editor.js must define Editor class"

    def test_editor_js_has_helper_class(self):
        """Test that helpers.js defines the required helper functions"""
        helpers_file = JS_DIR / "helpers.js"
        content = helpers_file.read_text()

        # Check for key helper functions
        required_functions = [
            "function renderHeaderId",
            "function renderPropertyItem",
            "function renderTextInput",
            "function renderNumberInput",
            "function renderBitToggle",
        ]

        for func in required_functions:
            assert func in content, f"helpers.js must define {func} function"

    def test_renderer_js_has_renderer_class(self):
        """Test that renderer.js defines the Renderer class"""
        renderer_file = JS_DIR / "renderer.js"
        content = renderer_file.read_text()

        assert "class Renderer" in content, "renderer.js must define Renderer class"

    def test_app_js_has_app_class(self):
        """Test that app.js defines the App class"""
        app_file = JS_DIR / "app.js"
        content = app_file.read_text()

        assert "class App" in content, "app.js must define App class"

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
