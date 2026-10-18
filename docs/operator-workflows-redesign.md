# Operator workflows redesign — proposal

**Status:** AGREED DIRECTION, 2026-10-17. Revised the same day after review: D4 was
reversed, and D7–D10 were added. All decisions in section 6 are settled.
Nothing in `design_new.md` or the code has changed yet. The agreed parts move into
`design_new.md` in the order given in section 7, and this file stays behind as the record
of why.

**Scope:** the three things an operator does before and while trains run:

1. setting up the hardware and putting the layout live,
2. calibrating a locomotive (lighting voltage and speed curve),
3. starting a train, running it on a route under ATO, and stopping it.

It also covers two things all three depend on:

- how the whole layout is stopped in an emergency,
- how components pass information to each other inside a tick.

---

## 1. Why now

The code around train start-up, movement, ATO and calibration keeps being fixed, and the
fixes keep undoing each other. Here is what the git history since 2026-08-01 shows:

- **88 commits** touched these areas, and **49** of them are fixes.
- The hotspots are `simulation.py` (32 commits), `route_executor.py` (31),
  `train_controller/__init__.py` (28) and `speed_calibration.py` (21).

Several pieces of logic were built, deleted, and built again in a different shape:

| What | How many times it was redone | Path it took |
| --- | --- | --- |
| What lets ATO start driving | 5 | reactive state machine → depart handshake → `ato.running` → engaged on `set_mode` → `/ato/go` → `/ato/go` needs a route |
| Deciding a train has arrived at its stop | 6 | missed real stops, then fired on false ones (the 2026-10-16 "track dead after power-on" bug) |
| Operator cab and which end leads | 6, then deleted | head flipped on curves, read backwards, toggled twice by calibration, then the cab was removed |
| Where calibration runs | 4 | Action step → second-phase step → propulsion override → a one-off route planned in Plan |
| What calibration needs before it starts | 4 | refuse unready train → more checks → drop cab check → prepare the train itself |
| Halt and recovery | 6 | which flags activation clears, and in what order |
| Entering the safe state | 5 | — |

Every fix in these chains was correct for the rule it touched. The chains exist because no
single place says what the operator is trying to do and what the train must be doing at
each step, so each fix was checked only against the rule next to it.

---

## 2. What is going wrong

### 2.1 The doc describes the tick, not the operator's job

`design_new.md` is about 180,000 words. The four chapters that carry the train loop (Core
Runtime Loop, Route And RouteExecutor, TrainController, Power) are about 107,000 of
those words. Every rule is tied to a tick stage, a step ID (there are 103) or a latch (the
word appears 605 times).

The doc has no chapter that says, start to finish, "this is how an operator calibrates a
locomotive" or "this is how a train goes from placed to running a route". The closest is
**End-To-End ATO Path Run**, and it walks the tick stages, not the operator's steps.

So when something the operator sees goes wrong, the fix is a new clause on some tick step.
Nothing checks that the whole journey still makes sense, and the doc grows with each fix.

### 2.2 "May this train move?" has no single home

About **27 separate conditions** can keep a train from moving. They are checked across 12
files: `runtime.py`, `simulation.py`, `route_executor.py`, five `train_controller/` files,
`train_physics.py`, `speed_calibration.py`, `path_dispatcher.py` and `block_power.py`.
They come in two groups:

- **Session-wide conditions:**
  - `simulation_running`
  - `environment_active`
  - `failed_runtime`
  - `hardware_failure`
  - an open commissioning session
- **Per-train conditions:**
  - `powered`, `control_mode`, `commands.engaged` and `manual_mode_type`
  - EB and `eb.cut_out`
  - the five Action latches
  - the assigned route and the `RouteRun` state
  - a running dwell, or a `run` task's timer
  - holding points and the meet cap
  - the ATC limit
  - an open calibration run
  - `presence_unknown` and `is_unidentified`
  - the energized-block exclusion

Three places try to explain them to the operator, and they disagree:

- `diagnose_stopped_train` (`services/train_service.py`) covers 8 of the 27 conditions. It
  tells a stuck ATO train to "set mode to ATO", but `set_mode` *clears* `commands.engaged`,
  so that advice leaves the train still not driving.
