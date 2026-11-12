# atcs v2 design

Status: draft, 2026-11-10.

## About this document

This document is the one bridge between the person who owns this project, the AI agents who write code for it, and the code. Keep these points in mind as you read:

- **The code is a straight translation of this document.** Every behavior the code has is described here, and every rule here is in the code. When the code and this document disagree, this document wins, and the code is changed to match.
- **Code no section describes is not part of v2.** A gap analysis treats such code as something to remove, not as a rule the doc forgot.
- **Rules live only here.** Tests check that the code follows this document. A test never holds a rule of its own that this document does not state.
- **v2 means v2.** This document describes v2 as it is built. It holds no "removed", "legacy", "deprecated" or "no longer used" text, and no comparison with an earlier design, apart from the section **Why v2 exists**. Git keeps the history. The earlier design's document is kept at `archive/design_new.md` for reference only; nothing in v2 depends on it.
- **One thread at a time.** The runtime loop and every web request hold the same lock while they read or change runtime state, so a request always runs between two runtime steps, never during one (see **Operator requests**). Rules such as first come, first served under **Claiming the next block** depend on this.
- **This document changes often.** When a rule changes, every place that refers to it is updated in the same edit. A half-updated document is a bug, not history.
- **Start with The evening v2 is for.** Every other section exists to make that evening work. Then read **Setting up the hardware and going live** and **Running a train**: they walk through what the operator does and name the section behind each step.

## Why v2 exists

The goal of the project has not changed: run model trains on my layout and enjoy watching them.

v1 got there by copying a real railway: every train needs a destination, a route and a reservation, and any departure from the plan is an emergency. That suits a simulator of real train control, which is what v1 started as. It does not suit a model layout. Bumping into each other is the worst risk (expensive models, no lives), and an evening where nothing runs is the most common failure.

v2 is built for a model railway. When a real railway's rule and an enjoyable evening disagree, the evening wins.

## The evening v2 is for

I put a few trains on my layout, open the web page, press run, and watch them go round without hitting each other. I can take over any train from the page at any time and see the layout respond the way I asked.

Every feature has to make this evening easier. If a feature makes it harder to get a train moving, it waits until later or gets dropped.

**The evening test.** Before a rule goes in, ask: is it here because the evening needs it, or because a real railway has it? Only the first kind goes in.

## The rules that come from that goal

1. **The detectors say where trains are.** A block that reads occupied has a train in it. The runtime never decides a train is somewhere the detectors disagree with, and it keeps no calculated position for a train.
2. **The basic instruction is "run".** A running train keeps going, follows the switches as they are currently set, and stops only for the reasons listed under **When a train waits** and **When the runtime stops a train**. A destination is an extra the operator can add; when one is set, the runtime also throws the switches on the way there. A train never needs a destination to move.
3. **The only protection is: a train does not enter a block another train is in.** A train's voltage reaches a block only after the train has claimed it. Nothing a train does is treated as an emergency.
4. **Speed is a voltage knob, like a power brick.** No speed curve and no speed calibration are needed to run. A train stops roughly: somewhere in the block it was stopped in, or a little past it.
5. **Few steps.** Once the hardware is set up, putting a train on the track and turning its knob is about as quick as a power brick. A train does not have to be created, named, measured or placed by hand before it runs.

## Terms

These are the terms this document uses for v2's own ideas. Names of code (classes, methods, fields, endpoints) are given a plain meaning where they first appear. Any other word is plain English.

- **The runtime**: the part of the server that owns the runtime loop, the trains and the session. In the code it is the class `SimulationController`; where this document names one of the runtime's methods or fields, it is a method or field of that class.
- **Block**: a stretch of track with one power feed and its own occupancy detection. It gets one voltage at a time. See **Block**.
- **Reads occupied / reads empty**: what a block's detector reports after debounce. See **Occupancy detection**.
- **Switch**: a point where the track splits. Its single end is its **toe**; its two other ends are its **main leg** and its **diverge leg**. It is set to one of the two legs, and the runtime or the operator can throw it. It belongs to no block and has no detector. See **Switch**.
- **Feed block**: the block whose power feed also feeds a switch's rails. It is the first block reached out of the switch's toe. See **Block and switch power controllers**.
- **Tile, segment, tunnel**: the pieces the layout is drawn from. See **Tile**, **Segment** and **Tunnel**.
- **Train**: a label the runtime puts on one or more blocks that read occupied. See **Trains**.
- **A train's blocks, front block, rear block**: the blocks that carry a train's label, in order along the track; the front block is the one the train leaves by, the rear block the one at its other end.
- **Direction**: which way a train faces in its front block. See **Direction**.
- **Speed knob**: the voltage, from 0 V to 12 V, that a train's blocks get while it moves.
- **Stopped, running, waiting, moving**: a train is stopped while its knob is at 0 V and running while it is above 0 V. A running train is waiting while the runtime keeps its blocks at 0 V because it cannot claim the next block ahead, and moving otherwise.
- **Wholly in its front block**: a train is wholly in its front block when that block is the only one of its blocks that reads occupied. The detectors then say its rear has left the block behind.
- **Next block ahead**: the block a train enters next. See **Claiming the next block**.
- **Claim**: a train's hold on the next block ahead before it enters it. See **Claiming the next block**.
- **Taken**: a block is taken when it reads occupied, is one of a train's blocks, or is claimed.
- **The way**: the blocks and switches a train with a destination will pass to reach it. See **Switches**, The way to a destination.
- **Held switch**: a switch a train may be on or about to cross, which is therefore not thrown. See **Switches**.
- **Destination**: a block the operator picks for a train to run to and stop in. Optional.
- **Hold time**: 5 seconds (the runtime setting `hold_time_s`). A block that reads empty while no block further forward in its train reads occupied stays the train's block for this long. See **Trains**.
- **Pass**: one turn of the runtime loop that reads the detectors. While the session is Live a pass is a whole runtime step; in Setup it is the stopped pass; in Halted the loop makes no pass. See **Runtime loop**.
- **Safe state**: every block at 0 V and every point motor switched off. See **Safe state, watchdog and hardware failure**.
- **Setup tool**: one of the Hardware Setup test tools, which may hold the outputs while the session is in Setup. See **Setup tools**.
- **Drive zone**: which of three ranges a block's voltage is in, which decides which of its two detection sources is believed. See **Occupancy source and drive zones**.
- **Snapshot**: the one JSON object that carries the layout and its live state to the page. See **Data flow to the page**.
- **Lost train**: a train that was running, or had only just stopped, and whose blocks have all read empty for the hold time. See **Trains**.
- **Stop all**: the one action that stops every train at once. See **Running a train**.
- **Runtime step**: the work under **Runtime step**, done ten times a second while the session is Live.
- **Session state**: Setup, Live or Halted. Trains run only in Live. See **Session state**.
- **Hardware Setup**: the saved description of the rig's chips and wiring. See **Hardware setup**.
- **Virtual Hardware, Real Hardware**: the two operating environments. Real Hardware drives the rig; Virtual Hardware drives an in-memory stand-in for it. See **Operating environment and activation**.
- **Event, warning, error**: an event is one entry in the runtime's one event stream, which the page and the event log read. A warning is an event with `kind = "warning"`: something was refused or went wrong, and the layout keeps running. An error is an event with `kind = "error"`: a hardware failure has stopped the session. See **Notifications and errors**.

## Setting up the hardware and going live

This section walks through what the operator does to get from a new rig to a session where trains can run. Each step says what the operator does, what is true afterwards, and what is refused, and names the section that defines each rule. This section defines no rule of its own. When this section and the section it names differ, the named section is right, and the difference is a bug in this section.

**What the operator sees.** The header shows the session state: Setup, Live, or Halted with its reason (see **Session state**). Trains may be driven only while it reads Live. At boot the session reads Halted for `not_activated` until the operator activates it (see **Operating environment and activation**, Boot activates no environment).

Virtual Hardware and Real Hardware share one setup, and Virtual Hardware runs every step against its fake device (see **Hardware setup**, Virtual Hardware).

1. **Choose the environment.**
   - **What the operator does.** The operator selects Virtual Hardware or Real Hardware with `PUT /api/operation/environment`.
   - **What is true afterwards.** The environment is selected, not activated, and the session state does not change. The response lists the readiness blockers (the faults in the setup that would stop activation).
   - **Where it is stored.** The selected environment is stored apart from the setup, so loading a layout never selects or activates Real Hardware.
   - See **Operating environment and activation** and **Hardware REST API**, REST surface.
2. **Build the setup.**
   - **What the operator does.** The operator enters the controller, the chips, a binding for every block and switch, the detector calibration, and the two drive-zone levels (`idle_deadband_v` and `current_sense_min_drive_v`), and saves the setup with `PUT /api/hardware/config`.
   - **What a save checks.** A save rejects malformed fields, incompatible chips, out-of-range or duplicate ports, and references to deleted entities. It accepts an incomplete setup.
   - **When changes reach the rig.** Coil ports, the `reversed` flag and `switch_track_source` are read on every write, so a saved change to them reaches the rig on the next write. Everything else is captured at activation, and a saved change to it never rewires a session in place.
   - **Saving with a layout.** Saving a layout also saves the setup into the layout file. Loading a layout never activates physical output.
   - See **Hardware setup**: Controller, Chips, Bindings, A binding must name a valid, uniquely-used chip port, An incomplete setup may be saved and tested in Setup, but not activated, A binding is read per write, and Saved with the layout. See also **Hardware REST API**, REST surface.
3. **Enter Setup.**
   - **What the operator does.** The operator enters Setup with `POST /api/operation/setup`.
   - **When it is refused.** While the session is already in Setup.
   - **What it does.** It drives the outputs to the safe state (every block at 0 V and every point motor switched off; see **Safe state / fail-safe**) and deactivates the environment, and the session reads Setup.
   - **Who owns the outputs in Setup.** The Setup tools (the Hardware Setup test tools listed in step 4) own the outputs.
   - **The detectors in Setup.** The detectors keep being read whenever the session entered Setup with detector interfaces open. A session that entered Setup from Halted for `not_activated`, or after a refused activation, has none open and reads nothing until an activation succeeds (see **Runtime loop**, Which pass runs).
   - **Trains in Setup.** The runtime moves no train. A Setup tool may move a locomotive on purpose, as the track direction sweep does.
   - See **Setup tools**: Setup, Setup tools own the writes; the detectors stay shared, and The runtime moves no train in Setup.
4. **Prove each chip and each wire.** Each test except the occupancy bench carries a setup in its request body. The tools that take the outputs are accepted only in Setup. They are the commissioning session, the track direction sweep, the occupancy bench, and a port-test output pulse.
   - **Self-test** (`POST /api/hardware/scan`) shows that each I²C chip answers at its address. A shift register cannot be found this way:
     - an `hc165` is proven when the occupancy bench's block check has run on every block bound to it, each block's bit followed the drive, and no other block's detectors changed;
     - an `hc595` is proven only once the operator confirms the board did what a port test or the commissioning session asked;
     - a later failed test of that chip takes the proof back.
   - **Port-test** (`POST /api/hardware/port-test`) pulses one output port or reads one input port, to confirm a single wire before it is bound. The read is accepted in any state.
   - **The commissioning session** (`POST /api/hardware/commission/open`, then its `output`, `pulse`, `inputs` and `currents` actions, then `close`) drives and reads raw chip ports for a bounded time. A failed action names its error, and the panel shows it and stops the test. The session's `output` action refuses to hold on a shift-out bit that a switch binding names. A held coil burns out (see **Setup tools**, The commissioning session). The panel pulses such a bit only for the switch-coil width, and may pulse every other bit long enough to see, up to `MAX_COIL_PULSE_S`.
   - **Track direction sweep** (`POST /api/hardware/track-test`) drives one block forward, then off, then reverse, and leaves the block de-energized. It is how the operator finds the block's `reversed` flag.
   - **Switch throw test** (`POST /api/hardware/switch-test`) pulses one coil to throw one switch to a named position, then records the result in `Switch.position`. A switch a train stands across is thrown and recorded too, because the coil moves the points before anything is checked. When the session does not own the switch coils, the test runs only in Setup.
   - **Block occupancy test** (`POST /api/hardware/occupancy-test`) drives nothing and reports both occupancy sources, the drive zone, and the verdict.
   - **Occupancy bench** (`POST /api/hardware/bench/open`, then `close`) holds every bound block at one voltage and shows both occupancy sources for every block side by side, so the operator can set the detector calibration. Its block check (`POST /api/hardware/bench/check-block`) pulses one block and reports whether that block's detectors follow the pulse and whether any other block's do.
   - **Test the rig** is the Hardware Setup panel's one-button run of the tools above: self-test, the block check on every block with a rail binding, and a switch throw test of every bound switch to its other position and back. It asks the operator nothing and ends with one summary.
   - See **Setup tools**: Self-test, A shift register is proven by a read-back or by the operator, Port-test, The commissioning session, Track direction sweep, Switch throw test, Block occupancy test, Occupancy bench, and Test the rig. See also **Hardware REST API**, REST surface.
5. **Close each tool.**
   - **What the operator does.** The operator closes the tool.
   - **Other ways it ends.** A timeout, a disconnect or a failure also ends it.
   - **What is true afterwards.** Every ending completes the tool's safe-state sequence, and the session stays in Setup.
   - See **Setup tools**, Closing a Setup tool restores the safe state.
6. **Check readiness.**
   - **What the operator does.** The operator reads the readiness report with `GET /api/operation/readiness`. Reading it activates nothing.
   - **What the report lists.** Every blocker:
     - a missing binding,
     - a binding that names a missing or incompatible chip,
     - a bad or reused port,
     - a binding to a deleted entity,
     - an unreachable device.
   - See **Hardware setup**: Completeness is activation policy, not edit policy, and One shared readiness evaluator.
7. **Activate.**
   - **What the operator does.** The operator calls `POST /api/operation/activate`. Activate is the only way out of Setup.
   - **When it is refused.** While the session is Live. It also runs the readiness check again and refuses on any blocker.
   - **What it does.** Once the readiness check passes, it closes any Setup tool that still holds the outputs. It then builds the interfaces and verifies the environment. It then leaves Setup, clears a latched hardware failure, and goes Live.
   - **What it keeps.** The layout and the setup.
   - **Switches.** Activation throws every switch once to the position the layout shows, even one a train may stand across, and the event log records which were thrown (see **Operating environment and activation**, Activation throws every switch).
   - **When activation is refused or fails.** No environment is active. The session reads Setup if it was in Setup, and otherwise Halted for `hardware_failure` or `not_activated`.
   - **What is true afterwards.** The session is Live. There is no separate start step: the operator goes straight on to **Running a train**.
   - See **Operating environment and activation**; **Session state**; and **Recovery is reactivation**.

**What can interrupt it**

- **Hardware failure.**
  - **What causes it.** Any of these halts the session:
    - an output write fails,
    - a detector read fails,
    - the controller is lost,
    - the safe state cannot be completed.
  - **What happens.** The outputs go to the safe state, the environment is deactivated, and `hardware_failure` is latched. The session reads Halted for `hardware_failure`, or Setup when it was in Setup, and runs no further pass.
  - **The way back.** In this order:
    1. Repair the device. The operator may enter Setup to find the fault.
    2. Activate again. A successful activation clears the latch and goes Live.
  - See **Hardware I/O failure is fatal to the session**, **The runtime step stops while `hardware_failure` is set**, and **Recovery is reactivation**.
- **Loss-of-control watchdog.**
  - **Where it applies.** Real Hardware only.
  - **What causes it.** The runtime stops writing to the outputs for too long.
  - **What happens.** A watchdog drives the outputs to the safe state.
  - **What it leads to.** A drop that succeeds does not change the session state. A drop that fails becomes a hardware failure.
  - See **Loss-of-control watchdog**, and A successful drop does not end the session.

## Running a train

These are all the things the operator can do to trains and switches from the Operate screen. Every request here and under **Trains** runs between two runtime steps and answers `{ success, reason }` (see **Operator requests**).

- **Speed knob.** `PUT /api/trains/<train_id>/knob` with `{ "volts": number }` sets `knob_volts`. A value above 0 makes the train running; the next runtime step puts that voltage on the train's blocks, the same way turning a power brick does, and turning the knob while the train moves changes its voltage on the next step. It is refused when **any of these holds**:
  - the session state is not Live (`not_live`);
  - the train is lost (`train_lost`);
  - `volts` is below `0` or above `MAX_TRACK_VOLTAGE` (`out_of_range`).
- **Stop.** `POST /api/trains/<train_id>/stop` sets the knob to 0 V. It is always accepted.
- **An unknown train.** Every request that names a `train_id` the runtime does not hold is refused with `unknown_train`.
- **Reverse.** See **Direction**, Reverse turns the train round.
- **Set a destination.** `PUT /api/trains/<train_id>/destination` with `{ "block_id": id }` sets the destination, and `{ "block_id": null }` clears it. The train runs to that block and the runtime stops it there (see **When the runtime stops a train**). A destination does not start a train: it moves when its knob is above 0 V. Without a destination the train just runs, following the switches as they are set. Setting one is refused when **any of these holds**:
  - the session state is not Live (`not_live`);
  - the train is lost (`train_lost`);
  - the layout has no such block (`unknown_block`);
  - the block is one of the train's blocks (`already_there`);
  - there is no way forward to it (`no_way`, see **Switches**, The way to a destination).
- **Stop all.** `POST /api/trains/stop-all` stops every train at once. It is always accepted. It is the off switch of a power brick.
  - **While the session is Live**, it sets every train's knob to 0 V and commands every block to 0 V through `BlockPowerController` in the same request, without waiting for the next runtime step. These are ordinary block writes, not the safe state. Each train keeps its claim as any stopped train does (see **Claiming the next block**, A stopped train keeps its claim for the hold time). The session state stays Live, and each train runs again when its knob is turned up.
  - **While the session is not Live**, it writes nothing to the outputs. It closes any Setup tool that holds them (see **Closing a Setup tool restores the safe state**).
- **Throw a switch.** `PUT /api/switches/<switch_id>/state` throws a switch (see **Switch**, Canonical `Switch.position` mutation workflow). It is refused while the session is not Live (`not_live`), and while the switch is held (`switch_held`, see **Switches**, A held switch is not thrown).
- **Edit the layout.** The layout is drawn and edited on the Editor screen. Loading a layout, clearing it, and every edit to tiles, segments, blocks, switches and tunnels are refused while the session is Live (`live`), because the rig is wired to the layout it was activated with. Saving a layout is accepted in every session state. Loading or clearing a layout, and any edit that adds, deletes or regroups a block, removes every train; the detectors then find them again.
- **Save the Hardware Setup.** `PUT /api/hardware/config` is accepted in every session state, so a wiring fix can be saved while trains run (see **A binding is read per write**). The setup is the operator's to get right: a save that takes away a binding the running session needs makes the next write to it fail, and that is a hardware failure like any other.
- **Manage trains.** Rename, note a length, mark two trains as one, split, and remove are defined under **Trains**.

### Seeing trains on the layout

The Operate screen draws a line along the track of each train's blocks, using
one style from the layout theme. The line stays on blocks the train still
holds when they read empty. A claim ahead is shown separately from the line.
The line breaks at tunnel mouths and joins across switches only when the
layout shows one connection between consecutive blocks.

The train ID on the layout matches the ID on the card. An arrow at the outgoing
end of the front block shows **Direction**, including while stopped or waiting.
A successful Reverse updates the arrow from the next snapshot. A lost train
shows its retained block with a dashed line and a question mark beside its ID.

Hovering or focusing a card highlights the train's line, arrow and ID. Clicking
the card's ID or the train on the map selects it. Temporary highlighting takes
precedence over selection and the selected train is highlighted again when it
ends. The card remains the place to turn the knob or reverse the train.

## Trains

A train is a label the runtime puts on blocks that read occupied. It is not a model of a locomotive: it has no position inside a block and no speed.

- **What a train holds.**
  - `train_id`: given by the runtime from a counter that only counts up, so an id is never used twice in one session.
  - `name`: the operator's name for it. A new train's name is its `train_id`.
  - `blocks`: the train's blocks, ordered from its rear block to its front block along the track. A new train has one.
  - `direction`: which way the train faces in its front block, `forward` or `reverse` along that block's tile order (see **Block**). The front block is the last entry of `blocks`.
  - `knob_volts`: the speed knob, from `0` to `MAX_TRACK_VOLTAGE` (12 V).
  - `destination_block_id`: the destination, or none.
  - `length_cm`: a length the operator may note. Nothing in v2 reads it.
  - `lost`: whether it is a lost train.
  - `stopped_at` and `waiting_since`: the pass `wall_time` at which the train last became stopped, and at which it began to wait, or none. The rules that say "less than the hold time ago" read these (see **Clocks**, The hold time is measured on pass wall time).
- **Running, stopped, waiting.** A train is **stopped** while its knob is at 0 V and **running** while its knob is above 0 V. A running train is **waiting** while the runtime keeps its blocks at 0 V because it cannot claim the next block ahead and is wholly in its front block (see **When a train waits**), and **moving** otherwise.
- **An occupied block with no train gets one.** On each pass, the runtime takes every block that reads occupied and is not one of a train's blocks, and every lost train's block that reads occupied, and gives it to the first of these that applies. It does not matter whether the block began to read occupied on this pass or earlier: a block left without a train, for example after its train was removed, gets one on the next pass.
  1. **The train that claimed it.** The block becomes that train's new front block, and the claim ends.
  2. **A lost train it belongs to.** When the block is a lost train's block, or can be reached from that block across switches as currently set with no other block between, the block becomes that train's only block and the train is no longer lost. The train stays stopped, keeps its direction when the block is the same one, and otherwise faces away from the end it came in by. The runtime emits the event `"train_found"`.
  3. **A train that just overran.** When the block is the next block ahead of a train that stopped or began waiting less than the hold time ago, the block becomes that train's new front block (see **When a train overruns**).
  4. **A new train.** Otherwise the runtime makes a new train with this one block, `direction = forward`, and the knob at 0 V.
- **A train that is not moving keeps its blocks.** While a train is stopped or waiting, and at least one of its blocks reads occupied, a block of its that reads empty stays its block and no count runs for it. A train at 0 V does not move, so it cannot have left a block. The reason this matters: a block at 0 V is read by the '165 detector, which sees a motor car and does not see a car that only has lights, so the block under the lit cars of a standing train reads empty while the train is in it. When every one of the train's blocks reads empty, the rules below apply, and a standing train that was lifted off is removed.
- **A block that reads empty is taken off at once or after the hold time.** For a moving train, and for a train none of whose blocks reads occupied, when one of its blocks reads empty, which of the two applies depends on what the detectors see ahead of it:
  - **The rear block, while a block further forward reads occupied, is taken off at once.** When the train's rear block reads empty and another of the train's blocks reads occupied, the runtime takes the rear block off the train's blocks on that pass, and repeats this for the new rear block. The detectors see the train further forward, so the block behind it is free. "At once" still follows the detector's own delay: a block takes about 1 second to read empty (see **Occupancy detection**).
  - **Any other block stays for the hold time.** A block that reads empty and is not taken off by the rule above stays one of the train's blocks until it has read empty for the hold time (5 seconds, the runtime setting `hold_time_s`, measured as **Clocks**, The hold time is measured on pass wall time, says). Then the runtime takes it off and the other blocks keep their order. When it reads occupied again before that, the count is dropped. This is the case where the detectors have lost sight of the front of the train: it is on a switch and reads on no block, or its wheels lost contact for a moment.
  - **A train keeps reaching forward while a block is held.** A block ahead it has claimed gets its voltage, and when that block reads occupied it joins the train as rule 1 says.
  - **Every train's last car must be seen by the detector.** A train whose last car draws no current leaves its rear block reading empty while that car is still in it, and the block is then free for the train behind.
- **A train with no blocks left is lost or removed.** When the runtime takes the last block off a train:
  - if the train was running when that block started to read empty, or had stopped less than the hold time before, the train becomes a **lost train**: `lost` is set, its knob goes to 0 V, its claim ends, the block stays its one block, and the runtime emits the warning event `"train_lost"`. When several of its blocks reach the hold time on the same pass, the block it keeps is its front block;
  - otherwise the train is removed. A train that had been standing still and then reads empty was lifted off the track.
- **A lost train keeps its block taken.** A lost train's block counts as taken, so no other train can claim it, and it gets 0 V, where the '165 detector can find the train again if it is still there. A lost train refuses the speed knob, reverse and a destination. It stays until it is found (rule 2) or the operator removes it. The reason: the train may still be standing there unseen, and the models are expensive.
- **Marking two trains as one.** `POST /api/trains/merge` with `{ "train_ids": [a, b] }` makes two trains one, for a train that reads occupied in two blocks.
  - It is refused when **any of these holds**: either train is not stopped (`not_stopped`); either is lost (`train_lost`); the two trains' blocks are not next to each other (`not_neighbours`). Next to each other means an end block of one and an end block of the other are neighbours across switches as currently set, with no other block between.
  - The train with the older `train_id` survives and keeps its name; the other is removed. Its `blocks` are both trains' blocks in order along the track. Its front block is the end of the combined blocks that the older train faced, and its `direction` leads out of that block away from the rest of the train.
