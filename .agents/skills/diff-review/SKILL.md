---
name: diff-review
description: Spawn a fresh sub-agent to review the current diff for behavioral regressions in adjacent functionality. The sub-agent identifies the surface area touched, enumerates adjacent behaviors that could regress, writes/runs targeted probes for each, and reports findings. Trigger when the user has just finished a feature or fix and wants to catch regressions BEFORE committing or pushing — especially for changes that touch shared logic, control loops, state machines, UI components, or anything with ripple effects. Also trigger on phrases like "double-check this," "look for regressions," "what else could this have broken," or "review the diff for side effects." Do NOT trigger for code-correctness review of the change itself (use `code-review` skill); this skill is specifically for *adjacent* behavior that the change might have silently broken.
---

# Diff-review for adjacent regressions

The point of this skill is to catch the class of bug where a change works as intended but silently breaks something nearby. The insights flagged two examples: a subsecond clock change introduced unix/sim timestamp mixing; an ATO change worked at the tuned operating point but caused overspeed at low speeds. The author of the change is too close to it to see the blast radius. A fresh sub-agent reading the diff cold can.

This is complementary to the built-in `code-review` skill (which looks at the change for correctness) and `verify` (which confirms the change does what it should). This skill asks the different question: **what else, that isn't in the diff, might now be broken?**

## The workflow

### 1. Confirm the diff is stable and locally testable

Before spawning a sub-agent, make sure:

- The change is fully written — no in-progress edits, no half-applied refactors.
- `git diff` (or `git diff main...HEAD` for a branch) shows the complete change.
- The project test suite passes locally: `conda run -n general --no-capture-output python -m pytest tests/ -q`. If it doesn't, fix that first — adjacent-regression hunting is wasted effort on a red baseline.

### 2. Spawn the sub-agent with a self-contained brief

Use the Agent tool with `subagent_type: general-purpose` (or `Explore` if the work is read-only). The brief must stand alone — the sub-agent has no memory of this conversation.

The brief should include:

- **The change**: paste the relevant diff hunks or point to specific files/lines. Don't say "the change we just made" — name it.
- **What the change is supposed to do**: one sentence of intent.
- **The surface area to consider**: name the subsystem(s) that share state, helpers, or callers with the changed code. If you're not sure, ask the sub-agent to map it.
- **What to do**: enumerate adjacent behaviors, predict which could regress, write/run probes (pytest, curl, preview interactions), report findings.
- **What NOT to do**: don't modify production code, don't re-review the change itself for correctness, don't widen scope to unrelated subsystems.
- **Output format**: a structured report with sections "Surface area touched," "Adjacent behaviors checked," "Probes run," "Findings (severity-ranked)," "Suggested follow-ups."

Cap the response length if the diff is small: "Report in under 400 words."

### 3. Read the findings carefully — separate signal from noise

Common categories of finding:

- **Real regression**: a probe failed that should have passed. Fix immediately, add a regression test, then re-run diff-review on the new diff.
- **Probable regression**: a probe revealed surprising behavior the sub-agent couldn't conclusively attribute. Investigate before dismissing.
- **Non-regression flagged conservatively**: sub-agent flagged something that's actually fine. Note why (so the next round's brief can scope it out) and move on.
- **Question worth asking**: sub-agent surfaces an ambiguity in the design. Route to the user or to `journey-debug` / `predict-then-test`.

### 4. Close the loop

For each real or probable regression: fix the code, add a test, run the suite. Then either re-spawn the sub-agent on the updated diff (if the fix was non-trivial), or call it done.

If the sub-agent found nothing, that's a valid result — say so explicitly in your end-of-turn summary so the user knows the review happened and was clean.

## Example brief (template)

```
I just made a change to <subsystem>. Here is the full diff:

<paste diff or list file:line ranges>

Intent: <one sentence>

Adjacent behaviors I want you to consider:
- <subsystem A that shares helper X with the changed code>
- <subsystem B that calls the changed function>
- <subsystem C that depends on the data structure the change modified>

Your job:
1. Map the actual surface area — confirm or expand my list.
2. For each adjacent behavior, predict whether the change could affect it and how.
3. Write a probe (pytest case, curl, or preview interaction) for each likely-affected behavior. Use `conda run -n general --no-capture-output python -m pytest ...` for Python.
4. Report findings as: Surface area touched / Adjacent behaviors checked / Probes run / Findings (Critical/High/Medium/Low) / Suggested follow-ups.

Constraints:
- Do NOT modify production code.
- Do NOT re-review the change for internal correctness — only adjacent regressions.
- Cap your report at 400 words; cite file:line for every claim.
```

## Common failure modes

- **Spawning the sub-agent on a red baseline.** Failing tests look like regressions; you can't tell signal from noise. Get to green first.
- **Vague brief.** "Review the diff for regressions" sends the sub-agent on a wide-ranging tour. Name the diff, the intent, and the adjacent surfaces.
- **Letting the sub-agent edit code.** It doesn't have your full context; an autonomous fix here is a setup for a second, harder regression. Findings only.
- **Dismissing low-confidence findings.** The "huh, that's weird" findings are exactly the class that pays for this skill. Investigate before discarding.
