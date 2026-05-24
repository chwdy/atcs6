---
name: ui-debugger
description: When the user shares a UI screenshot from this codebase and expresses dissatisfaction — "this looks ugly", "I hate this layout", "too cramped", "redesign this", "make this not suck" — runs the project's UI-redesign loop. The phases are senior-designer critique → clarify the user's task on this screen → consult docs/DESIGN_LANGUAGE.md → co-decide layout via AskUserQuestion → implement → verify in browser preview → iterate on feedback → reflect lessons back into docs/DESIGN_LANGUAGE.md. Trigger whenever the user posts a UI screenshot and complains, asks "how would a good designer fix this", wants to redesign a panel / card / section / row, calls a layout "too cramped" or "wasteful" or "hard to read", or treats a UI as broken-by-design (not missing-a-feature, not a styling bug). Do NOT trigger for plain UI builds with no screenshot to react to, for CSS bug fixes (broken layout, browser quirks), or for code review of UI code — use `code-review` for that.
---

# UI debugger

The point of this skill is to turn "I don't like this UI" into a sequence of named violations, principled choices, and a layout the user is happy with — then bank the lessons into `docs/DESIGN_LANGUAGE.md` so the next session starts ahead of where this one started.

Every step exists because skipping it makes the redesign worse. Skipping the critique gives generic advice. Skipping the clarification gives a layout that misses the user's actual task. Skipping the doc read invents tokens that already exist. Skipping the iteration ships the first cut (which is never right). Skipping the reflection means re-learning the same lesson next time.

The canonical design language lives at `docs/DESIGN_LANGUAGE.md` — read it once before designing, patch it at the end with what you learned. The route-runs editor (`static/js/editor-route-runs.js` + `static/css/features/routes-paths.css`) is the worked reference implementation; copy from it when in doubt.

## The 7 phases

1. **Critique** as a senior designer
2. **Clarify** the user's task on this screen
3. **Read** `docs/DESIGN_LANGUAGE.md` for applicable tokens and patterns
4. **Plan** the layout with the user (use AskUserQuestion for real choices)
5. **Implement** the first cut
6. **Iterate** on rendered feedback
7. **Reflect** the lessons back into `docs/DESIGN_LANGUAGE.md`

Phases overlap and you may loop back — but don't skip 2, 3, or 7.

---

## 1. Critique

When the user shares a screenshot with a complaint, respond as a senior designer. Direct, opinionated, no "this could be improved" — say "this violates X because Y." The user shared the screenshot specifically because they wanted candid critique; wishy-washy phrasing wastes the turn.

Use the principle vocabulary from `docs/DESIGN_LANGUAGE.md` §12 (Composing list rows). Common offenders by symptom:

- **Label hugs left, value hugs right with a big gap** → §12 principle 1 (proximity broken).
- **Dotted leader lines between label and value** → §12 principle 11 (bandages reveal structural bugs — the layout, not the leaders, is the bug).
- **Tabular records rendered as a stack of cards** → §12 principle 2 (wrong container — cards are for objects you act on, rows are for comparable records).
- **Every element the same visual weight** → §12 principle 4 (no hero).
- **`STARTED` / `FINISHED` labels repeated on every row** → §12 principle 8 (repetition trains the eye to skip).
- **Raw timestamps with no duration** → §12 principle 9 (show derived values).
- **`F·E`, `↔`, other cryptic symbols where words would fit** → §12 principle 3 (density serves readability, not the reverse).
- **`#1234` with no role-label** → §12 principle 7 (format tells type; role still needs naming).

Lead with two or three of the loudest violations, not a full audit. A wall of critique is overwhelming and obscures the structural bug.

## 2. Clarify the user's task

Before designing, name what the user is **doing** on this screen. Everything else cascades from this — hero, density, what earns a column.

Use AskUserQuestion when the answer isn't already in conversation. Typical question:

> What's the primary task of someone looking at this screen?
> - Debug a finished run (hero ≈ duration / status)
> - Live monitor a running thing (hero ≈ current step / state)
> - Review the structure (hero ≈ type + target as a phrase)
> - **Mixed** (same view, multiple tasks)

When the answer is **mixed**, the hero splits into two layers — a stable row identity (same on every row) plus per-task scan signals as columns. See §12 principle 4. The two-layer hero is itself a lesson this codebase learned the hard way; it's not obvious from the doc alone.

If you find yourself proposing a layout without an answer to "what is the user doing on this screen?" — stop and ask.

