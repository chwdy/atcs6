---
name: doc-review
description: Adversarial self-consistency review of design_new.md — hunt for contradictions, broken contract chains, ambiguous ownership, and tenet violations that would block a repo-aware engineer from refactoring without making judgment calls. Produces a severity-ranked findings report (HIGH/MEDIUM/LOW) with exact line citations and the precise edit each finding needs. Trigger whenever the user asks to "review design_new.md", "review the design doc", "find contradictions in the doc", "is the doc ready to hand to an engineer", "audit the design doc", "does the doc contradict itself", "check the doc for refactor-blocking gaps", or pastes a review rubric aimed at design_new.md — even if they don't say the word "review". Also trigger after a large doc edit when the user wants to know whether cross-references drifted. Do NOT trigger for comparing the doc against the CODE (that is the `gap-analysis` skill), for reviewing a code diff (`diff-review` / `code-review`), or for debugging simulation behavior (`journey-debug`).
---

# Reviewing design_new.md against itself

`design_new.md` is the source of truth for **changed** target behavior in the ATCS6 refactor. It is
not a standalone spec: the reader has the repo, can read current code for **unchanged** behavior, and
has ordinary engineering sense. So this review is not "is the doc complete?" — it is:

> Can a repo-aware engineer refactor to this document without making a single judgment call on a
> changed cross-component contract?

Every finding must trace back to that question. A gap the engineer can close by reading `block.py` is
not a finding. A gap where two sections of the doc disagree about who writes a field **is**, because
the engineer has to guess, and two engineers will guess differently.

This is doc-vs-doc. Doc-vs-code is the `gap-analysis` skill.

## The review standard

- `design_new.md` wins over the code wherever they disagree about changed behavior.
- The code and other docs are valid background for **unchanged** local behavior, helper internals,
  and retained schemas. Use them to decide whether an omission is safe, never to supply a changed rule.
- **Backward compatibility is explicitly out of scope.** Never flag missing migration shims, dual-write
  transitional fields, alias retention, or "legacy path X is unaddressed". Cleanly replacing an old
  contract is the desired outcome, not a gap.
- Common sense may fill in an obvious local implementation detail. Common sense may **not** invent a
  missing ownership rule, payload field, state transition, teardown order, or special-case branch.
- A low-severity ambiguity the engineer could resolve with one throwaway question to the author is a
  LOW. Any blocking cross-component contract that needs a question is at least a MEDIUM.

## The seven tenets the doc must express

Flag prose that drifts from these even where the local rule is internally consistent.

1. **Three phases are canonical.** Phase 1 Setup/planning, Phase 2 Execute (controller + physics),
   Phase 3 Feedback/cleanup. Cross-phase work happens through controller-owned latches and mailboxes,
   never a direct cross-phase call.
2. **Communication is flag/latch-based, not call-based.** Behavior self-resolves as the tick advances,
   from controller state, latches, and queued reports.
3. **No single-use-case branches.** Every rule is a general mechanism. A carve-out for one workflow —
   one cancellation kind, one route shape, one operator action, a post-EB restart arming step, a
   "this run type only" normalization, a one-shot retarget that bypasses cancel-and-replan — is a smell.
4. **Loose coupling.** Components talk through schemas, latches, registries, and projections. No direct
   back-references, hidden mirrors, or duplicated ownership.
5. **The state machine is self-driving.** Each state has explicit entry triggers, exit triggers, and a
   meaning that does not depend on which workflow caused entry. Behavior under EB / cancel / power-off /
   route-edit must be derivable from the table without prose exceptions.
6. **Similar steps share one mechanism.** Dwell is a general task step, not a special branch of route
   completion. EB recovery is ordinary cancellation recovery. Parallel-but-different definitions of the
   same concept are findings.
7. **A mechanism carries its preconditions, and every reuse checks them.** A rule defined for one
   situation — a terminal chosen for a train that *stops* there, a filter for a block the train *climbs
   into* — depends on a condition that makes it correct (its *holds-when*). Where the same rule is
   applied to a second situation, the doc must show that situation meets the holds-when, or explicitly
   waive it. A blanket "X also applies here" with no precondition is a smell: the reuse silently inherits
   an assumption the new situation may break. This is the one tenet a self-consistent doc can violate
   while every individual sentence still reads fine.

