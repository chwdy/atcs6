"""Relative markdown links in docs/DESIGN_LANGUAGE.md must resolve to real files.

The design doc's authority rests on its references — when a linked CSS file
or TS module moves, the doc must move with it. This test fails on any
relative link whose target no longer exists, so doc rot surfaces in CI
instead of months later.
"""
import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "DESIGN_LANGUAGE.md"

# ](target) with an optional #anchor suffix; pure-anchor links (#section) are
# skipped because the capture requires at least one non-# character.
LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_design_language_links_resolve():
    text = DOC.read_text(encoding="utf-8")
    missing = []
    for link in LINK_RE.findall(text):
        if link.startswith(("http://", "https://", "mailto:")):
            continue
        target = (DOC.parent / link).resolve()
        if not target.exists():
            missing.append(link)
    assert not missing, (
        "Dead relative links in docs/DESIGN_LANGUAGE.md "
        f"({len(missing)}): {missing}"
    )