- The frontend has its own subset in `lib/train-status.ts`, and it checks them in a
  different order.
- The ATO Go button and the server's `ato_go` check are two copies of the same rule.

The count is not the real problem: a real train has many reasons not to move. The problem
is that the same condition is checked again by many modules — `powered` alone in about 10
places — and the copies drift apart. That is why "can ATO go now?" was rewritten five
times. Each rewrite fixed one copy.

### 2.3 Calibration and hardware setup reuse state that belongs to other workflows

**Calibration** has no train state of its own. Instead it borrows from other workflows:

- **It borrows manual mode.** The panel's Take control step sets `control_mode = manual`
  and the `power_percent` submode, and the pass then drives through an override inside
  the manual `power_percent` branch of the controller.
- **It borrows routes.** Each leg is a one-off route.
- **It adds a special-purpose flag.** `zero_reservation_end_standoff` is passed down
  through five layers: Route → RouteExecutor → `PathDispatcher.create_path` → the
  `received_path_plan` projection → `atc.effective_safety_margin`.
- **It moves the train's head directly** (`_apply_head_frame`). Every other workflow lets
  a new reservation decide which end leads.
- **It switches off a safety rule.** The overrun cut-out is skipped while a calibration run
  is open.
- **Five different places can end a pass**, and environment activation is not one of them
  (see 2.5).

**Hardware setup** spreads "what state is the layout in" over six pieces of state:

- the selected environment
- the active environment (`environment_active`)
- `commissioning_active`
- the occupancy bench's own flags
- `simulation_running`
- the two halt flags (`hardware_failure`, `failed_runtime`)

The code reads "which environment are we in" from five different sources. There are also
two activation paths with different effects (see 2.5).

### 2.4 Workflows change each other's state without saying so

Each item below is documented somewhere in `design_new.md`. But each sits in a different
chapter, and no list shows the operator what will happen to their train:

| Something happens… | …and the train quietly changes |
| --- | --- |
| EB is applied, including an EB raised by a deviation | `control_mode` becomes `manual`. After release, the operator must select ATO and press Go again |
| A new route is assigned | `commands.engaged` is cleared, so the train needs Go again |
| The environment is activated | every train is powered off, every path is cancelled, every running `RouteRun` ends |
| The operator selects a mode on a train that is OFF | the train powers on |
| The operator presses Stop on the session | an open speed calibration pass fails (see 2.5) |
| The operator assigns a route to a train being calibrated | the calibration pass is abandoned |

### 2.5 The doc and the code disagree, and nothing caught it

**Checked in the code:**

- **Operator commands.** The doc says they are queued and drained at the start of Action.
  The HTTP handlers instead call the command methods directly, on the request thread,
  without `simulation_lock` (for example `train_service.power_on`). The tick runs on its
  own thread (`runtime.start_simulation_thread`), so such a command can land in the middle
  of a tick. Only the demos and the layout cut-out use the queue.
- **Calibration and session Stop.** The doc says a calibration pass paused mid-crossing
  "resumes on the same measurement" (**Speed calibration**). In the code, Stop moves the
  output to the safe state, which bumps `safe_state_generation`, and the pass fails with
  "session stopped during the pass".
- **Play with nothing active.** `POST /api/simulation/control` start does not check
  `environment_active`. Pressing Play with no active environment starts nothing, and gives
  no reason.
- **Closing the occupancy bench.** This re-activates the environment without the railway
  reset that `POST /api/operation/activate` performs. So trains keep their power, paths and
  runs across a bench round-trip, but lose them across an ordinary activation.
- **The physics command is built twice per tick,** once in Observe and once in Action
  (`train_physics.py:379` and `:679`). The build has side effects.

**Found by reading the code, not run:**

- **A lighting-voltage run survives environment activation.** By the code's logic it would
  drive its candidate voltage again after the next power-on.
- **The boot-time readiness check skips the lighting-voltage check** that `/activate`
  performs.
