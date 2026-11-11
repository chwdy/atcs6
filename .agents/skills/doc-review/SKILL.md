---
name: doc-review
description: Adversarial self-consistency review of design_v2.md — hunt for contradictions, broken rule chains, unclear ownership, undefined terms, and rules that fail the doc's own evening test, anything that would force an engineer to make a judgment call while translating the doc into code. Produces a severity-ranked findings report (HIGH/MEDIUM/LOW) with exact line citations and the precise edit each finding needs. Trigger whenever the user asks to "review design_v2.md", "review the design doc", "find contradictions in the doc", "is the doc ready to hand to an engineer", "audit the design doc", "does the doc contradict itself", or pastes a review rubric aimed at design_v2.md — even if they don't say the word "review". Also trigger after a large doc edit when the user wants to know whether cross-references drifted. Do NOT trigger for comparing the doc against the CODE (that is the `gap-analysis` skill), for reviewing a code diff (`code-review`), or for debugging simulation behavior (`journey-debug`). Do NOT use it on archive/design_new.md, the archived v1 doc.
---

# Reviewing design_v2.md against itself

`design_v2.md` is the one bridge between the user, the agents who write code, and the code.
The code is a straight translation of it, and code that no section describes is not part of v2
(see the doc's **About this document**). So the review question is:

> Can an engineer translate this document into code without making a single judgment call?

Every finding must trace back to that question. A gap where two sections disagree about what
happens is a finding, because two engineers will guess differently. A gap the engineer can only
close by reading the current code is also a finding, because the code is changed to follow the
doc, never the other way round.

This is doc-vs-doc. Doc-vs-code is the `gap-analysis` skill. The v1 doc at `archive/design_new.md`
is not a source of rules: never use it to fill a gap in v2.

## The review standard

- `design_v2.md` wins over the code wherever they disagree.
- Code may show that a silence is harmless (an obvious local detail), never supply a missing rule.
- **Backward compatibility is out of scope.** Never flag missing migration notes or "the old
  behavior X is unaddressed". Code no section describes is removed, not grandfathered.
- Common sense may fill in an obvious local implementation detail. It may **not** invent a missing
  owner, trigger, ordering, voltage rule, stop reason, or special case.
- A low-severity ambiguity the engineer could settle with one throwaway question to the user is a
  LOW. Any rule that needs a question before two parts of the code can agree is at least a MEDIUM.

## What the doc must express

Flag prose that drifts from these even where the local sentence is internally consistent. The
first two come from the doc itself; read its **The evening v2 is for**, **The rules that come from
that goal**, and **Authoring rules** sections before reviewing anything else.

1. **Every rule passes the evening test.** A rule that exists only because a real railway has it —
   a reservation deeper than one block, an emergency for leaving a plan, a precondition before a
   train may move, a calibration step required to run — is a finding even when it is consistent.
2. **The doc's five rules hold everywhere.** The detectors decide where trains are; "run" is the
   basic instruction; the only protection is not entering another train's block; speed is a voltage
   knob; few steps. A section that quietly contradicts one of them is a finding.
3. **No single-use-case branches.** Every rule is a general mechanism. A carve-out for one
   situation is a smell unless the doc marks it as the one case the general rule cannot cover.
4. **Similar steps share one mechanism.** Two slightly different definitions of the same thing
   (two ways a claim ends, two ways a train becomes lost) are findings.
5. **One home per concept, and every term is in Terms.** Each rule is defined once; every other
   place points at it. A word that names a v2 concept and is missing from **Terms** is a finding.
6. **A mechanism carries its preconditions, and every reuse checks them.** A rule written for one
   situation (a train that is running, a block that is empty) and reused in another must still hold
   there. This is the one shape a self-consistent doc can get wrong while every sentence reads fine.

## Severity

- **HIGH** — two parts of the doc conflict; a required behavior cannot be built as written; a
  missing rule would cause two different implementations; a case the doc's rules can reach has no
  defined outcome; a rule breaks one of the doc's five rules.
- **MEDIUM** — an incomplete rule; unclear owner; a missing trigger or release; a term used but not
  in **Terms**; a per-case branch where a general rule would do; a rule that fails the evening test;
  a reason that explains a rule by pointing at a dropped design.
- **LOW** — non-blocking ambiguity, a weak cross-reference, a dead branch, vague message text.

**The HIGH gate.** A finding is HIGH only when **all three hold**:

- the rule's home is itself wrong or incomplete, or two homes disagree — a restatement that drifts
  from a complete home is MEDIUM at most, because the engineer follows the home;
- the failure scenario can actually happen using only rules the doc states — trace it step by step;
- an engineer who reads the home still has to make a judgment call.

A sentence that reads two ways is LOW. A place where the doc is **silent** is under-specified
(MEDIUM at most); never report it as "the doc says X happens", and never fill the silence from code.

Categories: `H-CONTRADICTION`, `H-UNDEFINED-OUTCOME`, `H-RULE-BREAK`, `H-SPECIAL-CASE`,
`H-PRECONDITION`, `M-OWNER`, `M-TRIGGER`, `M-RELEASE`, `M-TERMINOLOGY`, `M-EVENING-TEST`,
`M-CANONICAL-HOME`, `M-GENERALITY`, `M-PRECONDITION`, `M-RATIONALE`, `M-OPAQUE-REF`,
`M-EXTERNAL-DEPENDENCY`, `L-VACUOUS`, `L-DEAD-END`, `L-CROSSREF`, `L-WORDING`. Pick the best fit;
don't force one.

---

# The workflow

## 0. Pick the attack before you read anything.

**"Did my recent edits drift?"** — diff first:

```bash
git diff design_v2.md            # working tree vs HEAD
git diff HEAD~3 -- design_v2.md  # or against wherever the edits started
```

Catalogue every term, bold label, rule name and value (a voltage, a time, a list item) the edit
changed or renamed. Grep the current doc for each **old** one. Every surviving mention is a drift
candidate. Only fall through to the full sweep for what the diff does not explain.

**"Is the doc self-consistent / ready to hand off?"** — a cold review: steps 1–5.

If you cannot tell which was asked, ask. The two cost very different amounts.

## 1. Map the document.

```bash
PY=/opt/homebrew/anaconda3/envs/general/bin/python   # `conda run` dies in non-interactive bash
$PY .claude/skills/doc-review/scripts/doc_map.py --readers 1
```

This prints every section with its real line range. Never hardcode ranges: the doc changes often.

**Size decides the fan-out.** While the doc is under about 1,500 lines, review it yourself in one
pass and spawn no readers — a fan-out costs far more than it finds on a short doc, and only a
single reader holds every cross-reference at once. Above that size, run `doc_map.py --readers N`
with N = lines / 800, rounded up, and follow step 3.

## 2. Build the worklists.

```bash
$PY .claude/skills/doc-review/scripts/closure_claims.py            # closure claims
$PY .claude/skills/doc-review/scripts/closure_claims.py --token claim  # every mention of one term
```

Also list by hand: every term in **Terms**, every bold label, every numbered list (stop reasons,
release conditions, authoring rules) and every value (voltages, times). Read *High-yield
techniques* before using them.

## 3. Fan out readers (large doc only), all in one message.

One subagent per partition, sent together. Give each one: its line range; the review standard, the
six points under **What the doc must express**, the severity ladder with its HIGH gate, and the
categories; an instruction to grep the **whole** document, not just its range; an instruction to
verify each finding by re-reading the cited lines and grepping for restatements before reporting
it; and an instruction that every finding carries the rule's home (quoted), the refutation search
it ran, the scenario traced with doc rules only, and whether any step used code. Also ask for a
short list of what it checked and believes is **clean**, with line refs, and to return findings
as its final message rather than writing a file.

Cost rules: readers must not spawn subagents of their own; never respawn a slow reader; give each
a budget of a few dozen tool calls. Cut readers before you cut verification.

**Fail soft.** If the review dies mid-way, emit what you have verified, with a note naming the
sections not covered.

## 4. Verify everything. Especially the "clean" claims.

- **Never relay a finding you have not re-read the cited lines for.**
- **Verify the conclusion, not the quotes.** Accurate quotes prove nothing about whether the
  finding is true. For every HIGH and MEDIUM:
  - **Read the rule's home in full, not the grep hit.** Ask whether it already answers the finding.
  - **Try to refute it.** Search for the exception, the release rule, or the other condition that
    would make it false.
  - **Trace the scenario with doc rules and check every step can occur.**
  - **Keep code out of the argument.** When the doc is silent, report it as silent.
  - **Treat memory as a hint, not evidence.** Re-check anything recalled before citing it.
  - **Re-read before retracting, too.**
- **Re-test the "clean" claims** against the worklists.
- **Re-grade severity yourself, in both directions.** Apply the HIGH gate to every finding.
- **Kill backward-compat findings** and demands to describe things the doc deliberately leaves out.
- Deduplicate: merge findings with one root cause, keep the sharpest fix, union the line refs.

## 5. Write the report in exactly this shape.

**Output the whole report as your reply in the conversation. Do not write it to a file**, unless
the user explicitly asked for a file in this session. Length is not a reason to move it out.

Findings first, grouped HIGH, then MEDIUM, then LOW, ordered by importance within each group.
For each finding use exactly this header line:

```
[Severity]-[Category] — short title — line(s)
```

Then four labeled parts:

- the rule **the doc must state**;
- the conflicting or incomplete locations, with line numbers and short quotes — name the rule's
  home first and say whether it is itself wrong or only a restatement drifted from it;
- why it blocks an engineer translating the doc — *judgment call* versus *a question they could
  just ask*;
- the exact fix needed in `design_v2.md`, written in plain language that follows the doc's
  **Authoring rules** and `.claude/rules/design-v2-authoring.md`.

Write fixes in imperative, mechanical language: "the doc must say X", "replace Y with Z", "add T
to Terms with the meaning …", "state who releases the claim when …". Never ask a clarifying
question inside a finding.

After the findings, add two sections:

- **Silent Coverage Gaps** — rules that feel under-specified overall even where no single line
  captures it (for example: "nothing says what order a runtime step does its work in").
- **External-Context Dependencies** — every place you had to read code to understand intent
  because `design_v2.md` did not say it.

---

# High-yield techniques

**Closure claims are testable, and docs break their own.** Whenever the doc says *only*, *never*,
*always*, *all*, *every*, *at most*, *from these rules alone*, *the one case* — grep the subject
across the whole file and count what the rest of the document actually names. Example shape: "each
block's voltage comes from these rules alone" next to another section that sets a voltage.

**Trace every list item to every place that relies on the list.** Take each stop reason, each way
a claim ends, each operator action, each term. Grep it. Check it is handled everywhere its siblings
are. An item that is defined and then handled nowhere is the highest-value finding class.

**A condition checked at one moment and relied on later.** A check made when something is accepted
(the block is empty, the switch is free) and relied on later, with the layout changing in between.
Check the condition still holds when it is used.

**A rule has three sites, and they drift.** The rule's home, the **Terms** entry or **Review
checklist** item that restates it, and any message text. Compare all three.

**"Same as X" must be expanded step by step.** Walk the referenced rule and check every step is
legal in the new situation.

**A mechanism is reused where its precondition may not hold.** For each rule applied at more than
one site, find the condition it assumes at its home ("for a running train", "when the block is
empty") and read every other site through it.

**A rule referred to by position, or a term never defined.** References like "item 3", "the rule
above" are fragile and opaque: flag `M-OPAQUE-REF` and name the fix (give the rule a stable bold
label). A term used across sections but missing from **Terms** is `M-TERMINOLOGY`.

**The evening test, applied literally.** For every rule, ask what the evening loses if the rule is
deleted. If the answer is "nothing a model layout cares about", flag `M-EVENING-TEST`.

---

# Gotchas

- `conda run -n general python` fails with a permission error in non-interactive bash. Call the
  binary: `/opt/homebrew/anaconda3/envs/general/bin/python`.
- `git grep -E "\bX\b"` matches nothing on macOS BSD ERE and gives a false "0 references". Use
  `grep -n`, and confirm deletions with `grep -F`.
- The user edits `design_v2.md` in parallel with your review. Re-run `doc_map.py` if a citation
  looks off, run `git log -1 -- design_v2.md` before the final report, and drop any finding a newer
  commit already fixed.
- `archive/design_new.md` is v1. A finding that cites it, or that asks v2 to restore a v1 rule, is
  not a finding unless the rule passes the evening test on its own.

# Adjudicated — do not re-flag

None yet for v2. When the user rules that a raised finding is intentional, add it here with a
one-line reason.
