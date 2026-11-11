---
name: journey-debug
description: Debug train-simulation behavior by predicting from `design_v2.md`, running the project's journey-test harness, comparing observed vs predicted, and finding the root cause — either a doc-clarity gap or a code bug — and proposing the fix that closes the gap. Trigger this whenever a user asks "what happens when …", "the train does X but I expect Y", "investigate why …", "is this behavior correct per the design", or otherwise reports a discrepancy between observed simulation behavior and the design contract. Also trigger when the user wants to verify how a specific operator command (set_cab, depart, ato_stop, power_off, EB, etc.) affects state at the end of a scene. Also trigger when the user wants to **tune** a control parameter or curve and see the effect — e.g. "ATO brakes too early / too gently, make it more efficient" — even when there is no bug, because validating a tuning change is the same predict→run→compare loop. Do NOT trigger for plain test-writing or test-running tasks ("run the demo journey test", "add an assertion to E5") — those belong to ordinary test work; this skill is for the predict→run→compare→diagnose loop, not for executing a known-good test.
---

# Journey-driven debug

The point of this skill is to turn vague behavior questions into a clean falsifiable loop: predict from the design doc, run a deterministic journey, observe, then close the gap. Every step exists because skipping it makes the diagnosis worse.

The journey-test framework lives at `tests/journey/`. Framework details — Scene/Journey/Event/Snapshot/triggers/assertions/actions, how to run, what the Snapshot exposes — are in `references/journey-testing.md`. Read that file when you need to write a journey, capture a snapshot, or call a specific assertion helper. The workflow below uses it but doesn't restate it.

## The workflow

### 1. Capture the scenario — ask, don't assume

Most reports come in vague ("the train does Y but I expected X"). Pin down three things before doing anything else:

- **Scene** — `DemoScene` (full demo network with DEMO_TRAIN at origin), `EmptyScene`, or a custom build. Ask the user; default to `DemoScene` only if they confirm.
- **Setup sequence** — what runs before the question? "demo plays out fully," "depart but stop early," "after EB latch and release," etc. Operator actions matter; cab letter and direction matter.
- **The trigger moment** — when does the question apply? "After train settles at terminal," "the tick after `set_cab` is drained," "during the third block traversal." Map this to a journey trigger (`train_stopped_for`, `at_tick`, `head_visited`, `block_entered`, etc.).

