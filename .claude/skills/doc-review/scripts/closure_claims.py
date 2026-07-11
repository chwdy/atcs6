#!/usr/bin/env python3
"""Generate the two highest-yield worklists for a design-doc contradiction review.

Why this exists: almost every serious finding in this doc is one of two shapes.

  1. A CLOSURE CLAIM that a later section breaks. The doc says "the only writer",
     "the other legal case is", "defined here once", "trivially satisfied" — and
     somewhere else there is a second writer, a third case, a second definition,
     or a case where the gate does not hold. These claims are cheap to test and
     expensive to leave broken, because a code-generating reader takes them
     literally.

  2. A DISCRIMINATOR VALUE that is defined and then never handled. A new enum
     value, task kind, reason code, or failure kind gets a definition and a
     rationale, but no branch in the step chain, no writer in the writer table,
     and no consumer. Rare tokens are where these hide.

This script does not judge. It hands you a list of lines to go check.

Usage:
    conda run -n general python .claude/skills/doc-review/scripts/closure_claims.py [doc]
    conda run -n general python .claude/skills/doc-review/scripts/closure_claims.py --enums
    conda run -n general python .claude/skills/doc-review/scripts/closure_claims.py --token run
"""

import argparse
import collections
import pathlib
import re
import sys

# Phrases that assert a closed set, a unique owner, or an invariant.
# Each one is a promise the rest of the document has to keep.
CLAIM_PATTERNS = {
    # Plurals matter: "the only callers of the output interfaces" is a closure claim,
    # and a singular-only pattern silently skips it.
    "unique-owner": r"\b(the only|sole|single) (writer|owner|writer helper|mutator|"
                    r"entry point|source|site|definition site|caller|consumer|place|"
                    r"assignment site|store|authority)s?\b"
                    r"|\bexactly one (writer|owner|canonical|definition)\b"
                    r"|\bhas exactly one\b|\bmust be the sole\b|\bis the sole\b",
    "closed-enumeration": r"\bthe other (legal )?case\b|\bexactly (one|two|three|four)\b"
                          r"|\bthe (complete|canonical) (registry|set|list)\b"
                          r"|\bdefined here once\b|\bcanonical home\b"
                          r"|\bv1 values\b|\bthe only .{0,20}(values|kinds)\b"
                          r"|\bboth cases\b|\bthree stores\b",
    "universal": r"\bevery (consumer|writer|caller|site|other site|progress site|"
                 r"reference|entry|automatic)\b|\ball (four|three|five) \b"
                 r"|\balways\b|\bnever\b",
    "gate-assumption": r"\btrivially satisfied\b|\bby construction\b|\bcannot arise\b"
                       r"|\bis naturally satisfied\b|\bguaranteed by\b"
                       r"|\bno .{0,25} is needed\b|\bsame-tick consumption\b"
                       r"|\bthis never happens\b|\bcannot occur\b",
    "delegated-order": r"\bfollows (this|that|the) same (order|rule|workflow|procedure)\b"
                       r"|\bexactly as (for|it does)\b|\bthe same way\b"
                       r"|\bmirrors\b|\bsame all-or-nothing rule\b",
}

QUOTED = re.compile(r'"([a-z][a-z0-9_]{1,30})"')

# A markdown union: `"waypoint" \| "stop" \| "dwell" \| "run"`. Pipes inside
# tables are backslash-escaped, so tolerate both forms.
UNION = re.compile(r'"[a-z][a-z0-9_]*"(?:\s*\\?\|\s*"[a-z][a-z0-9_]*")+')
# The field the union belongs to: first backticked snake_case name on the line.
FIELD = re.compile(r"`([a-z][a-z0-9_]{2,})`")
# Enum values and tunable constants. Count bare occurrences, not backticked ones:
# a value is usually *declared* as `FOO` but *used* as `Kind.FOO` inside a wider
# code span, so counting only backticked names makes every value look dead.
CONST = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")
# A line that introduces an enum family, e.g. "`FatalConflictKind` ... v1 values:"
FAMILY = re.compile(r"`([A-Z][A-Za-z]*(?:Kind|Status|Mode|Condition|State))`")
FAMILY_CUE = re.compile(r"\bvalues\b|\bcanonical home\b|\bdefine[sd]? .* here once\b",
                        re.IGNORECASE)
# A bullet whose payload is a single backticked constant.
MEMBER = re.compile(r"^\s*[-*]\s+`([A-Z][A-Z0-9_]{3,})`")


