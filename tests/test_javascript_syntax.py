"""Frontend module-structure tests.

The classic ``static/js/`` scripts migrated to ``frontend/src/`` (longterm_todo.md
item #7) and are bundled by Vite. These tests assert that the migrated TypeScript
entry points exist and still define the classes and helpers the app relies on.
"""

from pathlib import Path


# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.parent


class TestJavaScriptSyntax:
    """Assert the migrated frontend/src modules exist and expose their classes/helpers."""

    def test_all_required_js_files_exist(self):
        """The migrated frontend entry points exist.

        The classic static/js/ scripts migrated to frontend/src/ (longterm_todo.md
        item #7) and are bundled by Vite; assert the committed TS sources.
        """
        required_files = [
            PROJECT_ROOT / "frontend" / "src" / "app.ts",
            PROJECT_ROOT / "frontend" / "src" / "main.ts",
        ]

        for file_path in required_files:
            assert file_path.exists(), f"Required file {file_path} is missing"

    def test_api_js_has_api_class(self):
        """Test that the migrated api module defines the API class"""
        api_file = PROJECT_ROOT / "frontend" / "src" / "lib" / "api.ts"
        content = api_file.read_text()

        assert "class API" in content, "api.ts must define API class"
        assert "export const api = new API()" in content, (
            "api.ts must export the singleton API instance"
        )

    def test_editor_js_has_editor_class(self):
        """Test that the migrated editor module defines the Editor class"""
        editor_file = PROJECT_ROOT / "frontend" / "src" / "editor" / "editor.ts"
        content = editor_file.read_text()

        assert "class Editor" in content, "editor.ts must define Editor class"

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
        """Test that the migrated app module defines the App class"""
        app_file = PROJECT_ROOT / "frontend" / "src" / "app.ts"
        content = app_file.read_text()

        assert "class App" in content, "app.ts must define App class"

    def test_editor_trains_waits_for_ato_stop(self):
        """ATO stop should wait for train to fully stop before refresh."""
        editor_trains_file = PROJECT_ROOT / "frontend" / "src" / "editor" / "editor-trains.ts"
        content = editor_trains_file.read_text()

        assert "waitForTrainStopped" in content, (
            "editor-trains.js must define waitForTrainStopped helper"
        )
        assert "await waitForTrainStopped()" in content, (
            "ATO Stop should wait for the train to stop before refresh"
        )

    def test_editor_trains_updates_cab_buttons_on_live_update(self):
        """Train control panel should update cab buttons during live updates."""
        editor_trains_file = PROJECT_ROOT / "frontend" / "src" / "editor" / "editor-trains.ts"
        content = editor_trains_file.read_text()

        assert "updateControlPanelValuesFromData" in content, (
            "editor-trains.js must define updateControlPanelValuesFromData"
        )
        assert "updateCabButtons" in content, (
            "Train control live update should include updateCabButtons helper"
        )