- **An expired commissioning session leaves `commissioning_active` set.** That blocks
  activation until the operator exits commissioning by hand.

### 2.6 Three ways to cut all power, three ways back

| What | What it does | How you get going again |
| --- | --- | --- |
| Session Stop (`POST /api/simulation/control`) | Every block goes to 0 V and the point motors turn off (the safe state). Time freezes. | Press Play. Every engaged ATO train that holds a reservation **starts driving again by itself**. |
| Layout emergency cut-out (`POST /api/simulation/emergency-cut-out`, hardware environments only) | Every train gets an emergency cut-out: 0 V at once. The session keeps running. | **Per train:** release EB (only once it is stopped and holds no reservation), select ATO, press Go. |
| Hardware failure halt | The same power cut. The session stops and the environment is deactivated. | Clear the failure, then activate again, which powers every train off. |

Stop and the cut-out remove the same power, but you recover from them in opposite ways.
After a Stop, trains restart by themselves. After a cut-out, the operator restarts every
train by hand. On real hardware, Play restarting moving trains on its own is the riskier
of the two.

Session Stop also means two different things depending on the environment:

- **On hardware,** it cuts power.
- **In Simulation,** it freezes time, because there is no power to cut.

### 2.7 Components pass information in nine different ways

| Way | Examples |
| --- | --- |
| Direct call | most things |
| Queue, emptied later | operator commands (in the doc only), the de-energize queue |
| Latch: a flag written now and acted on later, once a test passes | the five `phase_latches`: path cancel, route abort, power off, route deactivate, emergency request |
| Mailbox, emptied in the same stage | `physics_sync.queued_progress_reports` |
| Fields left for the next stage to pick up | `pending_detected_entered_blocks`, `entered_blocks_this_tick`, `pending_block_exit_edges` |
| Computed when read | the physics command, `received_path_plan`, `lookahead`, `train_status` |
| Callbacks | `on_physics_updated`, `on_path_complete`, `handle_path_completion` |
| A counter compared later | `safe_state_generation` (how calibration notices a Stop) |
| Event stream | `NotificationCenter` |

Each of these is reasonable on its own. The trouble is that one operator action crosses
several of them, and the operator sees none of the steps in between. For example, here is
what happens when the operator assigns a new route to a moving train:

1. The HTTP call runs `RouteExecutor.request_route_assignment(...)` directly.
2. That call writes the route-abort latch, which also writes the path-cancel latch. It then
   parks the request in `_pending_route_assignments`.
3. Plan reads the latches and stops extending the reservation.
4. ATO stays engaged and drives on to the current reservation end. Every tick, physics asks
   the controller for its command, and a callback writes back the new speed.
5. Once the train is stopped, Action acts on the path-cancel latch: the dispatcher cancels
   the path and calls back. Action then acts on the route-abort latch.
6. `remove_train_route_run(...)` tears down the old run, applies the parked request, and
   clears `commands.engaged`.

So one button involves:

- a direct call,
- two latches,
- a parked request,
- a planning rule that reads the latches,
- a command physics asks for every tick,
- two callbacks.

---

## 3. Proposal, part A — rebuild the doc around the operator's workflows

This part changes no behavior. It changes where rules live, so the next fix lands in the
right place.

### A1. A new first part: "Operator workflows"

Put three short chapters ahead of the mechanism chapters:

1. **Setting up the hardware and going live**
2. **Calibrating a locomotive**
3. **Running a train**

Each chapter follows the same template:

- **The steps the operator takes,** in order, each one a single action in the UI.
- **What the operator sees after each step:** the train's status, what the panel shows,
  and what is now allowed.
- **What the system promises at each step:** for example, "after Activate, every train is
  OFF and holds no reservation".
- **What can interrupt the workflow, and where it leaves the train.**
- **Links to the mechanism sections that carry it out.** The mechanism chapters stay the
  home of tick, latch and step detail. The workflow chapters never restate it.

The rule for future fixes: **a fix to anything the operator can see starts by updating
the workflow chapter.** If the chapter does not change, the fix is a mechanism fix and
must not change what the operator sees.

### A2. Four questions that decide whether a train moves

