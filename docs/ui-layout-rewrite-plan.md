# UI layout rewrite plan

*Plan, 2026-10-30. Not a contract: `design_new.md` stays the spec for behavior, and
`docs/DESIGN_LANGUAGE.md` stays the rulebook for tokens and components. This plan
replaces the page layout, not either of those.*

The quick fixes that came before this plan (Operate label, header Activate, steady
tool rail, train panel order) are on branch `claude/ui-quick-fixes`. Everything
below builds on them.

---

## 1. What we are fixing

Each problem from the review, and the part of this plan that removes it.

| # | Problem | Fixed by |
|---|---------|----------|
| 1 | Four controls answer "can my train move?" (session chip, Play, Editor/Operate switch, Activate buried in the Hardware Setup dialog), and "Simulation" meant two things | §3 Top bar: one status control with the next step built in. The Operate rename already shipped. |
| 2 | The tool rail is grouped by data type (9 groups behind hover menus), mixes building tools with running tools, and its numbers moved between modes | §4 Editor screen: one flat bar of the things you place. Path, train and route tools move to §5 Operate. |
| 3 | The header has 22 buttons, 11 of them icon-only, and five open floating debug windows that pile up over the layout | §3 Top bar (about 9 controls) and §8 Logs panel (one bottom panel with tabs instead of five windows) |
| 4 | The train panel mixes driving with debugging | §5 Operate: a card per train for driving, and the full panel with details folded |
| 5 | Clicking track shows tile bits, even while operating | §4 and §5: a click shows what that screen is about. In Operate that is the block; tile bits move to Editor → Advanced |
| 6 | Why it keeps getting harder: panels exist once and find their inputs by fixed ids, HTML is built from strings, 19 timers poll on their own, `editor.ts` is 3,000 lines | §10 How the code changes |

---

## 2. The shape: three screens, named the way the spec names them

`design_new.md` "Operator workflows" already describes the operator's jobs in
order: **setting up the hardware and going live**, **calibrating a
locomotive**, and **running a train**. Building the layout is the Editor
session mode. So the app gets three screens, picked from tabs in the top bar:

| Screen | The job | Spec section it follows |
|--------|---------|-------------------------|
| **Editor** | Draw track, switches, signals, tunnels, platforms; set segments and blocks | "Session mode and operating environment" (Editor) |
| **Hardware Setup** | Choose the environment, describe the rig, prove each chip and wire, go live | "Setting up the hardware and going live", steps 1–7 |
| **Operate** | Place trains, drive them, give them routes, calibrate locomotives, stop everything | "Running a train", "Calibrating a locomotive" |

Switching screens only changes what you see, the same as the Editor/Operate
switch does today. The session state changes only through its own buttons
(Enter Setup, Activate, Play/Pause, Emergency stop, Reset).

Two things live outside the screens:

- **The top bar** (§3), always visible.
- **The Logs panel** (§8), a bottom panel you can open on any screen.

---

## 3. Top bar

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ RAIL  my-loop ▾      [ Editor | Hardware Setup | Operate ]                            │
│                         Real Hardware · Halted — not activated  [Activate]  ⏹ E-STOP │
│                                                              Logs   View ▾   ⚙       │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

(Drawn on two lines here for width; it is one 48 px row.)

- **Left:** the app name, then the layout name with a menu: New, Open, Save,
  Save as. This replaces the three file buttons.
- **Centre:** the three screen tabs.
- **Right:**
  - **The status control.** The operating environment and session state as
    today, followed by **one** next-step button that changes with the state:

    | Session state | Button |
    |---------------|--------|
    | Setup, Halted `not_activated`, Halted `hardware_failure` | Activate |
    | Paused (Simulation) | Play, with the speed presets in its menu |
    | Live (Simulation) | Pause |
    | Halted `layout_emergency_cut_out` | Reset |
    | Halted `runtime_failure` | Clear conflict |

    Clicking the label itself opens Hardware Setup at step 1 (choose the
    environment).
  - **Emergency stop**, always in the same place on every screen.
  - **Logs** opens the Logs panel. **View** holds the editor and canvas
    themes, and the canvas layers that are now debug checkboxes in Settings
    (track bits, block voltage, block length, stuck arrows, and the rest).
    **Settings** keeps the rest, including Restart server, which is rarely
    needed and should not sit in the top bar.