## Severity

- **HIGH** — two parts of the doc conflict on a changed rule; a required behavior cannot be implemented
  as written; a changed method/payload/state contract breaks somewhere in the chain; a missing contract
  would cause divergent implementations; the engineer needs a cross-component judgment call; a state is
  unreachable or ambiguously entered; a single-use-case branch contradicts the general mechanism.
- **MEDIUM** — an incomplete changed contract; unclear ownership; a missing trigger or teardown rule;
  stale rationale explaining the new design via the old mechanism; terminology inconsistent enough to
  mislead; a per-case branch where a general mechanism would do; a cross-phase handoff described as a
  direct call; coupling tighter than necessary.
- **LOW** — non-blocking ambiguity, weak cross-reference, dead branch, vague failure text.

Categories: `H-CONTRADICTION`, `H-SIGNATURE`, `H-API-CHAIN`, `H-GUARD`, `H-DEAD-BRANCH`,
`H-UNREACHABLE-STATE`, `H-PHASE-BOUNDARY`, `H-SPECIAL-CASE`, `H-PRECONDITION`, `M-PAYLOAD`, `M-OWNER`,
`M-CHAIN-CONTRACT`, `M-STATE-TRIGGER`, `M-BYPASS-CONSISTENCY`, `M-TERMINOLOGY`, `M-RATIONALE`,
`M-EXTERNAL-DEPENDENCY`, `M-CANONICAL-HOME`, `M-COUPLING`, `M-FLAG-HANDOFF`, `M-GENERALITY`,
`M-PRECONDITION`, `M-OPAQUE-REF`, `L-VACUOUS`, `L-UNRESOLVED-CONSTRAINT`, `L-DEAD-END`,
`L-STALE-CONTEXT`, `L-CROSSREF`. Pick the best fit; don't force one.

---

# The workflow

## 0. Pick the attack before you read anything.

Two questions arrive at this skill, and they want different first moves.

**"Did my recent edits drift?"** — the user has been editing and suspects they broke Authoring Rule 2.
Do not review the whole document. Diff it first:

```bash
git diff design_new.md            # working tree vs HEAD
git diff HEAD~3 -- design_new.md  # or against wherever the edits started
```

Read the whole diff and catalogue every identifier, rule name, method signature, and enum value the
edit *changed or renamed*. Then grep the **current** document for each **old** term. Every surviving
mention of an old name is a drift candidate; every renamed rule needs its referencing sites checked.
This is dramatically cheaper and higher-yield than a cold review, because the diff tells you exactly
which contracts moved, and the doc is far too large to re-derive that by reading. Only fall through to
the full sweep below for whatever the diff does not explain.

**"Is the doc self-consistent / ready to hand off?"** — a cold review. Run the full sweep, steps 1–6.

If you cannot tell which question was asked, ask. The two attacks cost very different amounts.

## 1. Map the document. Never hardcode line ranges.

```bash
PY=/opt/homebrew/anaconda3/envs/general/bin/python   # `conda run` dies in non-interactive bash
$PY .claude/skills/doc-review/scripts/doc_map.py --readers 6
```

This prints every `##` section with its real line range and a balanced, contiguous partition for the
readers. Run it every single time. The doc grows weekly, and the user often edits it *while you are
reviewing*. A range list written last month stops covering the tail — and the tail is where the
unreviewed contradiction lives. The partition balances by character count, not line count, because
this doc puts 4,700-character lines next to 3-character ones.

## 2. Build the two worklists.

```bash
$PY .claude/skills/doc-review/scripts/closure_claims.py            # closure claims
$PY .claude/skills/doc-review/scripts/closure_claims.py --enums    # union + enum coverage
$PY .claude/skills/doc-review/scripts/closure_claims.py --token run  # every mention of one token
```

These generate the two highest-yield hunting grounds. Read *High-yield techniques* below before you
use them — the lists are only useful if you know what you're testing.

