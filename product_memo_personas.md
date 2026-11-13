# atcs6 — Persona Memo & Next-Step Brief

*Strategy brief. Not a contract — no authoring rules apply (those govern only `design_v2.md`).*
*Last updated: 2026-06-25.*

---

## The one insight that ties both personas together

Both target users **deliberately avoid switches and complex track**. The hobbyist *has* one loop; the store owner *chooses* only loops. That matters because the largest thing atcs6 has built — the ATC dispatcher / route / path / switch-locking engine — is built for exactly the layouts neither of them wants.

That is not wasted work. Split the engine in two:

- **Switch routing / depots / path contention** → over-built for these customers. Keep it; stop leading with it.
- **Block-occupancy + ATC spacing (following distance)** → *exactly* right for the one scenario both personas actually have: **several trains sharing one loop.** Rear-end spacing on a single circle is a real control problem with no switches involved.

So on a simple loop the value proposition shifts from *"watch the dispatcher resolve switch conflicts"* to:

> **"Watch your real, calibrated locomotives run with correct physics, hold safe distance, and stop where you tell them — with a log of what happened."**

That sentence is the product for these two personas.

---

## Persona A — Young hobbyist ("make my one loop run *right*")

New to layouts. Can't control power well. Needs derailment handling, good detection, an operation log, a straightforward UI to drive the train (speed up/down, stop, stop at station). His track is simple: one block that is a loop, physically connected, possibly with slope, turns, and speed limits.

| Pain today | What they need | Status in atcs6 |
|---|---|---|
| Can't control power properly | Dead-simple throttle UI: speed up/down, stop, stop-at-station | Sim controls exist; not framed as "one loop, one throttle" |
| Derailments / collisions | Detection + a clear "what just happened" | Grade-1 collision flash + stuck-train indicators done |
| No record of operation | An **operation log** | **Does not exist** |
| One loop, but it has slopes/curves/limits | A block model that fits *one powered circle with varying params* | **Modeling gap** (see Cluster 1) |
| Doesn't know how fast his loco really goes | **Speed calibration** tied to the specific train | Curve data model exists; **no measured-calibration workflow** |
| Owns several locos as physical things | **Inventory / roster** with purchase info + linked profile | **Does not exist** |

## Persona B — Store owner ("steady income, no drama")

Risk-averse. Rents track time and sells trains on a big in-store layout. Builds only simple loops on purpose. Hates managing track conflicts and handling customer complaints. Customers are roughly half young / half adult, mainly weekends. Switches wear out, are costly and hard to replace, and cause collisions; complex layouts crowd and confuse.

| Pain today | What they need | atcs6's answer |
|---|---|---|
| Switches wear out, costly to replace | Wear forecasting | **Maintenance counters — built** (`flip_count`, `occupied_seconds`, train distance/runtime) |
| Collisions on expensive trains | Keep trains apart | Block-occupancy + ATC spacing prevents two trains in a block (built) |
| "Whose fault?" customer disputes | An authoritative **incident replay / event log** | **Does not exist — the highest-value gap his story reveals** |
| Complex layouts confuse operators | Layout legibility | UI overhaul direction (in progress) |
| Half-young/half-adult, weekend crowds | Simple, robust, hard-to-break operation | The whole "simple loop done well" thesis |

**Adjacent segment worth keeping in view:** clubs / exhibition & modular groups (Free-mo, NTRAK) / museums — multi-operator complex track where "whose fault" + clarity matter, reachable *without* hardware.

---

## The three feature clusters the notes actually describe

### 1. Single-loop power model *(foundation / refactor)*

Today `Block` is simultaneously the power unit *and* the occupancy/params unit (`Block = electrically isolated track section`, `block.py`), and each block holds its own `current_voltage` — so two blocks on one physical loop can silently disagree on voltage ("the current system cannot sync voltage across block").

The fix is to **decouple them**:

- A **power district** (the physical circuit) owns one shared voltage.
- A **block** becomes a consistent-params *occupancy* segment *inside* a district.
- Many blocks per district; voltage is set once at the district; **a loop is a first-class shape.**

This is what makes the hobbyist's actual layout representable for the first time — and it is the substrate that multi-train spacing on a loop sits on.

### 2. Train asset layer *(net-new feature, mostly on top of what exists)*

- **Inventory / roster:** record each owned loco — box, purchase date, price — and link a speed profile. When placing a train, import from the roster.
- **Measured speed calibration:** the `voltage → power → speed` curve already exists as a data model (`speed_profile.py`: `voltage_power_curve`, `power_speed_curve`); what's missing is the *workflow* to **measure** it instead of hand-drawing it.
  - **Manual mode:** press "record" as the train passes a marker; repeat for reliability.
  - **Auto mode:** current / IR sensors time a lap (needs a layout with detection).
  - Save the result; tie it to the loco in the roster.

### 3. Operation log *(trust story)*

- **Operation / event log:** an append-only, replayable record (departures, stops, block entries, EB events, collisions). This is the store owner's "whose fault" answer *and* the hobbyist's "what just happened."

---

## Recommended next step

**Build the Train Asset Layer (Cluster 2): roster + measured calibration.**

Why this first:

- **Lowest risk, highest visible payoff.** It sits almost entirely *on top of* the existing `SpeedProfile` / `SpeedProfileRegistry` data model — adding a roster CRUD and a calibration *wizard*, not touching the tick loop or the block/power core.
- **Most emotionally resonant feature for the hobbyist** — "my trains are real objects I own and calibrate" — and it's naturally SaaS-shaped (per-user data; the thing free JMRI doesn't give nicely).
- **De-risks the power-model refactor** by shipping a win before opening up the `Block` concept (Cluster 1), which is the deepest change and is better done deliberately.

**Suggested sequence: 2 → 1 → 3.**
Asset layer (ship a win) → power model (fix the foundation the loop needs) → event log (the trust story, most compelling once the loop model is right).

### One caveat to decide consciously

*Literal hardware throttle control* (DCC / DC command station — real volts on real rail) is the one piece of "control power" that is genuinely outside the design + simulate lane and is JMRI's home turf. Everything else in these notes can be done in-sim. Decide deliberately whether atcs6 ever crosses that line.

---

## Status snapshot (verified 2026-06-25)

| Thing | Exists? | Where |
|---|---|---|
| Block = power unit *and* occupancy unit (conflated) | Yes (the gap) | `block.py` |
| Per-block `current_voltage` (no shared-district sync) | Yes (the gap) | `block.py` |
| Speed-profile calibration **curves** (data model) | Yes | `speed_profile.py`, `editor-speed-profiles.ts` |
| Measured calibration **workflow** (record/auto) | No | — |
| Inventory / roster | No | — |
| Operation / event log | No | — |
| Maintenance / wear counters | Yes | `switch.py`, `simulation.py`, `train_physics.py` |
| Grade-1 collision flash | Yes | `app.ts` |