Every one of the 27 conditions in 2.2 answers one of four questions. The doc gets one
section naming:

- the four questions,
- the one owner that answers each,
- which of today's conditions belong to each.

| # | Question | Owner | Today's conditions it covers | How the answer is given |
| --- | --- | --- | --- | --- |
| 1 | Is the layout live? | `SimulationController`, through the session state (B3) | `simulation_running`, `environment_active`, commissioning and the occupancy bench, `hardware_failure`, `failed_runtime`, a refused block write (which raises `hardware_failure`) | One value: Setup / Live / Paused / Stopped. Stopped always carries a reason. |
| 2 | Who is driving this train, and is it asking to move? | `TrainController`, through `train_status` | `powered`, `control_mode`, `commands.engaged`, `manual_mode_type` and the driver's handle or percentage, `eb.active` and `eb.cut_out`, an open calibration run, `presence_unknown`, `is_unidentified` | One value: OFF / MANUAL / ATO / CALIBRATING / EB. It comes with whether the driving source is asking to move: ATO engaged, or the driver's input above zero. |
| 3 | How far may it go? | `RouteExecutor` in Plan, with `PathDispatcher` | the assigned route and the `RouteRun` state, a running dwell, a `run` task's timer, holding points, the meet cap, target re-resolution, a pending path cancel, route abort or route deactivate | The reservation itself (`received_path_plan`). Whenever Plan grants nothing, it also records why. |
| 4 | How fast may it go right now? | `TrainController`, in the **Propulsion cap step** | the ATC limit (which already includes the reservation end and signals), overspeed escalation, EB, a pending power-off | The lowest ceiling and its reason code. That step already works this way. |

- **Two of today's 27 conditions are not decisions.** They are how the answers to questions
  3 and 4 are carried out on the rails:
  - the detector cap on the calculated span,
  - keeping a block past the reservation end unpowered.

  They need no reason of their own.
- **The operator sees the first "no".** The hold reason the UI shows for a train is the
  first of the four answers that says no, together with that answer's own reason.
  `diagnose_stopped_train`, `lib/train-status.ts` and the ATO Go button all show this
  reason. None of them keeps its own list.
- **In Part A this section describes today's behavior, grouped.** B5 then makes each answer
  one value that every module reads.

### A3. One table: "Things that change a train's state without the operator asking"

This lists every row of 2.4, plus every EB, power-off and cancel the system raises. Each
row gives:

- the trigger,
- what changes on the train,
- what the operator must do to get going again.

This is the table a hobbyist needs when the train "just stopped".

### A4. Optional later step: shrink the mechanism chapters

Once A1–A3 exist, many restated rules in the mechanism chapters can become one-line
references. That is a separate pass, and it can wait.

---

## 4. Proposal, part B — redesign the three workflows

These are behavior changes. The decisions behind them are in section 6.

### B1. Running a train

**Today**, getting a placed train to run a route under ATO takes six actions:

1. Activate the environment.
2. Press Play.
3. Power the train on.
4. Assign a route.
5. Select ATO.
6. Press Go.

The train's control state lives in four stored fields: `powered`, `control_mode`,
`commands.engaged` and `manual_mode_type`. An open calibration run sits beside them as a
fifth. Each field has its own list of writers in a different paragraph of the
TrainController chapter.

**Proposed:**

- **One transition table for the train's control state.** `train_status` keeps its values
  OFF / MANUAL / ATO / EB, gains `CALIBRATING`, and has one table listing every transition.
  Each row gives the transition, its trigger (an operator command, or a named system
  event), and its guard. Whether the code stores the status or derives it from fields is a
  code-phase detail. The doc gets one table instead of four writer lists.
- **One operator action to start a route (decision D2).** "Start route" on a stopped train
  that has a route powers it on if needed, selects ATO and engages it, as one guarded
  command. The separate Power, ATO and Go buttons stay for operators who want them.
- **EB release returns the train to where it was (decision D3).** After EB is released, a
  train that was in ATO comes back in ATO, not engaged (`ato_status == READY`), so the
  operator presses Go once. Today it comes back in manual, and the operator has to select
  ATO and then press Go.