- **Splitting a train.** `POST /api/trains/<train_id>/split` gives each of the train's blocks its own train. It is refused while the train is not stopped (`not_stopped`), is lost (`train_lost`), or has one block (`one_block`). The front block keeps the `train_id`, name and direction; each other block gets a new train as rule 4 describes.
- **Renaming and noting a length.** `PATCH /api/trains/<train_id>` with `name` and/or `length_cm` changes those two fields and nothing else.
- **Removing a train.** `DELETE /api/trains/<train_id>` removes a train. It is refused while the train is running (`not_stopped`). When one of its blocks still reads occupied, the next pass gives that block a new train (rule 4).
- **Trains are not saved.** Trains live only in the running server. A restart, and every activation, starts with no trains, and the runtime step finds them again from the detectors as soon as their blocks read occupied (see **Activation starts the railway over**).
- **Leaving Live stops every train.** When the session state leaves Live, every train's knob goes to 0 V and every claim ends. The trains themselves stay, because the detectors go on being read (see **Runtime loop**).

**Direction**

- **A train's line of travel.** A train runs from its rear block to its front block, and on out of the front block in its `direction`. In each block before the front one, the train runs toward the end that leads to the next of its blocks.
- **Each block's voltage is signed by the line of travel.** A block's voltage is positive when the train runs that block in its `forward` direction (along `Block.ordered_tiles`) and negative when it runs it in `reverse`. The sign is worked out per block, because each block's tile order is drawn on its own: two blocks one train stands in may run opposite ways against the train's line of travel.
- **Which rail a sign reaches is wiring.** It is settled below the hardware interface by the rail binding's `reversed` flag (see **The rail binding bonds tile order to rail polarity**). Nothing above the driver names a channel.
- **Entering a block sets the direction there.** When a block becomes a train's new front block, the train's `direction` becomes the direction that leads away from the end it came in by.
- **Reverse turns the train round.** `POST /api/trains/<train_id>/reverse` reverses the order of `blocks`, so the rear block becomes the front block, and sets `direction` to lead out of the new front block away from the rest of the train. For a train with one block it flips `forward` and `reverse`. It clears the train's destination, because a way is only ever looked for forward. It is refused when **any of these holds**:
  - the session state is not Live (`not_live`);
  - the train is lost (`train_lost`);
  - the train is not stopped (`not_stopped`);
  - the train still holds a claim (`still_claiming`). A train the operator has just stopped keeps its claim for the hold time, because it may still be rolling into that block (see **Claiming the next block**, A stopped train keeps its claim for the hold time). A train that was waiting, or that the runtime stopped, holds no claim and can be reversed at once.

## Claiming the next block

Two trains are never let into the same block. To make sure, a train claims a block before it enters it, and its voltage goes onto a block only once it holds the claim.

- **The next block ahead.** The runtime finds it by walking the track from the far end of the train's front block, in the train's direction, across each switch it meets, up to the first block.
  - A switch met at its toe (its single end) leads on by the leg it is currently set to.
  - A switch met from a leg leads on out of its toe when it is set to that leg. When it is set to the other leg, the walk ends and there is no next block ahead.
  - When the track ends before another block, there is no next block ahead.
  - For a train with a destination, the walk follows **The way to a destination** in place of the switches' current settings.
- **Who claims.** On each runtime step, every train that is running, is not lost, and holds no claim asks for its next block ahead.
- **When a claim is given.** The train gets the claim when **all five hold**:
  - there is a next block ahead;
  - that block reads empty;
  - that block is not one of any train's blocks, a lost train's included;
  - no other train has claimed it;
  - for a train with a destination, no switch on the way to that block that needs throwing is held (see **Switches**, A held switch is not thrown).
- **A taken block.** A block is taken when it reads occupied, is one of a train's blocks, or is claimed. A train is never given a claim on a taken block.
- **One block ahead.** A train holds at most one claim, and nothing is claimed all the way to a destination.
- **First come, first served.** A train that asks and is not given the claim keeps asking on every pass. The runtime records the pass on which it first asked for that block, and that pass is when it began to wait for the order below, whether or not it has come to rest yet (see **When a train waits**). On each step the runtime serves the trains in the order they began to wait, the longest wait first, and trains that began on the same step in order of `train_id`. Claims have no other priority.
- **A claimed block counts as taken.** No other train can claim it. It gets the claiming train's voltage (see **Track power**), and every switch on the way to it is held (see **Switches**).
- **A claim ends in one of these ways:**
  - the block reads occupied, and becomes the train's front block (see **Trains**, An occupied block with no train gets one);
  - the train has been stopped for the hold time without entering it;
  - the train's front block is taken off its blocks, so the claim was made from a block the train is no longer in; the train asks again from its new front block;
  - the train becomes a lost train, or is removed;
  - the session state leaves Live.
- **A stopped train keeps its claim for the hold time.** A train the operator stops may still be rolling. Keeping the claim, with the block at 0 V, means a train that rolls into it has entered a block it holds.

A claim is one block deep on purpose. It is how "trains don't hit each other" works when two trains want the same track. A later need that seems to call for a deeper or more complicated claim passes **The evening test** first.

## Switches

How a switch is thrown, and what a throw does on the rig, is under **Switch**. This section says when the runtime throws one and when a throw is refused.

- **Without a destination, the runtime never throws a switch for a train.** The train follows the switches as they are set, and the operator throws them from the page.
- **With a destination, the runtime throws the switches on the way.** Once every condition for the claim holds (see **Claiming the next block**, When a claim is given), and only then, the runtime throws each switch between the train's front block and the block being claimed that is not already set for the way, and gives the claim. A switch that is already set for the way is not thrown. A switch is thrown as part of claiming, not earlier, so a train that is waiting throws nothing.
- **The way to a destination.** The runtime looks for it forward from the train's front block, in the train's direction.
  - At a switch met at its toe, the way may take either leg. At a switch met from a leg, the way leads on out of the toe.
  - The way never turns round and never passes the same block twice.
  - Of all the ways that reach the destination, the runtime takes the one with the fewest blocks; among those, the one with the shortest length, adding each block's `length_cm` and each switch's `main_length_cm` or `diverge_length_cm` for the leg taken; among those, the one that takes the main leg at the first switch where they differ.
  - The search ignores which blocks are taken. When the chosen way is blocked by another train, the train waits; the runtime does not look for a way round.
  - When no way reaches the destination, setting the destination is refused (`no_way`). The runtime never reverses a train by itself.
  - **A destination that can no longer be reached is cleared.** A way that existed when the destination was set can stop existing: the train entered a block it had already claimed down another leg, or was found again on another block. When a train with a destination asks for a claim and no way forward reaches the destination, the runtime clears the destination and emits the warning event `"destination_unreachable"`. The train stays running and follows the switches as they are set, like any train with no destination.
- **A held switch is not thrown.** While the session is Live, a switch is held, and neither the runtime nor the operator throws it, while **any of these holds**:
  - it lies on the way between a train's front block and the block that train has claimed;
  - it lies between two blocks of the same train, so the train stands across it;
  - it can be reached from one of a train's blocks that reads empty, across switches with no other block between. This covers a train that is on the switch and reads on no block, and a lost train.

  The reason: a switch has no detector, so these are the only signs that a train may be on it. The activation throw and a Setup switch throw test are not bound by this rule (see **Switch**, Two throws do not check for a train).
- **The runtime does not throw a switch a train has just left.** When a train's rear block is taken off at once (see **Trains**, A block that reads empty is taken off at once or after the hold time), the train's last car may still be on a switch next to that block. For the hold time after that pass, the runtime does not throw any switch that can be reached from that block across switches with no other block between; a train with a destination that needs it thrown waits with `switch_held`. The operator may throw it during that time: it is not a held switch, and its `held` field stays `false`.

## When a train waits

A running train begins to wait, with its blocks at 0 V, when **both hold**:

- it cannot be given a claim;
- it is wholly in its front block.

A train that cannot be given a claim and is not yet wholly in its front block keeps moving on its own blocks until the block behind it reads empty. The detectors, not a guess, say when the whole train is inside, so a waiting train stands clear of the block and the switch behind it and another train can use them. Its payload names the reason it cannot be given the claim:

- `block_taken`: the next block ahead is taken by another train (see **Claiming the next block**, A taken block);
- `switch_against`: a switch met from a leg is set to the other leg, and the train has no destination;
- `switch_held`: the train has a destination, and a switch on its way that needs throwing is held;
- `own_block`: the next block ahead is one of the train's own blocks.

- **A waiting train stays waiting until it is given the claim.** Once a train waits, its blocks stay at 0 V until it is given the claim or it is stopped, whatever its front block reads in between. A train at 0 V does not move, so a front block that reads empty for a moment is a detector flicker, and putting voltage back on the train would drive it toward the train ahead.

A waiting train starts moving by itself on the step it is given the claim. A train whose track ends ahead is not waiting; see **When the runtime stops a train**, End of track.

## When the runtime stops a train

Apart from the operator's own Stop and Stop all, the runtime sets a train's knob to 0 V in these cases and no others:

- **Arrival.** The destination block is the train's front block and the train is wholly in it. The destination is cleared, and the runtime emits the event `"train_arrived"`.
- **End of track.** The train's front block has no next block ahead because the track ends there, and the train is wholly in it. The runtime emits the warning event `"end_of_track"`. When the operator turns the knob up again, the train moves further into the block and the runtime does not stop it a second time: a train in its last block is the operator's to drive.
- **Overrun.** See **When a train overruns**.
- **Lost train.** See **Trains**, A train with no blocks left is lost or removed.
- **Leaving Live.** See **Trains**, Leaving Live stops every train.

- **A train comes to rest only once it is wholly in its front block.** Arrival, End of track and waiting all wait for the block behind to read empty. A block takes about 1 second to read empty, so the train rolls a little further in before its voltage goes. A train with one block is wholly in it already and is brought to rest at once.
- **A train longer than its block.** A train that is longer than the block it has to stop in is never wholly in it, so this rule cannot bring it to rest and its front rolls on into the next block. Each train must be shorter than the shortest block it stops in.

## When a train overruns

A train runs on a voltage knob, not a speed curve, so it will often roll past the block it was stopped or made to wait in. The runtime treats this as ordinary.

- **Rolling into an empty block.** When a block that reads empty and belongs to no train starts to read occupied, and it is the next block ahead of a train that stopped or began waiting less than the hold time ago, the block becomes that train's front block (see **Trains**, An occupied block with no train gets one). The block is at 0 V, so the train stops there. The runtime sets the train's knob to 0 V, clears its destination, and emits the warning event `"train_overran"`, for example "Train A stopped one block past where it was held." The operator can turn the knob up again straight away.
- **Rolling into a block another train has claimed, or has just left.** The block reads empty, so the detector does see the overrunning train arrive, but the block is not free. A block another train has claimed goes to that train (rule 1 of **Trains**, An occupied block with no train gets one), and a block another train still holds for the hold time stays that train's. Either way the overrunning train is not given the block. Its own blocks then read empty, and it becomes a lost train with the warning `"train_lost"`.
- **Rolling into a block another train stands in cannot be seen.** That block already reads occupied, so the detector reports nothing (see **Occupancy detection**, A second train on an occupied block is not detected). What the runtime does see is the overrunning train's own blocks reading empty, and it becomes a lost train. This is why a train that cannot claim the next block loses its voltage as soon as it is wholly in its front block: it has the rest of that block to come to rest in.

A train that rolls a whole block past its stop is a reason to turn its knob down, not to leave a bigger gap between trains.

## Track power

Each block gets one voltage at a time, signed for direction (see **Direction**). Item 6 of **Runtime step** decides every block's voltage on every step from these rules alone, and `BlockPowerController` is the one place that sends a block's voltage while the session is Live:

- **A moving train's blocks, and the block it has claimed, get its knob voltage.** Each block is signed by the train's line of travel through it. A block the train still holds for the hold time (see **Trains**, A block that reads empty is taken off at once or after the hold time) counts as the train's, so it keeps its voltage through a short loss of contact.
- **Every other block gets 0 V.** That covers an empty block, a stopped train's blocks, a waiting train's blocks, and a lost train's block. At or below `idle_deadband_v` the '165 detector decides whether the block reads occupied, and it sees a train standing on rails that carry no voltage (see **Occupancy source follows drive state**). A stopped train therefore stays visible, and a train put on an empty block shows up straight away.
- **A switch has no voltage of its own.** Its rails carry its feed block's voltage (see **Block and switch power controllers**, A switch's rails are fed from one block). A train crossing a switch from the toe stands on the feed block; a train crossing toward the toe is entering the feed block, which it has claimed. Either way the feed block already carries the train's voltage.
- **A block is never asked for two voltages.** A block is one train's block or one train's claim, never two, so each block has one answer.
- **When the hardware fails**, the outputs go to the safe state and the session reads Halted for `hardware_failure` until an activation succeeds (see **Hardware I/O failure is fatal to the session**).

Keeping a held block's voltage for the hold time is what lets a train ride through a short loss of wheel contact, for example on a switch. While the wheels have no contact the detector cannot see the train, and only the voltage still on the rails can bring it back.

## Runtime step

While the session state is Live, the runtime loop runs one runtime step ten times a second. A step does these things in this order, and each one finishes for every block and train before the next begins:

1. **Read the detectors.** One read of every block, then the debounce (see **Occupancy detection**). The result is which blocks read occupied and which read empty. The read is written to the **Detector reading log** before anything else uses it.
2. **Update each train's blocks.** In this order:
   1. give a train to each block that reads occupied and has none (see **Trains**, An occupied block with no train gets one);
   2. leaving alone each train that is not moving while one of its blocks reads occupied (see **Trains**, A train that is not moving keeps its blocks): take off each rear block that reads empty while a block further forward in its train reads occupied, then each block that has read empty for the hold time, except the last block of a train that is about to become a lost train;
   3. make each train whose blocks have all read empty for the hold time a lost train, or remove it.
3. **Stop trains.** Apply **When the runtime stops a train** to every running train.
4. **End claims.** End every claim that **Claiming the next block** says is over.
5. **Give claims.** Serve the running trains in first-come order. For each, in this order: find the next block ahead; check the conditions under **Claiming the next block**, When a claim is given; when they all hold, throw the switches a train with a destination needs thrown, then give the claim; otherwise throw nothing and record that the train is waiting, and why.
6. **Set every block's voltage** (see **Track power**).
7. **Publish.** Emit this step's events (see **Event log**, Train events). The page reads the result through the snapshot (see **Data flow to the page**).

Giving claims after ending them, in one fixed order, is what makes first come, first served exact: a block freed on this step goes to the train that has waited longest for it.

While the session state is Setup, the loop runs items 1, 2 and 7 only, so trains still appear and disappear on the page, and nothing is claimed or powered. While it is Halted, the loop runs none of them (see **Runtime loop**, Which pass runs).

## Occupancy detection