- **Gone from the top bar:** the two panel-visibility toggles (the panels
  manage themselves), Diagnostics and the four bench/monitor buttons (they
  move into the Logs panel and Hardware Setup), and the separate Play and
  speed buttons (they are part of the status control).

That is about 9 controls instead of 22.

---

## 4. Editor screen

```
┌ Select  Track  Switch  Signal  Tunnel  Platform  Segment  Block ┐   ← tool bar, keys 1–8
├ Track:  ( Draw )  Extend  Remove                                 ┤   ← options for the active tool
│                                                     ┌───────────┐ │
│                  layout canvas                      │ Selected   │ │
│                                                     │ block BLK_3│ │
│                                                     │ name, len, │ │
│                                                     │ speed lim. │ │
│                                                     │ ▸ Advanced │ │
│   − 1:1 + Fit                                       └───────────┘ │
└───────────────────────────────────────────────────────────────────┘
```

- **One flat tool bar of the things you place.** No hover menus. Each tool's
  variants (Track: Draw / Extend / Remove; Select: Box / Lasso / Brush / Trace
  / Entity; Platform: New / Extend / Remove) sit in a second row that shows
  only the active tool's options. Number keys follow the bar and never move.
- **The right panel edits what you selected.** One panel, docked. Lists
  (all switches, all stations, all blocks) open from a small list button in
  the panel header instead of from the tool bar.
- **Tile bits** (the NS/EW/… checkboxes and the bit readout) move under
  "Advanced" in the tile's panel. A plain click on track selects the segment
  or block, not the tile.
- **Not here:** paths, trains, routes and speed profiles. They are about
  running trains, so they move to Operate.

---

## 5. Operate screen