- **Operator commands run straight away, under the lock (decision D4).**
  - **When it runs.** Every HTTP train command runs immediately while holding
    `simulation_lock`. The tick holds the same lock (`runtime.py`), so a command always
    runs between two ticks, never in the middle of one.
  - **What it returns.** The HTTP response carries the command's real result and the
    train's new status, so the UI updates without waiting for its next refresh. This works
    the same whether the session is live or not.
  - **The queue goes.** The command queue (`commands.command_inbox` and its drain) is
    deleted.
- **A command that has to wait shows in the train's status.** Some commands can only
  finish once the train is stopped, such as switching a moving train to a new route. The
  command is accepted at once. Until it finishes, the train's status says what it is
  waiting to do, for example "switching to the new route once stopped". Part C, rule 3, is
  the general rule.

### B2. Calibrating a locomotive

**Today**, the panel's Take control step puts the train in manual mode with the
`power_percent` submode. The pass then drives through an override inside that submode,
runs its legs as one-off routes with a special flag, and can be ended from five places.

**Proposed:**

- **Calibrating is a train status of its own** (`CALIBRATING`, from B1).
  - Starting a pass or a lighting run moves a stopped train that has no route into it, in
    one command. That command replaces the Take control step.
  - While it is calibrating, nothing else can command the train: no route assignment, no
    ATO, no mode change. Each of those is refused with a reason, instead of quietly
    abandoning the pass.
  - Save, Discard or a failure returns the train to MANUAL, stopped.
- **One list of what ends a run,** in one place, covering both kinds of run. It includes:
  - environment activation,
  - every way the session leaves Live: the layout emergency cut-out, entering Setup, a
    hardware or runtime failure, and Pause in Simulation (decision D5).
- **A guided order the panel walks the operator through:** lighting voltage first, then
  the speed pass, then Save. Save asks whether the result belongs to this locomotive only
  or to its whole model (that choice already exists in **What commit writes**).
- **The mechanism stays open until the code phase.** Calibration legs may keep using
  one-off routes. The workflow chapter only promises what the operator sees. The overrun
  cut-out exemption and the special flag are things the code phase must justify or remove.

### B3. Setting up the hardware, and the session state

**Today**, there are separate states for selected environment, active environment,
commissioning, occupancy bench and running/stopped, and two activation paths with
different effects.

**Proposed:**

- **One session state, as the operator sees it.** It takes one of four values:
  - **Setup** (Virtual Hardware and Real Hardware only) — the hardware tools own the
    outputs: port test, track sweep, switch throw test and block occupancy test. The
    detectors keep reading, and no train can move. Commissioning and the occupancy bench
    both become tools inside Setup, not separate sessions.
  - **Live** — trains may be driven.
  - **Paused** (Simulation only) — time is frozen (B4).
  - **Stopped** — power is cut everywhere, nothing is driven, and the detectors keep
    reading. It always carries a reason: the layout emergency cut-out, a hardware failure,
    or a runtime failure. Each reason has its own way back (B4).

  This replaces running/stopped, `environment_active`, `commissioning_active`, the bench
  flags and the two halt flags as what the operator sees. The five ways the code now reads
  "which environment" collapse into one.
- **One way into Live: Activate.** Every time, it runs:
  - the same readiness check (the shared readiness evaluator, also used at boot),
  - the same railway reset (**Activation starts the railway over**), including ending open
    calibration runs.

  Activate leads straight to Live in every environment. The reset leaves every train OFF,
  so nothing moves until the operator starts a train. On hardware there is no separate
  Play step.
- **Entering Setup cuts power first,** the same way the layout emergency cut-out does.
  Leaving Setup is always Activate.
- **A guided setup order in the panel:** connect and scan chips → bind each block and
  switch → test each wire → readiness check → Activate. The readiness list comes only from
  the server. The frontend's own checklist goes away.

### B4. One kill switch: the layout emergency cut-out

**Today**, three things cut power to everything, and each one has a different way back
(2.6).

**Proposed:**

