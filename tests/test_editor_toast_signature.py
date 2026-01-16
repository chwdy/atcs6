from pathlib import Path


def test_editor_toast_signature_is_three_args():
    files = [
        "static/js/editor-signals.js",
        "static/js/editor-stations.js",
    ]
    for file_path in files:
        content = Path(file_path).read_text()
        assert "showToast('" in content
        assert "'success', 'error'" not in content
