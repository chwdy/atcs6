#!/usr/bin/env bash
set -u

if [[ "${CONDA_DEFAULT_ENV:-}" != "general" ]]; then
    echo "Activate the project's Conda environment first: conda activate general" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python "${SCRIPT_DIR}/run_all_tests.py"