- **The layout emergency cut-out is the one kill switch, in every environment.** Its
  button is labeled Emergency stop.
  - It cuts power to every block and point motor at once (the existing safe state) and
    puts the session in Stopped (B3).
  - Today the cut-out is refused in Simulation. Under this proposal it is not.
- **Nothing restarts on its own.**
  - Leaving Stopped takes one Reset for the whole layout, not a release per train.
  - After Reset, every train is stopped and holds no reservation. It keeps its assigned
    route, and is back in the mode it had, not engaged (the same rule as D3).
  - The operator presses Go on each train that should run.
- **A hardware or runtime failure leads to the same Stopped state,** with its own reason
  and its own way back:
  - a hardware failure needs the fault fixed and Activate again;
  - a runtime failure needs the explicit clear.
- **Per-train EB and the per-train emergency cut-out stay,** for stopping one train.
- **Play/Pause exists only in Simulation.** It freezes and unfreezes time, which is useful
  for looking at the layout. A real moving locomotive cannot be paused, only braked or cut
  off, so a hardware session has no Pause.
- **Hold all is the gentle way to stop every train (decision D10).**
  - **What it does.** It sends ATO stop to every train in ATO. Each one brakes at the
    normal service rate and stands, keeping its route and its reservation.
  - **Getting going again.** The operator presses Go on each train that should run again.
  - **Who it leaves alone.** Trains in MANUAL or CALIBRATING are not affected, because ATO
    stop applies only to ATO trains.
  - **Where it works.** It is nothing more than ATO stop sent to each train, so it works in
    every environment.

  Three ways to stop every train were compared, from gentlest to hardest:

  | | Hold all (ATO stop on every train) | EB on every train | Layout emergency cut-out |
  | --- | --- | --- | --- |
  | How it stops | brakes at the normal service rate | brakes at `emergency_brake_rate` | power cut at once; the model stops the train that same tick |
  | Reservation and route | kept | path cancelled; route kept | path cancelled; route kept |
  | Getting going again | press Go per train | release EB per train (only once stopped), then Go | one layout Reset, then Go per train |
  | Manual trains | not affected (ATO stop applies only to ATO trains) | stopped | stopped |
  | Exists today | per train only | per train only | yes, hardware only |

  An "EB on every train" button is not added. It would be a slower version of the kill
  switch, and recovering from it needs a release per train.

### B5. Answer each of the four questions once

A2 groups today's conditions under four questions. B5 makes each question have one
answer, computed in one place:

- **Each owner computes its answer and keeps its reason.**
  - The session state (question 1) changes only through the transitions in B3 and B4.
  - `train_status` (question 2) changes only through the transition table in B1.
  - Plan (question 3) records why it granted nothing, whenever it grants nothing.
  - The **Propulsion cap step** (question 4) keeps its reason code, as it does today.
- **Every other module reads the answer instead of checking the flags again.** The copies
  to remove include:
  - the `powered` checks spread over about 10 places;
  - the "may ATO go" rule, which today lives in `ato_go`, `is_clean_for_create`, the run
    task timer, `ato_status_for`, `TrainController.tick` and `lib/train-status.ts`;
  - the ATC limit, which is computed two ways, plus ATO's own ceiling in `ato.py`.
- **The UI shows the server's hold reason.** `diagnose_stopped_train`, `lib/train-status.ts`
  and the Go button keep no rules of their own.

---

## 5. Proposal, part C — the target for how components pass information

This part goes into `design_new.md` as the shape the code moves toward.

- **Rules 1 and 3 land early,** with B1.
- **Rule 2 lands last, in stages.** It reshapes the tick core that was rewritten in
  September, across `simulation.py`, `route_executor.py`, `train_controller/` and
  `train_physics.py`. Those are the four biggest churn hotspots, so it is the largest
  change in this proposal. The tests stay green between stages.

**Rule 1 — Operator input comes in one way.**

- Every operator command runs between ticks under `simulation_lock` (B1, decision D4).
- It returns its real result plus the train's new status.
- Nothing else accepts operator input: no queue, and no call without the lock.

**Rule 2 — Inside a tick, each stage hands its result straight to the next.**

