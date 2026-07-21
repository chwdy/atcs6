#!/usr/bin/env python3
"""Run every local verification gate used by CI.

Invoke this from the ``general`` Conda environment. The runner deliberately
uses the active interpreter for pytest so it cannot silently fall back to a
different system Python.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Gate:
    name: str
    command: Sequence[str]


GATES = (
    Gate("Python tests", (sys.executable, "-m", "pytest", "tests/")),
    Gate("TypeScript typecheck", ("npm", "run", "typecheck")),
    Gate("Frontend unit tests", ("npm", "run", "test:unit")),
    Gate("Frontend production build", ("npm", "run", "build")),
)


def _print_header(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}", flush=True)


def _validate_environment() -> None:
    if os.environ.get("CONDA_DEFAULT_ENV") != "general":
        raise SystemExit(
            "Activate the project's Conda environment first: "
            "`conda activate general`, then rerun this command."
        )
    if shutil.which("npm") is None:
        raise SystemExit("npm is required for the frontend verification gates.")


def main() -> int:
    _validate_environment()
    failures: list[str] = []

    _print_header("ATCS6 full verification suite")
    for gate in GATES:
        _print_header(gate.name)
        completed = subprocess.run(gate.command, cwd=ROOT, check=False)
        if completed.returncode:
            failures.append(gate.name)

    _print_header("Verification summary")
    if failures:
        print("Failed gates:")
        for name in failures:
            print(f"  - {name}")
        return 1

    print("All Python, TypeScript, frontend-unit, and build gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
