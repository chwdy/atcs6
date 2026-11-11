# atcs v2 design

Status: draft, 2026-11-10.

## About this document

This document is the one bridge between the person who owns this project, the AI agents who write code for it, and the code. Keep these points in mind as you read:

- **The code is a straight translation of this document.** Every behavior the code has is described here, and every rule here is in the code. When the code and this document disagree, this document wins, and the code is changed to match.
- **Code no section describes is not part of v2.** A gap analysis treats such code as something to remove, not as a rule the doc forgot.
- **Rules live only here.** Tests check that the code follows this document. A test never holds a rule of its own that this document does not state.
- **v2 means v2.** This document describes v2 as it is built. It holds no "removed", "legacy", "deprecated" or "no longer used" text, and no comparison with an earlier design, apart from the section **Why v2 exists**. Git keeps the history. v1's document is kept at `archive/design_new.md` for reference only; nothing in v2 depends on it.
- **One thread at a time.** The runtime loop and every web request hold the same lock while they read or change runtime state, so a request always runs between two runtime steps, never during one. Rules such as first come, first served under **Claiming the next block** depend on this.
- **This document changes often.** When a rule changes, every place that refers to it is updated in the same edit. A half-updated document is a bug, not history.
- **Start with The evening v2 is for.** Every other section exists to make that evening work.

## Why v2 exists

The goal of the project has not changed: run model trains on my layout and enjoy watching them.

v1 got there by copying a real railway: every train needs a destination, a route and a reservation, and any departure from the plan is an emergency. That suits a simulator of real train control, which is what v1 started as. It does not suit a model layout. Bumping into each other is the worst risk (expensive models, no lives), and an evening where nothing runs is the most common failure.

v2 is built for a model railway. When a real railway's rule and an enjoyable evening disagree, the evening wins.

## The evening v2 is for

I put a few trains on my layout, open the web page, press run, and watch them go round without hitting each other. I can take over any train from the page at any time and see the layout respond the way I asked.

Every feature has to make this evening easier. If a feature makes it harder to get a train moving, it waits until later or gets dropped.

**The evening test.** Before a rule goes in, ask: is it here because the evening needs it, or because a real railway has it? Only the first kind goes in.

## The rules that come from that goal

1. **The detectors say where trains are.** A block a detector reports as occupied has a train in it. v2 never decides a train is somewhere the detectors disagree with.
2. **The basic instruction is "run".** A running train keeps going, follows the switches as they are currently set, and stops only for the reasons listed under **When a train stops**. A destination is an extra the operator can add; when one is set, v2 also throws the switches on the way there. A train never needs a destination to move.
3. **The only protection is: a train does not enter a block another train is in.** Nothing else is treated as an emergency.
4. **Speed is a voltage knob, like a power brick.** No speed curve and no calibration are needed to run. A train stops roughly, somewhere in the block it was told to stop in, or a little past it.
5. **Few steps.** Putting a train on the track and pressing run is about as quick as a power brick.

## Terms

These are the only terms this document uses for v2. A term not listed here is plain English.

- **Block**: a stretch of track with one power feed and one occupancy detector. It gets one voltage at a time and reads either occupied or empty. Blocks and their wiring come from the layout file.
- **Switch**: a point where the track splits. It is set one of two ways, and v2 or the operator can throw it.
- **Train**: a label v2 puts on one or more blocks that read occupied. It is not a model of a locomotive. See **Trains**.
- **Speed knob**: the voltage a train's blocks get while it runs. See **Running a train**.
- **Next block ahead**: the block a train enters next, found by walking the track from the train's front block in its direction of travel, through each switch as it is currently set.
- **Claim**: a train's hold on the next block ahead before it enters it. See **Claiming the next block**.
- **Destination**: a block the operator picks for a train to run to and stop in. Optional.
- **Lost train**: a train whose blocks have read empty for 5 seconds while it was running. See **Track power**, Losing a train.
- **Warning**: a message on the page that needs no action before the train can run again.
- **Error**: a message on the page that comes with every affected block set to 0 V.

## Trains