## 3. Read `docs/DESIGN_LANGUAGE.md`

Always before designing. Sections by use case:

- **Critique vocabulary** → §12 principles 1–11 and §12 "Color channels"
- **Tokens** (no inventing) → §1
- **Buttons** (ghost / flat ghost / icons) → §3
- **Form controls** → §4
- **Field rows** (small inline label/value) → §5A
- **Overflow** — wrap names, ellipsise IDs → §5 "Overflow: identifiers vs names"
- **Status accents** (left border, banner, badge) → §6
- **Chips** — single base class + modifier variants, no per-feature chips → §7
- **Hero number** for big metrics → §9
- **Cards** (Standard, Status-accented, Status header, Disclosure) → §11
- **List rows** (rows vs cards) → §12
- **Detail page composition** (status header + hero-stat summary) → §11 "Composing a detail page top"
- **Don'ts** → §14

Hard rules from the doc (do not break):

- **No new colors.** If a token doesn't exist for what you want, pause and discuss — that's a doc question, not a freelance choice.
- **No per-feature chip classes** (no `.foo-direction-pill`). Add a modifier (`-success`, `-warn`, etc.) on the base.
- **No color emoji.** Tabler icons (`<i class="ti ti-X"></i>`) or monochrome Unicode only.
- **No solid filled buttons in panels.** Ghost + accent-tint.
- **Container OR children bordered, never both.**

## 4. Plan the layout with the user

Once you know the task (phase 2) and the palette (phase 3), walk decisions through with the user. Use AskUserQuestion for real choices — not for things already settled. Common decision points:

- **Hero of the row / view** — which field answers the user's primary question?
- **Verb vs noun visual weight** — for list rows, the categorical filter (the action verb) often deserves more weight than the noun (place / identifier), because the eye scans in two passes. See §12 principle 5.
- **Position** — left edge = state / filter, right edge = numbers (right-alignable), middle = identity. See §12 principle 6.
- **What earns a column** vs what lives off-row (tooltip, expand, detail panel).
- **Derived values to compute** (duration from timestamps, percentage from a ratio, elapsed-since-start) — so the user doesn't have to. See §12 principle 9.
- **Wrap vs ellipsis** — display names wrap, opaque IDs ellipsise. See §5 "Overflow".

When the user gives an answer that surprises you, push back gently with the principle, then let them redirect — they often have context you don't. (Example from past sessions: user said "don't show duration as a column" — principle 9 said otherwise; the pushback surfaced a compromise of inline `started → finished · 16s`.)

## 5. Implement

Edit the relevant JS + CSS in this repo. Common surface areas:

- **Route-runs editor** (the worked reference): `static/js/editor-route-runs.js` (`renderTaskRow`, `buildRunEditorHTML`) + `static/css/features/routes-paths.css`.
- **Other editor panels**: same `editor-<feature>.js` + `features/<feature>.css` pattern.
- **Train monitor, train control, inspector**: see the file map in `docs/DESIGN_LANGUAGE.md` §15 (Reference panels).

**Verify in browser** before declaring done. Don't ship CSS you haven't seen rendered.

1. Check the preview server (`mcp__Claude_Preview__preview_list`). If absent, start it with `name: atcs6-dev` from `.claude/launch.json`.
2. Reload after CSS/JS edits — some files don't hot-reload.
3. Use `preview_eval` to inject **synthetic data** covering every state real runs rarely combine: live row, pending, finished/ok, finished/late, finished/errored, plus edge widths (sidebar min ~440px, wider ~880px). Mount a temporary host div, render the component, take a screenshot.
4. Use `preview_inspect` for exact computed CSS (colors, font sizes, widths) when a screenshot is ambiguous.
5. Clean up the test container after each verification — don't leave it sitting in the DOM.

### Subgrid gotcha

If a header row + data rows need to share column widths, both must live inside a parent grid with `display: grid; grid-template-columns: …`, and each child uses `display: grid; grid-column: 1 / -1; grid-template-columns: subgrid;`. Separate grids do NOT share column widths even if their `grid-template-columns` strings are identical — each grid sizes its `auto` / `min-content` / `max-content` columns independently.

This caused the "header columns don't line up with data rows" bug in the route-runs refactor; subgrid fixed it.

## 6. Iterate

The first implementation will not be right. The user will redirect, and the redirect is the most valuable signal in the loop. Common iteration patterns from past sessions:

