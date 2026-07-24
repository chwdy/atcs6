# CLAUDE.md

This file collects directives for working in this repo. Every rule below is global — it applies to all files.

## Scope

- The rules that govern document edits apply ONLY to `design_new.md`, and they live in `.claude/rules/design-new-authoring.md`, which loads when you work on that file.
- Other files (code, current docs, implementation index) follow their own conventions.
- "Running Python in this repo" applies to every Python invocation.
- "Browser preview" applies whenever you use the browser preview tools.

## Running Python in this repo

This project requires the `general` conda environment. Run Python through it for everything: server boot, scripts, ad-hoc `python -c` snippets, pytest, dependency checks.

- **Always call the env's Python binary directly:** `/opt/homebrew/anaconda3/envs/general/bin/python ...`. Examples:
  ```
  /opt/homebrew/anaconda3/envs/general/bin/python -c "import flask; print(flask.__version__)"
  /opt/homebrew/anaconda3/envs/general/bin/python -m pytest tests/
  ```
  Plain `python` / `python3` / `pytest` does NOT work — it falls through to `/usr/bin/python3` (system 3.9.6, no flask, no PEP 604 union syntax).
- **Bare `conda run -n general ...` FAILS in tool-call shells.** It exits 126 with:
  ```
  __conda_exe:6: permission denied:
  ```
  `conda` is a shell *function* that delegates to `__conda_exe`, itself a function restored from the shell snapshot; it execs `$CONDA_EXE`, which is unset in a non-interactive shell, so it tries to exec an empty path. (The trailing colon with nothing after it is the empty command name.) The binary itself is fine — only the function indirection is broken, so `/opt/homebrew/anaconda3/bin/conda run -n general python ...` (absolute path) also works if you need `conda run` specifically. Prefer the env binary; it is shorter and has no wrapper.
- **Background / long-running processes need no special flag.** The env binary streams stdout/stderr normally (it is `conda run`'s wrapper that buffers). Use:
  ```
  /opt/homebrew/anaconda3/envs/general/bin/python server.py --skip-startup-tests --port 5007 > /tmp/server.log 2>&1 &
  ```
  for the dev server, demo runs, or anything else you start with `&`. Add `-u` if you need unbuffered output from a chatty script. `.claude/launch.json` already points at this same binary.
- **Why this matters.** `general` provides Python 3.12.13 with flask and pytest installed. Code in this repo uses 3.10+ syntax (e.g. `Flask | None` in `server.py:66`); running under 3.9 raises `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` at import time.
- **How to apply.** Anywhere a tool call would invoke `python`, `python3`, `pytest`, `flask`, or run `server.py` / `demo_main.py` / any project script, use the env binary path above. When `pytest` or similar is reported as "not found," it's the env, not a missing install — re-run via the env binary.
- **Don't "fix" the 3.10+ syntax.** Modern union syntax (`X | None`, etc.) is intentional. Do not change it to `Optional[X]` or add `from __future__ import annotations` just because a wrong-Python invocation surfaced an error — fix the invocation instead.

## Browser preview

The preview pane opens at a tiny default size (around 2px wide), so the very first screenshot comes out as a useless thin sliver.

- **Resize first, before the first screenshot.** As soon as you open a preview, call `resize_window` to a normal desktop size (e.g. 1440×900, or `preset: "desktop"`). Do this up front — don't take a broken sliver screenshot, notice it's wrong, and only then resize.

## Editing `design_new.md`

The authoring rules, style rules, and cross-reference sweep rules for `design_new.md` live in
`.claude/rules/design-new-authoring.md`. They load automatically whenever you work on that file.

## Debugging

State your root-cause hypothesis and the file/function it lives in, and verify it with a probe before editing. Don't ship a first fix until the cause is confirmed.

## Cross-cutting changes

When a change spans docs + code + tests (e.g. a semantics change like direction / manual_override), enumerate every affected site first, then sweep them in one pass.