## 3. Fan out the readers, all in one message.

One subagent per partition. Send them in a single message so they run concurrently. Give each one:

- its line range and the sections it owns;
- the review standard, the six tenets, the severity ladder, and the categories above;
- an instruction to **grep the whole document**, not just its range — a rule stated in its section is
  usually restated, and drifted, three sections away;
- an instruction to **verify each finding by re-reading the cited lines and grepping for restatements
  before reporting it**, because a false positive costs more than a miss;
- an instruction to also return a short list of what it checked and believes is **clean**, with line
  refs. You are going to re-test those;
- an instruction to **return its findings as its final message**, not to write a file. Subagents here
  often cannot get Write approved, and a reader that spends its budget fighting the filesystem returns
  nothing. You carry their outputs into the final report.

Three cost rules, learned the hard way — a single unbounded fan-out over this document ran past a
million tokens and took the whole review down with it:

- **Readers must not spawn subagents of their own.** Say so explicitly. Nested fan-out is where the
  cost explodes, and a reader that delegates cannot honour its own budget.
- **Never respawn a slow reader.** A reader that has gone quiet is usually still working. Respawning it
  duplicates the entire section and buys nothing; wait, or proceed with the readers you have and cover
  the gap yourself.
- **Give each reader a budget** — roughly "a few dozen tool calls, then report what you have". A reader
  that returns four verified findings and an honest clean list beats one that dies at 90% coverage.

If the budget is tight, cut readers before you cut verification. Three well-verified findings are worth
more than nine unverified ones, and an unverified finding costs the user more than a missed one.

**Fail soft.** A review that dies mid-sweep should still hand back what it proved. Whenever you hit an
API error, a budget ceiling, or a reader that never returns, stop immediately and emit the findings you
have already verified, with an explicit note about which sections went uncovered. Never let a partial
review evaporate; a report that says "here are four confirmed contradictions, and I never reached the
Route section" is useful, and losing it because you were mid-grep is not. The same applies to readers:
tell each one that if it runs out of room, it must return verified findings rather than nothing.

**Checkpoint each reader the moment it returns.** As soon as a reader hands back its findings, append them
to `.claude/scratch/review-<section>.md` — one file per partition, named for the sections that reader owned
— *before* you dispatch, consolidate, or do anything else. Open each file with a header line naming the
sections and line ranges that reader covered, so a resume can tell what is already done even if the doc was
re-partitioned since. This is crash insurance: a sleep, an API error, or a context reset mid-sweep otherwise
loses every reader that already came back. `.claude/scratch/` is gitignored, so these files never reach a
commit. They are intermediate only — the final report still goes inline in the conversation per step 6,
never assembled from a file.

## 3a. Resuming an interrupted fan-out.

If a sweep dies partway and the user says **"resume review"**, do not start the review over. Re-run
`doc_map.py` with the same `--readers` count to get the current partition, read every
`.claude/scratch/review-*.md` already on disk, and compare the sections those files claim to cover against
the partition. Re-dispatch readers for **only** the sections with no checkpoint yet; carry the checkpointed
findings forward unchanged. Once every section has a checkpoint, delete the scratch files, then continue at
step 5 (verify everything) with the full set. If the doc was edited between the crash and the resume, trust
the section names in the checkpoint headers over partition position, and re-review any section whose line
range shifted enough that the old coverage looks stale.

## 4. While they run, read the spine yourself.

Do not idle. Read the Core Runtime Loop end to end (Phase Table, Latch Table, Phase-3 step order,
canonical procedures) and work the closure-claim list. In practice the orchestrator finds the largest
cluster, because only the orchestrator holds the whole document at once. `Read` on this file blows the
25k-token cap; use `limit: 60..130` per call, or grep and read around the hits.

## 5. Verify everything. Especially the "clean" claims.

This is the step that separates a real review from a summary of six opinions.

- **Never relay a finding you have not re-read the cited lines for.** Readers mis-cite, over-state, and
  occasionally invent.
