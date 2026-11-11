# CLAUDE.md

This file collects directives for working in this repo. Every rule below is global — it applies to all files.

## Scope

- `design_v2.md` is the design doc for this project. It is the one bridge between the user, you, and the code: the code is a straight translation of it, and when they disagree the doc wins. Read its **About this document** and **The evening v2 is for** sections before any design or code work.
- The rules that govern document edits apply ONLY to `design_v2.md`. The doc's own rules are in its **Authoring rules** section; the sentence-level style rules live in `.claude/rules/design-v2-authoring.md`, which loads when you work on that file.
- `archive/design_new.md` is the v1 design doc, kept for reference only. Do not edit it, and do not treat it as a source of rules.
- Other files (code, current docs, implementation index) follow their own conventions.
- "Running Python in this repo" applies to every Python invocation.
- "Browser preview" applies whenever you use the browser preview tools.
- "How to communicate" applies to every message, doc, comment, and commit you write.
- "Terminology discipline" applies whenever you discuss this system, in any message or artifact.

## How to communicate

Say what a thing does in plain words. Do not invent a short label for an idea and then spend a paragraph explaining what the label means — if a term needs a paragraph to decode, the term is wrong, so write the plain description instead. A reader should never have to hold a private glossary in their head to follow you.

- **No buzzwords, no coined jargon.** Words like "preamble," "fuse," "integrate all trains," "barrier" (as a coined tick term) hide meaning behind a label. Replace each with what actually happens: "the first step of the tick," "compare the calculated position against the detector reading and make them agree," "calculate every train's new position before any train plans."
- **The explanation is the writing, not a footnote to it.** If you catch yourself writing a term and then a sentence that translates it, delete the term and keep the translation.
- **Real identifiers are allowed, defined once, plainly.** A word that names actual code — a field, method, class, or an established project concept — may be used, but give it a one-line plain-English meaning the first time it appears (for example: "the span — the stretch of track from the back of the last car to the front of the loco"). This is the only kind of "term" permitted, and only because it points at something concrete in the code.
- **Short beats clever.** Prefer the sentence a junior engineer understands on first read over the compact phrase that sounds precise but has to be unpacked.

## Terminology discipline

Use ONLY terms listed in the **Terms** section of design_v2.md when discussing this system. Never invent paraphrases, shorthand, or jargon (e.g. 'braking cap', 'global Sense preamble', 'fuse', 'raw currents', 'safe place to stop'). If a concept has no term in the doc, say so explicitly and propose adding one — do not coin one silently. Prefer plain language over buzzwords.

## Before building anything

When the user raises a problem, a scope question, or a "why does this feel wrong" question, discuss it first. Do not answer with a feature to build. When you do propose a change, say which part of **The evening v2 is for** in design_v2.md it serves; if it serves none, do not propose it.

## Running Python in this repo

This project requires the `general` conda environment. Run Python through it for everything: server boot, scripts, ad-hoc `python -c` snippets, pytest, dependency checks.

- **Follow `README.md` for verification.** The canonical completion gate is
  `conda activate general` followed by `./run_all_tests.sh`. Targeted pytest or
  frontend commands are useful during iteration, but do not replace that gate.
- **Use port 5007 for the development server.** Always launch `server.py` with
  `--port 5007` and use `http://localhost:5007` for browser and API checks.

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
- **Why this matters.** `general` provides Python 3.12.16 with flask and pytest installed. Keep a conda-forge build made on Apple Silicon: older builds (3.12.13 and earlier) were compiled on Intel without profile-guided optimization and run the tests about 30% slower. Code in this repo uses 3.10+ syntax (e.g. `Flask | None` in `server.py:66`); running under 3.9 raises `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` at import time.
- **How to apply.** Anywhere a tool call would invoke `python`, `python3`, `pytest`, `flask`, or run `server.py` / `demo_main.py` / any project script, use the env binary path above. When `pytest` or similar is reported as "not found," it's the env, not a missing install — re-run via the env binary.
- **Don't "fix" the 3.10+ syntax.** Modern union syntax (`X | None`, etc.) is intentional. Do not change it to `Optional[X]` or add `from __future__ import annotations` just because a wrong-Python invocation surfaced an error — fix the invocation instead.

## Browser preview

The preview pane opens at a tiny default size (around 2px wide), so the very first screenshot comes out as a useless thin sliver.

- **Resize first, before the first screenshot.** As soon as you open a preview, call `resize_window` to a normal desktop size (e.g. 1440×900, or `preset: "desktop"`). Do this up front — don't take a broken sliver screenshot, notice it's wrong, and only then resize.

## Editing `design_v2.md`

The doc's own authoring rules are in its **Authoring rules** section. The sentence-level style
and text-cutting rules live in `.claude/rules/design-v2-authoring.md`, which loads automatically
whenever you work on that file.

## Debugging

State your root-cause hypothesis and the file/function it lives in, and verify it with a probe before editing. Don't ship a first fix until the cause is confirmed.

## Cross-cutting changes

When a change spans docs + code + tests (e.g. a semantics change like direction / manual_override), enumerate every affected site first, then sweep them in one pass.