- **Each stage's result:**
  - Observe gives where every train is: its span and its block occupancy.
  - Plan gives how far each train may go (its reservation), and why, when it grants
    nothing.
  - Action gives each block's track voltage and each train's status.
- **What each stage reads.** Each stage reads only the previous stage's result and the
  stored train and session state.
- **What goes away.** Nothing is parked on an object for a later stage to find. That
  removes:
  - the progress mailbox,
  - the fields left for the next stage to pick up,
  - the second build of the physics command,
  - the callbacks between `PathDispatcher` and `RouteExecutor`, which become return values.
- **What stays stored.** Whatever must last from one tick to the next is stored state,
  named as such.

**Rule 3 — Anything that has to wait is part of the train's visible state.**

- **Why waiting stays.** Some actions really must wait: a train cannot give up the track
  ahead of it while it is still rolling. That stays.
- **What changes.** Today that wait is a latch, a hidden flag that a later step acts on
  once a test passes (usually `is_stopped()`). Under this rule, the wait becomes a pending
  action:
  - It is part of the train's status, and the operator sees it.
  - It is finished by one named step.
- **What happens to the Latch Table.** The Latch Table becomes the table of pending
  actions. Each row gives:
  - what the train is waiting to do,
  - what the wait is for,
  - what the operator sees,
  - the one step that finishes it.

**What the nine ways in 2.7 shrink to:**

- direct calls, inside a stage;
- stage results, between stages;
- stored train and session state, between ticks;
- the event stream, for the operator's log.

`safe_state_generation` stops being a signal to calibration. A change of session state
ends calibration runs directly (B2).

---

## 6. Decisions

Settled with the user on 2026-10-17. D4 was reversed after review, the same day. D6 is a
default the user did not object to. Any decision can be reopened.

| # | Decision | Options | Decided |
| --- | --- | --- | --- |
| D1 | How far this round goes | (a) Part A only: restructure the doc, no behavior change. (b) Part A plus the Part B redesign. (c) Both, plus the A4 trim. | **(b)** Part A plus the Part B redesign. Part A on its own documents the tangle without removing it. |
| D2 | How the operator starts a route | (a) Keep the six separate steps. (b) Add one "Start route" command and keep the separate buttons. | **(b)** One "Start route" command. |
| D3 | Train state after EB release | (a) MANUAL, as today. (b) Back to the mode it had, not engaged. | **(b)** Back to the mode it had, not engaged. |
| D4 | How operator commands reach the controller | (a) Queue every HTTP command for the next Action, as the doc says now. (b) Apply each one straight away under `simulation_lock`, and rewrite the doc to match. | **(b)** Straight away, under the lock. Reversed from (a) after review. Two problems with a queue: it runs only while the session is live, so a command sent while stopped would do nothing until Play; and the HTTP response could only say "queued". The lock gives the same guarantee — the command runs between two ticks — with an immediate result. |
| D5 | What leaving Live does to a calibration run | (a) Pause, and resume the same measurement, as the doc says. (b) End the run, as the code does. | **(b)** End the run. A power cut mid-crossing makes that crossing's timing untrustworthy, so the doc is corrected to match the code. Since D7, "Stop" means any way of leaving Live: the layout emergency cut-out, entering Setup, a failure, or Pause in Simulation. Pause cuts no power, but it ends the run too so there is one rule. The user confirmed the Pause case. |
| D6 | Commissioning and occupancy bench | (a) Keep them as two sessions. (b) Make both tools inside one Setup state. | **(b)** One Setup state (default). |
| D7 | How the whole layout is stopped | (a) Keep session Stop, the layout cut-out and the failure halt, each with its own way back. (b) One kill switch, the layout emergency cut-out, in every environment, with one Reset and nothing restarting on its own. Play/Pause only in Simulation. | **(b)** One kill switch (B4). |
| D8 | How the doc covers "why a train is not moving" | (a) One row per condition, 27 rows. (b) Four questions, each answered once by one owner. The UI shows the first "no". | **(b)** Four questions (A2, B5). |
| D9 | How components pass information | (a) Leave the nine ways as they are. (b) The three rules of Part C as the doc's target: rules 1 and 3 with B1, rule 2 last and in stages. | **(b)** Part C as the target. |
| D10 | A second button that stops every train (B4 compares the three ways) | (a) None: the layout emergency cut-out is the only button that stops every train. (b) Hold all: ATO stop sent to every train, keeping reservations, resumed with Go. (c) EB on every train. | **(b)** Hold all, in every environment. (c) is not added: it would be a slower kill switch with a per-train recovery. |