- **Re-test the "clean" claims.** Subagents declare sections clean that aren't. Spot-check each reader's
  clean list against the closure-claim worklist. Two of six were wrong on the last run — one had missed a
  dead enum value, another had certified an EB state-pair enumeration that was provably incomplete.
- **Re-grade severity yourself.** A reader's MEDIUM is often a HIGH once you see the site it contradicts.
- **Kill backward-compat findings**, restatements of unchanged local behavior, and demands for greenfield
  completeness.
- Deduplicate: the same root issue surfaces from three readers with different line refs. Merge, keep the
  sharpest fix, union the line refs.

## 6. Write the report in exactly this shape.

**Output the whole report as your reply in the conversation. Do not write it to a file**, do not put it in
a scratchpad, and do not hand it back as an attachment — the user reads it inline, in the transcript, and a
file is one more thing for them to open. Length is not a reason to move it out of the conversation: a long
review belongs in the conversation at full length. Never summarize the report and offer the full version
elsewhere. The only exception is an explicit request from the user in this session for a file.

Findings first, grouped by severity, HIGH then MEDIUM then LOW, ordered by importance within each group.

For each finding use exactly this header line:

```
[Severity]-[Category] — short title — line(s)
```

Then four labeled parts:

- the canonical changed rule **the doc must state**;
- the conflicting or incomplete locations, with line numbers and short quotes;
- why it blocks a repo-aware refactor engineer — frame it as *judgment call* versus *low-severity
  question they could just ask*;
- the exact fix needed in `design_new.md`.

Write fixes in imperative, mechanical language: "the doc must say X", "replace Y with Z", "state that A
owns B", "define the callback payload as …", "state that the acceptance predicate is …", "express this
as a flag-based handoff: writer X writes latch Y in phase P, consumer Z reads it in phase Q", "express
this as a general mechanism rather than a per-case branch for …".

Never ask a clarifying question inside a finding. If the intent looks inferable from the repo, say so in
one clause, then state what the doc must say anyway.

After the findings, add two sections:

