from pathlib import Path


def test_switch_editor_subscription_uses_switch_id():
    content = Path("static/js/editor-switches.js").read_text()
    assert "this._switchId = switchData.switch_id" in content
    assert (
        "subscribe(\n"
        "            'switches',\n"
        "            switchData.switch_id" in content
    )
