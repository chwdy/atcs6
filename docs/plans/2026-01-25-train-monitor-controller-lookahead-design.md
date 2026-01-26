Train Monitor Lookahead via TrainController

Context
The Train Monitor currently derives lookahead data from Train.to_dict, which was
originally built when the Train entity owned route context. The system has since
shifted toward the TrainController (onboard computer) being the authoritative
source of operational context (path, constraints, forward elements). The monitor
should reflect that ownership split, while the Train entity remains responsible
for physical state (position, speed, state).

Goals
- Make TrainController the single source of truth for lookahead data.
- Keep the Train entity as the source for physical position and state.
- Update Train Monitor in an event-driven way via StateManager (no polling).
- Preserve head-relative visualization semantics (train at 0, elements ahead).

Non-Goals
- Change the renderer or TrainPosition semantics.
- Replace StateManager with a push server stream.
- Redesign Train Monitor UI layout.

Proposed Architecture
1) Backend: TrainController owns lookahead computation and exposes a new API
   endpoint for the Train Monitor.
2) Frontend: Train Monitor uses StateManager subscription for selected train
   and fetches lookahead from the new endpoint on each relevant update.
3) Train.to_dict remains for renderer and control panel but no longer provides
   monitor lookahead data.

API Design
Endpoint: GET /api/trains/<train_id>/lookahead

Response shape (head-relative):
{
  "train_id": "T01",
  "head_tile": [x, y],
  "head_distance_cm": 12.3,
  "travel_direction": "EAST",
  "cab": "a",
  "lookahead": {
    "blocks": [
      {"id": "BLOCK_A", "start_cm": -15.2, "end_cm": 134.8, "state": "occupied", "tile_count": 3}
    ],
    "signals": [
      {"id": "SIG_1", "distance_cm": 34.4, "state": "green"}
    ],
    "switches": [
      {"x": 3, "y": 0, "distance_cm": 134.4, "locked": true, "position": "main"}
    ],
    "stations": [
      {"station_id": "STA_1", "distance_cm": 280.0, "length_cm": 60.0}
    ],
    "clearance_cm": 540.0
  }
}

Data Rules
- Distances are head-relative in centimeters.
- The current block start must include tiles behind the head to keep stable
  block offsets within the same block.
- travel_direction must be derived from TrainController + TrainPosition to
  reflect cab direction changes.

Frontend Integration
- TrainMonitor subscribes to StateManager for the selected train.
- On update, if visible and train id matches, call
  api.getTrainLookahead(train_id) to refresh lookaheadData.
- Use train snapshot only for speed/state values; use controller payload for
  blocks/signals/switches/stations/clearance.
- Add a short debounce (e.g., 150ms) to prevent duplicate fetches per frame.
- Drop responses that do not match the currently selected train id.

Backend Computation
TrainController computes lookahead from its authoritative route/path.
Algorithm sketch:
- Determine head tile and head_distance_cm from TrainPosition + cab direction.
- Scan backward along current route until block id changes to compute
  block_back_length_cm.
- Set current_block_start_cm = -head_distance_cm - block_back_length_cm.
- Walk forward along route to build lookahead tiles and elements.
- When block id changes, reset block_start_cm to the new tile's
  distance_to_tile_cm.

Testing Plan
- New backend tests validating block start stability within a block.
- Test that block start resets at block boundaries.
- Test cab direction inversion (leading_end swap) changes "ahead" correctly.
- Existing JS syntax and smoke tests should remain unchanged.

Risks and Mitigations
- Increased API calls on frequent updates: mitigate via debounce and caching
  per train id (short-lived) in TrainMonitor.
- Missing route: return empty lookahead with warning, UI shows "no data".

Rollout Plan
1) Add controller lookahead endpoint and tests.
2) Update TrainMonitor to use event-driven fetch.
3) Remove dependency on Train.to_dict lookahead data.