- **Each block is its own occupancy section.** A block has its own detector. Its member segments are geometry only and are not detected on their own. A switch has no detector of its own: its rails are fed from its feed block (see **Block and switch power controllers**, A switch's rails are fed from one block).
- **One read per runtime step.** At the start of each runtime step the runtime reads every block's detector at once through `OccupancyInput.read_block_currents()`, which returns a plain `block_id -> current present` map with no train identity. Which physical source answers for a block is decided by **Occupancy source follows drive state**. The read shows where trains were at the end of the previous step, so a block a train just entered is seen no earlier than the next step.
- **The read does not stop in Setup.** A Setup tool takes the outputs, not the detectors (see **Setup tools own the writes; the detectors stay shared**). A block a train stands on reads occupied throughout Setup.
- **Debounce with on/off thresholds.** Real contacts flicker: a block a train stands on can briefly read open. `BlockOccupancyDetector` keeps a counter per block. It confirms a block **occupied** after `OCCUPANCY_ON_THRESHOLD` (2) consecutive current reads, and **empty** after `OCCUPANCY_OFF_THRESHOLD` (10) no-current reads counted as **A lone current read pauses the clear count** says. Both values live in `constants.py`. At ten reads a second that is 0.2 seconds to confirm occupied and 1 second to confirm empty: quick to occupy, slow to clear. A block in the `LIGHTING` zone takes 3 seconds longer to read empty (see **Occupancy source and drive zones**).
- **A lone current read pauses the clear count.** While an occupied block counts no-current reads toward empty, `BlockOccupancyDetector` handles a current read this way:
  - a current read that is not yet the `OCCUPANCY_ON_THRESHOLD`-th consecutive one pauses the count: the count neither grows nor restarts;
  - `OCCUPANCY_ON_THRESHOLD` consecutive current reads restart the count from zero.

  An empty block that carries voltage now and then reads one sample over its threshold. A restart needs the same evidence that confirms an entry, so a stray sample cannot keep a block occupied.
- **"Reads occupied" and "reads empty" mean the debounced result.** Everywhere else in this document, a block reads occupied or reads empty according to `BlockOccupancyDetector`, never according to one raw read.
- **Several changes in one step.** Each step the detector compares the whole set of occupied blocks with the previous step's set, so a train that enters or leaves several blocks between two reads is reported in one pass.
- **A second train on an occupied block is not detected.** When a train runs onto a block that already reads occupied, the detector reports no change. v2 keeps a second train out by never putting a train's voltage on a block it has not claimed (see **Claiming the next block**). A train that coasts in anyway is not seen there.

## Session state

The session state is the one value that answers "is the layout live?". `AppState.session_state()` derives it on every read and returns `{value, halt_reason}`. No request, runtime step, or event writes it.

- **The three values.**
  - **Setup** — the operator is building and testing the rig. All four of these are true:
    - no operating environment is active;
    - the Setup tools own the outputs (see **Setup tools**, Setup);
    - the detectors keep being read, unless a hardware failure is latched or no detector interface is open (see **Runtime loop**, Which pass runs);
    - no train is driven.
  - **Live** — trains may be driven, and the whole runtime step runs.
  - **Halted** — no train is driven, and nothing drives the outputs. A Halted session always carries a `halt_reason`, and each reason has its own way back (see the transition table).
- **The two halt reasons.** `halt_reason` is `None` unless the value is Halted. Otherwise it is one of these:
  - `hardware_failure` — the session hit a hardware I/O failure (see **Hardware I/O failure is fatal to the session**);
  - `not_activated` — no operating environment is active and no other reason applies: the environment was never activated, or its activation was refused or failed.

The table is in precedence order: the first row whose condition holds is the session state.

| Order | `value` | `halt_reason` | Derived when |
| --- | --- | --- | --- |
| 1 | Setup | `None` | `AppState.setup_active` |
| 2 | Halted | `hardware_failure` | `hardware_failure` |
| 3 | Halted | `not_activated` | `environment_active == False` |
| 4 | Live | `None` | no row above holds |

- **The stored inputs.** The session state reads three stored flags. The transition table lists every write to them:
  - `AppState.setup_active` — set while the session is in Setup;
  - `hardware_failure` — the hardware-failure latch, which the runtime holds (see **Hardware I/O failure is fatal to the session**);
  - `environment_active` — a flag the runtime holds that says whether an operating environment is active. Active means its interfaces are built, and `BlockPowerController` (the part of the hardware layer that sets block voltages) and `SwitchPowerController` (the part that pulses point motors) own the outputs.
- **The order puts the cause first.**
  - Setup comes before a hardware failure, because an operator who enters Setup after a failure is there to repair it. The latch stays set in Setup until an activation succeeds.
  - A hardware failure and a refused activation both leave no environment active, so the failure row comes before `not_activated`: it names the cause.
- **When `not_activated` arises.** It follows a boot, or a refused or failed activation.

Every write to the three flags is one of these transitions:

| Trigger | Accepted while | What it writes | Session state afterwards |
| --- | --- | --- | --- |
| Boot | — | nothing is activated (`environment_active = False`) | Halted, `not_activated` |
| Activate succeeds (`POST /api/operation/activate`) | Setup; Halted for `not_activated` or `hardware_failure` | it closes any Setup tool that holds the outputs, then throws every switch to the layout (`SwitchManager.throw_all_to_layout()`); `environment_active = True`; `setup_active = False`; `hardware_failure = False` (see **Operating environment and activation**) | Live |
| Activate is refused on a readiness blocker, or fails to build, or one of its switch throws cannot be issued | the same states as a successful Activate | `environment_active = False`; a throw that cannot be issued is also a hardware I/O failure (the last row) | Setup when it was in Setup; otherwise Halted, `hardware_failure` when that latch is set, else `not_activated` |
| Enter Setup (`POST /api/operation/setup`) | Live, or Halted for `not_activated` or `hardware_failure` | the safe state, then `environment_active = False` and `setup_active = True` (see **Setup tools**, Setup) | Setup |
| A hardware I/O failure (the runtime's `enter_hardware_failure(...)`) | any state | it enters the safe state; `environment_active = False`; `hardware_failure = True` | Halted, `hardware_failure`, or Setup when it was in Setup |

- **Nothing else changes the session state.**
  - Selecting an environment (`PUT /api/operation/environment`) and every train request leave all three flags alone.
  - A loss-of-control watchdog drop that succeeds writes none of them. One that fails is a hardware I/O failure (see **Loss-of-control watchdog**).
  - `/api/server/restart` ends the process, whose next start is the boot row.
- **Every reader reads `AppState.session_state()`.** These readers must not recombine the three flags themselves:
  - the runtime loop, when it chooses which pass to run (see **Runtime loop**, Which pass runs);
  - `GET /api/operation/environment`, which the header and the panel's session controls read (see **Hardware REST API**, REST surface);
  - the speed knob, reverse and destination requests, and an operator's switch throw, which are refused while the session is not Live (see **Running a train**).
- **Why one value.** The operator needs one answer to "may trains run, and if not, what do I do next?". Each value and each halt reason names its own way back, so the answer carries the next step with it.

## Runtime loop

The runtime loop goes round many times a second. Each time round, it reads the session state (see **Session state**) and decides what to do. This section says what it does in each state.

A session that is not Live does not mean the railway stands still. The layout carries whatever it carried a moment ago: a locomotive stands on a block, a hand pushes a cut of wagons, a train left from the last session sits in a siding. The detectors go on reporting all of it. One rule therefore divides the loop:

> **While the session is not Live, the runtime causes nothing and keeps reading the detectors.**

- **Which pass runs.** Each time round, the runtime loop runs one of these:
  - **Live** — the whole runtime step (see **Runtime step**).
  - **Setup** — the stopped pass (see **The stopped pass**). Two cases in Setup run no pass:
    - `hardware_failure` is latched — the runtime stays off the failed device until an activation succeeds;
    - no `OccupancyInput` (the hardware layer's interface for reading the detectors) is open — the session entered Setup without interfaces built for the selected environment, as it does from Halted for `not_activated`, or a refused activation left none. There is nothing to read until an activation succeeds.
  - **Halted for `hardware_failure`** — no pass.
  - **Halted for `not_activated`** — no pass, because no interfaces are built.
- **Every time round closes timed-out Setup tools.** Whatever pass the runtime loop runs, or none, it also closes any Setup tool whose idle timeout has passed (see **Setup tools**, A timeout closes the tool on time).
- **Every time round consumes a watchdog trip.** The runtime loop consumes an `OutputWatchdog` trip record (the note the loss-of-control watchdog leaves when it has driven the outputs to the safe state) every time round, through the runtime's `consume_watchdog_trip()`, even when no pass runs (see **Trip-record consumption escalates a failed drop to hardware failure**). A watchdog may trip while the session is not Live, and a failed drop must still become a hardware failure.
- **The stopped pass.** The stopped pass runs items 1, 2 and 7 of **Runtime step** and nothing else: it reads and debounces the detectors, updates each train's blocks, and publishes. Trains therefore appear, lose blocks and are removed on the page while the session is in Setup. No claim is given and no train is stopped or started.
- **The stopped pass applies no power.** The stopped pass must not raise a block's voltage, throw a point motor, or energize anything else. In Setup it issues no device write at all, because the Setup tools own the outputs (see **Setup tools**, A Setup tool's writes select the same occupancy zone as operation).
- **Operator requests still run while the session is not Live** (see **Operator requests**).
- **Leaving Live enters the safe state.** Every way out of Live calls the runtime's `enter_output_safe_state()`. That method calls `OutputSafety.enter_safe_state()` (the hardware layer's safe-state sequence), so every block voltage goes to `0` and every point motor de-energizes (see **Safe state / fail-safe**). Before it, every train's knob goes to 0 V and every claim ends (see **Trains**, Leaving Live stops every train). When no interfaces are built, `enter_output_safe_state()` has nothing to write and succeeds. When `hardware_failure` is already set, a safe state that fails again changes nothing: the flag stays set and the caller carries on. The ways out of Live are:
  - entering Setup;
  - `/api/server/restart` and shutdown;
  - a hardware I/O failure.
- **Why the detectors read cleanly afterwards.** After the safe state every block is in the idle zone, where the digital detector decides (see **Occupancy source and drive zones**, Occupancy source follows drive state). A session that is not Live therefore reads its detectors through the one zone that carries no drive to disturb them.

## Operator requests

- **Requests run between runtime steps.** A request handler holds `AppState.simulation_lock` (the one lock that guards runtime state) while it reads or changes runtime state. The runtime loop holds the same lock for the whole of each runtime step and each stopped pass. An operator request therefore always runs between two of them, never during one.
- **A request's result is its return value.** Each request about a train checks its own conditions against the state at the moment it runs, and returns `{success, reason}`. The request handler returns that result to the page.
- **Requests run in every session state.** A request changes stored state at once, whether the session is Live or not. Whether a request is accepted in a given session state is part of that request's own rule.
- **A train request moves no train by itself.** A request about a train changes stored state only. The train's blocks change voltage on the next runtime step (see **Track power**).
- **The next runtime step sees the request's effect.** A request made between two runtime steps is visible to the whole of the next one.

## Operating environment and activation

A session has one **operating environment**:

- **Virtual Hardware** is the hardware-integration run.
  - It requires a complete, valid Hardware Setup.
  - It builds the same chips, binding maps, hardware interfaces, occupancy selection, and Setup tools as Real Hardware, but the device behind them is an in-memory fake.
  - The operator may control the raw detector and current inputs, the device's responses and failures, or a higher-level virtual train scenario that turns movement into those raw inputs.
  - It never opens the physical controller.
  - It never writes block occupancy directly as a shortcut. v2 learns of a train on Virtual Hardware only through the same detector read it uses on Real Hardware.
- **Real Hardware** is the physical-layout run.
  - It requires the same complete Hardware Setup, and every required device and binding must be available.
  - Its interfaces talk only to the physical device.
  - Missing or failed hardware blocks activation or enters the latched hardware-failure state. It never fills in a missing interface with a stand-in.

Both environments share the runtime step, the notifications, and the detector read and debounce. The environment changes only where the hardware layer meets the physical world:

1. **Hardware boundary.** The builder for an environment constructs all required interfaces as one transaction: it either returns the complete interface set or fails.
2. **Pacing.** Every environment advances in real wall-clock time, because it exercises hardware timing.
3. **Occupancy.** Every environment reads block occupancy from the configured occupancy interface. No environment works occupancy out from a model of where trains are.

- **`active_environment`** names the environment the session was last built for: the one the most recent activation attempt, Setup entry, or boot was for. Deactivation leaves it unchanged, so it still names that environment while no environment is active. `environment_active` says whether it is active now.
- **The selected environment** is the one the operator last chose with `PUT /api/operation/environment`. It is stored apart from the Hardware Setup, so loading a layout's wiring does not select or activate Real Hardware.
- **Neither is inferred.** The selected environment and `active_environment` are both stored session facts. Neither is worked out from an interface's type or from whether one chip happened to initialize.
- **When they differ.** They differ while the operator has selected an environment the session has not yet activated, because selecting changes no session flag (see **Session state**).
- **Each reader uses the one that matches its question.**
  - The selected environment says what the operator means to run next. The environment control, the readiness report, and Setup entry read it.
  - `active_environment` says what the session is running now. Detector polling reads it.

**Activation.** `POST /api/operation/activate` is accepted only while the session is not Live (see **Session state**). A successful activation does these in order:

1. It runs the readiness check and refuses on any blocker. A refused activation changes nothing else: the trains on the page stay as they were.
2. It closes any Setup tool that holds the outputs (see An open Setup tool is closed first).
3. It removes every train and clears the debounce counts (see Activation starts the railway over).
4. It builds the selected environment's interfaces and verifies the environment.
5. It sets `Switch.position_known` to `False` on every switch (see Activation sets what v2 knows about each switch).
6. It throws every switch to its recorded position (see Activation throws every switch).
7. It writes the session flags, and the session is Live (see Activation goes Live).

- **An open Setup tool is closed first.** When activation passes its readiness check in Setup while a Setup tool holds the outputs, it closes that tool before anything else, which completes the tool's safe-state sequence (see **Setup tools**, Closing a Setup tool restores the safe state). Only then does it build the new environment's interfaces. The outputs therefore pass from the tool to `BlockPowerController` and `SwitchPowerController` through the safe state and never have two owners.
- **What activation keeps.** The layout and the Hardware Setup.
- **Activation starts the railway over.** Activation removes every train before it builds the interfaces (step 3). Trains are not carried from one activation to the next: the runtime step finds every train again from the detectors, each as a new train with its knob at 0 V (see **Trains**, Trains are not saved). Activation clears the debounce counts, so a block a train stands on reads occupied `OCCUPANCY_ON_THRESHOLD` reads after the first step. Nothing moves after activation until the operator turns a knob.
- **Activation sets what v2 knows about each switch.** Before any throw, activation sets `Switch.position_known` (a runtime field that says whether v2 knows which way the points lie) to `False` on every switch. A fresh session has thrown no coil, and the points may lie either way. The throw then sets it `True` on each switch it throws.
- **Activation throws every switch.** Activation throws every switch once to the position the layout shows, through `SwitchManager.throw_all_to_layout()`, even one a train may stand across. The held-switch rule does not bind this throw: activation has removed every train, so no switch is held (see **Switches**, A held switch is not thrown). The event log records which were thrown.
- **Activation goes Live.** A successful activation writes the session flags its row in the **Session state** transition table lists, and the session is Live.
- **A refused or failed activation leaves no environment active.** The session reads Setup if it was in Setup, and otherwise Halted for `hardware_failure` or `not_activated` (see **Session state**).
- **Boot activates no environment.** The session boots Halted for `not_activated`, and the operator activates it. Boot runs no readiness check of its own. The operator has to switch the board's power on by hand first, so only the operator knows when the rig is ready.
- **Fail-closed environment lifecycle.**
  - **Before activation.** A requested Real Hardware session that cannot validate or initialize is not activated. The session reads Halted for `not_activated` with its readiness blockers, or Setup, or Halted for `hardware_failure` (see **Session state**), before anything drives.
  - **After activation.** A failure is reported to the runtime's `enter_hardware_failure(...)`, which attempts the full safe state and latches the hardware failure. The runtime does not keep running and retrying the failing I/O.
  - **Virtual Hardware.** It follows the same fault lifecycle against its fake device, so the fault handling can be tested.

## Tile

The layout is drawn on a grid. A `Tile` is one grid cell that holds track. An edge is one of a tile's four sides, named `N`, `E`, `S` and `W`. `RailwayNetwork` is the layout model: the object that holds every tile, segment, block, switch and tunnel of the loaded layout.

Tiles are grouped into segments (see **Segment**), and segments into blocks (see **Block**). A switch is one tile that belongs to no segment and no block (see **Switch**).

**Tile role and traversal**

- **Tile fields.** A `Tile` stores these fields, and each one is saved with the layout:
  - `x`, `y` — the tile's grid position;
  - `track_bits` — which rail pieces the tile holds, where each piece joins two of the tile's edges;
  - `dead_end_ports` — the edges where the tile's track ends with no track connected beyond it;
  - `switch_id` — the id of the tile's `Switch` record when the tile is a switch;
  - `length` — the tile's length.
- **Tile classification.** `Tile.track_type` is a computed read that classifies the tile from its track. It is not a stored field.
- **A tile holds no switch alignment of its own.** `Tile.switch_state` is a derived read.
  - When the tile is attached to a `RailwayNetwork`, the read resolves through `Switch.position` for the tile's `switch_id`.
  - A `_switch_state_local` value is a fallback for a bare tile attached to no network, such as one built in a unit test. It is never a second copy of the alignment beside `Switch.position`.

  `Switch.position` is the one alignment everywhere.
- **A switch tile points at its switch.** When the classification says the tile is a switch, `Tile.switch_id` must point to that tile's `Switch` record in the network. The switch's alignment must live only on that `Switch` record.
- **A tile holds no runtime state.** A tile does not store whether it is occupied or claimed. Occupied and claimed are recorded per block (see **Block**).
- **Each grouping level owns the ids of the level below it.**
  - `Segment.ordered_tiles: List[SegmentTileEntry]` is the ordered list of tiles in that segment. Each `SegmentTileEntry` carries `(x, y, entry_edge)` (see **Segment**).
  - `Block.segment_ids -> List[str]` is the ordered list of `Segment` ids in that block. It is a derived read, not a stored field: it is computed live from the block's stored `Block.segments` (its member `Segment` objects) as their ids in order (see **Block**).
- **A tile does not store its segment or its block.** `Tile`, `Segment` and `Block` are all registered with `RailwayNetwork`. The tile-to-segment and segment-to-block lookups go through indices that `RailwayNetwork` owns and computes from `Segment.ordered_tiles` and `Block.segment_ids`. A tile-to-block lookup takes two steps: tile to segment, then segment to block.

**Non-empty parent rule**

- **Non-empty parent rule.** Every grouping record always owns at least one child:
  - a `Block` keeps at least one `Segment` in its stored `Block.segments`, so its derived `segment_ids` is never empty;
  - a `Segment` keeps at least one tile in `ordered_tiles`.

  This holds at create time and at edit time: a grouping record is never created empty, and no edit may leave one empty.
- **Auto-delete on last-child removal.** When an edit removes a grouping record's last child, the parent is deleted in that same operation, through `delete_segment` or `delete_block`. This covers both of these:
  - `remove_tile_from_segment` or `remove_tiles_from_segment` taking a segment's last tile;
  - an edit that removes a block's last segment.
- **Deleting a segment updates its block.** Deleting a `Segment` removes it from its owning `Block.segments`. When that leaves the block empty, the block is deleted too. Every segment belongs to exactly one block.
- **Segment deletion keeps its block connected.** A segment may be deleted only when **either holds**:
  - it is a terminal member of its block — it sits at either end of `Block.segments`;
  - it is the block's only member.

  `SegmentGeometryManager.delete_segment(...)` rejects the deletion of any other member of a multi-segment `Block`, and changes nothing. This is the same end-only rule that `BlockGroupingManager.remove_segment_from_block` applies (see **Block grouping**). It keeps the remaining members one connected switch-free run (see **Block model**).
- **Several segments of one block emptied together are judged as one edit.** When one edit empties several segments of one block, such as erasing the track of a whole block, it is rejected only when the members left behind would not form one connected run.
- **Block deletion re-homes segments.** Deleting a `Block` through `BlockGroupingManager` (see **Block grouping**) never deletes its segments. Each freed segment is put into its own single-segment block, which is the default one-block-per-segment rule (see **Block grouping**). Every segment must belong to exactly one block, so a freed segment cannot be left without one.
- **Atomic commit.** The last-child removal and the parent's deletion commit together. When the parent's own delete checks reject the deletion, the child removal that triggered it is rejected as a whole, so an empty grouping record is never stored.

**Walking through a tile**

There are two separate families of helpers. One reads the track as the switches are currently set. The other lists every way through a tile whatever the switches are set to.

- **Live traversal helpers.** These two helpers map one edge through the traversal that `Switch.position` currently selects:
  - `Tile.get_active_exits(entry_edge, resolver) -> List[str]`
  - `Tile.get_active_entry_for_exit(exit_edge, resolver) -> Optional[str]`

  `resolver` is the network, or an equivalent object that can read `Switch.position` through `switch_id`.
- **Planning-time helper.** `Tile.enumerate_traversals(entry_edge) -> List[(exit_edge, required_switch_state)]` lists every legal way through the tile from `entry_edge`, whatever the switch's current position.
  - For a tile that is not a switch, it yields the one legal `(exit_edge, None)` pair.
  - For a switch tile, it yields each legal `(exit_edge, required_switch_state)` pair, where `required_switch_state` is the `Switch.position` that way through needs.
- **The planning-time helper is pure geometry.** `Tile.enumerate_traversals(...)` must not read the switch's current position.
- **No branch-to-branch traversal.** On a Y-shaped switch, going from one branch to the other branch is not a legal traversal. `Tile.enumerate_traversals(...)` must not yield it.
- **Which helper a reader uses.** A reader that follows the track as the switches are currently set, such as the walk that finds the next block ahead, uses the live helpers. The search for a way to a destination uses `Tile.enumerate_traversals(...)`.

## Tunnel

A tunnel joins two track ends that are not next to each other on the grid, so that track runs from one to the other as if the two tiles touched.

- **Tunnel table.** `RailwayNetwork.tunnels: Dict[Tuple[int, int, str], Tuple[int, int, str]]` pairs two dead-end edges, each given as `(x, y, edge)`, so they behave as a single tile-to-tile connection. The map is symmetric: every pair is stored in both directions, so a lookup takes one step.
- **One writer.** The only writers of the table are `RailwayNetwork.add_tunnel(a, b)` and `RailwayNetwork.remove_tunnel(endpoint)`.
- **Tunnel invariants.** `add_tunnel` accepts a pair only when **all four hold**:
  - the two endpoints are distinct;
  - each endpoint names an existing tile whose `track_bits` include the named edge;
  - each endpoint is a dead end at write time — no grid neighbour connects back on the opposite edge;
  - neither endpoint is already part of another tunnel.
- **The two edges may be any pair.** The edges of a tunnel may be opposite or not: W↔E, N↔S, N↔W and so on. A pair that is not opposite is needed in layouts such as zig-zag siding loops. `Tile._update_dead_ends` uses `RailwayNetwork.get_connection_edges(...)` and not the opposite edge, so its connectivity check follows the pair recorded on each tile.
- **Stale tunnels are swept.** `RailwayNetwork.propagate_from_tile(...)`, the update that recomputes dead-end ports around an edited tile, runs `_sweep_stale_tunnels()` first, so that it recomputes dead-end ports from the cleaned table. The sweep removes, through `RailwayNetwork.remove_tunnel(endpoint)`, any tunnel with an endpoint for which **any of these holds**:
  - the endpoint's tile has lost its track;
  - the tile's `track_bits` have lost the named edge;
  - the endpoint has gained a real grid neighbour that connects back.
- **Removing a tile sweeps too.** `RailwayNetwork.remove_track(x, y)` calls the sweep itself. Removing a tile does not always start an update from a grid neighbour, so the sweep would otherwise be missed.
- **A tunneled edge is not a dead end.** `Tile.dead_end_ports` is computed through `RailwayNetwork.get_neighbor(...)`, so a tunneled edge is left out of it. Every reader of dead-end status reads `dead_end_ports` and does no tunnel lookup of its own.
- **One shared neighbour helper.** `RailwayNetwork.get_connection_edges(tile_a, tile_b) -> Optional[Tuple[str, str]]` answers two questions at once: whether the two tiles are connected by track, and which edge on each tile connects to the other. It resolves both kinds of connection:
  - grid adjacency — the two tiles share an edge whose `track_bits` connect;
  - tunnel adjacency — the two tiles are linked through `RailwayNetwork.tunnels`.

  Traversal helpers use it and do not work out neighbour edges themselves.
- **Neighbour lookup.** `RailwayNetwork.get_neighbor(x, y, edge) -> Optional[Tile]` resolves in the same two steps. It returns the grid neighbour first. When the grid cell is empty, it returns the tile found through `RailwayNetwork.tunnels.get((x, y, edge))`.
- **Saved with the layout.** `to_dict()` and `to_layout_dict()` write `RailwayNetwork.tunnels` as a `tunnels` list of `{"a": {"x", "y", "edge"}, "b": {"x", "y", "edge"}}` records, one per pair. `from_dict()` reloads them through `RailwayNetwork.add_tunnel(a, b)` after all tracks are in place.
- **A malformed saved tunnel is skipped.** A saved entry that fails the **Tunnel invariants** is logged and skipped, and the load carries on.
- **A tunnel is always a block boundary.** Blocks stay grid-contiguous: `Block.ordered_tiles` does not walk through a tunnel.

## Segment

**Segment model**

- **What a segment is.** A `Segment` is a group of tiles that stands for one continuous run of track. Many tiles map to one segment, and one segment maps to one or more real rail pieces.
- **A segment is geometry only.** A segment does not store whether it is occupied or claimed, and it has no voltage of its own. Those belong to its owning `Block`.
- **A segment is never claimed.** A train claims the owning `Block` (see **Claiming the next block**).
- **One block per segment.** Every segment belongs to exactly one `Block`, listed in that block's `Block.segment_ids`.

**Segment geometry and traversal**

- **`ordered_tiles` is the segment's own tile order.** `Segment.ordered_tiles` lists the segment's tiles in the order a train moving forward along the segment passes them.
- **Edits never reorder a segment silently.**
  - Create, extend and split edits validate that the order the caller gives is connected and holds no switch, and never reorder or reverse it.
  - Adding or removing a tile at either end keeps the order of the other tiles.
  - An edit in the middle is rejected.
  - An edit that would reverse the order as a side effect is rejected.
- **One explicit reversal.** The only operation that reverses a segment is `SegmentGeometryManager.reverse_segment_orientation(segment_id)` (see **SegmentGeometryManager**).
- **Removing the last tile deletes the segment** (see **Non-empty parent rule**).
- **Segment tile entry.** Each entry in `Segment.ordered_tiles` is a `SegmentTileEntry` carrying `(x, y, entry_edge)`. `entry_edge` is the outside edge through which a train moving forward enters that tile.
- **No separate anchor field.** No field stores the first tile or the edge where forward movement enters the segment. Both come from `ordered_tiles[0]`:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ordered_tiles` | ordered list of `(x, y, entry_edge)` entries | yes | The segment's own order. `ordered_tiles[0]` is index `0`, and its `entry_edge` is the edge where forward movement enters the segment |

- **The exit edge is not stored.** The edge a forward-moving train leaves a tile by follows from the connection to the next tile in the list. For the last tile, it follows from the segment's outside boundary toward the next segment.
- **Block-direction helper.** One helper works out direction along a block.
  - For each tile, it derives the edge a forward-moving train enters by and the edge it leaves by, from which tiles are next to each other in each member `Segment.ordered_tiles`.
  - For a single-tile segment, and for validation, it reads `Segment.ordered_tiles[0].entry_edge`.
  - Over a block, it works on the member segments' tiles joined in `Block.segment_ids` order.
- **Stored-orientation invariant.** Every member segment's increasing order runs the same way as the block's increasing forward walk. Block grouping never reverses a segment when it reads one, and adding a segment at an end is rejected when that segment runs the opposite way.
- **Segment length.** `length_cm` is the segment's track length in centimetres. It is saved with the layout and changed through `SegmentGeometryManager`.
- **Segment name.** `name: str = ""` is the segment's display name. The operator sets it, it is saved with the layout, and nothing in the runtime reads it. It names this run of track only. The block that owns the segment carries its own separate `Block.name` (see **Block grouping**, One name per record).

**SegmentGeometryManager**

- **Where segment edits live.** Every change to a segment's tiles or geometry lives in `SegmentGeometryManager` (file `segment_geometry_manager.py`). Its methods include `SegmentGeometryManager.reverse_segment_orientation(segment_id)`.
- **Reversal checks.** `reverse_segment_orientation(segment_id)` checks that the segment exists, and rejects the operation unless the segment is the only member of its owning `Block`. A reversal inside a multi-segment block would break that block's **Stored-orientation invariant**.
- **What a reversal does.** When the reversal is accepted, the method does both of these:
  - it reverses `Segment.ordered_tiles`;
  - it rewrites each entry's `entry_edge`, so that the new first entry is the old last tile entered from the opposite edge.
- **What a reversal leaves alone.** A reversal keeps the tiles the segment owns, so the network's tile-to-segment index needs no rebuild. It does not change block membership, runtime state or `length_cm`.
- **A failed reversal changes nothing.** When the reversal is rejected, the segment is left as it was.
- **Network forwarders.** `RailwayNetwork` exposes forwarders for segment create, extend, delete, split, validation, tile removal and reversal.
- **A new segment gets a block.** Segment creation also creates the default single-segment block in the same operation (see **Block grouping**).
- **Deleting a segment.** `SegmentGeometryManager.delete_segment(...)` applies **Segment deletion keeps its block connected** (see **Tile**, Non-empty parent rule).

**Segment orientation REST surface**

Segment create, extend, delete, split, tile removal and `name` edits each have their own endpoint. Reversal has this one:

| Endpoint | Body | Behaviour |
| --- | --- | --- |
| `POST /api/segments/<segment_id>/reverse-orientation` | — | Calls `SegmentGeometryManager.reverse_segment_orientation(segment_id)`. Returns the updated segment on success. Returns not-found or a validation failure and changes nothing otherwise, including when the segment is not the only member of its `Block`. Grouping never calls this endpoint by itself. |

## Block

**Block model**

- **What a block is.** A `Block` is the unit of power and of occupancy: a connected run of one or more segments with no switch in it, wired to one power feed. It carries one voltage and holds one train at a time (see **One train per block**).
- **Block fields.** A `Block` stores its `block_id`, its display `name`, and `segments` — the ordered `Segment` objects it owns. The derived read `segment_ids -> List[str]` gives their ids in that order, and the derived read `length_cm` is the sum of its segments' `length_cm`.
- **Block state.** A block reads occupied or empty, and may be claimed by one train (see **Claiming the next block**).
- **Block tile order.** `Block.ordered_tiles` is a derived, read-only property: the member segments' `Segment.ordered_tiles` joined in `segment_ids` order. The block stores no tile list of its own. The segments in a block join end to end into one connected walk with no switch in it, so the joined list is one well-defined tile order for the block.
- **Forward and reverse along a block.** `forward` means movement in the increasing direction of `Block.ordered_tiles`, and `reverse` means movement in the decreasing direction.
- **The voltage sign follows the tile order.** The sign of the block's voltage is expressed along `Block.ordered_tiles` (see **Track power**).
- **Block direction has one source.** Direction along a block is computed only by the **Block-direction helper** (see **Segment**), using `Block.ordered_tiles`.
- **`Block` is a data container.** A block stores no train state. Which train a block belongs to, and which train has claimed it, is read from the runtime's trains (see **Trains** and **Data flow to the page**, Live fields). A decision about one block needs to check all trains at once, so that state is kept in one place.

**Block grouping**

- **Where grouping lives.** Creating a block, adding a `Segment` to the stored `Block.segments`, removing one from it, and deleting a block all live in `BlockGroupingManager` (file `block_grouping_manager.py`). `RailwayNetwork.create_block`, `delete_block`, `add_segment_to_block` and `remove_segment_from_block` are forwarders to it.
- **Grouping edits membership only.** `BlockGroupingManager` edits `Block.segments`, and the derived `segment_ids` follows. It never touches trains or claims, and it never writes segment geometry, which belongs to `SegmentGeometryManager`.
- **Grouping edits happen at the ends.** `add_segment_to_block` and `remove_segment_from_block` may add or remove a `Segment` only when **all three hold**:
  - the segment is added or removed at either end of `Block.segments`;
  - the result is still one connected walk with no switch in it;
  - every member's `Segment.ordered_tiles` runs the way of increasing block order.

  The backend rejects an insertion in the middle, a removal from the middle, a reorder, and an end-add that would need the stored segment order reversed. This is the same end-only rule as `Segment.ordered_tiles` (see **Segment**).
- **Grouping infers member order, never member geometry.** `BlockGroupingManager.create_block()` and `add_segment_to_block` both work out the member order from the stored geometry, not from an unordered set the caller gives.
- **Grouping never reverses a segment.** When a segment runs against the run it is being grouped into, grouping rejects the edit and names that segment. The operator first reverses it through `POST /api/segments/<segment_id>/reverse-orientation` and then retries the grouping.
- **A new segment is created with its block.** `RailwayNetwork.create_segment(...)` and `create_segment_with_tracks(...)` finish by calling `BlockGroupingManager.create_block(...)` for the one new segment in the same operation. The segment create and the block create commit together: a rejected block create rejects the segment create as a whole, and `RailwayNetwork` never returns a segment with no block. This is the default one-block-per-segment rule. The new block's id is derived from the segment's id (see **One id per record**).
- **An ungrouped segment stays its own block.** A segment the operator has not grouped into a larger block remains its own single-segment block under the default one-block-per-segment rule.
- **Adding a segment moves it.** `add_segment_to_block` moves a segment out of its earlier block. When that leaves the earlier block empty, that block is deleted (see **Tile**, Non-empty parent rule).
- **Removing a segment re-homes it.** `remove_segment_from_block` returns the segment to its own single-segment block. On a block whose only member is that segment, it does nothing.
- **Deleting a block re-homes every member.** `delete_block` returns every member segment to its own single-segment block.
- **One id per record.** A `Block` id and a `Segment` id must never be equal. The two are separate records with separate lifetimes. One string naming both would be ambiguous wherever either is shown, addressed or bound: a panel row, an event's `entity_id`, a `block_id` key in a Hardware Setup binding.
  - **The default pairing is a matched prefix pair.** Under the default one-block-per-segment rule, a segment `SEG_<suffix>` is owned by block `BLK_<suffix>`.
  - **The pairing is stable.** The pairing must be derived from the id alone, and must not change across a save, a load, or a re-bake of the same layout (generating a Python scene module from the layout file again). The `block_id` keys in the Hardware Setup bindings name blocks that outlive any one session.
  - **An id that an outside record names stays put.** When one of the two records already carries the id an outside record names, that id is kept and the other record takes the derived id. `create_segment(...)` and `create_segment_with_tracks(...)` create the segment's id and derive its block's id. A caller that must pin an exact block id states it.
  - **Splitting a segment.** When a segment is split and each child gets its own block, each child block's id is the parent block's id with a suffix (`PARENT_1`, `PARENT_2`), and each child segment takes the matching derived id.
- **One name per record.** `Segment.name` and `Block.name` are separate display names. No create, split, group or re-home step copies one onto the other.
  - `create_segment(...)` and `create_segment_with_tracks(...)` name the segment they create and leave the new block unnamed.
  - `create_block(...)` names the block and leaves every member segment's name as it stands.
  - Splitting a segment names the child segments after the parent segment and leaves the child blocks unnamed.
  - A segment re-homed by `remove_segment_from_block` or `delete_block` keeps its name and lands in an unnamed block.

  Each name says what its own record is: a segment's names a run of track, and a block's names a power feed. A record with no name is shown by its id, so a reader always sees which of the two they are looking at.

**One train per block**

- **One voltage per block.** A block owns the one voltage its member segments share. There is no separate voltage per segment. Which voltage a block gets is decided under **Track power**.
- **A switch has no feed of its own.** A switch between blocks runs on its switch feed block — the block its rails are wired to (see **Switch**, A switch belongs to no block).
- **An ungrouped segment is its own feed.** A block the operator has not grouped from a larger run is a single-segment feed with its own power. A layout with no grouping applied therefore behaves as one feed per segment. Grouping is how the layout records that several segments are really wired to one feed.
- **Detection isolation is separate from geometry.** A block is one occupancy section with its own detection. Its member segments are geometry only and are not detected one by one.
- **A block is a one-train power domain.** A block carries one train's voltage at a time. Two trains cannot be controlled separately on one shared feed: one voltage cannot give two speeds, and two locomotives on one feed run at different speeds and close on each other until they collide.
- **The rule that enforces it is the claim.** A block that another train is in, or has claimed, cannot be claimed by a second train (see **Claiming the next block**).
- **A second train on an occupied block is not detected.** When a locomotive runs onto a block another train is already in, nothing records it: the block already reads occupied, so the detector reports no change. What keeps a second train out is the claim: a train's voltage goes onto a block only once the train has claimed it (see **Claiming the next block**). A locomotive that coasts in anyway is not seen.
- **Current size is not used to count trains.** v2 does not tell a second motor from the size of the current a block draws. The size could show a second motor only while the block is driven, and a block whose train is standing still carries too little current to tell.
- **Several trains need several blocks.** Running several trains on one layout uses several blocks, with one powered train in each.

## Switch

**Switch model**

- **A switch belongs to no block.** A block has no switch in it (see **Block model**), so a switch's tile sits between blocks. A switch has no detector of its own. Its rails are fed from its feed block, which the `switch_track_source` binding in Hardware Setup names (see **Block and switch power controllers**, A switch's rails are fed from one block).
- **Identity and location.** A `Switch` stores `switch_id`, its display `name`, and `x`, `y` — the grid position of its tile.
- **Saved geometry fields.** A `Switch` stores these fields, and each one is saved with the layout:
  - `main_length_cm` — the length of the way through the switch used at MAIN, in centimetres;
  - `diverge_length_cm` — the length of the way through the switch used at DIVERGE, in centimetres;
  - `main_radius_cm` — the curve radius of the MAIN way, in centimetres;
  - `diverge_radius_cm` — the curve radius of the DIVERGE way, in centimetres;
  - `switch_type` — the kind of switch;
  - `notes` — free text the operator writes about the switch.
- **Runtime fields.** `position` and `position_known` are runtime fields and are not saved with the layout. After a boot or a layout load, every switch's `position` is `0` (MAIN) and its `position_known` is `False`.
- **`position` is the commanded alignment.** `position: int` is the switch's alignment: `0 = MAIN`, `1 = DIVERGE`. It is the single alignment every reader uses. It is the alignment the runtime last commanded, and it is the only one the runtime has, because a point motor reports no position back.
- **Readers resolve the alignment from the switch.** When traversal code needs the current alignment, it must read it from the `Switch` record. It must not keep a second copy on `Tile`.
- **`position_known` says whether the app knows where the points are.**
  - Activating an operating environment sets it to `False` on every switch. A fresh session has fired no coil, and the points may lie either way.
  - Every `Switch.position` write sets it to `True` (see **Canonical `Switch.position` mutation workflow**).
  - The activation throw sets it to `True` (see **Activation throws every switch to the layout**).

  While it is `False`, `Switch.position` is the last alignment the model held.
- **A switch changes only itself.** `Switch` holds no reference back to the runtime and never notifies another record. Its methods change the switch's own fields only.
- **`SwitchManager` pairs the write with the throw.** `SwitchManager` is the part of the runtime that changes switch positions. `SwitchPowerController` is the hardware-layer class that fires a point motor's coil. `SwitchManager.request_position_change(...)` pairs a position write with the physical throw. `SwitchManager` looks up `SwitchPowerController` on `RailwayNetwork` each time it throws.
- **Editor writes need no runtime.** Editor writes to a switch — placing it, wiring its connections, editing a saved field — call no runtime method.

**Activation throws every switch to the layout**

- **Activation throws every switch to the layout.** When an operating environment activates, the runtime calls `SwitchManager.throw_all_to_layout()`. For every switch, it drives one forced throw to `Switch.position` through `SwitchPowerController`, and sets `position_known = True` on each switch as its throw is issued. A throw that cannot be issued is a hardware failure like any other (see **Block and switch power controllers**, An unissued throw is fatal to the session): the activation fails, and the session reads Halted for `hardware_failure`.
- **Why one throw is enough.** A fresh session has fired no coil, and one pulse lands solenoid points on the side it drives wherever they started. After the throw, the model and the points agree.
- **The activation throw writes no position.** `throw_all_to_layout()` does not write `Switch.position`. The position it throws to is the one the model already holds.
- **The activation throw does not wait for clear points.** It throws even a switch a train may be standing across. Points carry no detector, so nothing at activation can say whether a train is on them.
- **The activation throw reports once.** When every throw was issued, `throw_all_to_layout()` emits one `kind = "info"` event with `operation = "throw_all_to_layout"` that lists the switches thrown. The event carries no `ErrorRecord`.

**Canonical `Switch.position` mutation workflow**

- **Canonical `Switch.position` mutation workflow.** Every successful change of `Switch.position` runs the same three stages in order, whether it comes from an operator's throw or from a throw v2 makes for a train with a destination (see **Switches**, With a destination, the runtime throws the switches on the way):
  1. **Check.** While the switch is held (see **Switches**, A held switch is not thrown), the request fails with `reason = "switch_held"`, and nothing is written or thrown.
  2. **Write.** Write `Switch.position`, the commanded alignment, and set `Switch.position_known = True`.
  3. **Throw.** Drive the physical throw through `SwitchPowerController`.
- **A forced throw fires the coil every time.** `SwitchPowerController` fires no coil for a throw to the alignment the output last drove, which it reads through `get_last_throw`. A forced throw fires the coil anyway. The activation throw is the one forced throw.
- **A throw for a destination uses the same entry point.** The runtime throws a switch for a train with a destination through `SwitchManager.request_position_change(switch_id, new_position)`, the same call an operator's throw makes. It makes the call only for a switch that is not already set for the way (see **Switches**, With a destination, the runtime throws the switches on the way).
- **An operator's throw is not forced.** `SwitchManager.request_position_change(...)` drives the throw on every accepted command, without force. Whether a coil fires is then decided by the last alignment the output itself drove (`get_last_throw`), not by whether `Switch.position` changed. A command that names the alignment the model already holds therefore still re-seats points the model has drifted from.
- **Who checks and who writes.** `SwitchManager.request_position_change(...)` performs the check, because whether a switch is held depends on every train's blocks and claims, which the runtime holds. `Switch.apply_position_change(new_position)` performs the write only, and the write sets `position_known = True`. It is a pure data operation: it drives no motor, and its caller issues the throw.
- **One entry point for a throw while Live.** `SwitchManager.request_position_change(switch_id, new_position)` is the single entry point for an operator's throw request and for the runtime's throw for a destination. Every page and API handler that throws a switch goes through it. It runs the whole check, write and throw at once, and returns the `(success, reason)` result for that request.
- **The endpoint.** `PUT /api/switches/<switch_id>/state` with body `{ "state": int }` is the operator's throw request, where `state` is the commanded `Switch.position` value. The request service calls `SwitchManager.request_position_change(switch_id, position)` with `position` taken from `state`. The HTTP handler only maps the result to an HTTP reply.
- **Operating-throw gate.** `SwitchManager.request_position_change(...)` is refused while the session is not Live, before any stage runs. It returns `success = False` with `reason = "not_live"`. A throw command reaches a coil, and the coil belongs to whoever owns the outputs; while the session is not Live, the runtime does not.
- **How the gate reads the session.** The gate reads `AppState.session_state()`, like every other reader (see **Session state**).
- **An operator's throw is applied between runtime steps.** The request commits between two runtime steps (see **Operator requests**).
- **An operator's alignment change is reported once.** A `Switch.position` write from `request_position_change(...)` emits the alignment-change event with `operation = "request_position_change"` once, when the write changes the alignment.
- **A throw for a destination is reported once per switch.** A throw the runtime makes for a train with a destination emits one `kind = "info"` event with `operation = "request_position_change"` through `NotificationCenter`, the event stream the page reads. Its `entities` name the switch, its `message` states the new position, and its payload carries the train's id as `for_train_id`. No operator command drives this throw, so it has no command-result event.
- **No silent write.** Nothing writes `Switch.position` without emitting its alignment-change event.
- **Command result.** The result of an operator's throw request is delivered through `NotificationCenter`, not read off the raw HTTP reply. The request service turns the `(success, reason)` result into one command-result event with `source = "Switch"`, `operation = "change_state"`, `kind = "info"` on success or `"warning"` on failure, and payload `{command_id, switch_id, success, reason}`.
- **The command result and the alignment change are two events.** The alignment-change event records that the write changed the alignment. The command-result event records that the operator's request was accepted or rejected.
- **A refused request changes nothing.** When the check refuses an operator's request, the request fails through the command-result event and the switch is not changed.
- **A throw the hardware could not make.** When the hardware could not be commanded to make the throw, the command-result event carries `success = False` and that `reason`, and `Switch.position` is not rolled back.
- **The bench-throw recorder.** `SwitchManager.record_bench_alignment(switch_id, position)` records the alignment a Setup switch throw test has already driven (see **Switch throw test**). It runs the write stage only:
  - It calls `Switch.apply_position_change(position)` with no check. The coil has already moved the points, so the model must follow them.
  - It emits the alignment-change event with `operation = "record_bench_alignment"`.
  - It drives no throw. The test already pulsed the coil in the operating write order and pulse width, and no operating environment is active in Setup for `request_position_change(...)` to accept.

  The switch throw test is an alignment the operator asked for, and its coil has already moved the points, so the model must follow them.
- **Two throws do not check for a train.** These two throws fire the coil without the check stage:
  - a Setup switch throw test, which pulses the coil before anything is checked (see **Switch throw test**). A switch-test throw made while the session is Live is an ordinary operator throw and is checked;
  - the activation throw (see **Activation throws every switch to the layout**).
- **Ending a claim does not move a switch back.** Nothing restores an earlier `Switch.position`. `Switch.position` stays at the last alignment written, and a later throw sets it again when needed.

## Block and switch power controllers

**`BlockPowerController`**

- **`BlockPowerController` is the software side of block voltage.** It holds `TrackOutput` (the hardware interface that drives a block's rails) and is its only caller while the session is Live. It applies a voltage the runtime step has already decided (see **Track power**), or the 0 V of Stop all, and owns nothing but sending that voltage and reading it back. A Setup tool is the one other caller of `TrackOutput`, and only while it holds the outputs (see **Setup tools own the writes; the detectors stay shared**).
- **The signed value is recorded on the block.** `BlockPowerController.set_block_voltage(...)` records the signed voltage it commanded, so the page reports which way each block is driven as well as how hard.
- **Every block is written on every runtime step.** Item 6 of **Runtime step** commands every block's voltage on every step, whether or not it changed. On Real Hardware each write that lands pushes out the loss-of-control watchdog's deadline (see **Safe state, watchdog and hardware failure**), so a runtime loop that stops stepping stops refreshing it.
- **A refused write is fatal to the session, and never recorded as voltage.** An environment cannot activate with a missing binding or chip, so a refusal or exception after activation means the device has failed at runtime. `BlockPowerController.set_block_voltage(...)` records the commanded voltage only when the write landed. Otherwise it reports the failure to the runtime's `enter_hardware_failure(...)`, which drives the safe state itself, latches the halt, and emits one `kind = "error"` event with an `ErrorRecord` naming the failed device (see **Hardware I/O failure is fatal to the session**).
- **The first unreachable block is named.** The first time it cannot reach a given block, `BlockPowerController.set_block_voltage(...)` also emits one `kind = "warning"` event with `ErrorRecord.origin = "block_track_output_unreachable"` naming that block, so a dead rail can be found block by block. That warning is emitted at most once per block.
- **A write that fails inside the safe-state sequence does not re-enter it.** `OutputSafety.enter_safe_state()` calls the non-recording device writes (`SwitchPowerController.de_energize_device_outputs()` and `BlockPowerController.zero_device_outputs()`), not the ordinary recording writes. When a per-channel write fails inside that sequence, the failure is recorded in that run's failure set and the sequence goes on to the next channel. The result is reported once, as the "safe state cannot be completed" case of **Hardware I/O failure is fatal to the session**, never by a second call into `enter_hardware_failure(...)` or a second `enter_safe_state()` from inside the first.
- **The runtime drives nothing again until a successful activation.** While `hardware_failure` is set, the session reads Halted for `hardware_failure`, or Setup once the operator enters it, and the runtime loop runs no pass (see **Runtime loop**, Which pass runs). Only a Setup tool may drive an output meanwhile. Only a successful activation clears the flag (see **Recovery is reactivation**).
- **The page never shows a block powered while the rail is dead.** The runtime never goes on running trains around missing hardware.
- **A watchdog drop is reconciled on the next pass.** A watchdog drop is the one path that writes the rails to 0 V before any recording write. The pass that consumes the trip record sets the recorded voltages to match (see **Safe state, watchdog and hardware failure**), and the next runtime step commands every block again from **Track power**.

**`SwitchPowerController`**

- **`SwitchPowerController` is the software side of a switch's two electricals.** They are the throw it drives onto the point motor, and the block feed the switch's rails run on. It holds `SwitchOutput` (the hardware interface that pulses a point motor) and is its only caller while the session is Live, the safe-state call included, so `SwitchManager` asks it for a throw and never touches the interface itself. A Setup tool is the one other caller, and only while it holds the outputs.
- **The two electricals are different wires.** `SwitchOutput` takes a target position (`0` = MAIN, `1` = DIVERGE, matching `Switch.position`) and drives the point motor. It holds no wiring knowledge: how a position reaches the motor is the driver's business (see **A switch throw is a pulse on one of two coils**). The rails are the other wire: they carry the voltage of the switch's feed block, which `BlockPowerController` commands. The two are never compared.
- **A switch's rails are fed from one block.** A switch has no track feed of its own. Its rails are wired to its feed block and carry that block's voltage. The wiring is the `switch_track_source` binding in the Hardware Setup (`switch_id` to `block_id`), and `SwitchPowerController` is that map's only reader. The feed block is the first block reached by walking out of the switch's toe, across any other switch in between.
  - A train crossing the switch from its toe is standing on the feed block.
  - A train crossing toward the toe is entering the feed block, which it has claimed, so the feed block already carries the train's voltage (see **Track power**).

  Either way the feed block is one **Track power** already powers, so a switch is live under a train with no voltage rule of its own.
- **Rail voltage is read, never written.** `SwitchPowerController.get_switch_voltage(switch_id)` reports the voltage on a switch's rails by reading its feed block through `BlockPowerController.get_block_voltage(source_block_id)`. While an incomplete setup is being edited or tested in Setup, it returns `None` for a switch with no `switch_track_source` entry; that is a readiness blocker and cannot happen while Live. It is a read for the page, and no rule reads it.
- **A throw is blind.** A point motor reports no position back: it has no contact, no encoder and no sensed position. `Switch.position` is therefore the commanded position, and nothing confirms the points moved. `SwitchManager` must not treat a throw that was issued as proof the switch moved. The rail-voltage read is no such proof either: it reports the feed block's voltage and says nothing about where the points are.
- **An unissued throw is fatal to the session.** `SwitchPowerController.set_throw(switch_id, target_state)` returns `True` when the command reached the device and `False` when it did not. A missing port binding or chip is rejected before activation, so a refusal or exception after activation means the device has failed at runtime. The controller reports the failure to the runtime's `enter_hardware_failure(...)`, which drives the safe state itself, and reports the failed command through `NotificationCenter`. It is the only reporter of an unissued throw: `SwitchManager` sees the `False` return and reports nothing itself. The throw is never retried by itself, and `Switch.position` stays the commanded position.
- **Virtual Hardware can make a throw fail.** Virtual Hardware can inject any fault listed under **Hardware interface** that applies to `SwitchOutput`, to show the same hardware-failure course with no rig. It does not pretend to see a point motor stick after a pulse that was issued.

## Hardware interface

The hardware interface is the line between the runtime and the rig. Above it, code speaks only of blocks and switches. Below it, the hardware layer turns each call into chip and port transactions on the device.

- **Three interfaces, addressed by logical id.** The runtime drives and reads the rig only through three abstract interfaces. Each call names a logical id (`block_id`, `switch_id`), never a pin or a wire:
  - `TrackOutput` — sets a block's voltage;
  - `SwitchOutput` — throws a switch;
  - `OccupancyInput` — reads the detectors.
- **One complete set per environment.** Virtual Hardware and Real Hardware build the same implementations of the three interfaces from Hardware Setup, as one complete set. The only difference between the two environments is the device underneath: in-memory in Virtual Hardware, physical in Real Hardware.
- **A missing part fails the build.** When a role (the job a chip kind does: track output, switch output, or occupancy input) or an interface is missing, the build of the set fails. A missing interface (`None`) never asks the runtime to install a simulated replacement.
- **Who calls the output interfaces.** `BlockPowerController` (the class that commands and records each block's voltage) holds `TrackOutput`. `SwitchPowerController` (the class that commands each point motor) holds `SwitchOutput`. During operation, these two are the only callers of the two output interfaces, and they hold no wiring knowledge themselves. This includes the per-channel zero and de-energize writes of the safe state (see **Safe state / fail-safe**).
- **A Setup tool is the one other caller.** A Setup tool calls the output interfaces only while it holds the outputs (see **Setup tools own the writes; the detectors stay shared**).
- **No wiring knowledge above the interface.** No code above the interface names a GPIO pin, an expander chip, a shift-register bit, or a bus type. A concrete implementation of an interface owns the map from logical id to physical channel and is the only place that knows the wiring. The map itself is data: the binding table in **Hardware setup**, which the concrete implementation loads.
- **Implementations are interchangeable.** Direct GPIO, a 74HC595 / 74HC165 shift-register chain, and an I²C expander such as MCP23017 are interchangeable implementations of the same interfaces. Choosing one changes which implementation is built and nothing else.

**Fault injection**

- **Fault injection belongs to Virtual Hardware.** Hardware faults the operator can trigger are modeled by Virtual Hardware's in-memory device, below the chip drivers and the interface implementations. A test may put a stub directly in place of an interface. The injectable fault kinds are exactly these:
  - `transaction_refusal`
  - `timeout`
  - `device_disconnect`
  - `invalid_reading` (`OccupancyInput` and the block-voltage readback)
  - `input_hold_or_flicker` (`OccupancyInput`)
  - `output_disagrees_with_command` (`TrackOutput` readback)
- **The fault endpoint takes one of these values.** `PUT /api/virtual-hardware/fault` takes one of the six fault kinds (see **Hardware REST API**, `VirtualHardwareFaultRequest`).
- **What an injected fault leads to.** Each fault follows the path the same fault takes in Real Hardware:
  - on `OccupancyInput`, `transaction_refusal`, `timeout`, `device_disconnect`, and `invalid_reading` are hardware I/O failures (see **Hardware I/O failure is fatal to the session**);
  - on the block-voltage readback, every fault is a diagnostic read that never becomes a hardware I/O failure, `invalid_reading` and `output_disagrees_with_command` included;
  - `input_hold_or_flicker` is a successful read, which debounce handles (see **Occupancy detection**).

**Input and sensing**

- **`OccupancyInput` reads one thing.** `OccupancyInput` is the read side for sensors that no software output drives. It reads block current detection (one yes or no per block: whether the block's detector reports current) through `read_block_currents()`. Block current detection is the source of truth for block occupancy. Its read and debounce are defined in **Occupancy detection**.
- **The block-voltage readback is not an `OccupancyInput` read.** It is the output interface reading back its actual output, through `TrackOutput.get_readback_voltage`. Its one consumer is the diagnostic block-voltage read `BlockPowerController.get_block_voltage(block_id)` (see **Block and switch power controllers**).
- **The last commanded output is a separate value.** `TrackOutput.get_actual_voltage` returns the last output the app commanded. The track output driver classifies that value into the drive zone (see **Occupancy source follows drive state**). The diagnostic block-voltage read does not read it.
- **`OccupancyInput` names the blocks it can answer for.** It does so through `detected_block_ids()`. Activation requires both occupancy bindings for every layout block, so this set is the complete set of layout blocks.
- **A switch's position has no sensor read.** A switch's position is the commanded `Switch.position`. No sensor is used to detect a fault in a switch's position, to verify it, or to recover it.

## Safe state, watchdog and hardware failure

**The safe state**

- **Safe state / fail-safe.** The safe state is every point motor de-energized and every block at `0` V. Each output interface defines its own safe state, and one entity for the whole session owns the sequence: `OutputSafety`. Its one method, `enter_safe_state()`, performs the whole sequence itself, in order:
  1. de-energize every point motor through `SwitchPowerController.de_energize_device_outputs()`;
  2. command every block voltage to `0` through `BlockPowerController.zero_device_outputs()`.

  One owner means track and point outputs cannot disagree about whether the rig is driving. Point motors go first because a solenoid is the output that destroys itself if left energized; track voltage only keeps a train moving.
- **Both steps are non-recording device writes.** A non-recording device write commands the device transport (the code path that carries each read and write to the device) directly. It does not touch the block voltages `BlockPowerController` records, `Switch.position`, or any other value the runtime records. This makes `enter_safe_state()` safe to call from any thread, including the Real Hardware `OutputWatchdog` daemon thread (see **Loss-of-control watchdog**).
- **The safe state still updates the driver's last-commanded voltage.** Both non-recording writes pass through the driver. So the safe state itself sets to `0` the per-channel last-commanded voltage that the drive zone is classified from (see **Occupancy source follows drive state**), and every block reads as the idle zone. Only the values the runtime records wait for the separate, later reconciliation.
- **Every chip is attempted even when one fails.** A single dead chip cannot leave the rest energized, and the failures are reported together.
- **Reconciling the recorded values is a separate, later step.** Setting the block voltages `BlockPowerController` records, and the other values the runtime records, to match the safe state is always done by the runtime thread (whichever thread holds the runtime lock: the runtime loop during a runtime step, or a web request between two steps), never by `enter_safe_state()` itself. There are two paths:
  - for every runtime-thread caller, the runtime's `enter_output_safe_state()` reconciles synchronously, immediately after `enter_safe_state()` returns;
  - on the watchdog path, the runtime reconciles when it consumes the trip record, on the runtime loop's next iteration (see **Trip-record consumption escalates a failed drop to hardware failure**).
- **Runtime-thread callers reach the sequence through `enter_output_safe_state()`.** `enter_output_safe_state()` is the runtime's wrapper around `OutputSafety.enter_safe_state()`. The wrapper runs the sequence. Entering Setup, a restart or shutdown, and entering hardware failure all use the wrapper.
- **The watchdog is the sole direct caller.** The Real Hardware `OutputWatchdog` is the only caller that calls `enter_safe_state()` directly.
- **A Setup tool's ending is not a caller.** A Setup tool zeros the outputs it drives through its own writes (see **Setup tools**, Closing a Setup tool restores the safe state).
- **Environment behavior for safe state.** Virtual Hardware performs the same sequence as Real Hardware, against the in-memory device.
- **Safe state is a de-energize, not a lock.** Nothing is withheld from the outputs afterwards: the next ordinary output write drives the layout again, with no reactivation. The safe state is the whole physical safety mechanism. Its limit is that reaching it is a write, so it needs a live process and a working transport. A process or host that dies leaves each board holding its last commanded value.
- **A process shutdown writes the safe state before it closes the device.** When the server process exits, it must run the safe state through the session's device while that device is still open, and must close the device only after the safe state returns. No other step of the exit may close the device. The order matters because a closed device refuses every write, the safe state included.

**The watchdog**

- **Loss-of-control watchdog.** In Real Hardware, activation starts one safety-only daemon, `OutputWatchdog`, which puts the outputs in the safe state when the runtime thread stops refreshing them. Virtual Hardware does not start it, because it drives no physical motor.
- **What refreshes the watchdog and what expiry runs.** Each ordinary output transaction that lands refreshes the watchdog's deadline, which is `DEFAULT_WATCHDOG_TIMEOUT_S` (2 seconds) after that transaction. The runtime step writes every block on every step (see **Block and switch power controllers**, Every block is written on every runtime step), so a loop that keeps stepping keeps the deadline ahead. When the deadline expires, the daemon runs only `OutputSafety.enter_safe_state()`, under the same transport lock that ordinary device transactions use, so the two never collide on the device.
- **The daemon writes only below the model.** The daemon calls `enter_safe_state()` directly, and that method performs its whole sequence through each controller's non-recording device write (see **Safe state / fail-safe**). Those writes go straight to the device transport. The daemon records their transport results in its own thread-safe trip record (`WatchdogTripRecord`). The daemon must not read or change trains, notifications, or any other runtime state. Calling only the non-recording writes, never the ordinary write that records, is what guarantees that.
- **Trip-record consumption escalates a failed drop to hardware failure.** The runtime loop consumes the trip record on every iteration, through the runtime's `consume_watchdog_trip()`. This holds in every session state: at the start of the runtime step when one runs, and on its own in an iteration that runs no runtime step, a halt for `hardware_failure` included. A watchdog may trip while the session is not Live, and a failed safe-state attempt must still become a `hardware_failure`. On the next runtime-loop iteration after a trip, the runtime:
  - consumes that trip record;
  - sets every block's recorded voltage to `0` (see **Block and switch power controllers**);
  - enters hardware failure when the safe-state attempt failed;
  - otherwise emits one `kind = "warning"` event through `NotificationCenter`.
- **A successful drop does not end the session.** A successful safe-state drop protects the layout. A loop that recovers may drive again on its next write.
- **Arming and disarming the watchdog.** The runtime's `enter_output_safe_state()` disarms the watchdog before it calls `OutputSafety.enter_safe_state()`. The watchdog's own expiry call disarms it too. In both cases the next ordinary output write that lands re-arms it. An ordinary output write is one `BlockPowerController` or `SwitchPowerController` makes.
- **A Setup tool runs with the watchdog unarmed.** A Setup tool drives the outputs through its own drivers, and its writes do not arm or refresh the watchdog. The occupancy bench holds every block at one voltage on purpose, and a watchdog would take that voltage away 2 seconds after the bench's last write. What takes the voltage off a forgotten tool is its idle timeout (see **A timeout closes the tool on time**).

**Hardware failure**

- **Hardware I/O failure is fatal to the session.** When any of these occurs, the code that sees it reports the failure to the runtime's `enter_hardware_failure(...)`:
  - a failed output write;
  - a failed or invalid detector read;
  - a lost controller;
  - an inability to complete the safe state.
- **`enter_hardware_failure(...)` is the sole writer of `hardware_failure`.** That method, not the code that reports the failure, is the only writer of `hardware_failure = True`. In order, it:
  1. sets every train's knob to 0 V and ends every claim (see **Trains**, Leaving Live stops every train);
  2. calls the runtime's `enter_output_safe_state()`, which runs the sequence that attempts every de-energize and zero action even when one of them fails;
  3. records every failure in the payload of the event it emits;
  4. deactivates the environment (`environment_active = False`);
  5. sets the `hardware_failure` flag, which stays set until **Recovery is reactivation** clears it;
  6. emits one `kind = "error"` event with `operation = "enter_hardware_failure"`.

  A runtime step that reports a hardware failure does nothing more after it: the rest of that step's items are skipped.
- **What the session reads after a hardware failure.** The session reads Halted for `hardware_failure`, or Setup when it was in Setup (see **Session state**). The halt keeps no `ErrorRecord` and no second diagnostic payload from one runtime step to the next.
- **The runtime step stops while `hardware_failure` is set.** While the flag is set, the runtime loop runs no runtime step. The one thing it still does is consume the watchdog trip record. The failed session does not log the failure and retry the step, and it never replaces an interface with a simulated one.
- **Recovery is reactivation.** The way back from Halted for `hardware_failure` is a successful `POST /api/operation/activate`, and nothing else clears the flag.
  - **What activation proves.** It rebuilds every interface, verifies the environment, and passes the readiness check, so a success shows the device answers again.
  - **What a success does.** It clears `hardware_failure`, starts the railway over, and goes Live (see **Activation starts the railway over**). A refused or failed activation leaves the flag set.
  - **Diagnosing first.** The operator may enter Setup to find the fault before activating (see **Setup tools**, Setup). The flag stays set in Setup, and a successful activation out of Setup clears it.
- **Virtual Hardware follows the same flag and recovery flow.** This lets the flow be tested without the rig.

## Hardware setup

The Hardware Setup is the description of the physical rig: which bridge the PC talks through, which chips sit on it, and which chip port each block and switch is wired to. This section says what it holds, when it is complete, and how it is stored. The detector rules are under **Occupancy source and drive zones**, and the tools that build and test a rig are under **Setup tools**.

**What the setup holds**

- **Hardware Setup.** The Hardware Setup is the physical description shared by Virtual Hardware and Real Hardware. It holds:
  - a `controller` — the transport bridge;
  - a `chips` list;
  - the binding maps — they wire each logical id (a `block_id` or a `switch_id`) to chip ports;
  - detector calibration;
  - the two drive-zone levels `idle_deadband_v` and `current_sense_min_drive_v` (see **Two configured levels split that range into three zones**).
- **No environment flag.** The Hardware Setup holds no `enabled`, `develop`, or selected-environment flag.
- **Controller.** The controller is the transport a session drives through. The one controller is `ch347`, a USB-HID bridge that carries an I²C bus, an SPI bus, and GPIO. It may carry a `device_port`; the port is optional, because `ch347` opens by USB VID/PID. The controller list stays open, so another bridge is one added entry.
- **Chips.** Each chip has a `chip_id`, a `kind`, a `bus`, and the parameters that kind needs. There are five kinds:
  - `pca9685` (PWM) drives track power;
  - `hc595` (shift-out) drives switch throws;
  - `hc165` (shift-in) is the digital detector, one of block occupancy's two sources;
  - `ina3221` is a current monitor, which reads block supply current directly. A current monitor is block occupancy's other source;
  - `ina219` is a current monitor too. The two current monitors differ only in how many blocks one chip covers.
- **A chip's interface `role` is derived from its `kind`, never stored.** The occupancy kinds share the `occupancy_input` role and differ by port kind.
- **A chip's `bus` is user-set, not derived.** It is stored, and chosen from the buses its kind allows:
  - `pca9685`, `ina3221` and `ina219` — I²C only;
  - a shift register (`hc595` or `hc165`) — `spi` or `gpio`.
- **What a shift register's `bus` means.** A shift register clocks its data over the SPI peripheral on either bus. Its `bus` names where its latch line sits: one of the two hardware chip-select pads (`spi`), or a general-purpose pin (`gpio`). The `gpio` bus exists because the bridge carries only two chip-select pads, so a third shift register must latch somewhere else.
- **The rest of a chip's parameters are user-set as well.** An I²C chip has an `i2c_address`. A shift register on either bus has a latch line (`spi_select`) and a cascade count (`chain_length`).
- **Each chip kind contributes a fixed number of channels or bits.**
  - A `pca9685` contributes 16 PWM channels, of which a block's track takes two.
  - A shift register contributes `8 × chain_length` bits.
  - An `ina3221` contributes 3 current-sense channels.
  - An `ina219` contributes 1 current-sense channel.

**A block's rails**

- **A block's track is two PWM channels.** A block's voltage is signed, and the sign is the direction along the block's tile order. A block's track is therefore an H-bridge whose two inputs are driven by two `pca9685` channels. The binding names them `forward` and `reverse`, and its `reversed` flag states which of them a positive voltage drives (see **The rail binding bonds tile order to rail polarity**).
- **How the driver turns a voltage into channel writes.** The driver maps the voltage's magnitude to a duty cycle and the voltage's sign to which of the two channels drives. It holds the other channel at zero, so the two inputs are never driven together.
- **The two channels are chosen independently.** They are not a fixed adjacent pair. A bench build may wire a block's rails to any two channels on the chip.
- **A block at `0` has nothing applied to it.** Zero is a commanded voltage like any other: the channel matching the sign carries the magnitude and the other rests at zero. At zero neither channel drives, and the H-bridge is given no input at all. That is what a block at 0 V is: one the app applies no voltage to.
- **0 V is not a meter reading.** A block at 0 V may still show a voltage on a meter. While a locomotive's wheels span the insulated joint between two blocks, the block it is leaving feeds the block it is entering through the locomotive itself. Which blocks get a voltage is decided under **Track power**.
- **The rail binding bonds tile order to rail polarity.** A block's tile order and its rail polarity are independent facts: the order comes from how the layout was drawn, and which rail a channel drives comes from how the rig was wired. A block driven with the wrong polarity runs its trains backwards. The `reversed` flag on the block's `block_track` binding ties the two together.
- **What the `reversed` flag says.** The flag states which of the two bound channels drives the block in its `forward` direction (the direction that moves a locomotive toward the block's higher tile indices):
  - flag clear — a positive voltage drives the binding's `forward` channel;
  - flag set — a positive voltage drives the binding's `reverse` channel.
- **Neither flag value is a fault.** The two values are the two ways a block can be wired. The **Track direction sweep** is the measurement that shows which one this block is.
- **The flag is wiring, not layout.** It lives with the Hardware Setup like every other binding and persists independently of the layout.
- **The flag stays below the hardware interface.** The commanded voltage keeps its sign along the block's tile order everywhere above the driver. **Occupancy source follows drive state** reads that same value, and no layer above the driver carries a wiring branch.
- **The `forward` and `reverse` channels themselves must not change to flip a block.** They name two physical rails, and one of those rails may carry the block's idle detection feed, so rebinding the channels to flip a block would move that feed.
- **A channel edit keeps the flag.** A channel edit that does not name the flag keeps the flag's stored value, so re-picking the rails never silently resets the flag of a block whose direction was already found.
- **The flag covers the whole block.** A block is fed as one: the binding feeds every member segment, and a segment carries no polarity of its own.
- **The sweep resolves the flag the same way the driver does.** The track direction sweep first drives the channel a positive voltage would drive on that block, which is the channel that must move a locomotive toward that block's higher tile indices. A block whose flag does not match its wiring runs the locomotive the wrong way on that first leg. With the flag corrected it runs the right way, which is the operator's check that the flag is right.

**Switch throws**

- **A switch throw is a pulse on one of two coils.** A solenoid point motor has one coil per side, so a `switch_output` entry names two `hc595` output bits: `port_main` throws toward MAIN and `port_diverge` throws toward DIVERGE. A throw energizes one coil, waits the fixed throw pulse width, and de-energizes it. Both coils rest low between throws, and the points themselves hold the position.
- **The pulse width is one value.** The pulse is short, because a coil left energized burns out. Its width is one value in the driver, `SWITCH_THROW_PULSE_S` (10 ms, in `hardware/catalog.py`). An operating session and the commissioning throw test use it unchanged, so a bench test proves the same drive the runtime issues.
- **`port_standby` is optional.** `port_standby` is the bit wired to a motor driver's standby/enable pin, for drivers that ignore their inputs until that pin is high. A build whose driver needs no such pin leaves `port_standby` unset.
- **Every shift-out bit is driven as a coil.** Every shift-out bit the commissioning session drives is driven this same way, because any bit may be wired to a coil.
- **No shift-out bit is driven for longer than 200 ms.** A solenoid coil burns out when it is held, and any shift-out bit may be wired to one, whether or not a switch binding names it yet. `MAX_COIL_PULSE_S` (0.2 seconds, in `hardware/catalog.py`) is the longest any request may drive a shift-out bit. It binds every way a shift-out bit is driven: an operating throw, the switch throw test, the commissioning session's `pulse`, and a port-test output pulse. A request for a longer pulse on a shift-out bit is refused, never shortened and driven. A port-test pulse on a PWM output is not a coil and is not bound by this.
- **The order of a throw's writes is part of the contract.** The two coil bits are the motor driver's two inputs, so a throw must write them in this order:
  1. raise `port_standby`
  2. drive the target coil high
  3. drive the *other* coil low
  4. wait the pulse width
  5. drop `port_standby`
  6. drop the target coil
- **The other coil is always driven low, never assumed low.** This makes a throw correct from any earlier bit pattern, including the one a shift-register chain powers up in, and it never drives both inputs at once.
- **`port_standby` drops before the coil.** Dropping `port_standby` puts the driver's outputs into high impedance. When the coil input drops first, the driver stays enabled with both inputs low, which is its brake mode and shorts the coil.
- **The final coil write is unconditional.** With a standby bit, the final coil write only settles the resting pattern. On a driver with no standby bit, the final coil write is what de-energizes the coil, so the throw always makes it.
- **A failed throw leaves no coil driven.** The de-energizing writes must happen even when the throw fails part-way.

**Latch lines**

- **Latch lines are named by pad.** A shift register latches on a line named by the CH347 board pad. The set of lines depends on the chip's bus:
  - the SPI bus offers `cs0` and `cs1`;
  - the GPIO bus offers `gpio0`–`gpio7`.
- **The driver holds the pad-to-library map.** The `ch347api` library names the SPI lines off by one: its `set_CS1` drives pad `cs0`, and its `set_CS2` drives pad `cs1`. The driver holds the pad-to-library map and the correct active polarity per chip kind. No layer above the driver names a pad or a library call.
- **A GPIO-latched chip borrows a chip-select pad.** The bridge clocks SPI data only while a chip-select is asserted, and it releases that select when the transfer ends. A shift register latched on a GPIO pin must therefore borrow one of the two pads for the length of its transfer.
- **Which pad is borrowed.** The driver borrows a pad that no `hc595` latches on. Releasing an `hc595`'s latch pad would clock stray bits into that chip and may throw a switch. An `hc165`'s load pad is safe to borrow, since a transfer only freezes and reloads its parallel inputs.
- **No pad to borrow.** A build in which every chip-select pad latches an `hc595` must not also carry a GPIO-latched chip.

**Bindings**

- **Bindings.** The binding maps carry the wiring from logical id to chip port. Each map is keyed by logical id:
  - `block_track` — `block_id` to the two PWM channels driving its rails, `forward` and `reverse`, plus the `reversed` flag (see **A block's track is two PWM channels** and **The rail binding bonds tile order to rail polarity**);
  - `switch_output` — `switch_id` to its two coil ports plus an optional standby port (see **A switch throw is a pulse on one of two coils**);
  - `block_occupancy` — `block_id` to a digital-detector port on an `hc165` (see **Detector polarity is resolved in the hardware layer**);
  - `block_occupancy_current` — `block_id` to a current-sense channel on an `ina3221` or an `ina219`, plus a `calibration_id`.
- **Detector calibration is looked up by `calibration_id`.** `OccupancyInput` (the hardware interface that reads the detectors) resolves a channel's `baseline_ma`, `threshold_ma`, and `driven_threshold_ma` from `detector_calibration[calibration_id]`.
- **`switch_track_source` wires a switch to its feed block.** This further required map wires every `switch_id` to the `block_id` whose feed powers that switch's rails. It must name the switch's feed block. It is the one binding whose value is another logical id and not a chip port, because a switch's rails have no channel of their own. `SwitchPowerController` (the hardware-layer class that throws a switch's point motor and knows which block feeds its rails) is its only reader.
- **The concrete driver alone reads the rest of the binding table, with one exception.** The rest of the table is the physical-channel map named under **Hardware interface**. The exception is `block_occupancy_current`: Real Hardware's block-voltage readback (`TrackOutput.get_readback_voltage`) also reads it, where that chip provides a bus-voltage measurement (see **Block and switch power controllers**, A refused write is fatal to the session, and never recorded as voltage).
- **A binding must name a valid, uniquely-used chip port.** A binding is valid when **all of these hold**:
  - it names a configured chip whose role matches the map;
  - for the two occupancy maps, the chip's port kind matches — a digital detector bit for `block_occupancy`, a current-sense channel for `block_occupancy_current`;
  - the port is within that chip's range;
  - the port is not bound more than once;
  - the two channels of one `block_track` entry differ;
  - the two coil ports and the standby port of one `switch_output` entry all differ.

**Completeness and readiness**

- **Completeness is activation policy, not edit policy.** An incomplete setup may be saved and exercised in Setup while a rig is being built, but it cannot activate an operating environment. Completeness is checked against the loaded layout, and holds when **all of these hold**:
  - every block has one `block_track` binding plus both required occupancy bindings (`block_occupancy` and `block_occupancy_current`);
  - every switch has one `switch_output` binding and one `switch_track_source` binding;
  - every binding names a present compatible chip and a unique in-range port;
  - no binding names a deleted block or switch;
  - every required device is reachable.
- **Absence is always a blocker.** Absence of a required item is always a readiness blocker. A missing binding, chip, device, or detector source is never read as an empty block, as an allowed exception for an unpowered or undetected block, or as a request for a simulated stand-in.
- **An incomplete setup may be saved and tested in Setup, but not activated.** The editor may save any of the binding maps incomplete, and the Setup tools may exercise the entries that exist. The readiness report keeps activation blocked until the completeness and validity rules pass.
- **One shared readiness evaluator.** The readiness report is checked against the loaded layout by one shared evaluator. `GET /api/operation/environment`, `GET /api/operation/readiness`, and `PUT /api/operation/environment` all return its report. `POST /api/operation/activate` re-runs the same evaluator and refuses activation on the same blockers.
- **Readiness is evaluated live.** The check runs at each of those calls. A `PUT /api/hardware/config` made while an environment is already active takes effect on the next such check, not retroactively against the running session.
- **Virtual Hardware and Real Hardware share one setup.** Both environments build every required chip, binding, interface and occupancy rule from the same Hardware Setup. Virtual Hardware supplies an in-memory transport. Real Hardware supplies the physical transport.
- **The build is all-or-nothing.** Missing roles, bindings, chips, or required safety facilities are activation blockers, never requests for simulated stand-ins.

**What is read when**

- **A binding is read per write.** A binding names a port, and a session is built from chips; the two have different lifetimes. The chips, their transport, and the maps the chips are constructed and range-checked against are captured at activation. These three values are looked up on every write instead, from the setup as it stands at that moment:
  - a switch's `port_main`, `port_diverge`, and `port_standby` (`switch_output`);
  - a block's `reversed` flag on its `block_track` binding;
  - a switch's `switch_track_source` feed block.
- **Why those three.** These are the values a rig being wired gets wrong and corrects, and none of them changes which chips a session builds: each only re-aims an existing chip, or names a block. An operator who re-picks a coil port must see the next throw land on the new bit. A correction that waits for the next activation looks like a control that does nothing, and the operator changes the wiring instead of the setting.
- **Everything else is captured at activation.** The controller, the chip list, a `block_track` binding's two channels, both occupancy maps and their calibration, and the two drive-zone levels are captured at activation, because changing any of them means building different chips.
- **Reporting captured changes.** `GET /api/operation/environment` reports whether the saved setup holds changes to captured values that the running session was not built with. The three per-write values never raise that flag, because they are already in use.

**Storing the setup**

- **Persisted setup; separate selected environment.** A setup `PUT` writes the whole Hardware Setup to an app-managed JSON file. Setup changes never rewire a running session in place. The selected environment is stored independently.
- **A blocked activation leaves no environment active.** Activating an environment with a blocker returns its readiness blockers and leaves no environment active (see **Session state**). It does not fall back to another environment.
- **Activation owns the rebuild.** Activation owns rebuild, reconnect, verification, and starting the railway over (see **Activation starts the railway over**). The operator-facing workflow never requires a raw "restart server" step.
- **Saved with the layout.** Saving a layout writes its Hardware Setup into the layout file, because bindings are keyed by that layout's `block_id` and `switch_id`.
- **Loading a layout's setup.** Loading the layout may adopt that setup after validation. Loading does not import or change the selected operating environment, and never activates physical output.
- **A loaded setup replaces the previous setup as a whole.**
  - Absent sections become empty or default edit-state values, and therefore remain readiness blockers wherever they are required.
  - Dangling bindings (bindings that name a block or switch the layout does not have) are reported.
  - An invalid setup is reported without preventing the track geometry from loading.

## Occupancy source and drive zones

Every block has two occupancy sources: its `hc165` digital detector bit and its current-sense channel. This section says which one is believed at each read.

- **Both occupancy sources are mandatory.** This holds for every block. A missing binding blocks activation. A source that becomes unavailable after activation is a hardware I/O failure.
- **The choice of source is hidden.** The choice of source lives inside the hardware-backed `OccupancyInput`. It returns one boolean per block, `current present`, in the same shape whichever zone answered, so **Occupancy detection** consumes it without knowing the source.
- **Occupancy source follows drive state.** A block is read from whichever of its two sources is trustworthy at that read. The block's drive zone decides which one, because how hard software is driving the rails is what makes a detector believable.
- **The drive zone comes from the magnitude of the last-commanded voltage.** It is the magnitude of the last-commanded voltage on that block's track channels. The sign is direction and has no bearing on the zone.
- **The track output driver owns the drive zone.** The `TrackOutput` driver (the hardware interface that sets a block's voltage) classifies each block's last-commanded voltage into one of the three zones and reports it through `drive_zone(block_id)`. `OccupancyInput` asks that driver for the zone and does not compare a voltage against the zone levels itself, so the two configured levels are handed to the track output driver. The driver that put a block's rails in a state is the one that reports which state they are in, so what a commanded voltage means for the rails and for detection is decided in one place.
- **The block occupancy test classifies a readback.** The **Block occupancy test** builds no track output driver, so it applies the same classification to the voltage it reads back off the PWM chip.
- **The last-commanded voltage is tracked below the hardware interface.** It is held in one place, the track output driver, and written by whichever owner currently drives the channel:
  - `BlockPowerController` (the hardware-layer class that applies each block's voltage to `TrackOutput`) during operation;
  - a Setup tool while it holds the outputs (see **Setup tools own the writes; the detectors stay shared**);
  - the safe-state zero (see **Safe state / fail-safe**), which updates this same store.
- **A reconnect zeroes the store.** A reconnect re-initialization zeroes every PWM channel, so it zeroes this store too. The drive zone therefore always reflects the voltage the rail actually carries, whichever owner put it there.
- **Two configured levels split that range into three zones.** Both levels are one value for the whole Hardware Setup:
  - `idle_deadband_v` — at or below it, nothing is driving the rails;
  - `current_sense_min_drive_v` — at or above it, the drive is strong enough to put a readable load on a current channel.
- **Idle zone** (`IDLE`, at or below `idle_deadband_v`). There is no drive and no drive current to sense, so the digital detector decides. The track output driver holds both inputs of the block's H-bridge low.
- **The H-bridge must leave idle rails undriven.** With both inputs low, the H-bridge must leave both rails undriven, because the `hc165` digital detector senses the locomotive through an undriven rail. A DRV8871 does so: with both inputs low it lets both outputs float.
- **Driven zone** (`DRIVEN`, at or above `current_sense_min_drive_v`). The current channel decides: current is present when `abs(current - baseline_ma)` reaches the channel's driven threshold. The rail drive swamps the digital detector, but a motor under real drive draws a current the sense channel reads cleanly.
- **The driven threshold.** The driven threshold is the channel's `driven_threshold_ma` when it is set, and its `threshold_ma` otherwise.
- **Why the driven zone may need a higher threshold.** Two channels on one current-sense chip can couple: while a train draws drive current on one block, an empty neighbour on the same chip may read a stray current larger than what a train in the lighting zone draws. A train under drive draws several times that stray current. A driven threshold above the stray current rejects the stray reads without missing a driven train, and the lighting zone keeps the lower `threshold_ma` a train in that zone needs.
- **Lighting zone** (`LIGHTING`, between the two levels). This is the zone of a locomotive driven slowly. The current channel decides, judged against the channel's `threshold_ma`, and the digital detector is not read at all.
- **No digital fallback in the lighting zone.** A missing or unavailable current binding is an activation blocker or a hardware I/O failure, never a reason to read the digital detector.
- **Why the digital detector is not read in the lighting zone.** In this zone the digital detector reads the drive, not the train. A voltage in this zone reaches the rails as the full supply switched on for a fraction of each cycle (a duty cycle), so the detector's comparator reads occupied on roughly that fraction of reads whether or not a locomotive is there. The current channel tells the two cases apart across the same band by close to a factor of ten.
- **One read never confirms occupancy.** `OccupancyInput` returns current present as raw `true` to `BlockOccupancyDetector` (the class that turns the per-read booleans into a block's occupied or empty reading by counting consecutive reads). `BlockOccupancyDetector` still applies `OCCUPANCY_ON_THRESHOLD`; it does not confirm occupancy on one interface read.
- **Established presence.** A block's presence is established once its deciding source has read current present on `OCCUPANCY_ON_THRESHOLD` consecutive reads, in any zone, since the interface last returned raw `false` for it.
- **The lighting hold.** When a block's presence is established and its deciding source in the lighting zone reads no current, the hardware-backed `OccupancyInput` goes on returning its last reported value. The source must read no current on every one of `LIGHTING_CLEAR_HOLD_READS` consecutive reads before the interface returns raw `false`. `LIGHTING_CLEAR_HOLD_READS` is `30`, in `constants.py`.
- **After the hold.** Only after the hold releases does the interface return raw `false`. At that point `BlockOccupancyDetector` begins its separate `OCCUPANCY_OFF_THRESHOLD` count.
- **No hold without established presence.** When the presence is not established, the interface returns raw `false` on the first read with no current.
- **What resets the hold.** Any current-present read resets the lighting hold, and so does leaving the zone.
- **Why the lighting hold exists.** A locomotive can still go quiet on the current channel: one that draws less than its channel's `threshold_ma` reads the same as an empty block. A slowly driven locomotive near the threshold would then flicker between occupied and empty on every few reads. Holding the last reported value until the deciding source has read no current for a whole window keeps the block reading occupied through that flicker. The window still ends, so the hold has an exit.
- **Why only an established presence is held.** The hold answers every read of its window with the last reported value. Holding a single current-present read would hand `BlockOccupancyDetector` the consecutive reads `OCCUPANCY_ON_THRESHOLD` asks for, so one noisy sample would confirm an empty block. A locomotive starting on the next block puts exactly such samples on this block's channel.
- **Why the run counts reads in any zone.** A train confirmed while its block was driven or idle is then held when its block drops into the lighting zone.
- **Detector polarity is resolved in the hardware layer.** A shift-in chip carries an `active_low` flag describing how its detector drives the line:
  - a comparator with no inverter stage pulls its output low when a train draws current, so that chain's raw bit reads 0 for occupied and 1 for empty;
  - a detector with an inverter reads the other way.
- **Polarity is stated once per chip.** A detector chain is wired one way throughout: every block bound to that chip reads the same way, and a binding carries no polarity of its own.
- **Where polarity is applied.** The real `OccupancyInput` applies the chip's polarity as it reads each bit. The boolean it returns must always mean `current present`, whichever way the detector is wired. No code above the interface, and no other physical source, carries a polarity branch.
- **Polarity applies to the digital detector only.** A current-sense channel is judged on the magnitude `abs(current - baseline_ma)`, which has no polarity to resolve.

## Setup tools

Setup is the session state for building and testing the rig. This section says how the session enters and leaves it, what each tool does, and how the tools share the device with the detector reads.

**The Setup session state**

- **Setup.** Setup is the session state for building and testing the rig (see **Session state**). It is not an operating environment, and it never borrows outputs from live operation.
- **Entering Setup.** `POST /api/operation/setup` enters Setup for the selected environment. It is accepted while the session state is Live, or Halted for `not_activated` or `hardware_failure`. It is refused with `409` when the session is already in Setup.
- **What entering Setup does.** In order:
  1. from Live, it sets every train's knob to 0 V and ends every claim (see **Trains**, Leaving Live stops every train);
  2. it drives the outputs to their safe state through the runtime's `enter_output_safe_state()`;
  3. it deactivates the environment (`environment_active = False`), so `BlockPowerController` and `SwitchPowerController` give up the outputs, and it makes the selected environment the `active_environment` (the environment the session was last built for);
  4. it sets `AppState.setup_active`, and the session reads Setup.
- **What stays open on entering Setup.** When the session's interfaces were built for the selected environment, its device handle and its `OccupancyInput` stay open (see **Setup tools own the writes; the detectors stay shared**). Otherwise the session's interfaces are released, and no `OccupancyInput` is open until an activation succeeds.
- **Leaving Setup.** Activate is the only way out of Setup (see **Activation starts the railway over**, An open Setup tool is closed first).
  - A refused activation leaves the session, and any open tool, in Setup.
  - An activation that fails to build after closing the tool leaves the session in Setup with no tool open.
- **What Setup may work on.** Setup may inspect an incomplete setup. It cannot run trains, and it does not hand the outputs to the runtime for ordinary operation.
- **The tools.** Inside Setup the operator uses these tools: the commissioning session, self-test, port-test, track direction sweep, switch throw test, block occupancy test, and the occupancy bench. **Test the rig** runs several of them in one go.

**Self-test and proving chips**

- **Self-test.** Self-test (`POST /api/hardware/scan`) opens the selected device and scans the I²C bus. An I²C chip is proven present when its address answers. The ports of every configured chip form the pool the binding maps draw from.
- **A shift register cannot be auto-detected.** A shift register (`hc595` or `hc165`) has no acknowledgement and no id register, so it reads the same wired as unwired. It is proven another way (see **A shift register is proven by a read-back or by the operator**).
- **A shift register is proven by a read-back or by the operator.** Running a test is not proof on its own: a shift-out test that drives a dead chain and a shift-in read of an unwired chain both complete without an error.
- **An `hc165` is proven by a read-back.** The panel marks it proven when **both hold**:
  - the occupancy bench's block check has run on every block bound to it;
  - for each of those blocks, the block's bit followed the drive with no other block's detectors changing (see **Block check**).
- **A bit follows the drive in either of two ways.**
  - It read empty at rest and occupied during the pulse.
  - It read occupied at rest, and the block's current channel read a load during the pulse.
- **An `hc595` is proven only when the operator confirms it.** The operator runs the port-test or the commissioning session against the chip, watches the board, and confirms it worked. Nothing on the rig reads a coil or a shift-out bit back.
- **A failed test takes the proof back.** When a later test of that chip fails, the panel takes the proof back.
- **Proof is a panel record.** It is a record for the operator, not an activation blocker. Activation cannot gather it: a read-back drives the rails, and a confirmation needs the operator watching.

**The commissioning session**

- **The commissioning session.** It is the raw-port tool with a time limit, accepted only in Setup. `POST /api/hardware/commission/open` opens it and builds the chips it drives from the posted setup. Its `output`, `pulse`, `inputs`, and `currents` actions drive and read raw chip ports, and `close` ends it.
- **Its idle timeout.** The idle timeout is the request's `timeout_seconds`, or a default of 30 seconds (`IDLE_TIMEOUT_S` in `hardware/commission.py`) when the request names none. When no action reaches the session for that long, it closes.
- **Switch bits.** `open` reads the `switch_output` bindings of the chips it builds, from both the posted setup and the saved one. Every port such a binding names (`port_main`, `port_diverge`, and `port_standby`) is a switch bit. It reads the saved setup too because the Hardware Setup panel posts its chips without their bindings.
- **A switch bit is never held on.** `output` refuses to hold a switch bit on, because a held coil burns out.
- **`open` lists the switch bits.** `open` lists the switch bits in its response, so a caller walking the bits pulses a switch bit only for the switch-coil width and may pulse every other bit for longer, up to `MAX_COIL_PULSE_S`.
- **Every failure reaches the operator.** A failed action returns `ok == false` with its error. A current channel that cannot be read fails the whole `currents` action; it is never reported as `0` mA.

**Occupancy bench**

- **Occupancy bench.** It is the Setup tool for setting detector calibration, accepted only in Setup. It holds every block that has a rail binding at one track voltage. It reports both occupancy sources for every bound block side by side, with each current channel's peak reading, so the operator can choose each channel's `baseline_ma`, `threshold_ma`, and `driven_threshold_ma` (see **Occupancy source follows drive state**).
- **Opening and closing the bench.** `POST /api/hardware/bench/open` opens it and `POST /api/hardware/bench/close` closes it. Its idle timeout, 120 seconds (`IDLE_TIMEOUT_S` in `hardware/occupancy_bench.py`), also closes it.
- **Block check.** `POST /api/hardware/bench/check-block` with `{ "block_id": str }` checks one block's wiring while the bench is open. It is refused with `409` when the bench is not open, and with `400` when the block has no rail binding. When the bench is driving, the check first stops the drive. In order, it:
  1. holds every block at `0` V and reads both occupancy sources of every block 3 times (`CHECK_REST_READS`) — the rest reading;
  2. drives the checked block at `current_sense_min_drive_v` for a pulse of 0.3 seconds (`CHECK_PULSE_S`), and reads both sources of every block repeatedly during the pulse;
  3. drives the checked block back to `0` V.
- **What the block check reports.** For the checked block:
  - its `hc165` bit — one of: followed the drive (empty at rest, occupied during the pulse), present at rest, or no response;
  - its current channel — read a load during the pulse, or read none;
  - every other block whose bit or current channel was quiet at rest and changed during the pulse.
- **Why a pulse proves the wiring.** At `current_sense_min_drive_v` the drive swamps the digital detector (see **Occupancy source follows drive state**), so a bit wired to this block's rails reads occupied whether or not anything stands on the block. A bit or current channel of another block that changes during the pulse is wired to the wrong rails.
- **An empty block leaves its current channel unchecked.** A current channel reads only a load, so a block with nothing on it leaves its current channel unchecked, not failed.

**Test the rig**

- **Test the rig.** It is the Hardware Setup panel's one-button run, and it uses only the tools in this section. It is accepted only in Setup, and only when the setup has no unsaved edit, because it tests the saved setup. In order, it:
  1. runs self-test;
  2. opens the occupancy bench, runs the block check on every block with a rail binding, and closes the bench;
  3. runs the switch throw test on every switch that has a binding and that the loaded layout has: to the position the switch is not in, then back, so each switch ends where it started.
- **Test the rig asks the operator nothing.** It ends with one summary that lists what each check proved, what failed and why, and what still needs the operator. An `hc595` stays unproven until the operator confirms it (see **A shift register is proven by a read-back or by the operator**).
- **A failed step does not stop the run.** One bad wire must not hide the state of the others. Every tool still completes its own safe-state sequence.

**The single-wire tools**

- **Port-test.** A port-test drives one output port or reads one input port, so a single wire can be confirmed before binding. Its output pulse on a shift-out bit lasts `SWITCH_THROW_PULSE_S` unless the request gives a width, and never longer than `MAX_COIL_PULSE_S` (see **No shift-out bit is driven for longer than 200 ms**).
  - An output pulse is accepted only in Setup, because it drives an output that an active environment's power controllers would otherwise own (see **Setup tools own the writes; the detectors stay shared**).
  - An input read drives nothing and is accepted in any state.
- **Track direction sweep.** It runs only in Setup. It drives the block's two bound channels one way, off, then the other way, with a hard duration bound and safe-state cleanup. It resolves the block's `reversed` flag as the driver does (see **The sweep resolves the flag the same way the driver does**).
- **Switch throw test.** It pulses one coil of a bound switch through the same write order and pulse width an operating throw uses, then records that position on the switch through `SwitchManager.record_bench_alignment(switch_id, position)`.
- **Recording a bench throw drives no coil.** `record_bench_alignment(...)` sets `Switch.position`. It drives no coil, because the test already pulsed it. It writes model state, and only that: it drives no train and does not hand the outputs to the runtime.
- **Why the bench throw has its own recording entry.** The operating throw `SwitchManager.request_position_change(...)` refuses while no operating environment is active, and no environment is active in Setup, the state every bench throw runs in. Recording the already-driven position cannot go through that entry, and going through it would drive a second time a coil the Setup tool has taken.
- **The bench throw is recorded without the held check.** `record_bench_alignment(...)` records the position without the check an operator's throw makes while Live. The operator explicitly asked for the test throw.
- **Why the position is recorded.** The points have moved, and the app must not go on describing the layout the way it was before the operator watched them move.
- **Block occupancy test.** It drives nothing. When both required bindings exist, it reports both sources, the drive zone, and the resulting verdict. Otherwise it reports the missing source as a readiness blocker; it does not make up a result.

**Virtual Hardware**

- **Virtual Hardware runs the identical workflow.** Virtual Hardware exercises every tool in this section against its fake device.
- **The fake device's `hc165` bit follows the drive.** The fake device's `hc165` bit for a block reads occupied while either of the block's rail channels is driven, as the swamped detector does on the rig, so the block check reads the same way in both environments.
- **A bench that opens its own fake device.** When the occupancy bench opens its own fake device, because no session holds one, every bound block starts empty.

**Sharing the device**

- **Setup tools own the writes; the detectors stay shared.** A session opens one device handle, and every owner reaches the rig through it. What a Setup tool takes is the exclusive right to write: while it holds the outputs, no other ordinary owner may drive a track channel or a coil. It does not take the reads.
- **A tool owns the outputs only while it drives them.** A tool owns the outputs only while it drives them, and the detectors go on being read throughout. The one exception to the reading is while **either holds**:
  - a hardware failure is latched;
  - no `OccupancyInput` is open.
- **The detector read keeps running throughout Setup.** `OccupancyInput.read_block_currents()` is served for the whole time the session is in Setup, so the detector read and its debounce (the consecutive-read counts in `BlockOccupancyDetector`) keep running, and the page keeps showing which blocks read occupied (see **Occupancy detection**, The read does not stop in Setup). An operator opens a bench to find out why a block reads wrong. The layout beside it must go on saying what the app currently believes, because that comparison is the diagnosis.
- **The runtime moves no train in Setup.** In Setup the runtime sets no block's voltage for a train and throws no switch for a train. A Setup tool with a time limit may physically move a locomotive — the track direction sweep does so deliberately — but that movement is not a train command.
- **Ordinary reads and writes run on the runtime thread, with one exception.** The exception is the Real Hardware watchdog's safety-only sequence that sets every output to zero. It shares the same transport lock, so device transactions never collide.
- **A Setup tool's writes select the same occupancy zone as operation.** A Setup tool writes the same last-commanded voltage that **Occupancy source follows drive state** reads. A rail a bench is driving is therefore read in the driven zone and its current channel decides, as it would under operation.

**Closing a tool**

- **Closing a Setup tool restores the safe state.** Closing, timing out, disconnecting, or failing a Setup tool completes the tool's own safe-state sequence, which zeros all outputs before another tool or an activation may take them. The session stays in Setup.
- **The tool performs its safe-state sequence itself.** It writes `0` to every output channel of the chips it drives — every track channel, coil, and raw port it touched, whether bound to a block or not — through the session's device handle, then gives up the outputs.
- **The tool's sequence is not `OutputSafety.enter_safe_state()`.** `OutputSafety.enter_safe_state()` (the hardware layer's safe-state call) reaches only the bound outputs of `BlockPowerController` and `SwitchPowerController`. The tool's sequence is its own, because the runtime's power controllers do not own the outputs while a tool holds them.
- **A failed closing write is a hardware I/O failure.** A failure of one of the tool's zeroing writes is a hardware I/O failure (see **Hardware I/O failure is fatal to the session**).
- **A timeout closes the tool on time.** Every iteration of the runtime loop, in every session state, closes any Setup tool whose idle timeout has passed, through that tool's own safe-state sequence. A tool that no request reaches therefore still closes on time, so an operator who walks away from an open bench does not leave the blocks driven.

## Hardware REST API

**Hardware REST payload schemas**

- **Unknown values are rejected.** Unknown fields and unknown discriminator values are rejected.
- **Validity rules.** These rules all hold:
  - every referenced layout entity and `chip_id` must exist;
  - ports must be integers inside the selected chip's advertised range;
  - numeric values must be finite;
  - one physical port may appear in only one binding of the same output/input role.
- **`HardwareSetupRequest`.** It carries exactly `controller`, `chips`, `detector_calibration`, `block_track`, `block_occupancy`, `block_occupancy_current`, `switch_output`, `switch_track_source`, and the two drive-zone levels `idle_deadband_v` and `current_sense_min_drive_v`. The two levels are validated `0 <= idle_deadband_v < current_sense_min_drive_v` at request time. The fields are:
  - `controller` is `{ "kind": "ch347", "device_port": Optional[str] }`;
  - each I²C chip is `{ "chip_id": str, "kind": "pca9685" | "ina3221" | "ina219", "bus": "i2c", "i2c_address": int }`;
  - each shift-output chip is `{ "chip_id": str, "kind": "hc595", "bus": "spi" | "gpio", "spi_select": int, "chain_length": int }`, with `chain_length >= 1`;
  - each shift-input chip is `{ "chip_id": str, "kind": "hc165", "bus": "spi" | "gpio", "spi_select": int, "chain_length": int, "active_low": bool }`, with `chain_length >= 1`. `active_low` is stored only on this chip variant and applies to every digital detector bit in its chain;
  - `detector_calibration` maps detector calibration id to `{ "baseline_ma": float, "threshold_ma": float, "driven_threshold_ma": Optional[float] }`, with every set value nonnegative and `threshold_ma > baseline_ma`. An unset `driven_threshold_ma` means the driven zone uses `threshold_ma` (see **Occupancy source follows drive state**);
  - `block_track` maps `block_id` to `{ "chip_id": str, "forward_port": int, "reverse_port": int, "reversed": bool }`. Both ports address PWM channels on that chip and must differ;
  - `block_occupancy` maps `block_id` to `{ "chip_id": str, "port": int }`. The referenced `hc165` chip supplies polarity;
  - `block_occupancy_current` maps `block_id` to `{ "chip_id": str, "port": int, "calibration_id": str }`;
  - `switch_output` maps `switch_id` to `{ "chip_id": str, "port_main": int, "port_diverge": int, "port_standby": Optional[int] }`. `port_main` and `port_diverge` must differ, and a populated `port_standby` must differ from both. No binding carries a pulse duration: the throw width is one driver-wide value (see **A switch throw is a pulse on one of two coils**);
  - `switch_track_source` maps `switch_id` to a `block_id` string. Its value is a logical id, not a chip port, so it carries no `reversed`. A switch has no rails of its own, and its rail polarity is whatever the feed block's `block_track` binding sets.
- **Commissioning-session bodies address raw chips and ports, not entities.** A bench brings up an incomplete setup whose entity bindings may not exist yet. The commissioning session is a server-side singleton, so no action body carries a `session_id`. The bodies are:
  - `POST /api/operation/setup` carries an empty body (see **Setup tools**, Setup);
  - `POST /api/hardware/commission/open` carries `{ "config": HardwareSetupRequest, "timeout_seconds": Optional[float] }`. A given `timeout_seconds` must satisfy `0 < timeout_seconds <= COMMISSION_MAX_SESSION_SECONDS` and is the session's idle timeout. Without it the session uses its default idle timeout;
  - `output` carries `{ "chip_id": str, "port": int, "duty": float }`, where `duty` in `0.0..1.0` is the PWM level (or on/off);
  - `pulse` carries `{ "chip_id": str, "port": int, "standby_port": Optional[int], "off_port": Optional[int], "pulse_s": Optional[float] }`. It drives one shift-out bit for the switch-coil pulse width, raises `standby_port` for the pulse when the motor driver needs it, and holds `off_port` low. A supplied `pulse_s`, in seconds, must be no longer than `MAX_COIL_PULSE_S` (see **No shift-out bit is driven for longer than 200 ms**); a longer one is refused. An omitted one uses the driver-wide fixed throw width;
  - `inputs` and `currents` each carry `{ "chip_id": str }`. `inputs` reads every bit of an `hc165`. `currents` reads every channel of the named current monitor (an `ina3221` or `ina219`);
  - `close` carries an empty body.
- **Commissioning limits live in `constants.py`.** `COMMISSION_MAX_SESSION_SECONDS` is `3600.0`.
- **Commissioning responses.** Every `/hardware/commission/*` response is `{ "ok": bool }`, plus `"error": str` on failure. Three actions add a field:
  - `open` adds `"switch_ports": { chip_id: [int] }` (see **The commissioning session**, Switch bits);
  - `inputs` adds `"ports": [{ "port": int, "value": bool }]`;
  - `currents` adds `"currents": [{ "port": int, "current_ma": float }]`.
- **The raw virtual-hardware input bodies address a block, not a chip port.**
  - `PUT /api/virtual-hardware/input/occupancy` takes `{ "block_id": str, "occupied": bool }`. The endpoint resolves the block's `block_occupancy` binding to the chip and latch line.
  - `PUT /api/virtual-hardware/input/current` takes `{ "block_id": str, "current_ma": float }`, with `current_ma` nonnegative. The endpoint resolves it through the block's `block_occupancy_current` binding.
- **`VirtualHardwareFaultRequest`.** It is `{ "action": "inject" | "clear", "target_kind": "controller" | "chip" | "port" | "transaction" | "readback", "target_id": str, "operation": Optional[str], "fault": Optional[VirtualHardwareFaultKind] }`. `VirtualHardwareFaultKind` is the set defined under **Fault injection belongs to Virtual Hardware**: `transaction_refusal`, `timeout`, `device_disconnect`, `invalid_reading`, `input_hold_or_flicker`, or `output_disagrees_with_command`. `fault` is required for `inject` and must be `None` for `clear`. A target and fault that do not fit each other are rejected, never translated to another fault name.
- **`VirtualHardwareScenarioRequest`.** The scenario is one virtual train: an object the in-memory device pretends is on the track. It is known only to the scenario and is not a train in the sense of **Trains**; the runtime learns of it only through the detectors. The request is one of these five:
  - `{ "action": "start", "blocks": [block_id, ...] }` puts the virtual train on the first block of the list, which is the course it will follow;
  - `{ "action": "advance" }` moves it to the next block of the list;
  - `{ "action": "stall", "stalled": bool }` sets or clears a stall, during which `advance` does not move it;
  - `{ "action": "remove" }` lifts it off the track;
  - `{ "action": "status" }` changes nothing and returns the scenario.

**REST surface**

| Endpoint | Body | Behavior |
| --- | --- | --- |
| `GET /api/hardware/config` | — | Returns the saved Hardware Setup: `controller`, `chips` (including stored bus plus derived role/direction/port metadata), detector calibration, binding maps, and the two drive-zone levels `idle_deadband_v` and `current_sense_min_drive_v`. It also returns a session summary saying which environment the session is built for, its session state (see **Session state**), and whether the saved setup holds changes the running session was not built with. The layout-aware readiness report is served by `GET /api/operation/environment` and `GET /api/operation/readiness`, not here. The response contains no selected-environment flag beyond that session summary. |
| `PUT /api/hardware/config` | `HardwareSetupRequest` | Validates the fields and bindings that exist and writes the whole setup, returning the stored config. Incompleteness is allowed while editing or in Setup, and the readiness report (`GET /api/operation/environment` / `GET /api/operation/readiness`) surfaces it, but it blocks activation (see **Completeness is activation policy, not edit policy**). Malformed fields, incompatible chips, out-of-range or duplicate ports, and dangling references are rejected. The running session keeps the chips and maps it was built with until explicit activation; the values named in **A binding is read per write** reach the rig on the next write. |
| `GET /api/operation/environment` | — | Returns `requested_environment` (the selected environment, see **Operating environment and activation**) and `active_environment`, each one of `"virtual_hardware" \| "real_hardware"`, the session state as `{value, halt_reason}` from `AppState.session_state()` (see **Session state**), readiness blockers, device health, and whether the saved setup holds changes the running session was not built with (the captured parts only, see **A binding is read per write**). This is the single session truth the header and diagnostics read. |
| `GET /api/operation/readiness` | — | Returns the same layout-aware readiness report as `GET /api/operation/environment`: readiness blockers and device health, evaluated live against the loaded layout and Hardware Setup (see **Completeness is activation policy, not edit policy**). It activates nothing. |
| `PUT /api/operation/environment` | `{ "environment": "virtual_hardware" \| "real_hardware" }` | Selects the environment but does not silently rewire a running session. It returns the readiness blockers that would prevent activation. |
| `POST /api/operation/activate` | — | Refused with `409` while the session is Live. Otherwise it validates the selected environment, closes any Setup tool that still holds the outputs, builds all interfaces atomically, reconnects, verifies the active environment, starts the railway over, and goes Live (see **Activation starts the railway over**). A successful activation clears `hardware_failure` (see **Recovery is reactivation**). A refused or failed activation returns the readiness blockers and leaves no environment active. |
| `GET /api/hardware/catalog` | — | Returns the selectable controllers and chip kinds that the setup UI reads. For each kind it gives the buses the kind allows, role, direction, port count, and required parameters. |
| `POST /api/hardware/scan` | `{ "config": {…} }` | Opens the controller and reports which I²C addresses answer: per-chip reachability for the I²C chips (the Hardware Setup Detect / Rescan button). It does not probe shift-register (`hc595` / `hc165`) transactions; those chips and individual output ports are exercised through the port-test, track-test, switch-test, and occupancy-test endpoints and commissioning. When a running session has a device that owns the outputs, the scan borrows that device under `simulation_lock` (the lock the runtime loop and every web request hold, see **One thread at a time**). Otherwise it opens a transient device of its own. |
| `POST /api/hardware/port-test` | `{ "config": {…}, "chip_id": str, "port": int, "action": "pulse_output" \| "read_input", "pulse_s": Optional[float] }` | Drives one output port or reads one input port on a named chip, so a single wire is confirmed before it is bound to any entity (see **Setup tools**, Port-test). `pulse_output` is accepted only in Setup and refused with `409` otherwise; `read_input` is accepted in any state. |
| `POST /api/hardware/occupancy-test` | `{ "config": {…}, "block_id": str }` | Reads both of a block's occupancy detectors (the `hc165` bit and the current-sense channel), reads the block's drive state back off its PWM chip, applies the **Occupancy source follows drive state** rule for which source decides, and reports the verdict. Drives nothing. |
| `POST /api/hardware/track-test` | `{ "config": {…}, "block_id": str }` | Sweeps a block's two bound PWM channels forward / off / reverse, then zeros every channel of that chip (the block editor's track Test). It mirrors each leg onto the voltage `BlockPowerController` records for the block, so the layout shows what the rails carry. It is a Setup tool of its own: it drives the channels itself through the session's device handle, not through the commissioning session (see **Setup tools own the writes; the detectors stay shared** and **Closing a Setup tool restores the safe state**). It runs only in Setup, where the runtime writes no block voltage during the sweep. |
| `POST /api/hardware/switch-test` | `{ "config": {…}, "switch_id": str, "position": int }` | Throws one switch to the named position (`0` = MAIN, `1` = DIVERGE): the switch editor's Test throw. When the session owns the switch coils, it is an ordinary operator throw through `SwitchManager.request_position_change(...)`. Otherwise the coil is pulsed through the commissioning session using the switch's binding, and the position is recorded through `SwitchManager.record_bench_alignment(...)` after the pulse lands (see **Setup tools**, Switch throw test); that path is accepted only in Setup and refused with `409` otherwise. |
| `POST /api/operation/setup` | — | Enters Setup (see **Setup tools**, Setup), through the steps in the Enter Setup row of **Session state**. Refused with `409` while already in Setup. It changes no setup. |
| `POST /api/hardware/commission/{open,output,pulse,inputs,currents,close}` | The commissioning body for that action under **Hardware REST payload schemas** | The commissioning session (see **Setup tools**, The commissioning session), accepted only in Setup. `open` builds the session's chips from the posted config, giving it exclusive **output** ownership. The `output` / `pulse` / `inputs` / `currents` actions drive and read raw chip ports through the same output interfaces and drivers, with the same duration limits, as operation. The occupancy read is shared and keeps running throughout (see **Setup tools own the writes; the detectors stay shared**). `close`, or a timeout, disconnect, or failure, completes the safe state before releasing ownership, and the session stays in Setup. These endpoints change no setup. |
| `POST /api/hardware/bench/{open,close,drive,threshold,peaks/reset,check-block}` and `GET /api/hardware/bench/state` | `drive` takes `{ "running": bool, "percent": number, "flipped": [block_id, ...] }`: `percent` is the held level as a percentage of `MAX_TRACK_VOLTAGE`, and `flipped` lists the blocks driven with the opposite sign and replaces the stored list whole. `threshold` takes `{ "threshold_ma": number or null }`, where `null` puts each channel back on its own calibration. `check-block` takes `{ "block_id": str }`. The others take no body. | The occupancy bench (see **Setup tools**, Occupancy bench), accepted only in Setup. `open` takes exclusive output ownership; `drive` sets the held level, `state` reads both sources per block, `threshold` and `peaks/reset` help pick each channel's calibration, and `check-block` runs the block check on one block. `close`, or its idle timeout, completes the safe state and leaves the session in Setup. It never activates an environment. |
| `GET /api/virtual-hardware` | — | Virtual Hardware only. Returns the in-memory device's connection state, raw inputs, measured/current values, output commands/readback, injected faults, and active high-level scenario. Each block reports which outputs and detectors it is bound to, the raw detector bit alongside that bit resolved through the chain's polarity, and the commanded duty on both of its rails alongside the signed voltage those duties mean once the binding's `reversed` flag is applied. |
| `PUT /api/virtual-hardware/input/occupancy` (`{ "block_id": …, "occupied": bool }`) and `PUT /api/virtual-hardware/input/current` (`{ "block_id": …, "current_ma": float }`) | see left | Change the in-memory device's input state only; the ordinary occupancy read observes it on its next read. Each call writes one raw source and leaves the block's other source saying whatever it said before, which is how a deliberate disagreement between the two is set up. `/input/occupancy` sets the block's digital detector bit at the raw level, so on an `active_low` chain the bit that means `current present` is 0 (see **Detector polarity is resolved in the hardware layer**). `/input/current` sets the block's current-sense reading in mA. |
| `PUT /api/virtual-hardware/input/presence` | `{ "block_id": …, "present": true \| false }` | Puts a virtual object on one block's detectors, or lifts it off. It writes every occupancy source the block is bound to in one call: the digital bit resolved through the chain's polarity, and a current clear of the larger of that channel's `threshold_ma` and driven threshold above its `baseline_ma`. The block then reads the same way whichever source **Occupancy source follows drive state** selects. A block bound to no occupancy source is rejected. |
| `PUT /api/virtual-hardware/fault` | `VirtualHardwareFaultRequest` | Injects or clears a failure of the in-memory controller, a chip, a port, a transaction, or a readback, below the interface implementations. |
| `GET /api/virtual-hardware/scenario` | — | Returns the scenario's status: its list of blocks, the block the virtual train is on, and whether it is stalled. |
| `POST /api/virtual-hardware/faults/clear` | — | Clears every injected fault on the in-memory device. |
| `POST /api/virtual-hardware/scenario` | `VirtualHardwareScenarioRequest` | Converts a virtual train action into raw detector and current changes on the in-memory device, one block at a time, through the same write `PUT /api/virtual-hardware/input/presence` performs. A step from one block to the next sets the entered block's detectors and clears the left block's in the same call. It never writes a train's label or any other runtime state directly. |

## Data flow to the page

The page draws the layout and its live state from one JSON object, the **snapshot**. The server builds the snapshot from the runtime and sends it to the page several times a second. This section says who fills each part of the snapshot, how the server avoids sending the layout again when it has not changed, and how the snapshot reaches the page.

### Who owns each part of a payload

- **Each owner writes its own payload.** Each part of the runtime that owns state exposes that state to the API through its own `to_dict()` method, or an equivalent read method of its own. A payload never carries live fields merged in from another owner.
- **What the snapshot carries.** The snapshot carries these keys:
  - `tracks` — one entry per tile;
  - `blocks` — one entry per block, keyed by `block_id`;
  - `switches` — one entry per switch, keyed by `switch_id`;
  - `tunnels` — the tunnels;
  - `trains` — one entry per train, keyed by `train_id` (see **Live fields**).
- **Other endpoints keep the same ownership.** Another endpoint may present the same owned data in a different wrapper. It must not merge one owner's live fields into another owner's payload.

### Live fields

The live part of the snapshot is these fields and no others.

- **A block's live fields.**
  - `reads_occupied` — `bool`, whether the block reads occupied (see **Occupancy detection**);
  - `train_id` — the train whose block this is, or `null`;
  - `claimed_by_train_id` — the train that holds a claim on this block, or `null`;
  - `voltage` — the signed voltage `BlockPowerController` last commanded on the block, in volts.
- **A switch's live fields.**
  - `position` — `0` (main) or `1` (diverge), the position last commanded;
  - `position_known` — `bool` (see **Switch**);
  - `held` — `bool`, whether the switch is held (see **Switches**, A held switch is not thrown).
- **A tile's live fields.** A tile that holds a switch carries `switch_state`, that switch's `position`, read from the switch when the snapshot is built. No other tile has a live field.
- **A train entry.** `trains` is a map from `train_id` to an entry with exactly these fields:
  - `train_id`, `name`, `length_cm`, `direction`, `knob_volts`, `destination_block_id`, `lost`, `stopped_at`, `waiting_since` — as **Trains**, What a train holds, defines them;
  - `blocks` — the train's blocks from rear to front, each as `{ "block_id": str, "reads_occupied": bool }`;
  - `state` — one of `"stopped"`, `"moving"`, `"waiting"`, `"lost"` (see **Trains**, Running, stopped, waiting);
  - `waiting_reason` — while `state` is `"waiting"`, one of the reasons under **When a train waits**; otherwise `null`;
  - `next_block_id` — the next block ahead, or `null` when there is none;
  - `claimed_block_id` — the block the train holds a claim on, or `null`.
- **Derived fields are worked out on each read.** `state`, `waiting_reason`, `next_block_id`, a block's `train_id` and `claimed_by_train_id`, and a switch's `held` are read from the runtime's trains when the snapshot is built. Nothing stores a second copy of them.

### Layout version

- **`GET /api/network` serves the snapshot in two parts.** The layout part is every field that changes only when the layout is edited, as the table lists. The live part is every other field.

| Snapshot key | Layout part |
| --- | --- |
| `tunnels` | the whole section |
| `blocks` | per block: `block_id`, `name`, `length_cm`, `ordered_tiles`, `segment_ids`, `segments` |
| `switches` | per switch: `switch_id`, `x`, `y`, `main_length_cm`, `diverge_length_cm`, `main_radius_cm`, `diverge_radius_cm`, `switch_type`, `name`, `notes` |
| `tracks` | per tile: every field, except `switch_state` on a tile that holds a switch |

- **`layout_version` names one layout.** It is a hash of the layout part, and the endpoint computes it on every read. No component stores it or bumps it. Two reads of the same layout carry the same `layout_version`. A layout loaded from a save keeps its `layout_version` across a server restart.
- **A request that names the current version gets a live-only reply.** When a request's `layout_version` query parameter equals the current `layout_version`, then the reply leaves the layout part out. That reply:
  - carries `layout_unchanged: true` and the current `layout_version`;
  - omits the `tunnels`, `blocks`, `switches` and `tracks` keys;
  - carries `live_blocks` (a map from `block_id` to that block's live fields), `live_switches` (a map from `switch_id` to that switch's live fields), and `live_tracks` (one entry of `x`, `y` and the live fields for each tile that holds a switch);
  - carries every other snapshot key unchanged, `trains` included.
- **Any other request gets the full snapshot.** When the parameter is missing or names another version, then the reply is the full snapshot plus `layout_version` and `layout_unchanged: false`.
- **The page rebuilds the full snapshot from a live-only reply.**
  - The page keeps the layout part of the last full reply and lays each `live_*` entry over the matching cached block, switch or tile.
  - When a block's, switch's or tile's live fields did not change, then the page keeps the same object for it. Its change check then skips that object without comparing fields.
  - When a live-only reply names a version other than the one the page holds, then the page asks for the full snapshot instead of using that reply.

Why this shape: the page receives the snapshot several times a second (see **Network stream**), and the layout is most of the snapshot. A hash means no edit path has to remember to bump a version. A field wrongly placed in the layout part costs a full reply or message each time it changes, and never leaves the page showing stale data.

### Network stream

- **`GET /api/network/stream` pushes the snapshot to the page.** It is a server-sent event stream: one long-lived response on which the server writes a message each time it sends, and the browser reads each message as it arrives. Data flows only from the server to the browser. While the stream is open, the page reads the snapshot from it. `GET /api/network` serves one-off reads.
- **Each message is one `GET /api/network` reply.** A message's data is the JSON reply `GET /api/network` would give at that moment, in the two-part shape defined under **Layout version**, with `layout_version` and `layout_unchanged`.
- **The layout part goes out only when it changed.**
  - The first message on a connection follows the `GET /api/network` rule: it leaves the layout part out only when the connection's `layout_version` query parameter equals the current `layout_version`. A new connection therefore gets the full snapshot first, unless the page already holds the current layout.
  - Each later message leaves the layout part out when the current `layout_version` equals the one that connection's previous message carried.
- **When the server sends.** The server sends a message on every open connection when **any of these holds**:
  - `NETWORK_STREAM_INTERVAL_SECONDS` (0.2 s) has passed since that connection's last message;
  - a request that can change state (`POST`, `PUT`, `PATCH` or `DELETE`) finished after that connection's last message was built.
- **One build serves every connection.** The server builds each message's snapshot once and sends it on every open connection. A second browser tab therefore adds no second build.
- **The build holds the lock, the wait does not.** The server builds the snapshot while holding `AppState.simulation_lock` (the one lock the runtime loop and every web request hold; see **One thread at a time**), between two runtime steps. It waits for the next send without holding the lock. An open stream therefore never holds up a runtime step or another request.
- **The page lays every message over the same cached layout.** A stream message and a `GET /api/network` reply go through the one cache and merge described under **Layout version**, The page rebuilds the full snapshot from a live-only reply. That includes asking for the full snapshot when a live-only message names a version other than the one the page holds.
- **When the stream drops, the page polls until a new one works.**
  - The stream counts as dropped when **any of these happens**: it fails to open; it closes; no message arrives on it for 5 s.
  - While the stream is dropped, the page reads `GET /api/network` every 0.2 s, passing the `layout_version` it holds, and tries a new stream every 2 s, naming that same version.
  - When a message arrives on a new stream, then the page stops polling.
- **A hidden tab may close its stream.** While the browser tab is hidden, the page may close the stream. It opens a new one when the tab shows again.

Why this shape: the snapshot changes on every runtime step and every open tab needs it. Building it once per send costs one build per 0.2 s however many tabs are open, where a poll costs one build per tab. Sending right after a state-changing request shows a command or an edit in every tab without waiting for the next interval. Polling while the stream is down keeps the page live when only the stream fails.

### Elapsed values are server-owned

- **Elapsed values are server-owned.** A payload that carries a duration carries it as a number the runtime computed. The page shows that number and never works a duration out from its own clock. A browser clock cannot see when the session stopped running, and it cannot see the server's own time (see **Clocks**).
- **The page may format a wall instant.** A wall instant is an absolute time in system-time Unix seconds, such as an event's `event_time`. The page may show a wall instant a payload carries as a local clock time. A wall instant is absolute, so showing it needs no elapsed arithmetic.

## Notifications and errors

### Runtime notifications

- **One event stream.** `NotificationCenter` owns the one runtime event stream, and the runtime exposes it as `notification_center`. Every component that has something to report (a command result, a failure, a state change worth reviewing later) reports it by calling `NotificationCenter.emit(...)`. No component keeps an event queue, a toast buffer or a log of events of its own. Every place on the page that shows "what happened" is a filter over this one stream.
- **The detector reading log is not a second event sink.** The **Detector reading log** records detector readings and operator commands, not events.
- **Event schema.** Every event carries exactly these fields:
  - `notification_id` — `int`, assigned by `NotificationCenter` in emit order as it appends. It is the order the stream reads back in.
  - `event_time` — `float`, system-time Unix seconds, sampled by the emitting function at the moment it emits.
  - `source` — `str`, the class that emitted the event, for example `"SwitchManager"` or `"DetectorReadingLog"`. For a command result it is the value **Command execution results** gives. The set of values is open.
  - `operation` — `str`, the name of the event. For most events it is the name of the function that emitted it, for example `"request_position_change"` or `"record_pass"`; for a train event it is the name its row in **Train events** gives; for a command result it is the command's kind. `source` plus `operation` is the "where did this come from" key a reader filters on.
  - `kind` — `"debug" | "info" | "warning" | "error"`, how serious the event is. It is the only level field on an event, and the only field runtime code may branch on.
    - `debug` is detail from a single runtime step that a developer reads.
    - `info` is a normal thing that happened.
    - `warning` is a recoverable problem or a safety action.
    - `error` is reserved for entry into the hardware-failure state (see **Hardware I/O failure is fatal to the session**).
  - `entities` — an ordered list of `{entity_type, entity_id}` refs naming everything the event is about: the train, and the blocks and switches involved. One event may name several entities. The list is the only entity routing key: every per-entity view is a membership filter over it. A per-train view therefore filters for a ref whose `entity_type` is `"train"` and does not read a separate scalar.
  - `error` — `Optional[ErrorRecord]`, diagnostic context (see **`ErrorRecord`**). It may accompany `info`, `warning` or `error`. A normal successful event usually leaves it `None`. `message` says what happened or was attempted, and `error.text` supplies the diagnostic detail.
  - `message` — `str`, one human-readable line saying what the operation did, present on every event at every level.
  - `payload` — the structured data the operation needs a reader to have. A command result carries `{command_id, success, reason}`.
- **Entity types.** The `entity_type` values are `"train"`, `"block"` and `"switch"`. `entity_id` is that entity's own id.
- **The emit call.** `NotificationCenter.emit(source, operation, kind, message, entities=None, error=None, payload=None, event_time=None, condition_key=None)` is the one way an event enters the stream. Its argument rules:
  - **`event_time` is optional.** When the caller omits it, `NotificationCenter` samples `time.time()` itself. `emit(...)` runs inline inside the calling function, so that sample is still taken at the moment the caller emits. A caller passes `event_time` only to match a timestamp it writes somewhere else in the same step.
  - **`condition_key` is a control argument, not a stored field.** It is not part of the **Event schema**. It is required for a suppressible error condition, and it identifies that one live occurrence until its owner calls `NotificationCenter.resolve_condition(condition_key)` (see **Error events**, Suppression mechanism).
  - **Every field except `error` and `payload` is required.** An event can therefore never reach the stream without a level, a message or an entity list.
- **A component may wrap the emit call.** A component with many emit sites may wrap `emit(...)` in a thin adapter of its own that fills in its `source` and its usual `entities`. The adapter holds no queue and no state, and it writes to this one stream.
- **Retention.** `NotificationCenter` keeps the most recent `NOTIFICATION_RETENTION_ROWS` events and drops the oldest beyond that. The default is `10000`, a **Runtime settings** tunable in `constants.py`.
- **Ids keep counting.** `notification_id` keeps counting past a drop, so a gap in the ids means events aged out, not that the stream lost order. At startup `NotificationCenter` seeds the counter from the highest `notification_id` in the persisted **Event log**, so ids stay strictly increasing across a server restart.
- **Delivery is `NotificationCenter`'s alone.** `NotificationCenter` is the only component that decides whether an event counts as read or acknowledged, and how the page gets events.
- **Command execution results.** A command's result is an ordinary event with these field values:
  - `source` names where the command surface lives. For a command that targets one block or switch it is the entity type (`"Block"`, `"Switch"`). For any other command it is the class that owns the command.
  - `operation` is the command's kind, for example `"change_state"` for a command that sets a switch's position.
  - `kind` is `"info"` on acceptance and `"warning"` on rejection.
  - `payload` is `{command_id, success, reason}`. A command result that targets one entity also carries that entity's id field, for example `switch_id` for a command that sets a switch's position. No other extension is permitted. The command name is not added to the payload, because it is already the event's `operation`.
- **The event is a record only.** The component that caused an event still owns the underlying state change. `NotificationCenter` owns only the event record and its delivery.
- **A request handler returns the command's own result.** A request handler must not read another component's internals to find out about a later failure of a command. The page and other API consumers see later results on this stream.
- **Per-entity reads are filters.** A per-entity read is a filter over this one stream, not a separate queue. `GET /api/trains/<train_id>/notifications` returns the events whose `entities` include that train, most recent first, and takes optional `kind` and `since_notification_id` filters. It is a read: it acknowledges nothing and empties nothing.
- **The event log is this stream.** The **Event log** is this same stream kept on disk, read in a viewer, and scrubbed in replay. It is not a second sink.

### `ErrorRecord`

- **`ErrorRecord` is the single diagnostic-error object.** It exists so a person can read what went wrong in a log, a notification or the page. It carries exactly:
  - `text` — `str`, the human-readable failure detail;
  - `origin` — `str`, a free-form label naming what produced the error. It is the field a reader groups errors by when `operation` alone is too coarse.
- **No code branches on an `ErrorRecord`.** No runtime code may switch on `text` or `origin`. `origin` is diagnostic only, and a writer may use any label.
- **Written whole.** Both fields are present together, or the whole record is `None`.
- **The event owns the time and the level.** An `ErrorRecord` rides in the `error` field of one event. The event's `event_time` is when the error happened, and the event's `kind` is how bad it is. An `ErrorRecord` therefore carries no timestamp and no severity of its own.
- **A result's `reason` is not an error record.** A plain `reason: Optional[str]` field on a result object (what an operation did) stays a free-form string. `ErrorRecord` is for the diagnostic a writer hands forward for a person to read later.

### Error events

- **Errors live in the event stream.** No block, switch or train stores an error. Every error and failure is emitted once into the `NotificationCenter` stream, and every "what went wrong here" read on the page, the event-log viewer included, is a filter over that stream. No `Block` or `Switch` field holds an `ErrorRecord`.
- **Hardware I/O failure keeps only a flag.** Hardware I/O failure retains only the `hardware_failure` flag (see **Hardware I/O failure is fatal to the session**). Its diagnostics are events.
- **`kind` and `error` are independent.** `kind` is the severity the writer declares for the operator. `error` is present whenever the writer has a diagnostic record, whatever the `kind`. Nothing derives severity from `ErrorRecord.origin`. A `"debug"` event carries no diagnostic.
- **Emit on change, not on every runtime step.** A condition that stays true over many runtime steps is emitted when it first appears, and not again while the same entities stay in that condition for the same reason. A changed reason, including a changed `payload`, is a new event.
- **Suppression mechanism.** `NotificationCenter` owns an in-memory map of active error conditions, keyed by the `condition_key` the caller supplies. `NotificationCenter.emit(...)` applies suppression to a call only when **both hold**:
  - the event carries an `ErrorRecord`;
  - the call passes a `condition_key` that is not `None`.

  When that key is already active with the same `entities`, `operation`, `ErrorRecord.origin` and `payload`, then `emit(...)` drops the call. When any of those four values differs, then `emit(...)` appends the event, and the new values replace the active ones.
- **Resolution and recurrence.** The component that owns the live condition must call `NotificationCenter.resolve_condition(condition_key)` the first time the condition turns false. Resolution removes the active key and may be called more than once with the same result. It does not delete or rewrite the event already in the stream. When the same condition later turns true again, its key is not active, so the otherwise-identical event is emitted again.
- **Active keys do not outlive the process.** Active keys are held in memory only and start empty after a restart. The first still-present condition seen in a new process therefore emits once for that process.
- **No per-caller event memory.** The condition's owner detects its own condition turning false and calls the resolver. It keeps no history of the events it emitted. `NotificationCenter` alone owns the active keys and the values stored with them.
- **Reading the current reason.** `NotificationCenter.latest_error_for(entity_type, entity_id, operation=None, origin=None)` combines the retained events with the map of active conditions. It returns the most recent retained `warning` or `error` event whose condition key is still active and whose entities and optional filters match, or `None`. It never returns a resolved condition. A viewer that needs past reasons reads the event stream directly.
- **Nothing branches on an error event.** No runtime code changes what it does because an error event was emitted (see **`ErrorRecord`**, No code branches on an `ErrorRecord`). Filtering a view by `operation` or `origin` is a display choice, not a runtime branch.

## Event log

The event log is the operator-facing record of what happened during a run. It is the stream `NotificationCenter` owns (see **Runtime notifications**), kept on disk and presented for review, so an operator can scroll back through a run instead of catching events only as they pass.

### What it records

- **Every event, at every level.** The log carries every event `NotificationCenter` emits:
  - command results (see **Runtime notifications**, Command execution results);
  - the error events defined under **Error events**;
  - the operational events that mark what happened on the layout, the switch-throw events among them.

  Each one is a single event with the fields defined by the **Event schema**, and each names the function that produced it in `operation`.
- **Three switch-throw events.** `SwitchManager` (the class that owns every switch's recorded position and its throws) emits each of these:
  - `"request_position_change"` — a throw the operator asked for, or one the runtime made for a train with a destination;
  - `"record_bench_alignment"` — a throw made from the Setup bench (`SwitchManager.record_bench_alignment(...)`);
  - `"throw_all_to_layout"` — the throw of every switch at hardware activation (`SwitchManager.throw_all_to_layout()`).
- **`Switch` methods emit nothing.** `Switch.apply_position_change(...)` emits no event of its own.
- **Train events.** The runtime emits these from the runtime step, each with `source = "SimulationController"` and `kind = "info"` unless the row says otherwise. Each one's `entities` name the train and every block and switch the event is about:

| `operation` | Emitted when | `kind` |
| --- | --- | --- |
| `"train_appeared"` | a new train is made for a newly occupied block | `info` |
| `"train_block_added"` | a block becomes a train's front block | `info` |
| `"train_block_removed"` | a block is taken off a train's blocks, at once or after the hold time | `info` |
| `"claim_given"` | a train is given a claim; the payload names the block and any switch thrown for it | `info` |
| `"train_waiting"` | a train begins to wait, or its waiting reason changes; the payload carries the reason | `info` |
| `"train_arrived"` | the runtime stops a train at its destination | `info` |
| `"end_of_track"` | the runtime stops a train where the track ends | `warning` |
| `"train_overran"` | a train rolls into a block it had not claimed | `warning` |
| `"train_lost"` | a train becomes a lost train | `warning` |
| `"train_found"` | a lost train is found again on its block or a neighbouring one | `info` |
| `"destination_unreachable"` | the runtime clears a destination no way forward reaches | `warning` |
| `"train_removed"` | the runtime removes a train whose blocks all read empty | `info` |

- **Operator requests about trains report a command result.** Each request under **Running a train** and **Trains** emits one command-result event, as **Runtime notifications**, Command execution results, describes, with the request's `{ success, reason }`. The `operation` values are `"set_knob"`, `"stop"`, `"reverse"`, `"set_destination"`, `"rename"`, `"trains_merged"`, `"train_split"`, `"remove_train"` and `"stop_all"`, each with `source = "SimulationController"`.
- **How the page shows events.** The Operate screen and the Logs screen read events through `GET /api/event-log` (see **Viewer**). The snapshot carries no events.
- **A switch the runtime throws for a destination** is reported as **Switch**, A throw for a destination is reported once per switch, says.
- **The stored `operation` is the unqualified function name.** An event from `SwitchManager.throw_all_to_layout()` carries `operation = "throw_all_to_layout"` (see **Event schema**).
- **Switch-throw events carry `kind = "info"` and no `ErrorRecord`.** A train event carries the `kind` its row in **Train events** gives.
- **Components do not write the log.** A component must not write the log directly. It emits each event through `NotificationCenter` at the point it already reports from, the same way as any other notification. The log therefore never becomes a second sink (see **Runtime notifications**, The event log is this stream).

### Persistence

- **The log is kept on disk.** The event log persists to disk, so an operator can review an earlier run's incidents after the session ends.
- **Same window as the in-memory stream.** The persisted log holds the same `NOTIFICATION_RETENTION_ROWS` window the in-memory stream holds (see **Runtime notifications**, Retention). A row an operator cannot see in the viewer is therefore a row no query can reach.
- **Its own file.** The log is data the runtime writes, kept in its own app-managed file and not in a layout save. It is a record, separate from live runtime state (which blocks are occupied or claimed, and the trains), which is kept in memory only.

### Viewer

- **A read-only panel.** The event-log viewer is a read-only panel on the page. It reads the persisted stream and lets the operator filter it by:
  - entity
  - `kind`
  - `source`
  - `operation`
  - `ErrorRecord.origin`
  - time
- **Selecting an entry highlights it.** When the operator selects an entry, then the viewer highlights the entities that entry names on the track drawing.
- **The viewer changes nothing.** It presents the record only and never changes runtime state.

### Replay

- **Replay shows the event timeline; it does not rebuild state.** Replay steps or scrubs through the retained events, shows the selected event's message and payload, and highlights the entities that event names on the matching saved layout.
- **Recorded values only.** When an operation's payload holds geometry or before-and-after values, then the viewer may draw those recorded values. It never infers a missing train position, a block's occupancy or a switch's alignment, and it never interpolates motion between events.
- **No world state at an arbitrary time.** The viewer makes no promise to rebuild the state of the layout at an arbitrary time. Retention may remove the beginning of a run, and no replay checkpoint is stored.
- **The viewer does not re-run the runtime.** Replay never runs the runtime forward or backward.

## Detector reading log

The detector reading log is a file of what the block detectors told the runtime. It holds one line per pass (one iteration of the runtime loop that reads every block's detector), a line for each operator command between passes, and, on its first pass line, what the server booted with. The event log records what the runtime decided; this log records what it decided from. A run that went wrong can therefore be read back as the exact readings the runtime acted on and when it read them, and re-run in a test (see **A recorded run can be replayed in a test**). The log records inputs, not events: no `NotificationCenter` event is written to it, and nothing in it is an event.

- **Who writes it.** The runtime owns one `DetectorReadingLog` (the class that writes this file), held as `detector_reading_log`, for the life of the process. The log is off until `python server.py` enables it at boot with a folder (in `server.py`'s `main()`). A unit test, or a WSGI server that imports `server.py`, therefore writes nothing. Nothing reads the log at runtime.
- **When a pass line is written.** Every pass gets one line: each runtime step run while the session is Live, and each pass run in another session state that still reads the detectors. The runtime calls `DetectorReadingLog.record_pass(...)` right after `BlockOccupancyDetector.step()` returns (`BlockOccupancyDetector` is the class that reads every block's detector once per pass and debounces the result) and before the runtime updates any train's label. `record_pass(...)` writes the line.
- **A failed read gets no line.** A pass whose read fails gets no line, because that failure is a hardware I/O failure (see **Hardware I/O failure is fatal to the session**). An iteration of the runtime loop that runs no detector read gets no line.
- **Setup passes are logged too.** While the session is in Setup, the passes that still read the detectors (see **Setup tools**, The detector read keeps running throughout Setup) each get a line like any other. On a Setup line that carries `full`, `environment` is `null`. The readings on a Setup line can come from blocks the Setup tools are driving.
- **What every pass line carries.**
  - `pass` — counts the passes the log recorded in this process, from 1. A gap in `pass` means lines were lost to a failed write.
  - `pass_kind` — `"tick"` for a runtime step run while the session is Live, `"stopped"` for a pass run in any other session state.
  - `wall_time` — the wall time (`time.time()`) at which the pass started.
  - `operating_delta_seconds` — the step this pass advanced `AppState.operating_time` by (`operating_time` is the clock that advances only while the session is Live; see **Clocks**). It is `0` on a `"stopped"` pass. The log never records an `operating_time` reading.
  - `readings` — a map from block id to that block's reading, holding the blocks **Only changed readings are written** selects.
- **A block's reading.** `present` is the boolean `OccupancyInput.read_block_currents()` returned for the block (`OccupancyInput` is the hardware layer's read side for the detectors). It is the value the debounce counts (see **Debounce with on/off thresholds**). The reading also carries what the driver reports about that read:
  - `source` — `"current"` or `"digital"`, the detector that answered (see **Occupancy source follows drive state**);
  - `zone` — the block's drive zone;
  - `current_ma` and `threshold_ma` — when the current channel answered, the measured current and the threshold it was judged against, each rounded to 0.1 mA;
  - `raw_bit` — when the digital detector answered, its bit before polarity is applied (see **Detector polarity is resolved in the hardware layer**);
  - `held_from_previous: true` — when the lighting hold kept the block present (the lighting hold is the hardware layer's rule that keeps reporting a block present in the `LIGHTING` zone until its current channel has read clear for a set number of reads).
- **Only changed readings are written.** A line lists only the blocks whose reading differs from the reading the file last holds for that block. When no block's reading differs, the line leaves `readings` out.
- **Some lines list every block.** A line lists every block the read returned, and adds four fields, when **any of these holds**:
  - it is the first pass line the process writes successfully — `full = "start"`. Command lines answered before it carry `after_pass = 0` and come before it in the file;
  - it is the first line of a new file — `full = "new_file"`;
  - the detector or the operating environment changed since the last line — `full = "environment"`. An activation builds a new detector, and entering or leaving Setup changes the environment;
  - a layout was loaded, or the set of blocks the read returned changed — `full = "layout"`;
  - the previous write failed — `full = "write_failure"`.

  The four added fields are `full` (naming why), `environment` (the active environment, or `null` while none is active, which is the case in Setup), `on_threshold` and `off_threshold` (the two debounce thresholds in force).
- **`full` names one reason.** When more than one reason holds, `"start"` wins over every other, and `"new_file"` is named only when no other reason holds.
- **A full line is a starting point.** Reading a file forward from any line that carries `full` gives every block's reading at every pass after it.
- **The run starts from what the server held at boot.** When `bootstrap_runtime(...)` (the function that builds the runtime at server boot) is given a folder, then once the layout is loaded, and before any pass or request runs, it calls `DetectorReadingLog.set_start_state(start_state_of(network))`. The `"start"` line carries that value as `start_state`, and no other line carries it. `start_state` holds:
  - `layout` — the layout as a layout save holds it (`RailwayNetwork.to_layout_dict()`);
  - `hardware` — the Hardware Setup as a layout save holds it;
  - `selected_environment` — the environment selected at boot.
- **Operator commands are written too.** Every state-changing API request the server answers gets one line as it is answered, whether it was accepted or refused. A state-changing request is a `POST`, `PUT`, `PATCH` or `DELETE` whose path starts `/api/`. The app's after-request hook (in `app_factory.create_app`) writes the line through `DetectorReadingLog.record_command(...)`, holding `AppState.simulation_lock`. A command line carries:
  - `command_no` — counts the commands the log recorded in this process, from 1. A gap in `command_no` means a command line was lost to a failed write.
  - `after_pass` — the `pass` of the last pass that ran before the request changed runtime state; `0` when no pass had run (see **`after_pass` comes from the lock**).
  - `wall_time` — the wall time at which the request was answered.
  - `command` — `method`, `path`, `query` (the raw query string), `body` (the JSON body, or `null`) and `status` (the HTTP status the server answered).
- **A command line is not a pass line.** A command line carries no `pass` and no `readings`, and it does not change which blocks the next pass line lists.
- **`after_pass` comes from the lock.** `AppState.simulation_lock` is a `SimulationLock`: each time a thread releases it, the lock reads `DetectorReadingLog.pass_count` and keeps that count for the thread. A request changes runtime state only while it holds the lock (see **One thread at a time**), so the count kept at its last release is the last pass before its change. A request that never took the lock records the count at the moment it is answered.
- **`after_pass`, not file order, says when a command ran.** A pass may run between a request's change and its answer, so a command line can follow the line of a pass that ran after the command.
- **A recorded run can be replayed in a test.** `tools/reading_replay.py` re-runs a recorded run in a server it builds for the test. The running server never replays.
  - **`RunReplay` replays a whole run.** `RunReplay` builds the server from `start_state`. It sends each command after the pass named by its `after_pass`, and runs each pass with its recorded readings, its recorded `wall_time` and its recorded `operating_delta_seconds`: a `"tick"` pass as a full runtime step, a `"stopped"` pass as the pass the runtime runs while the session is not Live. It reports every command the replay answers with a different `status`, and every pass that does not read the detectors exactly once.
  - **No device is opened.** Activation gets the recorded readings in place of a device.
  - **What a replay cannot follow.** A run in which a Setup switch throw test changed a switch's position, or in which a hardware failure happened, replays correctly only up to that point: the test's command is not sent (see the next rule), and a failed read writes no line.
  - **Commands that drive or read a device are not sent.** The readings those commands caused are already in the file. The commands not sent are: every path under `/api/hardware/` except `/api/hardware/config` and `/api/hardware/binding` (the write that binds one block or switch to a device port); every path under `/api/virtual-hardware/`; and every path under `/api/debug/hw` (the developer panel's reads and writes of the fake device).
  - **`ReadingPlayer` plays the readings alone.** `ReadingPlayer` plays the readings into a runtime the test builds, for a file with no `start_state`.
  - **`tools/cut_reading_log.py` cuts a pass range out of a file.** The first line of the cut lists every block.
  - **The readings do not react.** The readings are played back as recorded and do not react to what the replayed runtime commands. A replay therefore tests what the runtime decides from a run's readings and commands, not how a train responds to the voltage it is given.
- **Where it is kept.** The server writes the files into the per-machine `.runtime/detector_readings/` folder beside the code. Its parent `.runtime/` also holds the server's own shutdown record, which is outside this document. The files are not part of a layout save.
- **File naming and flushing.** Each file is named for the local time it was opened. Each line reaches the operating system as it is written, so a crash loses no line already written.
- **File size limits.** A file past `DETECTOR_READING_LOG_MAX_FILE_BYTES` (20 × 1024 × 1024 bytes) is closed, and the next line opens a new file. Opening a file deletes the oldest files until the folder holds no more than `DETECTOR_READING_LOG_MAX_TOTAL_BYTES` (200 × 1024 × 1024 bytes). Both values live in `constants.py`.
- **A failed write never stops the session.** `DetectorReadingLog` catches every failure of its own write, and the pass or the request carries on.
- **A failed write is reported once.** `DetectorReadingLog` reports the failure as one `kind = "warning"` event with `source = "DetectorReadingLog"`, `operation` naming the write that failed (`"record_pass"` or `"record_command"`), and an `ErrorRecord` naming the failure. It emits under one condition key, so a run of failing passes produces one event (see **Error events**, Suppression mechanism). It resolves that key on the next successful write.
- **A folder that cannot be created is a failed write.** When the folder cannot be created as the log is enabled, the log stays on and each later pass tries again. The first pass line that is written is the `"start"` line.

## Units and clocks

**Time sampling**

- **Sample at the write site.** A clock value that goes into a stored field is sampled where the field is written. It is never passed in as a method or callback parameter. `current_time` for a wall timestamp means `time.time()` sampled when the writer commits the write.
- **Every writer samples its own time.** Every writer of a wall timestamp, such as an event's `event_time`, samples it locally.
- **One sample for two fields.** When one writer records two wall timestamps in the same step, it samples `time.time()` once and uses it for both.
- **A predicate may take a clock reading.** The rules above cover values written to a stored field. A pure predicate may take clock readings as parameters.
- **Which clock.** Which clock a timestamp or a duration uses is decided by **Clocks**.

**Clocks**

There are two clocks, and every duration measures against exactly one of them. Every absolute or saved timestamp is a wall-clock instant.

| Clock | Advances | Reset |
| --- | --- | --- |
| wall | always | never; it is system time |
| `AppState.operating_time` | only while the session is Live | process start |

- **Wall.** The wall clock is `time.time()`: system time in Unix seconds. It measures what the world does whether or not the app is running trains. A wall field, such as an event's `event_time`, is sampled at its writer (see **Time sampling**).
- **The hold time is measured on pass wall time.** Each pass of the runtime loop samples the wall clock once at its start; that sample is the pass's `wall_time`, the value the **Detector reading log** writes. The hold time (see **Trains**) is measured between pass `wall_time` values. A count that starts inside a pass (a block begins to read empty, a train begins to wait) starts at that pass's `wall_time`. A count that starts with a request made between two passes (the operator stops a train) starts at the `wall_time` of the next pass. This is the one exception to **Time sampling**, Sample at the write site. The hold time therefore runs in every session state, and a replay that feeds each pass its recorded `wall_time` measures the same hold.
- **`operating_time`.** `operating_time` is monotonic seconds of operation since process start. Each pass of the runtime loop made while the session is Live adds that pass's wall time; any other pass adds nothing. It lives on `AppState`, the process-wide state object, so loading a layout does not reset it.
- **An `operating_time` value never leaves the session.** It may be held in memory for duration arithmetic and for computing the elapsed figures a payload carries. It is never written to any of these:
  - a layout save;
  - an **Event log** row;
  - another record that outlives the process.
- **A step of `operating_time` is not a value of it.** The **Detector reading log** writes each pass's `operating_delta_seconds`, the amount the pass advanced the clock, and never the clock's reading.
- **A client derives no durations.** The page shows the instants and the elapsed values a payload carries. It never computes an elapsed figure from its own clock (see **Elapsed values are server-owned**).

## Runtime settings

An operator can change a fixed set of values from a live session, with no restart and no code change.

- **One store.** `RuntimeConfig` (`runtime_config.py`) is the single store of the current value of every tunable field.
- **One entry point.** `GET /api/settings` and `PUT /api/settings` (`routes/settings.py`) are the only way to read or write it.
- **Seeded from code.** Each field is seeded at process start from a fixed code default. For example, `notification_retention_rows` is seeded from `constants.NOTIFICATION_RETENTION_ROWS`.
- **A write reaches the consumer on its next read.** A `PUT` writes the accepted value onto the `RuntimeConfig` field and onto every module attribute the consuming code reads that value from. Code that reads the value as a live module attribute sees the change on its next read.
- **A module attribute is a mirror.** A live module attribute mirrors `RuntimeConfig`. It is never a second source of truth.
- **In memory only.** The store is not saved. A process restart reseeds every field from its code default.
- **Applied between runtime steps.** Like every API request, a `PUT` is applied between two runtime steps, holding `AppState.simulation_lock` (see **Operator requests**).

**Settings REST API**

| Endpoint | Body / parameters | Behaviour |
| --- | --- | --- |
| `GET /api/settings` | none | Returns `{"settings": {...}}` with every field's current value. |
| `PUT /api/settings` | `{"settings": {field: value, ...}}`, or a flat `{field: value, ...}` body. When a `"settings"` key is present, its value is the update dict; otherwise the whole body is. | Applies the accepted part of the request per **Validation**, then returns `{"status": "success", "settings": {...}}` with every field's current value. This is the same shape as `GET`, not only the fields the request changed. |

**Validation**

- **Unknown field.** An unknown field name is ignored. It does not fail the request and is not reported as an error.
- **Null value.** A `null` value for a known field is ignored, and that field is left unchanged.
- **Coercion.** An accepted field's incoming value is coerced to that field's own type. A value that cannot be coerced is ignored, and that field is left unchanged. Every other field in the same request is still applied. Every tunable is `int` or `float` (see **Tunable fields**).
- **Field rules.** After coercion, every numeric value must be finite and must satisfy its field rule. A value that fails is ignored in the same way as a value that cannot be coerced, and valid fields in the same request still apply. The field rule is:
  - `notification_retention_rows >= 1`;
  - `hold_time_s > 0`.

**Tunable fields**

Every field is a top-level key in the `GET` / `PUT /api/settings` payload, named as its `RuntimeConfig` field (snake_case). When a field backs a value this document names elsewhere, the row gives the module-global name and that home, and the value's meaning stays defined there. Otherwise the row's own description is the field's complete definition.

*Trains (the runtime):*

| Field | Type | Default | Controls |
| --- | --- | --- | --- |
| `hold_time_s` | `float` | `5.0` | The hold time: how long a block that reads empty stays its train's block while no block further forward reads occupied — see **Trains**, A block that reads empty is taken off at once or after the hold time. Raising it carries a train across a longer loss of contact. Lowering it does the opposite. |

*Event stream (`notification_center.py`):*

| Field | Type | Default | Controls |
| --- | --- | --- | --- |
| `notification_retention_rows` | `int` | `10000` | `NOTIFICATION_RETENTION_ROWS`: how many of the most recent events `NotificationCenter` keeps before it drops the oldest — see **Runtime notifications**, Retention. Raising it lets an operator scroll further back through a long session, at the cost of memory and event-log file size. Lowering it trims both. |

**Why this shape**

- **One store and one endpoint pair.** They give a single place to read or change every tunable value, with no separate surface per consumer.
- **Coercion, not a declared schema.** Coercing to the field's existing type ties each field's type to the value seeded from code, so a field can never hold a type its consuming code does not expect.
- **Three separate surfaces.** Runtime settings, Hardware Setup, and the selected operating environment are separate surfaces, and each is changed only through the application's API:
  - **Runtime settings** reach the running session on the consumer's next read, and reseed from code defaults after a restart.
  - **Hardware Setup** is the saved description of the controller, chips and wiring, stored with its layout. An operating environment builds a session's chips from it at activation, and resolves the per-write values named in **A binding is read per write** on every write.
  - The **selected operating environment** is stored separately (see **Operating environment and activation**).
- **A running session is never rewired in place.** Changing the environment, or the part of the setup captured at activation, does not rewire a session that is Live. Activation performs the session rebuild, and activation never runs while the session is Live.

## Code map

Where each part of this document lives in the code. A file this list does not name, and no section describes, is not part of v2.

- **Hardware layer** (`hardware/`, `hardware_config.py`, `block_power.py`, `switch_power.py`, `block_occupancy.py`): the interfaces, chip drivers, bindings, safe state, watchdog and Setup tools. See **Hardware interface**, **Hardware setup** and **Setup tools**.
- **Track model** (`track.py`, `segment.py`, `block.py`, `switch.py`, and the layout model that loads a layout file): see **Tile**, **Segment**, **Block** and **Switch**.
- **Runtime** (`simulation.py`, `switch_manager.py`, and the modules they are split into): the loop, the session state, trains, claims, switch throws and the runtime step. See **Runtime step**, **Trains**, **Switch** and **Session state**.
- **Web requests** (`routes/`): see **Running a train**, **Hardware REST API** and **Data flow to the page**.
- **Events and logs** (`notification_center.py`, `detector_reading_log.py`, `tools/reading_replay.py`, `tools/cut_reading_log.py`): see **Notifications and errors**, **Event log** and **Detector reading log**.
- **Snapshot** (`network_poll.py`, `network_stream.py`): see **Data flow to the page**.
- **Page** (`frontend/src`): the Editor, Hardware Setup, Operate and Logs screens. Its look and layout follow `docs/DESIGN_LANGUAGE.md`; what it can do follows this document.
- **Layout files** (`layouts/*.json`): the track, blocks, switches, tunnels and Hardware Setup of one layout. A layout file may carry fields this document does not name; the loader ignores them.
- **Layout editor.** The requests the Editor screen uses to draw and edit tiles, segments, blocks, switches and tunnels, and the layout file format, are described in `docs/API.md` and `docs/PERSISTENCE_DESIGN.md`. They are the one part of v2 this document does not spell out; the rules they must keep are under **Tile**, **Tunnel**, **Segment**, **Block** and **Switch**. Both of those documents describe v2 only and are kept in step with this one.

## Out of scope

Each entry is a clear "no" for v2. None of them is promised for later.

1. **Planning past the next block.** No routes, no timetables, and nothing reserved beyond one claim. A train with a destination still claims one block at a time.
2. **Speed curves and calibration.** A train's speed is whatever its knob voltage gives. The runtime keeps no speed and no position inside a block for any train.
3. **Stopping at an exact spot.** A train loses its voltage once it is wholly in its front block, and it comes to rest wherever it rolls to.
4. **Signals, stations and platforms.** A destination is a block.
5. **Seeing what the detectors cannot.** A second train in an occupied block, and cars that draw no current, are invisible to v2.
6. **Finding a way round, or reversing by itself.** A train whose way is blocked waits. Two trains that meet head-on both wait until the operator reverses one.
7. **Saving trains.** Trains are found again from the detectors after every restart and activation.
8. **Running with no Hardware Setup.** The only way to run without the rig is Virtual Hardware, which uses the same Hardware Setup and the same code as the rig. Virtual Hardware is a tool for tests and debugging. It only has to be good enough to drive the code: its virtual train does not move by itself and does not have to behave like a real one.
9. **Packing trains close together.** v2 aims for what a power brick gives: one train on a plain loop, and a second where a siding and a switch let them pass. How closely one train can follow another is not something the rules try to improve.

## Authoring rules

These rules apply to every edit of this document. A reviewer rejects any edit that breaks them. The sentence-level style rules are in `.claude/rules/design-v2-authoring.md`.

1. **Every rule passes The evening test.** A rule that is here only because a real railway has it does not belong.
2. **v2 means v2.** No "removed", "legacy", "deprecated" or "no longer used" text, and no comparison with an earlier design, outside **Why v2 exists**.
3. **No cross-reference drift.** When a rule changes, every place in this document that refers to it is updated in the same edit.
4. **One home per concept.** Each rule, term and behavior is defined in one place. Every other place refers to it by its section or bold label.
5. **Terms come from Terms.** A word that names one of v2's own ideas is listed under **Terms** with a plain-English meaning. A name of code is given a plain meaning where it first appears. Any other word is plain English.
6. **No single-use-case branches.** A rule is a general mechanism. A special case for one situation is either rewritten as a general rule or marked as the one case the general rule cannot cover.
7. **Requirement words are binding.** `must` and `must not` state requirements, `may` states permission, and a plain statement of what v2 does ("the runtime throws the switch") is a requirement too. `should` is not used in a rule.
8. **Operator-visible changes update the operator sections.** An edit that changes what the operator can do, see or rely on updates **Setting up the hardware and going live** or **Running a train** in the same edit.
9. **Reasons are given on their own terms.** A "why" explains why the rule is right for the evening, never by pointing at a design that was dropped.

## Review checklist

A reviewer checks each of these against the code:

- Does every block's voltage, while the session is Live, come from **Track power** or from Stop all, and go out through `BlockPowerController`?
- Does a train's voltage reach a block only when the block is one of its blocks or its claim, and does a train never hold more than one claim?
- Does a claim end only in the ways listed under **Claiming the next block**, A claim ends in one of these ways?
- Is a train's rear block taken off at once when it reads empty and a block further forward reads occupied, and does any other block that reads empty stay for the hold time and no longer, a lost train's block apart, keeping its voltage for that time while the train is moving?
- Is a train that rolls into an empty block that belongs to no train given that block with a warning, and able to run again as soon as its knob is turned up?
- While the session is Live, does the runtime throw a switch only for a train with a destination, only once every condition for the claim holds, only when the switch is not already set for the way, and never while the switch is held?
- Is a held switch refused to the operator too, with `switch_held`?
- Does the runtime set a train's knob to 0 V only in the cases listed under **When the runtime stops a train**?
- Can a train run with no speed calibration, no speed curve and no destination, and without being created by hand?
- Does the runtime keep no calculated position and no speed for any train?
- Does a runtime step do its work in the order **Runtime step** gives?

## Open questions

These are for the owner to settle. Until one is settled, the rule it names stands as written.

1. **What the detectors read while a train is on a switch.** A switch has no detector of its own, and its rails are wired to its feed block. While a locomotive stands on a switch and on no block, does the feed block read occupied, or does no block read occupied? The rules work either way, but the answer says how often a train depends on the hold time to get across a switch.
2. **Names the carried-over text and the code disagree on.** The names are to be made the same by a gap analysis of the hardware sections against the code, which has not been run yet. Two known cases: the code keeps `baseline_ma`, `threshold_ma` and `driven_threshold_ma` directly on the `block_occupancy_current` binding where this document has a separate `detector_calibration` map, and the code's `operation_mode` is this document's `active_environment`.
3. **Loose ends in the carried-over text.** These are named and not fully defined: the `command_id` in a command result; `PUT /api/hardware/binding` and the `/api/debug/hw` requests; `operating_time`, which only the detector reading log still uses; and train ids, which start again after a restart while the saved event log still holds the old ones.
