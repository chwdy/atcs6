---
description: Authoring, style, and sweep rules that every edit to design_new.md must honor.
paths:
  - design_new.md
---

# Editing `design_new.md`

These rules apply to `design_new.md` only. They are scoped to this file so they load
when you work on it instead of sitting in every session's context.

## Authoring rules for `design_new.md`

These are the rules the document expects every patch to honor.

1. **v1 means v1.** No "removed in v1," "legacy," "deprecated," or "no longer used" prose anywhere in `design_new.md`. If a field, method, state, or flag is not part of v1, it must not appear at all. The absence of a row in a schema table is itself the statement that the thing does not exist.

2. **No cross-reference drift.** When any rule changes — ownership, payload field, state transition, callback signature, latch consumer, recovery gate — every site in the document that references the old rule must be swept and updated in the same edit. A patch that updates only one section is incomplete.

3. **One canonical home per concept.** Each shared schema, latch, predicate, and helper has exactly one canonical definition site. All other sites reference it by name. If two sites describe the same concept differently, the patch is incomplete.

4. **Latches in the canonical Latch Table.** Every latch must appear in the Latch Table (in the Core Runtime Loop section) with: writer stage + entity, consumer stage + entity, the "consumed by the last entity that uses it in the same tick" rule, and any multi-tick survival conditions. Latch behavior described elsewhere must cross-reference the table, not restate its rules. (The tick's stages are Observe → Plan → Action; see the Observe, Plan, and Action Table.)

5. **Stage boundaries in the canonical Observe, Plan, and Action Table.** The stage entity-chain table (the **Observe, Plan, and Action Table**) is the only place that defines which entity acts in which stage (Observe / Plan / Action). All other sections cross-reference it by stage name and entity.

6. **No single-use-case branches in shared contracts.** If a workflow step is written as a per-case branch, it must either be expressed as a general mechanism or explicitly justified as the only case the general mechanism cannot cover.

7. **Operator-visible changes start in Operator workflows.** When a patch changes what an operator does, sees, or can rely on — a command, a refusal, a status, a recovery step — update the matching step or table row in the **Operator workflows** chapter in the same edit (doc Authoring Rule 9). That chapter defines no rule of its own: each step names the section that defines it. If the workflow chapter does not change, the patch must not change operator-visible behavior.

## Style and language for `design_new.md`

- **Use simple English.** The user reads this doc directly and may push back when wording is jargon-heavy or hard to parse. Plain sentences beat clever phrasing. Spell out what something does instead of using terms like "canonical," "ordained," "defer to," or "anchored to" without context. If you would not say a term out loud to a junior engineer without defining it, do not write it; define overloaded terms in the Glossary section instead.
- **No stale or dead branches.** If a branch in a state machine, callback, or workflow describes a case that cannot occur in v1, remove it. Do not leave it as a defensive comment, "should not happen" note, or "for future safety."
- **No-op consumers must clear the latch.** If a consumer step finds nothing to do (no live path, no pending work, etc.) and is the last consumer of a latch in this tick, it must still clear the latch on this pass. Do not defer the clear to next tick. Audit every "no-op" or "skip" branch for this.
- **Every latch field has exactly one canonical writer helper.** The helper is the only assignment site for that field; outside callers route through it and never assign the latch field directly. The helper is also the canonical site for declaring the latch's sub-latch fan-out (when a higher-level latch implies a lower-level one). Latches have helpers regardless of whether they are leaf or composite. Non-latch state — ordinary booleans, optional fields, scalar settings that are not Action-consumed retained latches — is set by direct field write; helpers exist for those only when there is a multi-flag flow that a single helper coordinates.
- **Removed contracts are removed cleanly.** Backward compatibility is out of scope. Do not add migration shims, alias retention, dual-write transitional fields, or "kept for compatibility" notes.
- **Frame rationale positively.** When adding a "Why this shape" rationale to a canonical home, explain why the current design is correct on its own terms. Do not justify the current design by referring to a removed alternative ("Why X is excluded," "Why Y was rejected"). The doc does not document removed alternatives.
- **Requirement keywords are binding.** Write `must` / `must not` for contract requirements and `may` for permitted behavior; never write `should` in a normative statement (doc Authoring Rule 8). Code-generation agents treat these keywords literally.
- **Declarative v1 voice, not refactor-delta voice.** State what v1 is ("`X` is the…", "`Y` carries…"), never edit instructions against prior code ("Add X", "Remove Y", "Keep Z", "Stop doing W", "In the desired model…", "no longer", "for now"). The only sanctioned current-code name mappings live in the Scope terminology list and the Canonical Entity Index (e.g. `RouteRun` vs `RouteExecutionState`).

## Sentence and structure style for `design_new.md`

These rules govern how a dense rule is laid out on the page. Laying a rule out
differently is a pure syntax transform — every clause of the original survives, and
nothing is added except quantifier words ("all three", "any of these") and first-use
definitions of identifier-bound terms. Removing a clause is a different kind of edit;
it follows **Cutting text** below. If restructuring or cutting would change a rule's
meaning, its actor, or which entity owns the logic, stop and surface that instead of
"simplifying" it.

1. **One rule per bullet, bold label first.** Every rule opens with a **bold label**
   naming it, followed by one short sentence stating the action. A bullet that needs
   internal bold headers to stay navigable is actually several bullets — split it into
   labeled sub-bullets.
2. **Conditions are lists, not comma prose.** Multi-part conditions use an explicit
   quantifier line plus list items: "…when **all three hold**:" / "…when **any of these
   holds**:". Each item is one clause; its machine-checkable definition follows after a
   dash (e.g. "the authority did not grow this tick — the call reported
   `appended_resource_count == 0`"). This applies equally to trigger conditions and to
   reset/stop/exemption enumerations — a five-item reset list is a list, never a comma
   chain.
3. **When/then phrasing.** State rules as "When X, then Y," not trailing clauses ("…as
   soon as…", "…provided that…"). Enumerable step outcomes (branch targets, stop
   conditions) become lists too.
4. **Rationale is its own sentence, placed after the contract.** The *why* never lives
   inside a condition or as a mid-sentence parenthetical. State the contract, then state
   the reason.
5. **Parentheticals only for definitions.** A parenthetical may hold the
   machine-checkable definition of the term it follows — never an exception, a
   rationale, or a second rule.
6. **The actor rule.** The grammatical subject of a contract sentence must be the entity
   whose code performs the action. Never reword a sentence in a way that moves
   responsibility to a different entity — "higher priority overrides lower" and "lower
   priority yields when it sees a higher-priority waiter" describe different designs,
   not two phrasings of the same one.
7. **Two kinds of jargon, two treatments.** Pure prose jargon gets replaced with plain
   words ("Concrete" → "Exact", "yields" → "gives up"). Identifier-bound terms — a word
   that appears in a field, setting, or method name ("cap" / `acquisition_cap_block_id`,
   "idle-hold" / `idle_hold_ticks`, "contention snapshot") — are kept, but get a one-line
   plain-English definition at first use (e.g. "An idle hold is a run keeping its
   reserved blocks and switch locks while making no progress toward its target.").
   Renaming the prose without renaming the code creates a worse mismatch than leaving
   the term jargon-y and defining it.
8. **Bold labels must match how other sections reference the rule.** When a rule already
   has a name used elsewhere ("Idle-hold maintenance and release," "the reversal-trap
   exclusion"), its bold label is that exact name, so the restructure turns
   cross-references into visible anchors instead of orphaning them.

## Cutting text in `design_new.md`

The doc is read mostly through AI, so each fact is stated once, at its canonical home
(doc Authoring Rule 3), and every other site points to it instead of restating it. A
clause may be removed when one of these holds:

1. **It restates a rule whose canonical home is elsewhere.** Replace it with a pointer
   such as "(per **Label**)", using an existing bold label or heading copied verbatim.
   Before cutting, open the target and confirm it states the same fact at the same
   strength (`must` / `may` / plain statement) and with the same conditions. A pointer
   to a looser or narrower statement is wrong.
2. **It repeats a fact stated elsewhere in the same section.** Keep it once, where it is
   defined.
3. **It is a reason or consequence that only restates the rule.** Keep a reason that
   explains a non-obvious "why" an implementer would otherwise get wrong, such as an
   ordering that prevents a race.
4. **It is filler.** "in v1", "canonical" used as filler (keep it inside a real name such
   as "Canonical Entity Index"), "below" / "above", and "exactly" / "explicitly" when
   they add nothing.
5. **It is a negative clause that rules out something nothing in the text suggests.**
   Keep every prohibition (`must not`), every safety rule, and every negative that rules
   out a plausible wrong implementation.

Never cut:
- a heading, or a bold label that any other section cites (check with `grep -c -F`);
- a step ID, table row or column, schema field, state, transition, gate, refusal, or
  hardware value;
- a `must` / `must not` / `may` whose canonical home is the sentence being edited. A
  requirement keyword disappears only with a whole sentence cut under item 1 or 2.

Inside a canonical home (a schema table, state machine, step procedure, or a subsection
that names itself the home), cut only under items 2–5.

**Mistakes reviewers caught when shortening.** Check every merged or shortened sentence
against this list:
- **A call chain that blurs the actor.** "The consumer calls `helper()`, which does X,
  then calls Y" reads as if the helper calls Y. When two entities act, give each its own
  sentence.
- **A data type or value as the subject.** "A `StationTarget` builds the list" moves the
  work onto the type. Keep the entity that runs the code as the subject (the actor rule
  above).
- **A lost "otherwise" case.** "X, when one exists, and otherwise Y" must keep the Y.
- **A permission turned into a restriction, or an unscoped "only".** "They do accept X"
  is not "they accept only X"; "of the Action consumers, only the cancel consumer
  decides" is not "only the cancel consumer decides".
- **A dropped "while …" or "when …" exception.** "`/ato/stop` … idempotent while already
  not engaged" cut to a pointer whose home said "idempotent while `READY`" became looser,
  so a stop press could be ignored.
- **A condition stretched over more clauses.** "Writes A when P, and clears B" shortened
  to "writes A when P, clears B" reads as if P also gates B. Split the sentence instead.
- **A colon join** that makes a list define only its last item.
- **An implied extra writer or call** that does not exist.
- **A dropped scoping sentence.** Removing the sentence that defines a term widens every
  rule that uses it: "every forward span correction is reported" lost its definition and
  so covered vacate moves too.
- **Two rules merged into one.** When two rules name different callers or bans, merging
  them widens a ban or drops a read source. Keep them separate.

## When you edit `design_new.md`

- Sweep every cross-reference in one edit. The cross-reference drift rule means you cannot leave one section updated and another stale.
- Pick canonical homes carefully and link other sites to them by name.
- Match wording style to the rest of the section you are editing. Do not introduce new vocabulary mid-document.
- When in doubt about whether a branch is reachable in v1, remove it and let the engineer raise the question if it ever surfaces again.
- After cutting text, have a reviewer who did not do the cutting compare the old and new text sentence by sentence. The writer's own re-read missed most of the mistakes listed under **Cutting text**.