Ask all three in one turn (AskUserQuestion is fine if the answers aren't already in conversation). Don't move on until they're concrete enough that a journey can be written.

### 2. Predict the design-correct behavior — out loud, in writing

Before running anything, write down what the design says should happen. This is the falsifiable bit; you need it on the page so the comparison later is honest.

- Grep `design_v2.md` for the relevant area (operator command name, state name, latch name, reason code). Sweep the canonical home and every cross-reference — the doc has authoring rules forbidding drift, so contradictions you find are themselves a finding.
- Cite line numbers. "Per `design_v2.md:1730` the canonical lookahead without a path plan is …" beats "I think the design says …" because cited claims can be checked.
- List the predicted state per Snapshot field: `state`, `cab`, `ato_running`, `eb_active`, `speed_kmh`, `head_tile`, `active_path_id`, `assigned_route_id`, `atc_limit_kmh`, `atc_reason_code`, `latch_*`. Include the lookahead's distance-0 anchor (`atc_limit`, `operational_limit`, `atc_limit_reason_code`) — for ATC/ATO questions that field is where the action is.
- For each field, note WHY you predict that value (which line of the doc). The "why" is what the diagnosis later will compare against.

If the doc is silent or ambiguous on the trigger moment, **say so explicitly** — that's already a finding. Don't paper over it; ambiguity is one of the two outcomes this skill exists to surface.

### 3. Run the journey — write a focused one if the existing ones don't fit

Use the existing suite if a scenario matches. For new combinations, write a small journey at `tests/test_<scenario>.py` (and optionally a `tests/journey/journeys/<scenario>_journey.py`). The pattern is in `references/journey-testing.md`.

Hard rules from this codebase:
- **Always invoke Python through conda**: `conda run -n general --no-capture-output python -m pytest tests/test_<x>.py -v`. Plain `python` / `pytest` falls through to 3.9 and breaks on PEP 604 syntax in `server.py`. Same for ad-hoc probes.
- **Parametrize playback speeds** when the question might be dt-sensitive — `(0.1, "1x")`, `(0.2, "2x")`, `(0.3, "3x")` matches the demo journey's standard sweep. A bug that only shows at 3x is a real bug.
- **Don't write probe scripts that survive the investigation**. Investigation probes live in `/tmp/` or get deleted before reporting back. They're not the deliverable.

For state at a specific moment, the harness's `_capture_snapshot(tick, fixture, tracker)` returns the same `Snapshot` the assertion framework sees. Use it directly in a probe if you need raw fields not exposed through assertions yet.

### 4. Compare observed vs predicted — field by field, not "looks fine"

Render the comparison as a small table or list. For each field where observed disagrees with predicted, name the difference and the magnitude (not "different" but "atc_limit predicted 0, observed 50.0"). Don't summarize until the table is laid out — premature summarizing hides the field that turns out to be the real issue.

For a **tuning or dt-sensitivity** question the comparison artifact is different: a *metric × dt table*, not a single-moment snapshot. Pick the metric the user cares about (peak speed on a block, speed entering a restriction, total trip time), run the demo journey at dt=0.1/0.2/0.3, and lay out baseline vs candidate side by side. dt-invariance — the same metric across all three rows — is usually the actual goal; a value that swings with dt is the bug. When the question is "is the controller using the actuator the way the design intends?", **histogram a discrete decision over the whole run** (brake notch, handle position, reason code): "train maxes at B3 but the design implies B6" is answered by counting notch usage across the run, not by reading one tick.

If everything matches the design, the user's report is probably about a different scenario than the one you ran. Go back to step 1 and re-tighten.

### 5. Ask the user which behavior is correct — never assume

This is the part most easy to skip. The design doc and the user's intuition can both be right or wrong; you don't know without asking. The mismatch could be:

- Doc is right, code is wrong → code bug
- Code is right, doc is wrong → doc says the wrong thing; needs revising
- Doc is silent/ambiguous, code did something defensible but unwanted → doc clarity gap
- Doc is right, code is right, user's expectation was wrong → close the loop with explanation

State the three observable values plainly ("the doc says X, the code does Y, you said you expected Z") and ask which one is the intent. If the user volunteers the answer in their original message, still confirm — it's cheap insurance against a misread.

### 6. Find the root cause — doc gap vs code gap

Once you know the correct behavior, characterize the gap:

- **Doc actively says the wrong thing.** Quote the exact line. The fix is a doc edit plus a sweep of every cross-reference (see step 7).
- **Doc is silent.** Identify where it *should* speak (the canonical home for that concept). Adding a sentence there is the fix.
- **Doc says the right thing; code does something else.** Find the responsible function. The fix is a code edit. If the divergence has been there a while, also check whether other code paths bake in the same assumption.
- **Cross-referenced contradictions.** The doc's authoring rules (`CLAUDE.md`) forbid drift; if two sections disagree, the patch must reconcile both in a single edit, not pick one.

For code-side gaps, trace one call chain end-to-end before proposing the fix. Don't fix in the symptom layer if the offender is two layers up — that creates the same class of bug later.

### 7. Propose the fix — and identify ALL the sites it touches

Doc edits in this repo are governed by `CLAUDE.md`. The two rules that matter most for debug-driven patches:

- **No cross-reference drift.** If you change ownership, payload field, state transition, callback signature, latch consumer, or recovery gate at the canonical home, sweep every site that references the old rule in the same edit. Use grep to find them. A patch that updates one section is incomplete.
- **Frame rationale positively.** Explain why the new design is correct on its own terms. Don't justify it by reference to a removed alternative ("why X was excluded"). The doc doesn't document removed alternatives.

Code edits should:

- Touch the canonical writer/reader, not a downstream consumer.
- If removing a function or contract, delete it cleanly — no shim, no alias, no "kept for compatibility" comment. The repo's authoring style is removal, not deprecation.
- Add or refresh tests that pin the new behavior. The new tests should fail if the bug regresses.

For each fix, list:
1. The exact files and lines that change.
2. The cross-references swept (if doc) or call sites updated (if code).
3. The test that proves the fix works.

### 8. Verify — run the suite, not just the targeted test

After applying fixes, run the full `tests/` suite (or at minimum every file that imports anything you touched). The targeted test passing is a necessary but not sufficient signal — many of this codebase's tests bake in the old contract and need updating. New failures fall into three buckets:

- **Bakes in the old contract.** Update or delete per the new design.
- **Real regression.** Genuinely broken by your fix — back out or refine.
- **Razor-thin numerical thresholds.** A 0.02 km/h shift in a margin test is usually noise, not a regression; relax the threshold and document why.

Sort each failure into one of those buckets explicitly. Don't relax thresholds without saying so.

**A green probe is not a green suite.** A focused probe measures the metric you optimized; the journey suite's continuous invariants (`no_overspeed`, `no_eb`, …) measure what you might have *broken*. A tuning change can look perfect on its target metric and still trip an invariant elsewhere — e.g. a faster, more efficient approach that then overshoots a fast-collapsing authority-stop limit during the final creep to the platform. That regression is invisible to a probe that only plots approach speed; only the full journey tests catch it. Always run them before declaring a tuning change done.

## Things worth doing during the loop, not after

- **Stash and re-run on main** when a borderline test fails after your change. `git stash` → run → `git stash pop`. If it passed before and fails now, the change is real. If it failed before, it's pre-existing.
- **Print the full `get_lookahead()` payload** when ATC/ATO mismatches don't match field-level intuition. The structure matters: how many `speed_curves` anchors, what `reason_code` each carries, whether `clearance_cm` reflects the path or a fallback.
- **Check `_last_atc_limit_reason_code` (enum) and `speed_limit_reason` (string) together.** They can disagree if the structured code isn't being mirrored from the lookahead — that's a known shape of bug.
- **Build a minimum-repro probe before instrumenting production code** with prints. Probes live in `/tmp/` and the project root, get deleted at the end, and use `DemoScene().build()` for fast iteration without a full pytest spin-up.

## What this skill produces

A debug session under this skill ends with one of:

1. **No bug.** Predicted matches observed and the user agrees — explain why, link the design line that proves it, walk away.
2. **Code bug + fix.** Diff in code + new/updated test that pins the corrected behavior + a one-line summary of why the doc was right.
3. **Doc bug + fix.** Diff in `design_v2.md` (with sweep of cross-references) + a one-line summary of why the code was right.
4. **Both.** Doc was ambiguous, code interpreted it one way, intent was the other. Doc gets clarified; code gets updated to match; tests pin the resolved behavior.
5. **Tuning change + validation.** Doc and code agree and the behavior is *correct but suboptimal* — the user wants to change the design intent itself (brake later, hold speed, smooth a curve, track a notch the design implies but the code never reaches). There is no bug. The predict→run→compare loop is identical; only the root-cause step changes — instead of naming a doc/code gap you name the current behavior, the desired behavior, and the knob (a parameter, a curve shape, a control law) that moves between them. The deliverable is that change, validated against a metric across the dt sweep, plus a journey assertion that pins the new target so it can't silently regress.

In every case, the deliverable identifies whether the fix touches the doc, the code, or both — and proves the fix works by running the suite.

## Anti-patterns to refuse

- **Jumping to a fix before predicting.** If a session goes straight to "the code does X, let me change it," you've skipped step 2 and you don't know whether the code or the design is wrong.
- **Hand-waving "the doc probably means …".** If the doc is unclear, that *is* the finding; surface it.
- **Marking a test broken because it fails.** Tests fail for reasons; categorize the reason before editing.
- **Sweeping fixes that touch dynamics outside the reported scope.** A debug session is narrow by design. Out-of-scope cleanup goes into a separate task.
