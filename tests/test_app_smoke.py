"""
App Smoke Tests

These tests verify that critical application files exist and have valid content.
This catches basic issues before attempting to run the server.

Critical: These tests MUST pass before any commit.
"""

import pytest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


class TestAppSmoke:
    """Smoke tests for the application"""

    def test_static_directory_exists(self):
        """Test that the static directory exists"""
        static_dir = PROJECT_ROOT / "static"
        assert static_dir.exists(), "static/ directory is missing"
        assert static_dir.is_dir(), "static/ is not a directory"

    def test_index_html_exists(self):
        """Test that index.html exists and is not empty"""
        index_file = PROJECT_ROOT / "static" / "index.html"
        assert index_file.exists(), "static/index.html is missing"

        content = index_file.read_text()
        assert len(content) > 0, "index.html is empty"
        assert "<html" in content.lower(), "index.html doesn't look like HTML"

    def test_all_required_javascript_files_exist(self):
        """Test that all required JavaScript files exist"""
        js_dir = PROJECT_ROOT / "static" / "js"
        required_files = ["api.js", "renderer.js", "editor.js", "app.js"]

        for filename in required_files:
            file_path = js_dir / filename
            assert file_path.exists(), f"Required file {filename} is missing"

            content = file_path.read_text()
            assert len(content) > 0, f"{filename} is empty"

    def test_css_file_exists(self):
        """Test that the CSS file exists"""
        css_file = PROJECT_ROOT / "static" / "css" / "style.css"
        assert css_file.exists(), "static/css/style.css is missing"

        content = css_file.read_text()
        assert len(content) > 0, "style.css is empty"

    def test_index_html_includes_all_scripts(self):
        """Test that index.html includes all required script tags"""
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        required_scripts = ["api.js", "renderer.js", "editor.js", "app.js"]
        for script in required_scripts:
            assert script in content, f"index.html missing script tag for {script}"

    def test_index_html_has_canvas(self):
        """Test that index.html has the canvas element"""
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        assert 'id="rail-canvas"' in content, (
            "index.html missing canvas element with id='rail-canvas'"
        )

    def test_index_html_has_correct_script_order(self):
        """Test that scripts are loaded in the correct order"""
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        # Find positions of script tags
        api_pos = content.find('src="js/api.js')
        renderer_pos = content.find('src="js/renderer.js')
        editor_pos = content.find('src="js/editor.js')
        app_pos = content.find('src="js/app.js')

        # Verify order: api -> renderer -> editor -> app
        assert api_pos < renderer_pos, "api.js must load before renderer.js"
        assert renderer_pos < editor_pos, "renderer.js must load before editor.js"
        assert editor_pos < app_pos, "editor.js must load before app.js"

    def test_server_py_exists(self):
        """Test that server.py exists"""
        server_file = PROJECT_ROOT / "server.py"
        assert server_file.exists(), "server.py is missing"

        content = server_file.read_text()
        assert "Flask" in content, "server.py doesn't import Flask"
        assert "app = Flask" in content, "server.py doesn't create Flask app"

    def test_python_backend_files_exist(self):
        """Test that core Python backend files exist"""
        required_files = [
            "network.py",
            "track.py",
            "block.py",
            "railway_signal.py",
            "station.py",
            "train.py",
            "pathfinder.py",
        ]

        for filename in required_files:
            file_path = PROJECT_ROOT / filename
            assert file_path.exists(), f"Required backend file {filename} is missing"
