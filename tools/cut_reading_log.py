"""Cut a pass range out of a detector reading log, as a replay test fixture.

A rig run's log (`.runtime/detector_readings/readings-*.jsonl`) holds every
pass since the server started, and the operator commands between them, and most of its lines list only the blocks whose
reading changed, so a slice taken from the middle cannot be read on its own.
This tool writes a file that can: its first line lists every block's reading at
`--from-pass` (carrying `full`), and every line after it is copied as written,
up to `--to-pass`, with the operator commands sent between those passes.
`tools/reading_replay.py` reads the result. A cut that starts at pass 1 keeps
the run's `"start"` line, with what the server booted with, so `RunReplay` can
replay it whole; a cut from later on is for `ReadingPlayer`, with the starting
state set by hand.

    /opt/homebrew/anaconda3/envs/general/bin/python tools/cut_reading_log.py \\
        .runtime/detector_readings/readings-20261107-164742-921313.jsonl \\
        --from-pass 1 --to-pass 140 -o tests/replay/2026-11-07-two-locos.jsonl

Give several files, oldest first, for a run whose log was split into a new file
part-way. The first line of the cut keeps its own `full` value when it already
listed every block, and says `"cut"` otherwise. It also carries the
`environment`, `on_threshold` and `off_threshold` of the last line before it
that listed every block, so it reads like any other line that lists every block.

`--summary` prints, instead of writing a file, which blocks changed `present`
on which pass, to find the stretch worth cutting.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.reading_replay import fold_lines, read_lines  # noqa: E402

FULL_CUT = "cut"
_FULL_LINE_FIELDS = ("environment", "on_threshold", "off_threshold")


def cut(lines: List[Dict[str, Any]], from_pass: int, to_pass: int) -> List[Dict[str, Any]]:
    """The lines of the cut, first line listing every block."""
    passes = fold_lines(lines)
    by_no = {p.pass_no: p for p in passes}
    if from_pass not in by_no:
        raise SystemExit(f"pass {from_pass} is not in the log")
    if to_pass < from_pass:
        raise SystemExit("--to-pass is before --from-pass")

    # The full-line fields in force at `from_pass`.
    context: Dict[str, Any] = {}
    for line in lines:
        if "command" in line:
            continue
        if line["pass"] > from_pass:
            break
        if "full" in line:
            context = {k: line.get(k) for k in _FULL_LINE_FIELDS}

    out: List[Dict[str, Any]] = []
    for line in lines:
        if "command" in line:
            # A command sent after pass from_pass - 1 runs before the cut's
            # first pass, so it belongs to the cut.
            if from_pass - 1 <= line["after_pass"] < to_pass:
                out.append(line)
            continue
        n = line["pass"]
        if n < from_pass or n > to_pass:
            continue
        if n == from_pass:
            first = dict(line)
            first["full"] = line.get("full", FULL_CUT)
            for key in _FULL_LINE_FIELDS:
                first.setdefault(key, context.get(key))
            first["readings"] = by_no[n].readings
            out.append(first)
        else:
            out.append(line)
    return out


def summary(lines: List[Dict[str, Any]]) -> str:
    """One row per pass on which some block's `present` changed."""
    rows = []
    previous: Dict[str, bool] = {}
    for p in fold_lines(lines):
        present = p.present()
        if p.full is not None:
            rows.append(f"pass {p.pass_no:>6}  full={p.full}  present={sorted(p.present_blocks())}")
        else:
            changes = [
                f"{b}:{'on' if v else 'off'}"
                for b, v in sorted(present.items())
                if previous.get(b) != v
            ]
            if changes:
                rows.append(
                    f"pass {p.pass_no:>6}  t={p.simulation_time:8.1f}  {p.pass_kind:7}  "
                    + " ".join(changes)
                )
        previous = present
    return "\n".join(rows)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", help="log files, oldest first")
    parser.add_argument("--from-pass", type=int, help="first pass to keep (default: the first)")
    parser.add_argument("--to-pass", type=int, help="last pass to keep (default: the last)")
    parser.add_argument("-o", "--output", help="file to write")
    parser.add_argument(
        "--summary", action="store_true", help="print the passes where `present` changed"
    )
    args = parser.parse_args(argv)

    lines: List[Dict[str, Any]] = []
    for path in args.files:
        lines.extend(read_lines(path))
    if not lines:
        raise SystemExit("the log is empty")

    if args.summary:
        print(summary(lines))
        return 0
    if not args.output:
        raise SystemExit("give -o OUTPUT, or --summary")

    pass_numbers = [line["pass"] for line in lines if "command" not in line]
    if not pass_numbers:
        raise SystemExit("the log has no passes")
    from_pass = args.from_pass if args.from_pass is not None else pass_numbers[0]
    to_pass = args.to_pass if args.to_pass is not None else pass_numbers[-1]
    out = cut(lines, from_pass, to_pass)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for line in out:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
    commands = sum(1 for line in out if "command" in line)
    print(
        f"wrote passes {from_pass}..{to_pass} and {commands} commands to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
