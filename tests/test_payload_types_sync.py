"""Drift guard for the generated TypeScript payload types.

longterm_todo.md item #8, Phase 1. ``frontend/src/payloads.generated.ts`` is
generated from the Python payload dataclasses in ``runtime_payloads.py``. This
test re-runs the generator and asserts the committed file is byte-identical, so
a payload schema change cannot land without regenerating the TS contract.

If this fails, regenerate with:
    conda run -n general python tools/gen_payload_types.py
"""

from tools.gen_payload_types import OUTPUT_PATH, render


def test_generated_payload_types_match_python_dataclasses():
    expected = render()
    assert OUTPUT_PATH.exists(), (
        f"{OUTPUT_PATH} is missing. Generate it with:\n"
        "    conda run -n general python tools/gen_payload_types.py"
    )
    actual = OUTPUT_PATH.read_text(encoding="utf-8")
    assert actual == expected, (
        "frontend/src/payloads.generated.ts is stale relative to the Python "
        "payload dataclasses. Regenerate with:\n"
        "    conda run -n general python tools/gen_payload_types.py"
    )
