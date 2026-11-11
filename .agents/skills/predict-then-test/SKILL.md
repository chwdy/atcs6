---
name: predict-then-test
description: General-purpose predict→test→reconcile debugging workflow for non-simulation code. Read the spec/doc/contract, write down the predicted behavior with citations BEFORE looking at runtime behavior, then run a probe, then attribute any gap to (a) spec ambiguity, (b) spec-wrong, or (c) code-bug. Trigger when the user reports a discrepancy between expected and observed behavior in non-train-sim code — API responses, UI state, request handling, data flow, config interpretation — and wants grounded reasoning rather than speculation. Use when the user asks "why does X return Y," "is this behavior correct," "what should happen when," or pushes back on a guess-first answer. Do NOT trigger for train-simulation behavior (use `journey-debug` instead), for plain test-writing tasks, or when the spec is the user's own informal request rather than a written contract.
---

# Predict-then-test debugging (general)

The point of this skill is to keep diagnosis honest. The temptation, when a behavior question lands, is to read the code and reason backwards from what it does — but that path bakes in the assumption that whatever the code does is correct. Predicting from the spec FIRST forces the comparison to be falsifiable.

This is the generalized companion to `journey-debug` (which covers train-sim behavior via the journey harness). Use this one for backend routes, frontend state, config-driven behavior, library wrappers, or anything where the spec is a written contract but the runtime isn't the journey harness.

## The workflow

### 1. Find the canonical contract

What is the spec for this behavior? Candidates in this repo:

- `design_v2.md` — runtime contract for the simulator/server
- `docs/DESIGN_LANGUAGE.md` — UI contract
- A docstring or module-level comment on the function in question
- A README or `docs/` page
- An external library's documentation

If there is no written spec, **say so** and ask the user what the contract should be — don't reverse-engineer it from the code. "There's no spec for this; what's the intended behavior?" is a valid answer.

### 2. Predict — write it down, cite lines, BEFORE running anything

Open the spec. For the scenario in question, write:

- The predicted output, state change, or side effect.
- For each field/value, the line in the spec that justifies it: "Per `design_v2.md:1842` the response should include `assigned_route_id` when a route is locked."
- If the spec is silent or contradictory on any aspect, name it explicitly — ambiguity is itself a finding.

Do not look at the implementation yet. The whole point is that the prediction is unbiased.

### 3. Probe — minimal, scoped, reproducible

Construct the smallest probe that exercises the scenario. Options:

- A `curl` or HTTP request against the running server.
- A `python -c` snippet that imports and calls the function.
- A targeted pytest case in `/tmp/` or as a temporary test file.
- A preview interaction (`preview_eval`, `preview_click`) for UI behavior.

Hard rules:

- **Conda env**: `conda run -n general --no-capture-output python ...`. Plain `python`/`pytest` falls through to system 3.9 and breaks on the project's PEP 604 syntax.
- **Right surface area**: if the question is about route V2, probe V2 — not V1. The insights flagged "wrong route version" as a recurring waste; the probe checklist before running is: route version correct? tick limit high enough to reach the trigger moment? state initialized correctly?
- **Disposable probes go in `/tmp/`** or are deleted before reporting. They're not the deliverable.

### 4. Compare — field-by-field, not "looks fine"

Lay out predicted vs observed as a table or list. For each field that disagrees, name the difference concretely: not "different" but "`assigned_route_id` predicted `route-2`, observed `None`."

Don't summarize until the table is laid out. Summarizing early hides the field that turns out to be the real issue.

### 5. Attribute the gap — which layer is wrong?

For each disagreement, classify as:

- **Spec-ambiguous**: the spec doesn't pin down this case → doc clarity work needed; ask the user which behavior is intended.
- **Spec-wrong**: the spec says one thing but the user/team actually intends another → doc fix needed.
- **Code-bug**: the spec is clear and the user agrees with it, the code doesn't honor it → code fix needed.

**Ask the user** which of these applies when it isn't obvious — both the spec and the user's intuition can be wrong. Don't assume.

### 6. Fix in the right layer

- Code-bug → patch the code, add a regression test, run the suite.
- Spec-wrong → update the spec, sweep cross-references (per AGENTS.md rule 2 for `design_v2.md`).
- Spec-ambiguous → resolve the ambiguity in the spec, then either the code already complies or move to "code-bug."

## Common failure modes

- **Skipping the prediction step** and reading the implementation first. The prediction stops being a prediction; it becomes a justification.
- **Speculating about code behavior when the user asked for design-correct behavior.** If the user is asking "what SHOULD happen," the answer is from the spec, not from `grep`. The insights flagged this twice.
- **Probing the wrong surface** (route V1 instead of V2, wrong endpoint, stale preview context). Run the probe checklist before executing.
- **Fixing before attributing.** Don't reach for `Edit` until you know which layer is wrong.