- **Silent Coverage Gaps** — changed or shared contracts that feel under-specified overall even where no
  single line captures it, and tenet violations that span the document ("EB recovery is described in five
  places with five subtly different rules"; "dwell is treated as a special branch of route completion in
  three places").
- **External-Context Dependencies** — every place you had to read code or another doc to infer changed
  intent because `design_new.md` did not encode it.

Qualify every function as `Entity.method()`.

---

# High-yield techniques

Almost every serious finding is one of these shapes. Hunt them deliberately.

**Closure claims are testable, and the doc breaks its own.** Whenever the doc says *the only writer*,
*exactly one canonical home*, *the other legal case is*, *defined here once*, *every consumer*,
*trivially satisfied*, *by construction*, *never*, *always* — grep the subject across the whole file and
count what the rest of the document actually names. Nearly every HIGH comes from here. Real examples: a
field with a "the only runtime writer" claim in one section and three enumerated writers in another; a
state pair described as having "the other legal case" when four paths reach it.

**Trace every union member and enum value to every consumer.** Take each member of `task_kind`,
`FatalConflictKind`, `atc_limit_reason_code`, `PathAcquisitionFailureKind`, `initiator`, `deviation_kind`,
each latch name. Grep it. Check it is handled in the step chain, the writer table, the callback branches,
the validation rule, and the consumer list wherever its siblings are. A value that is defined, given a
rationale, and then handled nowhere is the single highest-value finding class. `closure_claims.py --enums`
sorts each union fewest-mentions-first; start at the top of every union and take no comfort from a small gap.

**Gate predicates that assume acceptance-time state.** A command accepted in Phase 2 under precondition P,
consumed in Phase 3 — with Phase-2 physics running in between. Check P still holds at consumption. "Trivially
satisfied" and "same-tick consumption" are the tells.

**A rule has three sites, and they drift.** The prose predicate, the `reason` string that describes it, and
the Logic Review Checklist item that tests it. Compare all three; the predicate is usually right and the
other two are stale.

**"Follows the same order/procedure as X" must be expanded step by step.** Walk the referenced procedure and
check every step is legal for the referring caller. A shared teardown order that includes a completion-only
controller callback will silently be applied by the cancellation path.

**Out of Scope versus required capability.** Grep the capabilities the recovery, teardown, and admin-repair
workflows depend on, then check them against the Out of Scope table. A recovery gate whose only exit
requires a capability the doc says "do not implement" is a dead end.

**Re-layer leftovers.** When a refactor merges or renames entities, old phrasing survives in prose that was
never swept. Grep the old concept's vocabulary. Two enum values describing the same physical event usually
means an entity merge left a dead one behind.

**A procedure referenced by name but absent from the canonical step chain.** Grep every "step" the prose
names and check it appears in the Phase Table and the Planning Kernel list.

**A mechanism is reused where its precondition may not hold.** Find each named rule / helper / exclusion
applied at more than one site. At its canonical home, look for the condition it assumes — "holds when the
train stops here", "when the block is climbed into", "for a stopped run". Read every *other* site through
that condition. Two shapes are findings: (i) the home states a precondition and a reuse site's context
violates it (`H-PRECONDITION` — this is where a self-consistent doc still specifies wrong behavior); (ii)
the home states *no* precondition though the rule is reused across visibly different contexts
(`M-PRECONDITION` — the reader cannot tell which reuses are valid). This is the one shape a pure
self-consistency pass otherwise misses, because each site reads fine alone. Real example: the
truncation-terminal rules are written for choosing where a train *holds*, then applied verbatim to a
moving train's per-tick braking cap, which the train rolls through rather than stops at — correct for the
parked case, wrong for the rolling case.

**A rule is referenced by position, or a load-bearing term is never defined.** Grep for cross-references
of the form "item N", numeric "step N", "the rule / bullet / table above / below". A rule pointed at by
list position is fragile (renumber the list and every reference silently moves) and opaque (the number
carries no meaning), so it hides what the rule assumes — the same blank that lets a bad reuse pass the
precondition check above. Flag each `M-OPAQUE-REF` and name the fix: give the rule a stable name and refer
to it by that. **Do not** flag the doc's intentional immutable step IDs (`66aa`, `9c4f`) — those are
stable handles, not positional. Separately, take each load-bearing term (a coined phrase, a mechanism
name, a defined-once concept) and confirm it has exactly one plain-English definition at a canonical home;
a term used across sections but defined nowhere is `M-TERMINOLOGY`.

---

# Gotchas

- `conda run -n general python` fails with a permission error in non-interactive bash. Call the binary:
  `/opt/homebrew/anaconda3/envs/general/bin/python`.
- `Read` on the whole doc exceeds the token cap. Slice it: `limit: 60..130`.
- `git grep -E "\bX\b"` matches nothing on macOS BSD ERE and yields a false "0 references". Use `grep -n`,
  and confirm deletions with `grep -F`.
- The user edits `design_new.md` in parallel with your review. Re-run `doc_map.py` if a citation looks off,
  and cite line numbers from the version you actually read.
- Section IDs (`9c4f`, `66aa`, …) are stable handles. Grep them to find every site that references a step.

# Adjudicated — do not re-flag

These were raised in earlier reviews and settled as intentional. Re-raising them wastes the user's time.

- `eb.clear()` as a separate ungated helper from `eb.release()`.
- `occupied_by` written in Phase 3 for known trains and in the Step-0 preamble for the unknown occupant.
- The reactive `RUNNING_MOVING` ↔ `READY` speed-based transition coexisting with `depart()`.
- All route progress routing through `RouteRun.advance_walk_position(...)`.
- `RouteExecutor.handle_path_completion(...)` matching only `stop` tasks.
- Non-deviation cancel being emergent (the train stops on its own authority, the cancel does not force it).
- The latch fan-out shape, the `c481`→`592a` re-resolve, and `display_state` as a derived projection: these
  are descriptions, not design bugs. Don't redesign them.
- `path_backed_run_active` means "has live authority", not "has a route".

Conversely, one thing that *is* a real doc bug and has been mistaken for a code bug before: tightening
`create_path()`'s covered-destination rejection from **head block** to **span coverage** breaks turnarounds,
where a long train's tail spills into the very block it must reverse into.
