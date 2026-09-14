"""Headless tick profiler for the default demo scene (demo_6).

Boots the runtime the same way the server does (bootstrap_runtime), then drives
sim_controller.update(dt) in a loop with NO sleep, measuring wall-clock per-tick
cost and how it scales as trains enter service. Finishes with a cProfile pass
over a busy window to surface the hot functions.
"""
import cProfile
import io
import pstats
import statistics
import sys
import time

import bootstrap

DT = 0.1            # matches runtime.py cadence (time.sleep(0.1), 1x speed)
WARMUP = 50         # let the first departures get going
TOTAL = 1200        # total ticks to drive (120 simulated seconds)
PROFILE_TICKS = 400 # cProfile window length, taken at the busy tail


def active_counts(sim):
    on = rolling = 0
    for c in sim.controllers.values():
        if c.powered:
            on += 1
            if c.physics_sync.current_speed_kmh > 0.1:
                rolling += 1
    return on, rolling


def main():
    print("Booting demo_6 ...", flush=True)
    network, path_dispatcher, sim, persistence = bootstrap.bootstrap_runtime(
        scene="demo_6"
    )
    print(
        f"scale: trains={len(network.trains)} blocks={len(network.blocks)} "
        f"tracks={len(network.tracks)} switches={len(network.switches)} "
        f"signals={len(network.signals)}",
        flush=True,
    )

    # Warmup (not measured).
    for _ in range(WARMUP):
        sim.update(DT)

    # Timed drive: record per-tick ms, print a scaling line every 100 ticks.
    per_tick_ms = []
    window = []
    print("\ntick   sim_t   on  rolling  | last100 ms  med / mean / p95 / max", flush=True)
    for i in range(WARMUP, WARMUP + TOTAL):
        t0 = time.perf_counter()
        sim.update(DT)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        per_tick_ms.append(dt_ms)
        window.append(dt_ms)
        if (i - WARMUP + 1) % 100 == 0:
            on, rolling = active_counts(sim)
            w = sorted(window)
            med = statistics.median(w)
            mean = statistics.mean(w)
            p95 = w[int(0.95 * len(w)) - 1]
            mx = w[-1]
            print(
                f"{i+1:5d}  {sim.simulation_time:6.1f}  {on:3d}  {rolling:5d}    | "
                f"{med:7.2f} / {mean:6.2f} / {p95:6.2f} / {mx:6.2f}",
                flush=True,
            )
            window = []

    allt = sorted(per_tick_ms)
    print(
        f"\nOVERALL per-tick ms over {len(allt)} ticks: "
        f"min={allt[0]:.2f} med={statistics.median(allt):.2f} "
        f"mean={statistics.mean(allt):.2f} "
        f"p95={allt[int(0.95*len(allt))-1]:.2f} max={allt[-1]:.2f}",
        flush=True,
    )

    # cProfile pass over a fresh busy window (sim is now at its busiest tail).
    print(f"\n=== cProfile over {PROFILE_TICKS} more ticks ===", flush=True)
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(PROFILE_TICKS):
        sim.update(DT)
    pr.disable()

    for sort_key in ("tottime", "cumulative"):
        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats(sort_key)
        ps.print_stats(35)
        print(f"\n----- sorted by {sort_key} -----")
        print(s.getvalue())


if __name__ == "__main__":
    sys.exit(main())