def read_lines(path: pathlib.Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        sys.exit(f"closure_claims: no such file: {path}")


def excerpt(line: str, at: int, width: int = 130) -> str:
    lo = max(0, at - width // 3)
    s = line[lo:lo + width].strip()
    return ("…" if lo else "") + s + ("…" if lo + width < len(line) else "")


def report_claims(lines: list[str]) -> None:
    print("CLOSURE CLAIMS — each asserts a closed set, a unique owner, or an\n"
          "invariant. Go verify each one by grepping the subject across the whole\n"
          "document and counting. A claim the doc itself breaks is a finding.\n")
    total = 0
    for kind, pattern in CLAIM_PATTERNS.items():
        rx = re.compile(pattern, re.IGNORECASE)
        hits = []
        for i, line in enumerate(lines, start=1):
            m = rx.search(line)
            if m:
                hits.append((i, excerpt(line, m.start())))
        total += len(hits)
        print(f"── {kind}  ({len(hits)} hits)")
        for i, ex in hits:
            print(f"   {i:>5}: {ex}")
        print()
    print(f"{total} closure claims to test.\n"
          "Highest yield: 'unique-owner' and 'closed-enumeration'. For each, grep the\n"
          "subject and count the writers/cases the rest of the doc actually names.")


def report_enums(lines: list[str], threshold: int) -> None:
    text_counts: collections.Counter[str] = collections.Counter()
    const_counts: collections.Counter[str] = collections.Counter()
    const_first: dict[str, int] = {}
    for i, line in enumerate(lines, start=1):
        for tok in QUOTED.findall(line):
            text_counts[tok] += 1
        for tok in CONST.findall(line):
            const_counts[tok] += 1
            const_first.setdefault(tok, i)

    # ---- Section A: union members, compared against their own siblings ----
    print("UNION MEMBERS vs THEIR SIBLINGS\n"
          "A discriminator value declared in a union and then handled far less often\n"
          "than its siblings is usually a value some step chain, writer table, or\n"
          "callback branch forgot. This is the highest-value finding class in this doc.\n"
          "Members are sorted fewest-first. Start with the top one of every union and\n"
          "check it is handled everywhere its siblings are — there is no safe threshold\n"
          "here, so do not skip a union because the gap looks small.\n")

    seen: set[tuple[str, ...]] = set()
    for i, line in enumerate(lines, start=1):
        for m in UNION.finditer(line):
            members = tuple(QUOTED.findall(m.group(0)))
            if len(members) < 2 or members in seen:
                continue
            seen.add(members)
            fields = FIELD.findall(line)
            name = fields[0] if fields else "(unnamed union)"
            print(f"  line {i:>5}  {name}")
            for t in sorted(members, key=lambda x: text_counts[x]):
                mark = "  <-- check first" if t == min(members, key=lambda x: text_counts[x]) else ""
                print(f"     {text_counts[t]:>4}x  \"{t}\"{mark}")
            print()

    # ---- Section B: enum families, member by member ----
    print("\nENUM FAMILIES AND THEIR MEMBER COVERAGE\n"
          "Members of one enum, with how often each is mentioned. A member with a single\n"
          "mention is defined and never consumed. Two members describing the same event\n"
          "mean an entity merge left a dead value behind — check that each member names a\n"
          "condition no sibling also names.\n")
    families: list[tuple[int, str, list[str]]] = []
    for i, line in enumerate(lines, start=1):
        fam = FAMILY.search(line)
        if not (fam and FAMILY_CUE.search(line)):
            continue
        members: list[str] = []
        for nxt in lines[i:]:
            mm = MEMBER.match(nxt)
            if mm:
                members.append(mm.group(1))
            elif nxt.strip() and not nxt.startswith((" ", "\t")):
                break
        if members:
            families.append((i, fam.group(1), members))

    if not families:
        print("  (no enum families auto-detected; grep `Kind`/`Status`/`Mode` by hand)")
    for i, name, members in families:
        print(f"  line {i:>5}  {name}  ({len(members)} values)")
        for t in sorted(members, key=lambda x: const_counts[x]):
            print(f"     {const_counts[t]:>4}x  {t}")
        print()

    # ---- Section C: thin constants outside any family ----
    in_family = {t for _, _, ms in families for t in ms}
    rare = sorted(((n, t) for t, n in const_counts.items()
                   if n <= threshold and t not in in_family),
                  key=lambda x: (x[0], x[1]))
    print(f"\nOTHER THIN CONSTANTS (<= {threshold} mentions, not in a detected enum)\n"
          "Runtime-settings tunables legitimately appear once each, in their table.\n"
          "Look instead for a constant that a rule depends on but nothing defines.\n")
    for n, tok in rare:
        print(f"{n:>5}  line {const_first[tok]:>5}  {tok}")


def report_token(lines: list[str], token: str) -> None:
    rx = re.compile(re.escape(token))
    print(f"Every mention of {token!r}:\n")
    for i, line in enumerate(lines, start=1):
        m = rx.search(line)
        if m:
            print(f"{i:>5}: {excerpt(line, m.start(), 160)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("doc", nargs="?", default="design_new.md")
    ap.add_argument("--enums", action="store_true",
                    help="list rare quoted tokens instead of closure claims")
    ap.add_argument("--threshold", type=int, default=4,
                    help="a token seen this many times or fewer is 'rare'")
    ap.add_argument("--token", help="dump every line mentioning this exact token")
    args = ap.parse_args()

    lines = read_lines(pathlib.Path(args.doc))
    if args.token:
        report_token(lines, args.token)
    elif args.enums:
        report_enums(lines, args.threshold)
    else:
        report_claims(lines)


if __name__ == "__main__":
    main()