- **A label appears when a detector sees something.** When a block reads occupied and no train's label covers it, v2 puts a new train's label on that block. I do not have to tell the software about a train before it runs.
- **Labels are mine to edit.** On the page I can rename a train, set its direction, and note its length.
- **A train longer than one block.** One train can read occupied in two neighbouring blocks at once, and v2 at first gives each block its own train. I can mark the two as one train; from then on both blocks belong to that train. I can also split them again.

## Running a train

These are all the things the operator can do from the page. Each train has its own controls:

- **Speed knob**: sets the voltage on the train's blocks, from 0 V up to full track voltage, the same way turning a power brick does. Turning it while the train runs changes the speed at once.
- **Stop**: sets the train's voltage to 0 V.
- **Reverse**: accepted only while the train is stopped.
- **Set a destination**: pick a block, and the train runs there and stops when it reaches it. Without a destination the train just runs, following the switches as they are set. See **Switches**.

The operator can also throw any switch from the page by clicking it, except a switch in a block that is occupied or claimed (see **Switches**, Never under a train).

## Claiming the next block

Two trains are never let into the same block. To make sure, a train claims a block before it enters it:

- **One block ahead.** A running train claims the next block ahead when that block is empty and unclaimed. The train's voltage goes onto that block only once the claim is made. When the train cannot claim the next block ahead, it stops where it is.
- **A claimed block counts as taken.** No other train can claim it, and no switch inside it can be thrown.
- **First come, first served.** Whichever train claims a block first gets it. The other waits until the block is empty and unclaimed again.
- **Release.** A claim ends when either of these happens:
  - the train entered the block and has left it again — the block read occupied after the claim was made, and has since read empty for 5 seconds;
  - the train stopped before it entered the block.
- **That is all a claim is.** A train claims at most one block ahead. Nothing is reserved all the way to a destination, and claims have no priorities.

This one-block claim is how "trains don't hit each other" works when two trains want the same track. If a later need seems to call for a deeper or more complicated claim, that need passes **The evening test** first.

## Switches

- **Without a destination.** v2 never throws a switch. The train follows the switches as they are set, and the operator throws them by hand.
- **With a destination.** v2 finds the way from the train's block to the destination block, going forward in the train's direction, and throws each switch on that way.
- **When a switch is thrown.** v2 throws a switch as part of claiming the block the switch sits in, not earlier, so a switch is not held for one train while others still need it.
- **Never under a train.** A switch inside a block that is occupied or claimed is not thrown, by v2 or by the operator.
- **No way there.** When there is no way forward to the destination, v2 does not start the train and shows a warning. v2 never reverses a train by itself.

## When a train stops

A running train stops for any one of these reasons:

1. The operator pressed stop.
2. The train reached its destination block.
3. The train cannot claim the next block ahead, because another train is in it or has claimed it.
4. The next block ahead is across a switch that is set the wrong way and v2 cannot throw it, because either:
   - the train has no destination, or
   - the switch's block is occupied or claimed.

## When a train overruns

A train runs on a voltage knob, not a speed curve, so it will often roll past the block it was meant to stop in. v2 treats this as ordinary. The only thing that matters is whether another train is there.

When a train rolls into a block it has not claimed:

- **If that block is empty and unclaimed:** v2 gives the block to the train, keeps it at 0 V so the train stops there, and shows a warning such as "Train A stopped one block past where it was told to stop." The operator can press run again straight away.
- **If another train is in that block or has claimed it:** v2 sets both trains' blocks to 0 V and shows an error. This is the one case worth stopping everything for, because two trains are now in one block, or about to be.

A train needs only one block to stop in. A train that rolls a whole block past its stop is a reason to turn its knob down, not to leave a bigger gap between trains.

## Track power

Each block gets one voltage at a time, signed for direction. v2 decides it from these rules alone:

