#!/usr/bin/env python3
"""Deterministic cross-reference checker for ``design_new.md``.

Every marked cross-reference in the doc -- ``see **X**``, ``per the **X**``,
``under **X**`` and the like -- is supposed to point at something the document
actually defines: a markdown heading, or a bold label used as a definition
somewhere in the prose. This script verifies that. A reference whose target is
defined nowhere is a dangling pointer, and in practice that means a section or
rule was renamed or removed without updating the places that cite it.

This is the cheap guard meant to run on every commit. It catches mechanical
drift only -- a pointer that resolves to nothing. It says nothing about whether
the prose is *correct*; checking that the described behaviour still matches the
code is the job of the separate, more expensive LLM-based doc-review sweep. The
two are complementary: this one passing does not mean the doc is right, only
that its internal pointers still resolve.

How it decides what is a reference and what is a definition
-----------------------------------------------------------
* A bold span (``**...**``) immediately preceded by a reference cue word
  (``see`` / ``per`` / ``under`` / ``following``, optionally + ``the``, or
  ``in/of/from/via/through the``) is a **reference** -- it must resolve.
* Every other bold span, plus every markdown heading, is treated as a
  **definition anchor** -- something a reference is allowed to point at.
* A reference resolves when its text, normalised, equals some anchor's text.

Matching against the whole set of non-reference bold spans (not only headings)
is deliberate: most anchors in this doc are inline bold rule-labels, not
headings (e.g. ``**Braking-distance lock**`` is referenced dozens of times but
is not a ``##`` heading). Requiring a heading would flag hundreds of legitimate
pointers. The only failures this produces are references whose target appears
*nowhere* as a definition -- the genuinely dangling ones.

Known limitation: a bare ``the **X**`` with no cue word is treated as a
definition, not a reference, so a dangling pointer written that way is not
caught. This keeps false positives near zero at the cost of some recall; the
high-confidence cued references are what gate the build.

Run standalone for a one-shot audit::

    /opt/homebrew/anaconda3/envs/general/bin/python tools/check_doc_cross_references.py

Exit code 0 = no dangling references, 1 = at least one dangling reference or a
stale allowlist entry.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "design_new.md"
ALLOWLIST_PATH = Path(__file__).resolve().parent / "doc_cross_reference_allowlist.txt"

# A bold span. Non-greedy and asterisk-free inside, so adjacent bolds on one
# line (``**a** **b**``) stay separate and never span across a ``**`` boundary.
BOLD_RE = re.compile(r"\*\*(?P<text>[^*]+?)\*\*")

# An ATX markdown heading (``#`` .. ``######``), tolerating up to three leading
# spaces and an optional trailing run of ``#``.
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<text>.+?)\s*#*\s*$")

# A reference cue sitting immediately before a bold span marks it as a pointer.
# Anchored at end-of-prefix so it only fires on the word(s) touching the ``**``.
CUE_RE = re.compile(
    r"(?:\b(?:see|per|under|following)(?:\s+the)?"
    r"|\b(?:in|of|from|via|through)\s+the)\s*$",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Fold a heading/bold/reference to a comparable key.

    Drops inline-code backticks, collapses whitespace, removes a single
    trailing period (labels are written both ``**X rule.**`` and ``**X rule**``),
    strips a leading article (``see **the canonical lookahead payload**`` and
    ``**canonical lookahead payload**`` name the same anchor), and lowercases.
    Nothing else semantic is stripped, so ``Units`` and ``Units contract`` stay
    distinct on purpose.
    """
    text = text.replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(".").strip()
    text = text.lower()
    text = re.sub(r"^(?:the|a|an)\s+", "", text)
    return text


def is_pure_identifier(raw: str) -> bool:
    """True when a bold span is nothing but one inline-code identifier.

    A cued reference like ``see the **`pending_route_deactivate`** row`` cites a
    code identifier, not a bold-labelled section, so it belongs to the companion
    identifier-existence checker, not this one. Bold spans that mix code with
    prose (``**`Foo.bar(...)` switch handoff**``) are still section labels and
    stay in scope.
    """
    stripped = raw.strip()
    return (
        len(stripped) >= 2
        and stripped.startswith("`")
        and stripped.endswith("`")
        and stripped.count("`") == 2
    )