```
┌ Trains · Routes · Paths · Runs · Stations ┐                     ┌───────────────┐
│ (collapsible list on the left)            │    layout canvas    │ TRAIN_0704    │
│                                           │                     │ MANUAL · Live │
│                                           │                     │ cab display   │
│                                           │                     │ controls      │
│                                           │                     │ ▸ Route       │
│                                           │                     │ ▸ Look-ahead  │
│                                           │                     │ ▸ Details     │
├───────────────────────────────────────────┴─────────────────────┴───────────────┤
│ [TRAIN_0704  32 km/h  ATO RUNNING  ■Stop]  [TRAIN_0812  0  Waiting for BLK_3  ▶Go] │
│ [+ Place train]                                                    Hold all     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- **A row of train cards along the bottom**, one per train. Each card shows
  the name, speed against the ATC limit, `train_status` and `ato_status`, and
  the one-line `hold_reason` when the train is stopped. It has the train's
  main button: Go or Stop for ATO, a small handle for manual. Clicking a card
  selects the train on the map, and the other way round. Hold all sits at
  the end of the row, and **+ Place train** starts placement on the map.
- **The right panel shows the selection:**
  - **A train:** the full train panel, in the order shipped in the quick
    fixes (status, cab display, controls), then folded sections:
    **Route** (assign a route, Start route, draw/extend/cancel a path; the
    path tools move here because a path belongs to a train),
    **Look-ahead** (today's bottom Train Monitor graph and its Upcoming list),
    **Details**, and a **Calibrate** button (§6).
  - **A block:** who occupies it, its voltage, its speed limit, and the
    operational controls (Normal / Manual / Disabled, allowed direction).
    This is what a click on track opens in Operate.
  - **A switch:** its position, Throw, and Lock.
- **The left list** holds the operational lists: Trains, Routes, Paths,
  Route runs, Stations. It folds away to give the map room.

---

## 6. Locomotives and calibration

The spec puts calibration in the **Speed Profile panel**'s calibration group
("Speed calibration": "The calibration group names the locomotive before
either pass opens"), and the Canonical Entity Index names a **Calibrate
control**. The plan
keeps those names:

- The Speed Profile panel opens in Operate's right panel, from a train's
  **Calibrate** button or from a **Locomotives** entry in the left list
  (the Train Roster plus profiles).
- Its calibration group walks the spec's five steps in order: name the
  locomotive, find the lighting voltage, name the measured block and start
  the speed pass, let it run, commit. While the run is open, the train card
  reads `CALIBRATING` and the commands the spec refuses are shown disabled
  with that reason.

Moving calibration anywhere other than the Speed Profile panel would need a
`design_new.md` edit first, so this plan does not.

---

## 7. Hardware Setup screen

```
┌ 1 Choose the environment   ✓ ┐ ┌────────────────────────────────────────────┐
│ 2 Build the setup          ✓ │ │  4 · Prove each chip and each wire          │
│ 3 Enter Setup              ✓ │ │                                             │
│ 4 Prove each chip and wire ● │ │  chip list with Read live / pulse / sweep   │
│ 5 Close each tool            │ │  Occupancy bench                            │
│ 6 Check readiness            │ │  Virtual bench (Virtual Hardware only)      │
│ 7 Activate                   │ │                                             │
└──────────────────────────────┘ └────────────────────────────────────────────┘
```

- A full screen instead of a dialog. The left column lists the spec's steps
  ("Setting up the hardware and going live") with a tick for each done one.
  The right side shows the current step.
- The **Occupancy Bench** and **Virtual Bench** live in step 4, where the
  spec says the Setup tools run, instead of behind two header icons.
- Step 6 shows the readiness report (blockers and the advisory notes, as
  the dialog footer does today). Step 7 is the same Activate as the top bar.
- The wiring overview stays available as a folded section.

---

## 8. Logs panel

One bottom panel, open from **Logs** in the top bar on any screen, with
tabs:

| Tab | Today |
|-----|-------|
| Event log | the Event Log floating window |
| Detectors | the Raw Occupancy floating window |
| Diagnostics | the Diagnostics floating window |
| Virtual inputs | the Virtual Bench's fake-detector part, shown only in Virtual Hardware |

- It pushes the canvas up instead of covering it, and remembers its height.
- Each tab asks the server for data only while it is visible.
- The old Diagnostics, Raw Occupancy, and Event Log windows go away.

---

## 9. First run

For the simple-loop owners in `product_memo_personas.md`:

- An empty layout shows a centred card: **Start from a loop**, **Open a
  layout**, **Draw from scratch**.
- **Start from a loop** loads a small oval in the Simulation environment,
  switches to Operate, and highlights **+ Place train**.
- The goal to measure: from opening the app to a moving train in under five
  clicks.
- Empty lists say the next step ("No trains yet. Place one on the map")
  instead of "0 shown".

---

## 10. How the code changes

**Kept as they are:**

- The canvas: `frontend/src/renderer/*`, `lib/camera.ts`, `lib/pathfinders.ts`,
  and the canvas mouse handling in `editor/editor.ts`. The renderer looks up
  only 19 page elements, so it can be hosted by a new page layout with small
  changes.
- `lib/api.ts`, `lib/state-manager.ts`, `lib/network-poll.ts`.
- The theme tokens and the component rules in `DESIGN_LANGUAGE.md` (buttons,
  segmented controls, cards, disclosures, status colors).
- The backend. No server behavior changes are needed, except the optional
  push in phase 9.

**Rewritten:**

- **The page layout and its window code, about 5,000 lines:** the regions in
  `static/index.html`, the header wiring in `app.ts`, the tab and navigator
  code in `editor/editor-ui.ts`, `editor/tab-group.ts`, `editor/tab-drag.ts`,
  and `lib/window-*.ts`.
- **The panels, about 26,000 lines** (`editor/editor-*.ts`, `panels/*`,
  `ui/*`). These are ported one at a time. Most of their logic (what to send
  to the server, how to format a value) moves over unchanged; what changes is
  how they produce their HTML.

**Building panels from components.** Each panel becomes a function that
takes the current state and returns its HTML, written with **Preact** (a
4 KB React-compatible library) and its **signals** package (values that
re-draw only the parts of a panel that read them). Why this choice:

- Vite already compiles TSX, so there is no new build step.
- A Preact panel can be mounted into any existing element, so old and new
  panels can live side by side during the move.
- A panel no longer looks up fixed element ids, so two copies can be open at
  once (the "compare two blocks" request that was blocked before).
- Event handlers are attached where the HTML is written, instead of in a
  separate pass that has to find the elements again.

**One shared copy of the server state.** The existing network poll fills
`stateManager`; panels read from it through signals instead of each running
its own timer. Panels that need their own endpoint (event log, diagnostics,
benches, route runs) use one shared helper that polls only while the panel is
visible. That takes the 19 timers down to the network poll plus whatever is
on screen.

**Tests.** Component tests need a browser-like environment in vitest. Add
`happy-dom` as a dev dependency for those test files only; the existing
713 tests stay in the node environment.

**Names the spec uses, which the new UI must keep:** "the header" showing the
session state (`design_new.md` "Operator workflows", What the operator sees),
"Hardware Setup", "Operate" for the operation session mode, the "Speed
Profile panel" and its calibration group, and the "Calibrate control". Any
change to these is a spec edit first.

---

## 11. Phases

Each phase ends with the app working and `./run_all_tests.sh` green. The new
layout is built next to the old one and opened with `?ui=next`, so the
current UI keeps working until phase 8 switches the default.

| Phase | What | Size | Needs |
|-------|------|------|-------|
| 0 | Add Preact, signals and happy-dom; a `?ui=next` entry that mounts an empty page with the canvas in it; signals over `stateManager`; the shared poll-while-visible helper | Small | — |
| 1 | Top bar (§3): layout menu, screen tabs, status control with its next-step button, Emergency stop, Logs, View, Settings | Medium | 0 |
| 2 | Operate screen (§5): train cards row, train panel, block and switch panels, left lists, placement | Large | 1 |
| 3 | Editor screen (§4): flat tool bar, options row, entity panels, Advanced tile bits | Large | 1 |
| 4 | Hardware Setup screen (§7): the seven steps, benches inside step 4 | Medium | 1 |
| 5 | Speed Profile panel and calibration group in Operate (§6), Locomotives list | Medium | 2 |
| 6 | Logs panel (§8) | Medium | 1 |
| 7 | First run (§9): empty-state card, loop layout, empty-list wording | Small | 2, 3 |
| 8 | Make the new layout the default; delete the old regions, tab-group and window code, and the old panels; rewrite `DESIGN_LANGUAGE.md` "Surfaces" | Medium | all above |
| 9 *(optional)* | Server push (a single server-sent event stream) in place of the 200 ms poll | Medium, touches the backend | 8 |

After phase 1, phases 2, 3, 4 and 6 touch different screens and can run in
parallel, each in its own session and branch.

---

## 12. Decisions for you

1. **Preact + signals** for the panels (recommended), Svelte (also good, but
   a new compiler plugin and a bigger change of style), or stay with plain
   TypeScript and only reorganize the layout (smallest change, but leaves
   problem 6 in place).
2. **The window model.** You designed the current tab groups, pin-to-detach,
   and split windows (DESIGN_LANGUAGE.md §3 "Group states", §12). This plan
   replaces them with one docked right panel per screen and the bottom Logs
   panel. Options: drop them (recommended, since floating windows are most of
   problem 3), or keep "pop out into a window" as an opt-in on the right
   panel only.
3. **Build next to the old UI behind `?ui=next`** (recommended), or rewrite in
   place.
4. **Locomotives and calibration inside Operate** (recommended, and what the
   spec's panel names fit), or a fourth screen.
5. **Server push now or later.** Later (recommended): the shared state in
   phase 0 makes it a drop-in replacement for the poll.
