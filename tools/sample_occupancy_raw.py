"""Sample the pre-debounce occupancy reads from a running server.

Run this WHILE a real train is on the layout (moving or parked). It polls
`GET /api/hardware/occupancy/raw` faster than the 10 Hz tick, dedupes on
`read_count`, and reports — per block — the raw read stream the detector saw
before debounce, the confirmed stream after it, which source answered
(digital '165 bit vs INA current channel), and the measured mA while the
current channel was deciding.

It answers the mechanism question directly:
  - raw flicker on the CURRENT source of a driven block  -> PWM-phase sampling
  - raw flicker on the DIGITAL source                    -> real contact dropout
  - raw steady but confirmed drops                       -> debounce/attribution bug
  - flicker clustered around drive on/off               -> source-switch edges

    /opt/homebrew/anaconda3/envs/general/bin/python tools/sample_occupancy_raw.py

Read-only: the endpoint returns recorded state, no hardware traffic.
"""

import argparse
import json
import time
import urllib.request


def sample(base: str, seconds: float, interval: float):
    ticks = []  # one entry per NEW detector read: {"blocks": {...}}
    last_count = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        with urllib.request.urlopen(
            base + "/api/hardware/occupancy/raw", timeout=5
        ) as response:
            snap = json.load(response)
        if snap.get("read_count") != last_count:
            last_count = snap.get("read_count")
            ticks.append(snap)
        time.sleep(interval)
    return ticks


def edges(series):
    return sum(1 for a, b in zip(series, series[1:]) if a != b)


def bitstring(series):
    return "".join("1" if v else "0" for v in series)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5007")
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--interval", type=float, default=0.03)
    args = parser.parse_args()

    ticks = sample(args.base, args.seconds, args.interval)
    if not ticks:
        print("no samples — is the server up?")
        return

    print(
        f"{len(ticks)} detector reads over {args.seconds}s "
        f"(on_threshold={ticks[-1].get('on_threshold')}, "
        f"off_threshold={ticks[-1].get('off_threshold')})\n"
    )

    block_ids = sorted({bid for t in ticks for bid in (t.get("blocks") or {})})
    for block_id in block_ids:
        rows = [(t.get("blocks") or {}).get(block_id) or {} for t in ticks]
        raw = [bool(r.get("raw")) for r in rows]
        confirmed = [bool(r.get("confirmed")) for r in rows]
        sources = [r.get("source") for r in rows if r.get("source")]
        driven = [bool(r.get("driven")) for r in rows]
        ma = [r.get("current_ma") for r in rows if r.get("current_ma") is not None]

        print(f"{block_id}")
        print(f"    raw       {bitstring(raw)}   ({edges(raw)} edges)")
        print(f"    confirmed {bitstring(confirmed)}   ({edges(confirmed)} edges)")
        if sources:
            counts = {s: sources.count(s) for s in sorted(set(sources))}
            print(f"    source    {counts}, driven {sum(driven)}/{len(driven)} reads")
        if ma:
            thresh = next(
                (r.get("threshold_ma") for r in rows if r.get("threshold_ma")), None
            )
            print(
                f"    current   min={min(ma):.2f}mA max={max(ma):.2f}mA "
                f"threshold={thresh}mA over baseline"
            )
        # Where the raw stream flickered while the source was the current
        # channel of a driven block, the PWM-sampling hypothesis is confirmed.
        flicker_driven_current = sum(
            1
            for a, b, r in zip(raw, raw[1:], rows[1:])
            if a != b and r.get("driven") and r.get("source") == "current"
        )
        if flicker_driven_current:
            print(
                f"    !! {flicker_driven_current} raw edges happened on the "
                f"current channel while DRIVEN (PWM-phase sampling suspect)"
            )
        print()


if __name__ == "__main__":
    main()
