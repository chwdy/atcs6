---
name: generate-scene-from-json
description: Bake a saved layout JSON (the editor's `layouts/*.json` save format, with a top-level `snapshot`) into a demo_6-style Python scene module by running this repo's `json_to_scene.py` baker. Use this whenever the user wants to "generate a scene from json", "convert a layout to a .py scene", "bake layouts/X.json", "turn a saved layout into a demo_* scene", or otherwise produce a runnable `demo_*`-style scene file (TILES / SWITCHES / STATIONS / BLOCKS / TUNNELS lists plus `create_comprehensive_demo`) from a saved layout. Reach for it even when the user just says "bake the layout" or names a `layouts/*.json` file and asks for a scene — the baker already exists and is round-trip-verified, so do NOT hand-write a new converter. Do NOT trigger for: saving/exporting a layout FROM the editor (that direction produces the JSON), loading a layout at runtime (`RailwayNetwork.from_dict` already does that), hand-editing an existing `demo_*.py` scene, or generic JSON parsing/transforms unrelated to scenes.
---

# Generate a scene from a layout JSON

This repo turns a saved layout JSON into a baked Python scene module that boots like `demo_6.py`. The converter already exists — **`json_to_scene.py` at the repo root** — so the job is to run it correctly and explain the result, not to write a new converter.

## Why a script, not hand-conversion

A layout has hundreds of tracks plus switches, platforms, station groups, blocks, tunnels, and signals, each with fiddly encodings (track bits as `TRACK_*` constants, platforms as an anchor tile + extensions, platform blocks that are auto-created rather than listed). Hand-converting is slow and easy to get subtly wrong.

`json_to_scene.py` avoids that by loading the JSON through the app's own canonical loader (`RailwayNetwork.from_dict`) and introspecting the result — so the output is exactly what loading the layout in-app would build. Its `--verify` flag rebuilds the generated module and the `from_dict` load of the same JSON and asserts the two networks are identical, failing loudly on any mismatch. That round-trip is the correctness guarantee, so always use it.

## Run it

From the repo root, with the project's `general` conda env:

```
conda run -n general python json_to_scene.py <INPUT.json> <OUTPUT.py> --scene-id <NAME> --verify
```

Env note: in a non-interactive shell `conda run -n general` often fails with `exit 126 (permission denied)`. When it does, run the env's Python binary directly instead — `/opt/homebrew/anaconda3/envs/general/bin/python json_to_scene.py ...` — which is the reliable invocation (per MEMORY.md). See AGENTS.md → "Running Python in this repo" for the full convention.

- `<INPUT.json>` — a saved layout, e.g. `layouts/demo_6.json`. The file can be the editor wrapper (top-level `snapshot`) or a bare snapshot; the script handles both.
- `<OUTPUT.py>` — where to write the scene. If the user doesn't name a path, write to a temp path (e.g. `/tmp/<name>.py`) and show it to them — don't overwrite an existing `demo_*.py` unless they ask.
- `--scene-id` — optional; defaults to the output filename stem. Pass one if the user names the scene.
- `--verify` — **always pass it** and report the result. A clean run prints `VERIFY OK` with entity counts; that line is the proof the scene matches the JSON.

The script must run from the repo root because it imports the repo's modules (`network`, `track`, ...). If `json_to_scene.py` is missing from the repo root, restore it from this skill's `scripts/json_to_scene.py` (a snapshot copy) — put it at the repo root and run it there.

## The gotchas that matter — flag these, don't just run the script

The real value is warning the user about the things that will otherwise surprise them:

- **It bakes the LAYOUT only.** Train placement and routes are not stored in a layout JSON, so the generated `setup_demo_train()` is a no-op stub. If the scene needs rolling stock, that logic must be written by hand (see `demo_6.py` for the pattern). Say this explicitly every time.
- **A saved JSON can be stale relative to its hand-edited scene.** For example, `layouts/demo_6.json` holds only the yard stations (20 platforms), while `demo_6.py` has been hand-extended to 54. Converting a JSON faithfully reproduces *that JSON*, which may be **less** than the current `.py`. So compare the `VERIFY OK` entity counts against what the user expects and call out anything that looks smaller than it should.
- **To bake the CURRENT live scene** (not a possibly-stale file), serialize it first, then convert that JSON:
  ```
  conda run -n general python -c "import json, demo_6; \
    json.dump({'snapshot': demo_6.create_comprehensive_demo().to_layout_dict()}, open('/tmp/live.json','w'))"
  ```
  then run the baker on `/tmp/live.json`. This is the right move whenever the user says "match the current demo_6" or similar.
- **`dead_end_ports` are intentionally not emitted** — the network re-derives them from full-track geometry (verified identical), so TILES stays the clean `(x, y, bits)` form. Don't "fix" this.
- **Non-default switch metadata isn't captured** by the `(id, x, y)` form. The script prints a `WARNING` listing any such switches — pass that warning along to the user.
- **A generated scene isn't loadable in-app until registered.** To boot it by name, add it to `bootstrap._SCENE_MODULES`. Mention this if the user wants to actually run the scene, not just hold the file.

## Output format

After a successful run, report concisely:
1. The output path and `SCENE_ID`.
2. The `VERIFY OK` entity counts (tracks / switches / platforms / groups / blocks / tunnels / signals).
3. Any `WARNING` lines the script printed.
4. The layout-only caveat (stub `setup_demo_train`), plus the stale-JSON note if the counts came out smaller than the user expected.

If `--verify` fails instead of printing `VERIFY OK`, do not present the scene as done — read the reported differences, since they pinpoint exactly which entity diverged, and fix the cause before reporting success.
