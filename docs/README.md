# Documentation map

This folder is intentionally small. The project's design discipline is to keep one authoritative spec and a few sharp operational docs, not a sprawl of narrative architecture documents. AI agents and human reviewers read every doc in here as if it were ground truth, so each file must earn its place.

## Where things live

| Kind of information | Where it goes |
|---------------------|---------------|
| Refactor contracts and changed cross-component behavior (the v1 spec) | `/design_new.md` (root) — single source of truth |
| Project rules and authoring conventions for AI agents | `/CLAUDE.md` (root) |
| Developer-facing onboarding and conventions | `/AGENTS.md` (root), `/README.md` (root) |
| REST endpoint reference | `docs/API.md` |
| API design patterns (naming, response shape) | `docs/API_CONVENTIONS.md` |
| Save/load file format | `docs/PERSISTENCE_DESIGN.md` |
| Frontend design language (panel chrome, tokens, components) | `docs/DESIGN_LANGUAGE.md` |
| Detailed v1 reference specs (track encoding, signals, etc.) | `docs/reference/` |
| Open bugs (one file per bug) | `docs/bugs/` |
| Forward plans for unbuilt work (dated filenames) | `docs/plans/` |

## What does NOT go here

- **Narrative "how the system works" docs.** They drift. Extend `design_new.md` instead. If a contract is missing, add it to the spec.
- **"Implementation status" or feature matrices.** Git history and the spec are the authority.
- **Restating contracts in another file.** One canonical home per concept (the same rule that governs `design_new.md`).
- **Completed plans or resolved bugs.** Once shipped, delete them — git keeps the history.

## Adding a new doc

Before creating a new `.md` file, ask:

1. Is this a contract or system behavior? → It belongs in `design_new.md`.
2. Is this an open issue? → `docs/bugs/`.
3. Is this a forward plan for unbuilt work? → `docs/plans/<YYYY-MM-DD>-<slug>.md`.
4. Is this a reference spec for an external system or convention? → `docs/reference/`.
5. None of the above? → Probably don't create the doc. Tell the engineer instead.

## When a doc goes stale

Delete it. Backwards compatibility is out of scope (per `CLAUDE.md`). Tombstones, "removed," and "legacy" prose are forbidden in `design_new.md` and discouraged everywhere else. Trust git history.
