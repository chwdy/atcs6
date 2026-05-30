#!/usr/bin/env python
"""Generate TypeScript types from the canonical Python payload dataclasses.

longterm_todo.md item #8, Phase 1. The frontend <-> server JSON contract is
defined by the ``@dataclass`` and ``Enum`` types in ``runtime_payloads.py``.
This script walks that module and emits ``frontend/src/payloads.generated.ts``
so the (TypeScript) frontend and the Python server share one source of truth.

Two project-specific rules the generator encodes:

* **Enums serialize by value.** On the wire an enum is its ``.value`` (see the
  ``to_dict`` methods in ``runtime_payloads.py``), so an enum becomes a union of
  its values, e.g. ``export type AcquisitionMode = "all_or_nothing" | ...``.
* **Optionals become ``X | null``.** ``Optional[X]`` is ``Union[X, None]`` and
  JSON carries ``null``, so the field type includes ``| null``. Fields are not
  marked ``?optional`` — the contract is "key present, value may be null".

A drift test (``tests/test_payload_types_sync.py``) re-runs ``render()`` and
fails if the committed ``.ts`` is stale, so the types cannot silently rot.

Run:  conda run -n general python tools/gen_payload_types.py
"""

import dataclasses
import enum
import json
import sys
import types
import typing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Allow `python tools/gen_payload_types.py` from any cwd to import the root
# modules that hold the payload schemas.
sys.path.insert(0, str(REPO_ROOT))

import runtime_payloads  # noqa: E402  (after sys.path setup)

# Canonical payload homes. Add modules here as new shared-contract homes appear;
# only ``@dataclass`` and ``Enum`` types *defined* in these modules are emitted.
SOURCE_MODULES = [runtime_payloads]

OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "payloads.generated.ts"

HEADER = (
    "// AUTO-GENERATED — DO NOT EDIT BY HAND.\n"
    "// Source of truth: runtime_payloads.py (Python @dataclass + Enum).\n"
    "// Regenerate: conda run -n general python tools/gen_payload_types.py\n"
    "// Drift guard: tests/test_payload_types_sync.py fails if this is stale."
)

_NONE = type(None)
_UNION_TYPES = (typing.Union, getattr(types, "UnionType", typing.Union))


def _collect():
    """Return (enums, dataclasses, known_names) defined in the source modules.

    Order follows definition order within each module so the output is
    deterministic. Imported names are skipped (only types whose ``__module__``
    matches the source module are emitted), which excludes the Protocols,
    typing helpers, and re-exported enums living elsewhere.
    """
    enums, dcs, names = [], [], set()
    for module in SOURCE_MODULES:
        for obj in vars(module).values():
            if not isinstance(obj, type) or obj.__module__ != module.__name__:
                continue
            if issubclass(obj, enum.Enum):
                if obj not in enums:
                    enums.append(obj)
                    names.add(obj.__name__)
            elif dataclasses.is_dataclass(obj):
                if obj not in dcs:
                    dcs.append(obj)
                    names.add(obj.__name__)
    return enums, dcs, names


def _wrap(ts: str) -> str:
    """Parenthesize a union before suffixing ``[]`` so precedence is correct."""
    return f"({ts})" if " | " in ts else ts


def _ts(ann, known) -> str:
    """Map a Python type annotation to a TypeScript type expression."""
    if ann is None or ann is _NONE:
        return "null"
    if ann is typing.Any:
        return "unknown"
    if isinstance(ann, str):
        return ann if ann in known else "unknown"
    if isinstance(ann, typing.ForwardRef):
        name = ann.__forward_arg__
        return name if name in known else "unknown"

    origin = typing.get_origin(ann)
    args = typing.get_args(ann)

    if origin is None:
        if ann is str:
            return "string"
        if ann in (int, float):
            return "number"
        if ann is bool:
            return "boolean"
        if isinstance(ann, type) and issubclass(ann, enum.Enum):
            return ann.__name__ if ann.__name__ in known else "unknown"
        if dataclasses.is_dataclass(ann):
            return ann.__name__ if ann.__name__ in known else "unknown"
        if ann is dict:
            return "Record<string, unknown>"
        if ann in (list, set, frozenset, tuple):
            return "unknown[]"
        return "unknown"

    if origin in _UNION_TYPES:
        parts = []
        for a in args:
            part = _ts(a, known)
            if part not in parts:
                parts.append(part)
        return " | ".join(parts)
    if origin in (list, set, frozenset):
        return f"{_wrap(_ts(args[0], known))}[]" if args else "unknown[]"
    if origin is dict:
        # JSON object keys are always strings, regardless of the Python key type.
        val = _ts(args[1], known) if len(args) > 1 else "unknown"
        return f"Record<string, {val}>"
    if origin is tuple:
        if not args:
            return "unknown[]"
        if len(args) == 2 and args[1] is Ellipsis:
            return f"{_wrap(_ts(args[0], known))}[]"
        return "[" + ", ".join(_ts(a, known) for a in args) + "]"
    if origin is typing.Literal:
        return " | ".join(json.dumps(a) for a in args)

    return "unknown"


def _hints(cls):
    """Resolve a class's annotations to type objects, tolerating forward refs
    that only exist under ``TYPE_CHECKING`` (fall back to raw annotations)."""
    try:
        return typing.get_type_hints(cls, globalns=vars(sys.modules[cls.__module__]))
    except Exception:
        return dict(getattr(cls, "__annotations__", {}))


def render() -> str:
    """Return the full TypeScript source for the payload contract."""
    enums, dcs, known = _collect()
    blocks = [HEADER]

    for e in enums:
        values = " | ".join(json.dumps(m.value) for m in e) or "never"
        blocks.append(f"export type {e.__name__} = {values};")

    for cls in dcs:
        hints = _hints(cls)
        lines = [f"export interface {cls.__name__} {{"]
        for fld in dataclasses.fields(cls):
            ann = hints.get(fld.name, fld.type)
            lines.append(f"  {fld.name}: {_ts(ann, known)};")
        lines.append("}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + "\n"


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
