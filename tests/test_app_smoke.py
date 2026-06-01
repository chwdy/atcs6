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
        """Required frontend entry points exist and are non-empty.

        The frontend is mid-migration to a Vite build (see vite.config.ts):
        the renderer/foundation/editor clusters moved under frontend/src/ and
        are bundled into static/dist/main.js, while only app.js is still served
        as a classic script from static/js/. "Required files" now spans two
        roots:

          - static/js/app.js — the surviving classic bootstrap, still loaded
            directly by static/index.html.
          - frontend/src/main.ts — the Vite entry that builds into
            static/dist/main.js.

        We assert the committed *source* (main.ts), not the build output:
        static/dist/ is gitignored (.gitignore) and absent on a fresh
        checkout until `npm run build` runs, so a pre-commit smoke test must
        not depend on it. This matches the other frontend-source assertions
        in this file (the train-monitor tests point at
        frontend/src/panels/train-monitor.ts).
        """
        required_files = [
            PROJECT_ROOT / "static" / "js" / "app.js",
            PROJECT_ROOT / "frontend" / "src" / "main.ts",
        ]

        for file_path in required_files:
            assert file_path.exists(), f"Required file {file_path} is missing"

            content = file_path.read_text()
            assert len(content) > 0, f"{file_path} is empty"

    def test_css_file_exists(self):
        """Test that the CSS file exists"""
        css_file = PROJECT_ROOT / "static" / "css" / "style.css"
        assert css_file.exists(), "static/css/style.css is missing"

        content = css_file.read_text()
        assert len(content) > 0, "style.css is empty"

    def test_emergency_reset_button_exists(self):
        """Ensure emergency reset button markup is present."""
        editor_trains = PROJECT_ROOT / "frontend" / "src" / "editor" / "editor-trains.ts"
        content = editor_trains.read_text()

        assert "train-emergency-reset-btn" in content, (
            "Emergency reset button markup is missing in editor-trains.ts"
        )

    def test_index_html_includes_all_scripts(self):
        """index.html wires up both the classic scripts and the Vite bundle.

        Post-migration, static/index.html loads the surviving classic editor
        scripts directly and the bundled modules through the Vite build entry
        `dist/main.js` (the `<script type="module">` near the bottom of the
        page). Assert all three are referenced so a dropped tag is caught.
        """
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        required_scripts = ["app.js", "dist/main.js"]
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

        # Only app.js remains a classic script; the renderer, foundation, and
        # editor clusters migrated under frontend/src/ and are served via the
        # Vite bundle. app.js (which bootstraps `new App()` → `new Editor(...)`
        # on DOMContentLoaded) must load before the bundle's `<script
        # type="module">` so its globals exist when the module bridges resolve.
        app_pos = content.find('src="js/app.js')
        bundle_pos = content.find('dist/main.js')

        # Verify order: app.js -> bundle
        assert app_pos != -1, "app.js script tag is missing"
        assert app_pos < bundle_pos, "app.js must load before the dist/main.js bundle"

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
        ]

        for filename in required_files:
            file_path = PROJECT_ROOT / filename
            assert file_path.exists(), f"Required backend file {filename} is missing"

    def test_train_monitor_upcoming_has_no_fixed_limit(self):
        """Train monitor upcoming list should not be hard-limited."""
        # train-monitor.js migrated to frontend/src/panels/train-monitor.ts
        # (longterm_todo.md item #7); the source assertion follows it there.
        monitor_file = PROJECT_ROOT / "frontend" / "src" / "panels" / "train-monitor.ts"
        content = monitor_file.read_text()

        assert "elements.slice(0, 3)" not in content, (
            "Train monitor upcoming list is still limited to 3 items"
        )

    def test_train_monitor_block_labels_use_dynamic_fit(self):
        """Train monitor block labels should use dynamic text fitting."""
        monitor_file = PROJECT_ROOT / "frontend" / "src" / "panels" / "train-monitor.ts"
        content = monitor_file.read_text()

        assert "fitTextToWidth" in content, (
            "Train monitor block label fitting helper is missing"
        )

    def test_train_monitor_upcoming_default_width(self):
        """Train monitor upcoming column default width should be widened."""
        css_file = PROJECT_ROOT / "static" / "css" / "features" / "train-monitor.css"
        content = css_file.read_text()

        assert ".tm-upcoming" in content, (
            "Train monitor upcoming styles missing"
        )
        assert "width: 280px" in content, (
            "Train monitor upcoming default width should be 280px"
        )
        assert "min-width: 220px" in content, (
            "Train monitor upcoming min width should be 220px"
        )
        assert "max-width: 420px" in content, (
            "Train monitor upcoming max width should be 420px"
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
        """JSON button should use the monochrome `{ }` glyph and the icon-button class."""
        index_file = PROJECT_ROOT / "static" / "index.html"
        content = index_file.read_text()

        assert 'id="view-lookahead-btn"' in content, "JSON view button missing"
        # Per docs/DESIGN_LANGUAGE.md §"No color emoji anywhere in the UI",
        # the clipboard emoji was replaced with a monochrome curly-brace glyph.
        assert "{ }" in content, "JSON button glyph `{ }` missing"
        assert "View raw lookahead JSON" in content, "JSON button tooltip missing"
        # tm-icon-btn supplies `color: var(--text-primary)` for icon buttons in
        # the train-monitor header (static/css/features/train-monitor.css).
        assert "tm-icon-btn view-lookahead-btn" in content, (
            "JSON button should carry the tm-icon-btn class for primary text color"
        )
