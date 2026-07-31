"""Sample live block voltage and occupancy from a running server.

Run this WHILE a train is moving. It answers one question: is the rail voltage
steady, or is it being cut and restored? A steady column of the same number
means the output is not pulsing and a stutter is coming from somewhere else; a
column that alternates with 0.00 means something is zeroing the block.

    /opt/homebrew/anaconda3/envs/general/bin/python tools/sample_track_voltage.py

Read-only: it drives nothing and changes no state.
"""

import argparse
import json
import time
import urllib.request
from collections import defaultdict


def sample(base: str, seconds: float, interval: float):
    volts = defaultdict(list)
    occupied = defaultdict(list)
    deadline = time.time() + seconds
    while time.time() < deadline:
        with urllib.request.urlopen(base + "/api/network", timeout=5) as response:
            network = json.load(response)
        for block_id, block in (network.get("blocks") or {}).items():
            volts[block_id].append(round(float(block.get("current_voltage") or 0.0), 2))
            occupied[block_id].append(block.get("occupied_by") or "-")
        time.sleep(interval)
    return volts, occupied


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5007")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args()

    volts, occupied = sample(args.base, args.seconds, args.interval)
    if not volts:
        print("no samples — is the server up?")
        return

    print(f"{len(next(iter(volts.values())))} samples\n")
    for block_id in sorted(volts):
        series = volts[block_id]
        # A drop to zero and back is the signature of the rail being cut.
        cuts = sum(
            1
            for a, b in zip(series, series[1:])
            if (a != 0.0 and b == 0.0) or (a == 0.0 and b != 0.0)
        )
        owners = sorted(set(occupied[block_id]))
        print(
            f"{block_id:<16} min={min(series):>6.2f} max={max(series):>6.2f} "
            f"zero-crossings={cuts:<4} occupied_by={owners}"
        )
        print(f"    {series[:40]}")


if __name__ == "__main__":
    main()