- **"The identity column got shrunk to `STA_…`"** → swap which column is `minmax(0, 1fr)`. Identity is `auto` (never shrinks). A secondary column (modifier / detail) becomes the flex shrinker, with a min-width floor (~60px) and `overflow: hidden; text-overflow: ellipsis` on the content. The row tooltip preserves what's truncated.
- **"The time column is too cramped"** → two-line stack when both timestamps exist (start on top, finish + duration on bottom); collapse to one line when only one exists. Vertical sequence implies "and then" — no arrow needed.
- **"What does `#1000001` mean?"** → format tells type, not role. Add the role word (`Run #1000001`). See §12 principle 7.
- **"The route name is truncated"** → wrap, don't ellipsise. Human-readable names use `word-break: break-word; overflow-wrap: anywhere;` — no `nowrap`, no `overflow: hidden`. See §5 "Overflow: identifiers vs names". Opaque IDs still ellipsise.
- **"`F·E` is cryptic"** → spell it out when space allows (`forward · east`). Density serves readability, not the reverse. See §12 principle 3. Keep the compact form in the `title` tooltip for power users.
- **"I want the tile coordinate too"** → the helper that builds the display string should accept the tile and slot it in (`forward · (9,4) · east`). If the helper is missing a parameter, add it; don't duplicate the formatting logic at the call site.
- **"Multiple rows is OK, you don't need to abbreviate"** → over-compaction is its own bug. For a detail-page summary, multi-row metadata is fine; the constraint is the user's screen, not your aesthetic.

Re-verify each iteration with a fresh screenshot. The loop ends when the user says they're satisfied, not when you think you're done.

## 7. Reflect into `docs/DESIGN_LANGUAGE.md`

This is the most important step and the easiest to skip. The user has asked for it explicitly in past sessions — they care about the compound interest.

When the user is satisfied, **ask what lessons should be reflected back into the doc**. Then propose specific edits. The most valuable lessons are the ones we only learned by getting it wrong first time — those are the principles that need sharpening.

For each lesson, decide where it lands:

- **Refine an existing principle in §12** — most common. Add a concrete example to make the rule sharper. (e.g. principle 7 "Format is the label" got `#1000001` as a worked example for the role-vs-type nuance.)
- **Split an existing pattern into two** — when the doc was collapsing cases that needed different answers. (e.g. §5 Overflow used to lump opaque IDs and human-readable names; the lesson was to split with different recipes.)
- **Add a new subsection** under §11 (Cards) or §12 (list rows) for a new composition pattern. (e.g. "Composing a detail page top" emerged from one session.)
- **Update the worked example in §12** — keep it matching what the code actually does. The example is documentation; if the implementation changes, the example must too.

When editing the doc, follow the spirit of `CLAUDE.md`'s authoring rules:

- Use simple English (the user reads the doc directly).
- Sweep all cross-references in one edit — never leave one site updated and another stale.
- Don't leave dead branches ("removed in vN", "kept for compatibility").
- Don't justify the current design by referring to a removed alternative.

After updating, confirm what landed where so the user can spot anything you got wrong.

---

## Anti-patterns

- **Designing without naming the user's task.** Skipping phase 2 is the most common way to spend hours on a refactor the user doesn't want.
- **Inventing tokens, classes, or hues.** The doc has all of them. If something is genuinely missing, that's a doc question, not a freelance choice.
- **Ellipsising human-readable names** to save horizontal space. Wrap them. The user wants to read them — ellipsis truncates exactly the differentiating tail.
- **Treating "rendered ok on my screen" as verification.** Use `preview_eval` + synthetic data covering every state at multiple widths. The narrowest sidebar (`440px` for `.route-editor-wide`) is where things break.
- **Skipping phase 7.** The doc updates are the compound interest on this work. Without them, the next session re-learns the same lessons.

## Pointers

- When in doubt about how a list-row or detail-page section should look, **copy the route-runs editor** as the reference implementation: `static/js/editor-route-runs.js` and the `.rr-*` rules in `static/css/features/routes-paths.css`. The patterns there are battle-tested — they're what produced §12 and the §11 "Composing a detail page top" subsection.
- `CLAUDE.md` has authoring rules for `design_new.md` that mostly apply to `DESIGN_LANGUAGE.md` too — simple English, no dead branches, sweep cross-references, no migration shims.
- All Python invocations need `conda run -n general` (per `CLAUDE.md`). UI work rarely hits Python, but the preview server uses it — `mcp__Claude_Preview__preview_start` with `name: atcs6-dev` handles the conda activation via `.claude/launch.json`.
