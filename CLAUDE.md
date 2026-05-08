# CLAUDE.md

This file collects directives for working in this repo. Most rules below apply ONLY when editing `design_new.md`. They do not apply to other files unless stated.

## Scope

- All rules in this file that govern document edits apply ONLY to `design_new.md` for now.
- Other files (code, current docs, implementation index) follow their own conventions.

## Authoring rules for `design_new.md`

These are the rules the document expects every patch to honor. They are duplicated here so they are loaded into every session.

1. **v1 means v1.** No "removed in v1," "legacy," "deprecated," or "no longer used" prose anywhere in `design_new.md`. If a field, method, state, or flag is not part of v1, it must not appear at all. The absence of a row in a schema table is itself the statement that the thing does not exist.

2. **No cross-reference drift.** When any rule changes — ownership, payload field, state transition, callback signature, latch consumer, recovery gate — every site in the document that references the old rule must be swept and updated in the same edit. A patch that updates only one section is incomplete.

3. **One canonical home per concept.** Each shared schema, latch, predicate, and helper has exactly one canonical definition site. All other sites reference it by name. If two sites describe the same concept differently, the patch is incomplete.

4. **Latches in the canonical Latch Table.** Every latch must appear in the Latch Table (in the Core Runtime Loop section) with: writer phase + entity, consumer phase + entity, the "consumed by the last entity that uses it in the same 3-phase period" rule, and any multi-tick survival conditions. Latch behavior described elsewhere must cross-reference the table, not restate its rules.

5. **Phase boundaries in the canonical Phase Table.** The phase entity-chain table is the only place that defines which entity acts in which phase. All other sections cross-reference it by phase name and entity.

6. **No single-use-case branches in shared contracts.** If a workflow step is written as a per-case branch, it must either be expressed as a general mechanism or explicitly justified as the only case the general mechanism cannot cover.

## Style and language for `design_new.md`

- **Use simple English.** The user reads this doc directly and may push back when wording is jargon-heavy or hard to parse. Plain sentences beat clever phrasing. Spell out what something does instead of using terms like "canonical," "ordained," "defer to," or "anchored to" without context. If you would not say a term out loud to a junior engineer without defining it, do not write it; define overloaded terms in the Glossary section instead.
- **No stale or dead branches.** If a branch in a state machine, callback, or workflow describes a case that cannot occur in v1, remove it. Do not leave it as a defensive comment, "should not happen" note, or "for future safety."
- **No-op consumers must clear the latch.** If a consumer step finds nothing to do (no live path, no pending work, etc.) and is the last consumer of a latch in this 3-phase period, it must still clear the latch on this pass. Do not defer the clear to next tick. Audit every "no-op" or "skip" branch for this.
- **Every latch field has exactly one canonical writer helper.** The helper is the only assignment site for that field; outside callers route through it and never assign the latch field directly. The helper is also the canonical site for declaring the latch's sub-latch fan-out (when a higher-level latch implies a lower-level one). Latches have helpers regardless of whether they are leaf or composite. Non-latch state — ordinary booleans, optional fields, scalar settings that are not Phase-3 retained latches — is set by direct field write; helpers exist for those only when there is a multi-flag flow that a single helper coordinates.
- **Removed contracts are removed cleanly.** Backward compatibility is out of scope. Do not add migration shims, alias retention, dual-write transitional fields, or "kept for compatibility" notes.
- **Frame rationale positively.** When adding a "Why this shape" rationale to a canonical home, explain why the current design is correct on its own terms. Do not justify the current design by referring to a removed alternative ("Why X is excluded," "Why Y was rejected"). The doc does not document removed alternatives.

## When you edit `design_new.md`

- Sweep every cross-reference in one edit. The cross-reference drift rule means you cannot leave one section updated and another stale.
- Pick canonical homes carefully and link other sites to them by name.
- Match wording style to the rest of the section you are editing. Do not introduce new vocabulary mid-document.
- When in doubt about whether a branch is reachable in v1, remove it and let the engineer raise the question if it ever surfaces again.
