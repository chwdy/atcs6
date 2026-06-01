from pathlib import Path


def test_switch_editor_subscription_uses_switch_id():
    # Migrated to TypeScript (longterm_todo.md item #7, Wave 2); the exact
    # indentation changed, so assert the tokens rather than the whitespace.
    content = Path("frontend/src/editor/editor-switches.ts").read_text()
    assert "this._switchId = switchData.switch_id" in content
    # Phase-4 subscription: registers against the 'switches' entity type
    # keyed by the switch id.
    assert "subscribe(" in content
    assert "'switches'," in content
    assert "switchData.switch_id," in content
