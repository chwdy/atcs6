"""
App Smoke Tests

These tests verify that critical application files exist and have valid content.
This catches basic issues before attempting to run the server.

Critical: These tests MUST pass before any commit.
"""

from datetime import datetime
from pathlib import Path
import threading

import pytest

from app_factory import create_app
from app_state import AppState
from speed_profile import SpeedProfileRegistry


PROJECT_ROOT = Path(__file__).parent.parent


class TestAppSmoke:
    """Smoke tests for the application"""

    def test_app_factory_attaches_state(self):
        """App factory should store state in extensions."""
        state = AppState(
            network=object(),
            path_dispatcher=object(),
            sim_controller=object(),
            persistence_manager=object(),
            speed_profile_registry=SpeedProfileRegistry(),
            simulation_lock=threading.Lock(),
            simulation_running=False,
            simulation_speed=1.0,
            last_update_time=datetime.now(),
        )

        app = create_app(state)

        assert app.extensions.get("state") is state

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

    def test_emergency_reset_button_exists(self):
        """Ensure emergency reset button markup is present."""
        editor_trains = PROJECT_ROOT / "static" / "js" / "editor-trains.js"
        content = editor_trains.read_text()

        assert "train-emergency-reset-btn" in content, (
            "Emergency reset button markup is missing in editor-trains.js"
        )

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
        assert "create_app" in content, "server.py doesn't use the app factory"

    def test_server_py_defines_main(self):
        """Server entrypoint should define a main() function."""
        server_file = PROJECT_ROOT / "server.py"
        content = server_file.read_text()

        assert "def main(" in content, "server.py is missing a main() entrypoint"

    def test_app_factory_and_state_exist(self):
        """Test that app factory and app state modules exist"""
        app_factory_file = PROJECT_ROOT / "app_factory.py"
        app_state_file = PROJECT_ROOT / "app_state.py"

        assert app_factory_file.exists(), "app_factory.py is missing"
        assert app_state_file.exists(), "app_state.py is missing"

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

    def test_train_monitor_upcoming_has_no_fixed_limit(self):
        """Train monitor upcoming list should not be hard-limited."""
        monitor_file = PROJECT_ROOT / "static" / "js" / "train-monitor.js"
        content = monitor_file.read_text()

        assert "elements.slice(0, 3)" not in content, (
            "Train monitor upcoming list is still limited to 3 items"
        )

    def test_train_monitor_block_labels_use_dynamic_fit(self):
        """Train monitor block labels should use dynamic text fitting."""
        monitor_file = PROJECT_ROOT / "static" / "js" / "train-monitor.js"
        content = monitor_file.read_text()

        assert "fitTextToWidth" in content, (
            "Train monitor block label fitting helper is missing"
        )

    def test_train_monitor_upcoming_default_width(self):
        """Train monitor upcoming column default width should be widened."""
        # CSS was refactored to modular files, so we check the specific feature file
        css_file = PROJECT_ROOT / "static" / "css" / "features" / "train-monitor.css"

        # Fallback to style.css if feature file doesn't exist (backward compatibility)
        if not css_file.exists():
            css_file = PROJECT_ROOT / "static" / "css" / "style.css"

        content = css_file.read_text()

        assert ".train-monitor-upcoming" in content, (
            "Train monitor upcoming styles missing"
        )
        assert "width: 280px" in content, (
            "Train monitor upcoming default width should be 280px"
        )
        assert "min-width: 200px" in content, (
            "Train monitor upcoming min width should be 200px"
        )
        assert "max-width: 400px" in content, (
            "Train monitor upcoming max width should be 400px"
        )

    def test_train_monitor_json_button_order(self):
        """JSON button should be after All in view controls."""
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        all_pos = content.find('data-distance="0">All</button>')
        json_pos = content.find('id="view-lookahead-btn"')

        assert all_pos != -1, "All distance button missing"
        assert json_pos != -1, "JSON view button missing"
        assert all_pos < json_pos, "JSON button should appear after the All button"

    def test_train_monitor_json_button_label_and_color(self):
        """JSON button should include icon and use primary text color."""
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        assert 'id="view-lookahead-btn"' in content, "JSON view button missing"
        assert "📋" in content, "JSON button icon missing"
        assert "JSON" in content, "JSON button label missing"
        assert "text-primary" in content, "JSON button should use primary text color"
