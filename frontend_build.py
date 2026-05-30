"""Build-on-serve guard for the frontend bundle.

longterm_todo.md item #8: once a frontend module is migrated to TypeScript
(Phase 2+), the running app depends on the Vite bundle static/dist/main.js for
the migrated pieces (e.g. window.Camera). That file is gitignored, so a fresh
checkout or a tree whose sources changed would otherwise serve a broken page.

This module rebuilds the bundle when it is missing or older than the frontend
sources, so `python server.py` always serves a current bundle. It is invoked
only from server.py's main() (the real boot path), never from create_app(), so
the test suite never shells out to npm.

It never raises: on any failure it logs a clear, actionable message and lets
the server start (degraded) rather than blocking the whole app on the JS
toolchain.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BUNDLE = REPO_ROOT / "static" / "dist" / "main.js"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
# Config files whose changes also invalidate the bundle.
EXTRA_SOURCES = ("vite.config.ts", "tsconfig.json", "package.json")
SKIP_ENV = "ATCS6_SKIP_FRONTEND_BUILD"


def _log(msg: str) -> None:
    print(f"[frontend] {msg}", flush=True)


def _newest_source_mtime() -> float:
    mtimes = [0.0]
    if FRONTEND_SRC.is_dir():
        for p in FRONTEND_SRC.rglob("*"):
            if p.is_file():
                mtimes.append(p.stat().st_mtime)
    for name in EXTRA_SOURCES:
        f = REPO_ROOT / name
        if f.is_file():
            mtimes.append(f.stat().st_mtime)
    return max(mtimes)


def _bundle_is_fresh() -> bool:
    if not BUNDLE.is_file():
        return False
    return BUNDLE.stat().st_mtime >= _newest_source_mtime()


def _run(npm: str, args: list, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [npm, *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ensure_frontend_bundle(skip: bool = False) -> None:
    """Rebuild static/dist/main.js if missing or stale, before serving."""
    if skip or os.environ.get(SKIP_ENV):
        _log("build-on-serve skipped")
        return

    if _bundle_is_fresh():
        _log("bundle up to date")
        return

    reason = "missing" if not BUNDLE.is_file() else "stale"
    npm = shutil.which("npm")
    if npm is None:
        _log(
            f"bundle {reason} but `npm` is not on PATH — run `npm install && "
            "npm run build`. Serving without a fresh bundle."
        )
        return

    try:
        if not (REPO_ROOT / "node_modules").is_dir():
            _log("node_modules missing — running `npm install` (one-time)…")
            r = _run(npm, ["install"], timeout=600)
            if r.returncode != 0:
                _log("`npm install` failed; serving without a fresh bundle:\n" + (r.stderr or r.stdout))
                return

        _log(f"bundle {reason} — running `npm run build`…")
        r = _run(npm, ["run", "build"], timeout=300)
        if r.returncode != 0:
            _log("`npm run build` failed; serving without a fresh bundle:\n" + (r.stderr or r.stdout))
            return
        _log("bundle rebuilt → static/dist/main.js")
    except subprocess.TimeoutExpired as exc:
        _log(f"frontend build timed out ({exc}); serving without a fresh bundle")
    except Exception as exc:  # never block serving on the JS toolchain
        _log(f"frontend build error ({exc}); serving without a fresh bundle")
