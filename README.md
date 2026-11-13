# ATCS6 — a controller for a model railway

ATCS6 runs model trains on a DC block-wired layout from a web page. Each train
has a speed knob, like a power brick: the voltage, from 0 V to 12 V, that its
blocks get while it moves. The block detectors say where trains are. Before a
train enters the next block ahead, it claims that block, and a train does not
enter a block another train is in.

The goal is one evening: put a few trains on the layout, open the page, turn
their knobs, and watch them go round without hitting each other. You can take
over any train from the page at any time. The full spec is `design_v2.md`.

It runs on your own machine: a Python + Flask server and a browser page, with
no database and no cloud.

---

## What it does

- **Finds trains from the detectors.** A train is a label the runtime puts on
  the blocks that read occupied. You do not create, name or place a train by
  hand before it runs.
- **Runs a train with a voltage knob.** Turn the knob up and the train runs,
  following the switches as they are set. Turn it to 0 V and it stops. Stop
  all stops every train at once.
- **Claims one block at a time.** A train's voltage reaches a block only after
  the train has claimed it. When the next block ahead is taken, the train waits
  with its blocks at 0 V until the block is free.
- **Runs to a destination, if you set one.** A destination is a block. The
  runtime throws the switches on the way and stops the train there. A train
  never needs a destination to move.
- **Drives the rig or a stand-in.** Real Hardware drives the rig. Virtual
  Hardware drives an in-memory stand-in with the same Hardware Setup and the
  same code, for tests and debugging.

The page has four screens:

- **Editor** — draw the layout on a grid: tiles, segments, blocks, switches and
  tunnels.
- **Hardware Setup** — describe the rig's chips and wiring, and test each chip
  and wire before going live.
- **Operate** — the speed knob, stop, reverse, destination and switch throws
  for each train, with every train drawn on the layout.
- **Logs** — the event log.

What v2 does not do (signals, routes, stations, speed curves, stopping at an
exact spot, and more) is listed in `design_v2.md`, **Out of scope**.

---

## Getting started

### Requirements

- **Python 3.12** with **Flask**. The maintainers use a Conda environment named
  **`general`**. The code uses `X | None` typing, so the macOS system Python 3.9
  does not work.
- **Node + npm**, only to build the browser bundle. The server builds it on
  start if it is missing or stale and `npm` is on your PATH.

### Run it

```bash
/opt/homebrew/anaconda3/envs/general/bin/python server.py --port 5007
```

On start the server:

1. runs its own test suite and refuses to start if a test fails,
2. builds the frontend bundle if it is missing or stale,
3. loads a layout, and
4. serves the page at **http://localhost:5007**.

The session reads Halted until you choose Virtual Hardware or Real Hardware and
activate it. `design_v2.md`, **Setting up the hardware and going live**, walks
through each step, and **Running a train** lists what you can do once it is
Live.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--port PORT` | Port to serve on (default 5001; use 5007 for development) |
| `--no-demo` | Start with an empty layout |
| `--scene demo7_1` | Boot a specific layout (`demo7_1`, `demo7_2`) |
| `--skip-startup-tests` | Skip the start-up test run |
| `--skip-frontend-build` | Skip building the frontend bundle |
| `--no-sim` / `--no-autosave` | Don't start the runtime loop / autosave threads |

### The layouts

Two layouts ship as scenes you can boot with `--scene`. Both are the rig's loop
with a passing siding:

- **`demo7_1`** — the rig with both switch motors working.
- **`demo7_2`** *(default)* — the same rig with the left switch (`SW_7_5`)
  moved by hand only.

### Saving layouts

Layouts are saved as JSON in `layouts/`. Saving a layout also saves the
Hardware Setup into the file. The server autosaves every five minutes to
`layouts/autosave.json`.

### Run the verification suite

Activate the project environment, then run the full local check:

```bash
conda activate general
./run_all_tests.sh
```

It runs the Python tests, the TypeScript typecheck, the frontend unit tests,
and the production frontend build. API tests use Flask's in-process test client,
so no server needs to be running.

### Turn a rig run into a test

Every run of `python server.py` writes a detector reading log to
`.runtime/detector_readings/`. It records what every block detector read on
each pass, every operator request and the pass it came after, and, on the first
line, what the server booted with. A run can be replayed from that file alone,
with no rig attached:

```bash
/opt/homebrew/anaconda3/envs/general/bin/python tools/reading_replay.py .runtime/detector_readings/readings-<run>.jsonl
```

That prints any request the replay answered differently from the server, and
which blocks each train has at the end. To keep part of a run as a test, cut it
out (`--summary` lists the passes where a block changed):

```bash
/opt/homebrew/anaconda3/envs/general/bin/python tools/cut_reading_log.py .runtime/detector_readings/readings-<run>.jsonl --to-pass 900 -o tests/replay/<name>.jsonl
```

Then replay it in a test with `RunReplay` from `tools/reading_replay.py` (see
`tests/test_run_replay.py`). For a file written before requests were recorded,
use `ReadingPlayer` and set up the trains in the test (see
`tests/test_reading_replay.py`). The readings play back as recorded and do not
react to the replayed runtime, so a replay tests what the runtime decides from
the readings, not how a train responds to its voltage. The rules are in
`design_v2.md`, **Detector reading log**.

---

## Where to read more

- **`design_v2.md`** — the design doc. The code is a straight translation of
  it, and when they disagree the doc wins.
- **`archive/design_new.md`** — the v1 design doc, kept for reference only.
- **`docs/`**:
  - `docs/API.md` — the REST API the page uses
  - `docs/PERSISTENCE_DESIGN.md` — the layout file format
  - `docs/DESIGN_LANGUAGE.md` — the page's look and layout
  - `docs/reference/Reference_6Bit_Track_System.md` — how a tile's track is encoded
- **`AGENTS.md`** — working notes for coding agents.
