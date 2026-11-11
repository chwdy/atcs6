---
description: Style, structure, and cutting rules that every edit to design_v2.md must honor.
paths:
  - design_v2.md
---

# Editing `design_v2.md`

These rules apply to `design_v2.md` only. They are scoped to this file so they load
when you work on it instead of sitting in every session's context.

The document's own rules — the evening test, v2 means v2, no cross-reference drift, one
home per concept, terms come from **Terms**, requirement words, and the rest — are in its
**Authoring rules** section. Read that section first. This file adds the sentence-level
rules for how a rule is written on the page.

## Style and language

- **Use simple English.** The user reads this doc directly and pushes back when wording is
  jargon-heavy or hard to parse. Plain sentences beat clever phrasing. If you would not say
  a term out loud to a junior engineer without defining it, do not write it; if a v2
  concept needs a name, add it to **Terms**.
- **No stale or dead branches.** If a rule describes a case that cannot occur in v2, remove
  it. Do not leave it as a defensive note, a "should not happen" remark, or "for future
  safety".
- **Removed rules are removed cleanly.** Backward compatibility is out of scope. Do not add
  migration notes, kept-for-compatibility fields, or two names for one thing.
- **Declarative voice.** State what v2 is and does ("v2 throws the switch", "a claim ends
  when…"), never edit instructions against the code ("Add X", "Remove Y", "Keep Z", "no
  longer", "for now"). How the code gets there belongs in a gap analysis, not in the doc.

## Sentence and structure style

Laying a rule out differently is a pure syntax change: every clause of the original
survives, and nothing is added except quantifier words ("all three", "any of these") and
first-use definitions. Removing a clause is a different kind of edit; it follows
**Cutting text** below. If restructuring or cutting would change a rule's meaning, its
actor, or which part of v2 does the work, stop and raise that instead of "simplifying" it.

1. **One rule per bullet, bold label first.** Every rule opens with a **bold label**
   naming it, followed by one short sentence stating what happens. A bullet that needs
   internal bold headers to stay readable is several bullets — split it.
2. **Conditions are lists, not comma prose.** A multi-part condition uses a quantifier line
   plus list items: "…when **all three hold**:" / "…when **any of these happens**:". Each
   item is one clause, with its exact meaning after a dash.
3. **When/then phrasing.** State rules as "When X, then Y", not trailing clauses ("…as soon
   as…", "…provided that…").
4. **Reasons are their own sentence, placed after the rule.** The why never sits inside a
   condition or in a mid-sentence parenthetical.
5. **Parentheticals only for definitions.** A parenthetical may hold the exact meaning of
   the word it follows — never an exception, a reason, or a second rule.
6. **The actor rule.** The subject of a rule sentence is the part of v2 that does the work.
   Never reword a sentence so that responsibility moves somewhere else: "the first train
   to claim wins" and "a later train gives up when it sees a claim" describe different
   designs, not two phrasings of one.
7. **Names that point at code are kept and defined.** A word that names real code — a
   field, method, or class — may be used, with a one-line plain-English meaning the first
   time it appears. Renaming it in prose without renaming the code creates a mismatch.
8. **Bold labels match how other sections refer to the rule.** When a rule is referred to
   by name elsewhere, its bold label is that exact name, so references stay findable.

## Cutting text

Each fact is stated once, at its home, and every other place points to it instead of
restating it. A clause may be removed when any one of these holds:

1. **It restates a rule whose home is elsewhere.** Replace it with a pointer such as
   "(see **Label**)", using the existing bold label or heading word for word. Before
   cutting, open the target and confirm it states the same fact at the same strength and
   with the same conditions. A pointer to a looser or narrower statement is wrong.
2. **It repeats a fact stated elsewhere in the same section.** Keep it once, where it is
   defined.
3. **It is a reason that only restates the rule.** Keep a reason that explains a
   non-obvious why an implementer would otherwise get wrong.
4. **It is filler.** "in v2", "below" / "above", "exactly" / "explicitly" when they add
   nothing.
5. **It is a negative that rules out something nothing in the text suggests.** Keep every
   prohibition, every safety rule, and every negative that rules out a plausible wrong
   implementation.

Never cut:
- a heading, or a bold label another section refers to (check with `grep -c -F`);
- a list item, a term in **Terms**, a value such as a voltage or a time, or a reason a
  train stops;
- a `must` / `must not` / `may` whose home is the sentence being edited.

**Mistakes reviewers have caught when shortening.** Check every merged or shortened
sentence against this list:
- **A call chain that blurs the actor.** "X calls the helper, which does A, then does B"
  reads as if the helper does B. When two parts act, give each its own sentence.
- **A lost "otherwise" case.** "X when one exists, and otherwise Y" must keep the Y.
- **A permission turned into a restriction, or an unscoped "only".** "It accepts X" is not
  "it accepts only X".
- **A dropped "while …" or "when …" exception.** Cutting it makes a rule looser than its
  home.
- **A condition stretched over more clauses.** "Does A when P, and does B" shortened to
  "does A when P, does B" reads as if P also gates B. Split the sentence.
- **A colon join** that makes a list define only its last item.
- **An implied extra writer or call** that does not exist.
- **A dropped scoping sentence.** Removing the sentence that defines a term widens every
  rule that uses it.
- **Two rules merged into one.** When two rules name different actors or bans, merging
  them widens a ban or loses a case. Keep them separate.

## When you edit `design_v2.md`

- Sweep every cross-reference in the same edit (the doc's **Authoring rules**, No
  cross-reference drift).
- Match the wording style of the section you are editing. Do not bring in new vocabulary;
  a new v2 concept goes into **Terms** first.
- When in doubt about whether a case can happen in v2, remove it and let the question come
  up again if it is real.
- After cutting text, have a reviewer who did not do the cutting compare the old and new
  text sentence by sentence. The writer's own re-read misses most of the mistakes listed
  under **Cutting text**.