---

## 7. Order of work

1. Settle the decisions. (Done 2026-10-17.)
2. Write Part A into `design_new.md`, with no behavior change. That means:
   - the workflow chapters,
   - the four questions, grouping today's conditions,
   - the "changes without the operator asking" table.

   Run the `doc-review` skill on the result.
3. Write the Part B changes into `design_new.md`, one workflow at a time:
   - **Running a train** first: B1, plus Part C rules 1 and 3, plus the B5 work for
     questions 2, 3 and 4. It has the most churn, and `CALIBRATING` depends on its
     transition table.
     Doc written 2026-10-17: `CALIBRATING`, EB keeps the mode, Start route,
     commands between ticks under the lock, the Latch Table's pending-action
     column, and the **Pending actions** and **Hold reason** read models.
     Code done 2026-10-18, in commits `80fb778` to `b0b1907`: commands run
     between ticks under the lock, `CALIBRATING`, EB keeps the mode, Start
     route, the read models in `operator_status_by_train_id`, and the panel
     (Start route button, pending actions, the hold-reason banner).
     `/why-stopped` is gone. Still open: the hold-reason table has no row for
     an operating environment that is not active, so a train in that state
     reads `no_reservation`; that row belongs with the session state (B3).
   - **Calibrating a locomotive** second (B2).
     Doc written 2026-10-18: opening a run takes control of the train (no Take
     control), commands refused while `CALIBRATING`, one list in **What ends a
     calibration run**, and a session stop ends a run (D5).
     Code done 2026-10-18, in `6df4ec8` and the panel part of `b0b1907`: the
     readiness check and taking control, the safe-state entry ending runs,
     route assignment refused while calibrating, and Stop / cancel-on-close /
     the readiness line in the speed panel. Still open: the shared locomotive
     row with its train picker, the lighting run in place on its row, and the
     measured curve drawn against the stored one.
   - **The session state, hardware setup, the kill switch and Hold all** third: B3, B4,
     and the B5 work for question 1. This can run in parallel with calibration, because it touches
     different code.
4. Write Part C rule 2 into `design_new.md` last, and move the code to it in stages.
5. After each doc change, run a `gap-analysis` on that area and fix the code phase by phase,
   keeping `./run_all_tests.sh` green between phases.

---

## 8. New terms this proposal would add to `design_new.md`

The doc has no canonical term for these yet. Each one is proposed here, and none is used
in `design_new.md` until it is agreed.

- **Operator workflows** — the name of the new first part of the doc (A1).
- **Session state** — the one operator-facing value Setup / Live / Paused / Stopped, where
  Stopped carries a reason (B3). It sits over the existing `environment_active`,
  `simulation_running`, `hardware_failure` and `failed_runtime`. It does not rename them.
- **Reset** — the one layout-wide action that leaves Stopped after a layout emergency
  cut-out (B4).
- **Hold all** — the layout-wide action that sends ATO stop to every train in ATO. Those
  trains keep their reservations, and each one resumes with Go (B4).
- **Hold reason** — the reason the UI shows for a train that is not moving: the first of
  the four answers that says no, with that answer's own reason (A2). It extends the idea
  of the existing `speed_limit_reason` from the ATC limit to all four questions.
- **Pending action** — something a train has accepted but can only finish later, shown in
  its status and finished by one named step (Part C, rule 3). It is what a latch becomes
  under rule 3.
- **`CALIBRATING`** — a new `train_status` value (B1, B2).

"Emergency stop" is not a new term. It is only the button label. The mechanism keeps the
doc's existing name, the layout emergency cut-out.
