---
name: gap-analysis
description: Audit an area of the codebase against the design doc (design_v2.md, DESIGN_LANGUAGE.md, or another canonical spec). Produce a severity-ranked gap report with file:line citations, propose a phased remediation plan, wait for user confirmation, then execute phase-by-phase with the test suite green between phases. Trigger when the user asks for a "gap analysis," "design vs implementation audit," "pre-v1 legacy cleanup," "doc/code reconciliation," "find leftovers from the old design," or otherwise wants to compare a documented contract to what the code actually does. Also trigger when the user asks to "clean up X" or "remove legacy Y" in a way that needs a survey-first approach. Do NOT trigger for narrow bug fixes or single-file edits — those are ordinary work; this skill is for the survey → plan → phased-execute loop where the design doc is the source of truth.
---

# Design-vs-code gap analysis

The point of this skill is to turn an "everything's drifted" suspicion into a structured, executable cleanup. Survey first, get the user to sign off on scope, then execute in phases with tests as gates. Skipping the sign-off step is the most common way this goes wrong — Claude over-scopes, the user pushes back, and the audit gets redone.

## The workflow

### 1. Pin down the scope before reading anything

The user's framing is almost always too broad ("audit pre-v1 leftovers," "find divergences"). Pin down three things:

- **Which doc is canonical?** `design_v2.md` for behavior, `docs/DESIGN_LANGUAGE.md` for UI, sometimes both. Ask if it's not obvious. `archive/design_new.md` is the archived v1 doc and is never the canonical doc.
- **Which code area?** A subsystem (route executor, ATO, train monitor), a file glob, or "everything" — push back on "everything" unless the user really means it.
- **What counts as a gap?** Missing-in-code, missing-in-doc, semantic divergence, naming drift, stale legacy fields, or all of these. Ask if any are out of scope.

One AskUserQuestion turn if the answers aren't already in conversation. Don't read code until scope is concrete.

### 2. Survey — read the doc first, then the code

Read the canonical doc section(s) fully before grepping code. The doc tells you what to look for; reading code first biases you toward "what the code happens to do is fine."

For `design_v2.md`, the code is a straight translation of the doc (see its **About this document**). That gives two kinds of gap, both real:

- **The doc says it, the code doesn't do it** — missing or wrong implementation.
- **The code does it, no section of the doc describes it** — code to remove, not a rule the doc forgot. Most of v1's code (routes, reservations past one block, ATO/ATC, calibration as a precondition) falls here. Report it as a legacy leftover. If you think the behavior passes the doc's evening test and is worth keeping, say so and propose the doc text, but never treat the code as the source of the rule.

For each documented concept, find its implementation site(s) in code. For each implementation site, check it matches the doc. Cite `file.py:line` for every claim. Don't summarize until the survey is complete — premature summarizing hides the gaps that turn out to be load-bearing.

### 3. Produce the gap report — severity-ranked, with citations

Render a table the user can read in under a minute:

| # | Severity | Gap | Doc cite | Code cite | Cause |
|---|---|---|---|---|---|
| 1 | Critical | Block voltage set outside **Track power** rules | `design_v2.md:120` | `block_power.py:412` | code bug |
| 2 | High | Claim release after 5 s empty not implemented | `design_v2.md:80` | — | missing impl |
| 3 | Medium | `bar` exists in code but not in doc | — | `block_manager.py:88` | doc gap or legacy |
| 4 | Low | Naming: code says `reservation`, doc says claim | `design_v2.md:47` | various | naming drift |

Severity rules:
- **Critical**: violates a documented invariant, could cause incorrect behavior in a normal scenario.
- **High**: documented contract not honored, could cause incorrect behavior in an edge case.
- **Medium**: documented vs implemented mismatch with no observable user-facing effect today but real cleanup value.
- **Low**: naming, comments, dead-code, doc tidiness.

For each gap, classify the cause: **code bug**, **doc bug**, **legacy leftover**, or **ambiguity** (doc is silent or contradictory). This drives where the fix lands.

### 4. Propose a phased remediation plan — then stop

Group gaps into phases that share a logical surface area or test gate. Each phase should:

- Land in one commit (or one PR if the user prefers).
- Leave the test suite green when complete.
- Be reversible independently of later phases.

Output the plan as a numbered list with the gaps each phase closes. **Then wait.** Do not start executing. The user's most common correction at this point is "skip phase N" or "do phase 2 first" — making this a sign-off step costs one turn and saves a re-do.

### 5. Execute phase-by-phase, tests between phases

After confirmation:

- Implement one phase at a time.
- Run the completion gate from `README.md` after each phase: `./run_all_tests.sh` under the `general` env (see CLAUDE.md, Running Python in this repo). Don't move on if anything fails.
- For UI phases, also verify in the preview (use the verify skill or preview tools per the project's verification workflow).
- After every phase, sweep cross-references — for `design_v2.md` work, the doc's **Authoring rules**, No cross-reference drift, means a patch that updates one section and leaves another stale is incomplete.

### 6. Close the loop — update the doc if the fix was code-side only

If a phase fixed code to match the doc, the doc is already correct — no update needed. If a phase fixed the doc (cause was "doc bug"), the code is already correct — no code change. If a phase resolved ambiguity, **update the doc in the same phase** so the next audit doesn't re-flag it.

## Common failure modes

- **Skipping the sign-off step.** Leads to over-scoped audits and rework. Always stop after the plan.
- **Reading code before doc.** You end up reporting "code does X, doc doesn't mention it" with a "legacy?" question mark on every line. Read the doc first, then you can tell what's actually drifted.
- **Citing "various files" instead of `file.py:line`.** A gap without a cite is a gap the user can't verify; rerun the grep and give the lines.
- **Fixing the easy gaps before the user agrees on scope.** Don't pre-execute "low-risk" fixes; some "low-risk" gaps turn out to be intentional.