@dataclass(frozen=True)
class Reference:
    line: int
    raw: str  # the bold text exactly as written (minus the ``**``)
    normalized: str


@dataclass
class AuditResult:
    dangling: list[Reference]
    suggestions: dict[str, list[str]]  # normalized ref -> nearest anchor displays
    stale_allowlist: list[str]
    reference_count: int
    anchor_count: int

    @property
    def ok(self) -> bool:
        return not self.dangling and not self.stale_allowlist

    def format_report(self) -> str:
        lines: list[str] = []
        lines.append(
            f"Scanned {self.reference_count} marked cross-references against "
            f"{self.anchor_count} definition anchors in {DOC_PATH.name}."
        )
        if self.dangling:
            lines.append("")
            lines.append(f"{len(self.dangling)} dangling reference(s):")
            for ref in self.dangling:
                lines.append(f"  {DOC_PATH.name}:{ref.line}: **{ref.raw}** -> no anchor")
                near = self.suggestions.get(ref.normalized)
                if near:
                    lines.append(f"      did you mean: {', '.join(near)}")
        if self.stale_allowlist:
            lines.append("")
            lines.append(
                f"{len(self.stale_allowlist)} stale allowlist entr(y/ies) "
                f"(no longer dangling; remove from {ALLOWLIST_PATH.name}):"
            )
            for entry in self.stale_allowlist:
                lines.append(f"  - {entry}")
        if self.ok:
            lines.append("No dangling cross-references.")
        return "\n".join(lines)


def load_allowlist(path: Path = ALLOWLIST_PATH) -> set[str]:
    """Normalised set of references intentionally exempt from the check."""
    if not path.exists():
        return set()
    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(normalize(line))
    return entries


def parse_doc(text: str) -> tuple[set[str], dict[str, str], list[Reference]]:
    """Return (anchors, anchor_display, references) parsed from doc text."""
    anchors: set[str] = set()
    anchor_display: dict[str, str] = {}
    references: list[Reference] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = HEADING_RE.match(line)
        if heading:
            raw = heading.group("text").strip()
            key = normalize(raw)
            if key:
                anchors.add(key)
                anchor_display.setdefault(key, raw)

        for match in BOLD_RE.finditer(line):
            raw = match.group("text").strip()
            key = normalize(raw)
            if not key:
                continue
            prefix = line[: match.start()]
            if CUE_RE.search(prefix):
                if is_pure_identifier(raw):
                    # An identifier citation, not a section pointer; out of scope.
                    continue
                references.append(Reference(lineno, raw, key))
            else:
                anchors.add(key)
                anchor_display.setdefault(key, raw)

    return anchors, anchor_display, references


def audit(
    doc_path: Path = DOC_PATH,
    allowlist_path: Path = ALLOWLIST_PATH,
) -> AuditResult:
    text = doc_path.read_text(encoding="utf-8")
    anchors, anchor_display, references = parse_doc(text)
    allowlist = load_allowlist(allowlist_path)

    dangling: list[Reference] = []
    suggestions: dict[str, list[str]] = {}
    hit_allowlist: set[str] = set()

    anchor_keys = list(anchors)
    for ref in references:
        if ref.normalized in anchors:
            continue
        if ref.normalized in allowlist:
            hit_allowlist.add(ref.normalized)
            continue
        dangling.append(ref)
        if ref.normalized not in suggestions:
            near = difflib.get_close_matches(ref.normalized, anchor_keys, n=3, cutoff=0.6)
            suggestions[ref.normalized] = [anchor_display.get(k, k) for k in near]

    stale = sorted(allowlist - hit_allowlist)

    return AuditResult(
        dangling=dangling,
        suggestions=suggestions,
        stale_allowlist=stale,
        reference_count=len(references),
        anchor_count=len(anchors),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--doc", type=Path, default=DOC_PATH, help="path to the design doc"
    )
    args = parser.parse_args(argv)
    result = audit(doc_path=args.doc)
    print(result.format_report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
