"""Scene Contract Tests

Locks the two things `server.run_startup_tests()` and `bootstrap` assume:

1. The block-validation gate (`scene_validation.validate_block_rules`) passes —
   the server calls it before it will boot.
2. Every scene in `bootstrap._SCENE_MODULES` imports and exports the builder
   contract bootstrap calls (`SCENE_ID`, `create_comprehensive_demo`,
   `setup_demo_train`).

Test 2 exists because scenes baked by `json_to_scene.py` (demo7, demo7_1) are
generated, not hand-written: a registry entry pointing at a module that is
missing part of the contract used to surface only as a boot-time crash.
"""

import importlib

import pytest

from bootstrap import _SCENE_MODULES, available_scenes
from scene_validation import validate_block_rules


def test_block_validation_rules_pass(capsys):
    """The server's block-validation boot gate must pass."""
    validate_block_rules()
    assert "All Block Validation Tests Passed" in capsys.readouterr().out


@pytest.mark.parametrize("scene_id", available_scenes())
def test_scene_module_exports_builder_contract(scene_id):
    """Every registered scene must expose what bootstrap calls on it."""
    module = importlib.import_module(_SCENE_MODULES[scene_id])

    assert getattr(module, "SCENE_ID", None) == scene_id
    assert callable(getattr(module, "create_comprehensive_demo", None))
