# ATCS6 — Prototype Signalling & DC Cab Control for Model Railroads

A browser-based **signalling, dispatching, and train-control simulator** built around how a real model layout actually works: a track plan divided into electrically isolated **blocks**, each fed its own **DC voltage**, with **multi-aspect signals**, **interlocking**, and an on-board **train computer** that can drive the train for you (ATO) while a safety layer (ATC) keeps it inside its movement authority.

Paint a layout on a grid, wire it into blocks, hang signals, define routes — then watch the dispatcher reserve the road ahead, the signals step through their aspects, and a train accelerate, hold line speed, brake on a planned curve, and stop dead on the platform mark. Or take the cab yourself and notch it along by hand.

It runs entirely on your own machine — Python + Flask backend, HTML5-canvas frontend, no database, no cloud.

> **Who this is for.** Railfans who care about how signalling and interlocking actually work, and experienced model railroaders who think in scales, blocks, momentum, and speed-matching. The vocabulary below is deliberately prototypical.

<!-- Add a screenshot here: the track editor with the demo loop running. -->

---

## Contents

- [What it actually models](#what-it-actually-models)
- [How it maps to the real thing](#how-it-maps-to-the-real-thing)
- [Feature tour](#feature-tour)
- [Getting started](#getting-started)
- [Driving the demo train](#driving-the-demo-train)
- [Building and saving layouts](#building-and-saving-layouts)
- [Toward real hardware](#toward-real-hardware)
- [Where to read more](#where-to-read-more)
- [Status and scope](#status-and-scope)

---

## What it actually models

Most "train games" move a sprite along a spline. This models the **railroad underneath the train**:

- **Block occupancy** — the layout is cut into blocks; the system always knows which block(s) each train is standing on, and a block can be Free, Reserved, Occupied, or Caution.
- **Multi-aspect signalling** — Green / Yellow / Double-Yellow / Red, driven by what's in the blocks ahead, not scripted by hand.
- **Interlocking with flank protection** — before a train is allowed onto a route, the conflicting turnouts and opposing moves are locked, including turnouts off to the *side* of the route that could let something roll into its flank.
- **Movement authority** — a train may only move as far as it has been given permission to, and it is *physically* held to it. No authority, no movement.
- **Automatic driving (ATO) and automatic protection (ATC)** — ATO is the driver (accelerate, cruise, brake, stop, dwell, depart); ATC is the safety envelope it can never exceed.
- **DC analog cab control** — each block gets an independent DC voltage, and polarity sets direction, exactly like a classic block-wired layout — except the wiring is decided live by the control logic instead of by a rotary cab selector.
- **Per-locomotive calibration** — every loco has its own speed profile: start voltage, momentum, braking rates, a speed-matching curve, even optional aerodynamic drag.
- **Scale** — distances are real centimetres and speeds are real km/h, scaled to your gauge (N, HO, O, G, …), so a train crosses the layout at a believable *scale* velocity.

It is a simulator first. But the control half — the part that decides voltages and aspects — is written so the very same logic could run a physical layout. See [Toward real hardware](#toward-real-hardware).

---

## How it maps to the real thing

If you already know the prototype, here's the Rosetta stone:

| In this app | What it means on a real railroad / layout |
| --- | --- |
| **Block** | A track section with occupancy detection. Trains are kept apart by blocks, not by collision checks. |
| **Aspect** (Green / Yellow / Double-Yellow / Red) | A 4-indication signal: clear, caution (be ready to stop at the next one), preliminary caution (the one after may be at danger), and stop. |
| **Caution block** | The block a train has just vacated while the next is still occupied — following moves are speed-restricted through it (≈40 km/h cap). |
| **Interlocking / route locking** | A requested route reserves its blocks and throws & locks its turnouts before the signal will clear. |
| **Flank protection** | Turnouts next to a set route are locked so nothing can foul it from the side. |
| **Authority / path** | A reserved, conflict-free run of blocks the train is cleared to occupy — its limit of movement authority. |
| **ATO** (Automatic Train Operation) | The automatic driver: power, coast, brake-curve, station stop, dwell, depart. |
| **ATC** (Automatic Train Control) | Continuous speed supervision: enforces the limit, brakes to a stand at the end of authority, refuses to move the wrong way. |
| **Cab (A / B)** | The selected driving end. Direction follows the cab; reverse running is not permitted. |
| **Master controller notches** (P1–P5 / N / B1–B8 / EB) | A notched power-and-brake handle: five power steps, neutral (coast), eight graduated service-brake steps, and emergency. |
| **Speed profile** | The loco's personality: start voltage, acceleration, service & emergency braking, coasting drag, and a voltage↔speed calibration curve. |
| **Block voltage** | The DC volts on the rail in that block (0–12 V), polarity = direction. |
| **Scale** | The model ratio (1:160 N, 1:87 HO, …) that converts prototype speed to model speed. |

---

## Feature tour

### Build it — the track editor

A web-based editor on a grid canvas. Tiles use a compact **6-bit geometry** encoding, which between them give you straights, diagonals, curves, and the junction shapes you'd expect:

- **Turnouts** (main / diverging), **crossovers**, **scissors crossovers**, **diamonds**, **wyes**, and **double-crossovers**
- **Signals** with validated placement
- **Blocks** with lengths, speed limits, and minimum-radius constraints
- **Stations and platforms**, including which side the platform is on

Geometry is validated as you build — certain physically impossible tile combinations are rejected outright.

### Signal it — blocks, aspects, interlocking

Aspects are **computed from the state of the road ahead**, not painted on. Set a route and the system reserves the blocks, throws and locks the turnouts, applies **flank protection** to the adjacent turnouts, and only then clears the signal. Two trains attempting to occupy the same block is treated as a **safety failure**, the way a real interlocking would never have permitted it in the first place.

### Operate it — dispatcher and routes

A built-in dispatcher does the CTC-style work:

- **A\* pathfinding** across the track graph, respecting geometry, switch positions, and minimum curve radius
- **Block & turnout reservation** along the chosen path, with conflict detection against other trains' authorities
- **Routes** expressed as a sequence of tasks — *stop*, *waypoint*, *dwell* — that resolve down to concrete block targets and platform stops
- **Station stops** with timed dwells and automatic departure

### Drive it — ATO, ATC, or by hand

Each train carries a small **train computer**. You choose how hands-on you want to be:

- **ATO** — hand it a route and it drives: accelerates to line speed, plans a smooth braking curve, stops on the platform mark, dwells, departs.
- **Manual** — select a cab end and work the **notched master controller** yourself: power notches **P1–P5**, **N** to coast, service brakes **B1–B8**, and **EB** for emergency.
- **ATC underneath both** — whichever you pick, the protection layer is always on: it caps you at the current limit, brakes you to a stand at the end of your authority, restricts you through caution blocks, and will not let you move with no authority, no cab, or the wrong cab for your direction.

The control handles expose exactly what you'd expect: pick the cab, choose Manual or ATO, set the handle, **depart**, **ATO-stop**, cut power (**power off**), drop the brakes (**emergency**), and **release** the emergency once stopped.

### Power it — DC block control and scale

Under the operations layer is an honest model of **DC analog control**:

- Each block is independently energised. A **block power controller** decides the voltage (0–12 V) for every occupied block from the train's state and target speed; lights stay lit at a low **lighting voltage (~2.5 V)** while stopped, and a **start voltage (~2 V)** gets a stationary loco moving against static friction.
- **Polarity is direction** — the controller can drive a block either way.
- **Scale is real.** Internally everything is centimetres and km/h; the model-scale factor (default **N, 1:160**) converts a prototype speed into the scale speed your train should actually run at. Presets cover the common gauges:

  | Ratio | Common name |
  | --- | --- |
  | 1:22.5 | G (garden) |
  | 1:43.5 / 1:64 | O / S |
  | 1:87 | HO |
  | 1:120 / 1:148 / 1:160 | TT / British N / N |
  | 1:220 | Z |

### Calibrate it — per-locomotive speed profiles

This is the part serious modelers tune for hours. Every loco has a **speed profile** you can dial in:

- **Start voltage** — the volts needed to break static friction (a smooth runner starts low; a stiff mechanism needs more).
- **Two-stage speed-matching curve** — `voltage ↔ power %` and `power % ↔ km/h`, so you can map a motor's real response and make different locos run at matching scale speeds for the same command.
- **Momentum** — independent **acceleration**, **service-brake**, and **emergency-brake** rates, plus a **coasting (friction) rate** for what happens when you notch to neutral. This is your inertia, set in software instead of with decoder CVs.
- **Lighting voltage** — keep markers and cab lights lit while standing.
- **Optional aerodynamic drag** — a quadratic air-resistance term for extra realism, with a single switch to turn it **off** when you're driving real metal.

ATO reads these to plan its braking curve (it brakes at a configurable fraction of the maximum so the slow-down feels prototypical rather than abrupt).

---

## Getting started

### Requirements

- **Python 3.11** with **Flask** (the code uses modern `X | None` typing, so 3.10+ is required — not the system Python 3.9 on macOS).
  The maintainers run it in a Conda environment named **`general`**; any 3.11 environment with the dependencies installed works.
- **Node 22 + npm** — only needed to build the browser bundle. The server builds it for you automatically on first run (if `npm` is on your PATH); you don't run any Node commands by hand.

### Run it

```bash
# From the repo root, using the project's conda env:
conda run -n general python server.py
```

On startup the server:

1. runs its own regression test suite and **refuses to boot if anything fails** (this is by design — you never serve a broken simulator),
2. builds the frontend bundle if it's missing or stale,
3. loads a demo layout, and
4. serves the app at **http://localhost:5001**.

Open that URL and you'll land in the editor with a worked-example layout already running.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--port 5007` | Serve on a different port |
| `--no-demo` | Start with an empty layout to build from scratch |
| `--scene demo_3` | Boot a specific example layout (`demo_main`, `demo_3`, `demo_4`, `demo_5`) |
| `--skip-startup-tests` | Skip the boot-time test gate (faster, for iterating) |
| `--no-sim` / `--no-autosave` | Don't start the simulation / autosave threads |

There's also a headless demo that builds a layout and runs trains in the terminal:

```bash
conda run -n general python demo_main.py
```

### The example layouts

Several worked layouts ship as "scenes" you can boot with `--scene`:

- **`demo_main`** — a main line with stations, signals, and a branch off a turnout.
- **`demo_3`** — a four-station double-track loop with scissors crossovers between platforms.
- **`demo_4`** — the same loop closed into a continuous circle through a pair of tunnels.
- **`demo_5`** *(default)* — the four-station loop plus a depot, with a one-way double-track corridor worked by convention.

### Run the verification suite

Activate the project environment, then run the canonical local gate:

```bash
conda activate general
./run_all_tests.sh
```

It runs the complete Python suite, TypeScript typecheck, frontend unit tests,
and the production frontend build. `npm test` is the frontend unit-suite alias;
API tests use Flask's in-process test client and require no separately running
server. See [the coverage and suite-splitting plan](docs/TEST_COVERAGE_AND_SUITE_PLAN.md)
for the planned fast/integration/journey CI structure.

---

## Driving the demo train

With the server running and the demo layout loaded, a typical ATO run looks like:

1. **Select a cab** (A or B) — this sets the driving end and therefore the direction.
2. **Choose ATO mode** and **assign a route** (or a destination) — the dispatcher finds a path, reserves the blocks, and locks the turnouts.
3. **Depart** — the train pulls away, holds line speed, and the signals ahead step from Red up to Green as the road clears.
4. Watch it **brake on a planned curve** into the platform and **stop on the mark**, then **dwell** and **depart** for the next stop.

Prefer to drive? Switch to **Manual**, and work the handle: **P1–P5** to power up, **N** to coast, **B1–B8** to brake, **EB** if you need to stop *now*. ATC is still watching — try to overspeed or run past your authority and it will brake you itself. Cut traction any time with **power off**; after an emergency stop, **release** the brakes once you're stationary.

> For a deeper, tick-by-tick look at how a run unfolds, see the journey-test harness under `tests/journey/`.

---

## Building and saving layouts

- **Editor** — drop tiles, signals, blocks, stations, and turnouts straight onto the grid. Placement and continuity are validated by the backend so you can't build something physically impossible.
- **Save / Load** — layouts are stored as compact JSON in `layouts/`. Each file holds both a fast-loading snapshot and the construction steps that produced it (a fallback if a snapshot is ever damaged).
- **Autosave** — your work is saved every five minutes to `layouts/autosave.json`, so a crash won't cost you the session.

Sample layout files are included in `layouts/` (loops and test plans) alongside the built-in demo scenes.

---

## Toward real hardware

The simulator is built to *become* a layout controller, not just imitate one. The voltage output sits behind a small abstract interface (`OutputInterface`):

- Today it ships a **simulated** implementation that keeps block voltages in memory.
- The same control logic — block power, aspects, interlocking, ATO/ATC — is written to drive a **real DC layout** through a hardware backend (Serial / I²C / GPIO) by swapping in a different output implementation.
- Each loco's profile has a one-call switch to **disable the simulation-only air-resistance model** when you're running real trains, so the physics doesn't fight a real motor.

The hardware backend itself is an extension point rather than a finished driver — but the seam is deliberately there.

---

## Where to read more

- **`design_new.md`** — the canonical, exhaustive design model for the control system (the train computer, blocks, dispatcher, paths, routes, ATO/ATC contracts). This is the source of truth for behavior.
- **`docs/`** — focused references:
  - `docs/reference/Reference_6Bit_Track_System.md` — the tile geometry encoding
  - `docs/reference/Reference_Signals_and_Blocks.md` — signals and block sections
  - `docs/reference/Reference_Construction_Pathfinding.md` — how the editor's pathfinding works
  - `docs/API.md` — the REST API the frontend speaks
  - `docs/DESIGN_LANGUAGE.md` — the UI's visual language
- **`AGENTS.md`** — developer/architecture notes and the module map.

---

## Status and scope

This is a **v1, single-network, single-machine** simulator. Some deliberate boundaries worth knowing:

- One railway network per session; the editor and the live simulation are separate modes (you build, then you run).
- State lives in memory with JSON layout files — there's no database and no save/restore of a *running* simulation.
- Hardware faults aren't modeled (it assumes healthy turnouts and signals); a two-trains-in-one-block situation is a hard safety failure rather than a fender-bender.
- It's a local tool — no accounts, no multi-user, no cloud.

If you came here to see prototype signalling and DC cab control modeled honestly — and maybe to point it at a real layout one day — you're in the right place. Build a loop, hang some signals, and let it run.
