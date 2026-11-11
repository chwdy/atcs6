#!/usr/bin/env python3
"""Map design_v2.md's structure and propose balanced reader partitions.

The point of this script is that section line ranges must never be hardcoded.
design_v2.md grows every week; a range list written last month silently stops
covering the tail of the document, and the tail is exactly where an unreviewed
contradiction hides. Derive the ranges every run.

Usage:
    conda run -n general python .agents/skills/doc-review/scripts/doc_map.py [doc] [--readers N]
"""

import argparse
import pathlib
import re
import sys

HEADING = re.compile(r"^(#{2,3})\s+(.*)$")


def read_lines(path: pathlib.Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        sys.exit(f"doc_map: no such file: {path}")


def find_sections(lines: list[str]) -> list[dict]:
    """Return top-level (##) sections with 1-indexed start/end lines."""
    heads = []
    for i, line in enumerate(lines, start=1):
        m = HEADING.match(line)
        if m and len(m.group(1)) == 2:  # '##' only; '###' are subsections
            heads.append({"line": i, "title": m.group(2).strip()})

    sections = []
    for idx, h in enumerate(heads):
        end = heads[idx + 1]["line"] - 1 if idx + 1 < len(heads) else len(lines)
        sections.append({"title": h["title"], "start": h["line"], "end": end,
                         "lines": end - h["line"] + 1})
    return sections


def weight(lines: list[str], start: int, end: int) -> int:
    """Characters, not lines. This doc has 1500-char lines next to 3-char lines;
    partitioning by line count hands one reader 10x the text of another."""
    return sum(len(l) for l in lines[start - 1:end])


def partition(sections: list[dict], lines: list[str], n: int) -> list[list[dict]]:
    """Greedy contiguous split so each reader gets a roughly equal character budget.
    Sections stay whole and in order: a reader that owns half a state-machine table
    cannot judge whether the table is complete."""
    for s in sections:
        s["weight"] = weight(lines, s["start"], s["end"])
    if n <= 1 or not sections:
        return [sections]
    total = sum(s["weight"] for s in sections)
    target = total / n

    groups, cur, cur_w = [], [], 0
    for s in sections:
        if cur and cur_w + s["weight"] / 2 > target and len(groups) < n - 1:
            groups.append(cur)
            cur, cur_w = [], 0
        cur.append(s)
        cur_w += s["weight"]
    if cur:
        groups.append(cur)
    return groups


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("doc", nargs="?", default="design_v2.md")
    ap.add_argument("--readers", type=int, default=3,
                    help="how many parallel section readers to plan for")
    args = ap.parse_args()

    path = pathlib.Path(args.doc)
    lines = read_lines(path)
    sections = find_sections(lines)
    if not sections:
        sys.exit(f"doc_map: no '## ' headings found in {path}")

    total_chars = sum(len(l) for l in lines)
    longest = max(len(l) for l in lines)

    print(f"{path}: {len(lines)} lines, {total_chars:,} chars, "
          f"longest line {longest:,} chars")
    print(f"  ~{total_chars // 4:,} tokens. Printing the whole file exceeds the output "
          f"cap, so use small sed slices.\n")

    print(f"{'lines':>12}  {'chars':>9}  section")
    print(f"{'-' * 12}  {'-' * 9}  {'-' * 40}")
    for s in sections:
        w = weight(lines, s["start"], s["end"])
        print(f"{s['start']:>5}-{s['end']:<6}  {w:>9,}  {s['title']}")

    groups = partition(sections, lines, args.readers)
    print(f"\nProposed {len(groups)} reader assignments "
          f"(contiguous, balanced by character count):\n")
    for i, g in enumerate(groups, start=1):
        lo, hi = g[0]["start"], g[-1]["end"]
        w = sum(s["weight"] for s in g)
        titles = ", ".join(s["title"] for s in g)
        print(f"  reader {i}: lines {lo}-{hi}  ({w:,} chars, {w * 100 // total_chars}%)")
        print(f"            {titles}\n")

    covered = groups[-1][-1]["end"]
    if covered < len(lines):
        print(f"WARNING: partitions stop at line {covered} but the file has "
              f"{len(lines)} lines. {len(lines) - covered} lines unassigned.")
    else:
        print(f"Coverage: lines 1-{covered} of {len(lines)}. "
              f"Front matter before the first '## ' heading "
              f"(lines 1-{sections[0]['start'] - 1}) belongs to reader 1.")


if __name__ == "__main__":
    main()