- **A block a running train is in, or has claimed,** gets the voltage from that train's speed knob.
- **A block with a stopped train, and an empty block,** get 0 V. With no voltage on the rails, the '165 detector still sees a train standing there, so a stopped train stays visible and a train placed on an empty block shows up straight away. The hardware layer picks the right detector from the block's voltage: the '165 at 0 V, the current sensor while the block is driven.
- **Losing a train.** When a block with a running train in it reads empty, v2 keeps the block at the train's voltage and keeps the label there for 5 seconds. If the train reads occupied again within that time, it carries on. If not, v2 marks it a lost train, shows a warning, and drops its block to 0 V, where the '165 can find it again if it is still there.
- **When the hardware fails.** When the hardware link drops or a chip stops answering, every block goes to 0 V and every point motor is switched off.

Keeping power on for 5 seconds is what lets a train ride through a short loss of wheel contact, for example on a switch. While the wheels have no contact, the detector cannot see the train, and only the voltage still on the rails can bring it back.

## Code map

The code that implements v2:

- **Hardware layer** (`hardware/`, `hardware_config.py`): sets a block's voltage, reads the detectors, throws a switch, and the hardware setup tools. It knows nothing about trains.
- **Layout files** (`layouts/*.json`): the track, blocks, switches and hardware wiring. The layout model loads a layout file's track, blocks, switches and wiring, and nothing else.
- **Track drawing** (`frontend/src/renderer`): draws the layout, which blocks read occupied, and each train's label.
- **Layout editor**: draws and edits the layout and saves it to a layout file.

## Authoring rules

These rules apply to every edit of this document. A reviewer rejects any edit that breaks them. The sentence-level style rules are in `.claude/rules/design-v2-authoring.md`.

1. **Every rule passes The evening test.** A rule that is here only because a real railway has it does not belong.
2. **v2 means v2.** No "removed", "legacy", "deprecated" or "no longer used" text, and no comparison with an earlier design, outside **Why v2 exists**.
3. **No cross-reference drift.** When a rule changes, every place in this document that refers to it is updated in the same edit.
4. **One home per concept.** Each rule, term and behavior is defined in one place. Every other place refers to it by its section or bold label.
5. **Terms come from Terms.** A word that names a v2 concept is listed under **Terms** with a plain-English meaning. Any other word is plain English.
6. **No single-use-case branches.** A rule is a general mechanism. A special case for one situation is either rewritten as a general rule or marked as the one case the general rule cannot cover.
7. **Requirement words are binding.** `must` and `must not` state requirements, `may` states permission, and a plain statement of what v2 does ("v2 throws the switch") is a requirement too. `should` is not used in a rule.
8. **Operator-visible changes update Running a train.** An edit that changes what the operator can do, see or rely on updates **Running a train** in the same edit.
9. **Reasons are given on their own terms.** A "why" explains why the rule is right for the evening, never by pointing at a design that was dropped.

## Review checklist

A reviewer checks each of these against the code:

- Does every block's voltage come from **Track power** alone, with no other place in the code setting a block's voltage while trains run?
- Does a train get voltage on a block only after it holds a claim on it, and does it never claim more than one block ahead?
- Does a claim end only in the two ways listed under **Claiming the next block**, Release?
- Is an overrun into an empty, unclaimed block a warning with the train able to run again straight away, never an error?
- Does v2 throw a switch only for a train with a destination, only as part of claiming the switch's block, and never while the switch's block is occupied or claimed?
- Does a running train that reads empty keep its voltage for 5 seconds before it becomes a lost train?
- Can a train run with no calibration, no speed curve and no destination?

## Open questions

1. **Knob for a train across two blocks.** A train marked as one across two blocks gets the same knob voltage on both. Is one knob per train right, or do I sometimes want a knob per block, like one brick per loop?
2. **Direction across a block boundary.** When a train is in two blocks wired with opposite rail polarity, each block's voltage needs its own sign so the train is pushed one way. The layout file already records each block's polarity; this needs checking on the rig.
3. **What happens on each runtime step, and in what order.** A proposal: read the detectors; update train labels and lost trains; release claims; make new claims and throw switches; set every block's voltage. The order matters, because a claim released and made in the same step decides first come, first served.
4. **Running without the rig.** Is there a simulated rig in v2, so the page and the tests can run with no hardware plugged in? The hardware layer already has a fake device. If yes, this document needs a section saying how a simulated train moves and what the simulated detectors read.
