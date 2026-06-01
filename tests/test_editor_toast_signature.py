from pathlib import Path


def test_editor_toast_signature_is_three_args():
    # Migrated to TypeScript (longterm_todo.md item #7, Wave 2); the toast
    # calls now go through the bridged optional `window.showToast?.(...)`.
    files = [
        "frontend/src/editor/editor-signals.ts",
        "frontend/src/editor/editor-stations.ts",
    ]
    for file_path in files:
        content = Path(file_path).read_text()
        assert "showToast?.('" in content
        assert "'success', 'error'" not in content
