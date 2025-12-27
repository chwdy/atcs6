# 6-Bit TrackBits Implementation Plan

## 1. Overview

### Goal
Replace the `directed_paths: List[DirectedPath]` system with `track_bits: int` (0-63) as the single source of truth for track geometry.

### Scope
- **Complete replacement**: No backward compatibility with `directed_paths`
- **Demo must work**: All existing demo functionality preserved
- **Forbidden validation**: Block 23 invalid configurations with user feedback

### Files Affected

| File | Changes |
|------|---------|
| `track.py` | Replace DirectedPath with track_bits, add computed properties |
| `server.py` | Update API endpoints for new payload format |
| `static/js/editor.js` | Update ribi editor to toggle track bits |
| `demo_main.py` | Update track creation to use track_bits |

---

## 2. Phase 1: track.py

### 2.1 Remove Old Classes

Delete or deprecate:
```python
# REMOVE
@dataclass
class DirectedPath:
    port1: str
    port2: str
    direction: str
```

### 2.2 Add Constants

```python
# Track bit constants
TRACK_NS = 1   # 0b000001 - North to South
TRACK_EW = 2   # 0b000010 - East to West
TRACK_NE = 4   # 0b000100 - North to East
TRACK_NW = 8   # 0b001000 - North to West
TRACK_SE = 16  # 0b010000 - South to East
TRACK_SW = 32  # 0b100000 - South to West

# Edge masks
EDGE_MASK = {'N': 13, 'E': 22, 'S': 49, 'W': 42}

# Bit to edges mapping
BIT_TO_EDGES = {
    0: ('N', 'S'), 1: ('E', 'W'), 2: ('N', 'E'),
    3: ('N', 'W'), 4: ('S', 'E'), 5: ('S', 'W'),
}

# Edge pair to bit mapping
EDGES_TO_BIT = {
    ('N', 'S'): 0, ('S', 'N'): 0,
    ('E', 'W'): 1, ('W', 'E'): 1,
    ('N', 'E'): 2, ('E', 'N'): 2,
    ('N', 'W'): 3, ('W', 'N'): 3,
    ('S', 'E'): 4, ('E', 'S'): 4,
    ('S', 'W'): 5, ('W', 'S'): 5,
}

# RIBI mapping
EDGE_TO_RIBI = {'N': 1, 'E': 2, 'S': 4, 'W': 8}

# Forbidden configurations (23 total)
FORBIDDEN_TRACK_BITS = frozenset({
    # 3-way switches (4 configs)
    13, 22, 42, 49,
    # Double slips with 3-way (4 configs)
    15, 23, 43, 51,
    # Complex 4-path with 3-way (8 configs)
    29, 30, 45, 46, 53, 54, 57, 58,
    # 5-path (6 configs)
    31, 47, 55, 59, 61, 62,
    # 6-path (1 config)
    63
})
```

### 2.3 Update TrackSegment Dataclass

```python
@dataclass
class TrackSegment:
    x: int
    y: int
    
    # === SINGLE SOURCE OF TRUTH ===
    track_bits: int = 0  # 0-63, defines all path geometry
    dead_end_ports: Set[str] = field(default_factory=set)  # Buffer stops
    
    # === PERSISTENT SWITCH STATES ===
    switch_states: Dict[str, int] = field(default_factory=dict)  # {edge: exit_index}
    
    # === TRACK FEATURES ===
    signal_state: Optional[SignalState] = None
    signal_direction: Optional[str] = None
    station_name: Optional[str] = None
    block_section_id: Optional[str] = None
```

### 2.4 Add Computed Properties

```python
@property
def edges(self) -> Set[str]:
    """Get all connected edges (from paths + dead-ends)."""
    path_edges = {e for e, mask in EDGE_MASK.items() if self.track_bits & mask}
    return path_edges | self.dead_end_ports

@property
def ribi(self) -> int:
    """Compute RIBI from edges (backward compatible)."""
    return sum(EDGE_TO_RIBI[e] for e in self.edges)

@property
def num_paths(self) -> int:
    """Count of track paths."""
    return bin(self.track_bits).count('1')

@property
def switch_points(self) -> Dict[str, List[str]]:
    """Compute switch points from track_bits."""
    edge_exits = {e: [] for e in 'NESW'}
    for bit in range(6):
        if self.track_bits & (1 << bit):
            e1, e2 = BIT_TO_EDGES[bit]
            edge_exits[e1].append(e2)
            edge_exits[e2].append(e1)
    return {e: sorted(exits) for e, exits in edge_exits.items() if len(exits) >= 2}

@property
def track_type(self) -> TrackType:
    """Classify track geometry."""
    if self.track_bits in FORBIDDEN_TRACK_BITS:
        return TrackType.FORBIDDEN
    
    n = self.num_paths
    if n == 0:
        return TrackType.EMPTY
    if n == 1:
        return TrackType.STRAIGHT if self.track_bits in (1, 2) else TrackType.CURVE
    if n == 2:
        return TrackType.DIAMOND if self.track_bits in (3, 24, 36) else TrackType.SWITCH
    if n == 3:
        return TrackType.WYE if self.track_bits in (14, 21, 41, 50) else TrackType.COMPLEX
    if n == 4:
        if self.track_bits in (27, 39):
            return TrackType.SCISSORS
        if self.track_bits == 60:
            return TrackType.DOUBLE_CROSS
    return TrackType.COMPLEX

@property
def is_valid(self) -> bool:
    """Check if configuration is allowed."""
    return self.track_bits not in FORBIDDEN_TRACK_BITS
```

### 2.5 Add Track Manipulation Methods

```python
def add_track(self, bit: int) -> bool:
    """Add a track segment. Returns False if result would be forbidden."""
    new_bits = self.track_bits | bit
    if new_bits in FORBIDDEN_TRACK_BITS:
        return False
    self.track_bits = new_bits
    self._update_switch_states()
    return True

def remove_track(self, bit: int) -> None:
    """Remove a track segment."""
    self.track_bits &= ~bit
    self._update_switch_states()

def has_track(self, bit: int) -> bool:
    """Check if track segment exists."""
    return bool(self.track_bits & bit)

def toggle_track(self, bit: int) -> bool:
    """Toggle a track segment. Returns False if result would be forbidden."""
    if self.has_track(bit):
        self.remove_track(bit)
        return True
    return self.add_track(bit)

def set_track_bits(self, bits: int) -> bool:
    """Set track_bits directly. Returns False if forbidden."""
    if bits in FORBIDDEN_TRACK_BITS:
        return False
    self.track_bits = bits
    self._update_switch_states()
    return True
```

### 2.6 Add Dead-End Methods

Dead-ends are **cached fields** updated via propagation when neighbors change.

**Design:**
- Dead-ends represent path edges with no connecting neighbor
- An edge can be in `dead_end_ports` if it has a path in `track_bits`
- Dead-ends are recomputed when network topology changes

```python
def add_dead_end(self, edge: str) -> bool:
    """Add buffer stop at edge.

    Note: An edge can be a dead-end if it has a path but no neighbor connection.
    This is a cached property updated via propagation.
    """
    norm = _normalize_edge(edge)
    if not norm:
        return False
    # Validate edge has a path
    if not (self.track_bits & EDGE_MASKS[norm]):
        return False  # Cannot have dead-end on edge with no path
    self.dead_end_ports.add(norm)
    return True

def remove_dead_end(self, edge: str) -> None:
    """Remove buffer stop from edge."""
    norm = _normalize_edge(edge)
    if norm:
        self.dead_end_ports.discard(norm)

def has_dead_end(self, edge: str) -> bool:
    """Check if edge is a dead-end."""
    norm = _normalize_edge(edge)
    return bool(norm and norm in self.dead_end_ports)
```

### 2.7 Add Switch Methods

```python
def _update_switch_states(self) -> None:
    """Sync switch_states with current switch_points."""
    sp = self.switch_points
    # Remove states for edges no longer switches
    self.switch_states = {e: v for e, v in self.switch_states.items() if e in sp}
    # Add default state for new switches
    for edge in sp:
        if edge not in self.switch_states:
            self.switch_states[edge] = 0

def get_active_exit(self, entry_edge: str) -> Optional[str]:
    """Get active exit for entry, considering switch state."""
    exits = self.get_exits(entry_edge)
    if not exits:
        return None
    if len(exits) == 1:
        return exits[0]
    idx = self.switch_states.get(entry_edge, 0)
    return exits[min(idx, len(exits) - 1)]

def get_exits(self, entry_edge: str) -> List[str]:
    """Get all possible exits for an entry edge."""
    exits = []
    for bit in range(6):
        if self.track_bits & (1 << bit):
            e1, e2 = BIT_TO_EDGES[bit]
            if entry_edge == e1:
                exits.append(e2)
            elif entry_edge == e2:
                exits.append(e1)
    return sorted(exits)

def toggle_switch(self, edge: str) -> bool:
    """Toggle switch at edge. Returns False if not a switch."""
    sp = self.switch_points
    if edge not in sp:
        return False
    num_exits = len(sp[edge])
    current = self.switch_states.get(edge, 0)
    self.switch_states[edge] = (current + 1) % num_exits
    return True

def set_switch_position(self, edge: str, position: int) -> bool:
    """Set switch to specific position. Returns False if invalid."""
    sp = self.switch_points
    if edge not in sp:
        return False
    if position < 0 or position >= len(sp[edge]):
        return False
    self.switch_states[edge] = position
    return True
```

### 2.8 Update Serialization

```python
def to_dict(self) -> dict:
    """Serialize to JSON-compatible dict."""
    return {
        'x': self.x,
        'y': self.y,
        'track_bits': self.track_bits,
        'dead_end_ports': list(self.dead_end_ports),
        'switch_states': self.switch_states,
        'signal_state': self.signal_state.value if self.signal_state else None,
        'signal_direction': self.signal_direction,
        'station_name': self.station_name,
        'block_section_id': self.block_section_id,
        # Computed (read-only)
        'ribi': self.ribi,
        'type': self.track_type.value,
        'edges': list(self.edges),
        'switch_points': self.switch_points,
        'num_paths': self.num_paths,
        'is_valid': self.is_valid,
    }

@classmethod
def from_dict(cls, data: dict) -> 'TrackSegment':
    """Deserialize from dict."""
    segment = cls(
        x=data['x'],
        y=data['y'],
        track_bits=data.get('track_bits', 0),
        dead_end_ports=set(data.get('dead_end_ports', [])),
        switch_states=data.get('switch_states', {}),
    )
    if data.get('signal_state'):
        segment.signal_state = SignalState(data['signal_state'])
    segment.signal_direction = data.get('signal_direction')
    segment.station_name = data.get('station_name')
    segment.block_section_id = data.get('block_section_id')
    return segment
```

### 2.9 Add Propagation System

Propagation updates derived data when track topology changes.

```python
def propagate_update(self, network: 'Network') -> None:
    """Update derived data after topology change.

    Triggers:
    - track_bits changed
    - switch_states changed
    - Neighbor tile added/removed/modified

    Updates:
    - switch_points (computed from track_bits)
    - dead_end_ports (computed from neighbor connectivity)
    - Derived properties (edges, ribi, track_type)
    """
    # Recompute switch states
    self._update_switch_states()

    # Recompute dead-ends based on neighbors
    self._update_dead_ends(network)

def _update_dead_ends(self, network: 'Network') -> None:
    """Recompute dead-ends based on neighbor connectivity."""
    self.dead_end_ports.clear()

    # For each edge with a path
    for edge, mask in EDGE_MASKS.items():
        if not (self.track_bits & mask):
            continue  # No path on this edge

        # Check if neighbor connects back
        neighbor = network.get_neighbor(self.x, self.y, edge)
        if neighbor is None:
            # No neighbor - this is a dead-end
            self.dead_end_ports.add(edge)
        else:
            # Check if neighbor has path connecting back
            opposite = OPPOSITE_EDGE[edge]
            neighbor_exits = neighbor.get_exits(opposite)
            if not neighbor_exits or edge not in [OPPOSITE_EDGE[ex] for ex in neighbor_exits]:
                # Neighbor doesn't connect back - dead-end
                self.dead_end_ports.add(edge)
```

---

## 3. Phase 2: server.py

### 3.1 Update POST /api/tracks

Accept new payload format:

```python
@app.route('/api/tracks', methods=['POST'])
def add_track():
    data = request.json
    x, y = data['x'], data['y']
    
    # New format
    track_bits = data.get('track_bits', 0)
    dead_end_ports = set(data.get('dead_end_ports', []))
    switch_states = data.get('switch_states', {})
    
    # Validate
    if track_bits in FORBIDDEN_TRACK_BITS:
        return jsonify({'error': 'Forbidden track configuration', 
                       'track_bits': track_bits}), 400
    
    segment = TrackSegment(
        x=x, y=y,
        track_bits=track_bits,
        dead_end_ports=dead_end_ports,
        switch_states=switch_states
    )
    
    # Set optional features
    if 'signal_state' in data:
        segment.signal_state = SignalState(data['signal_state'])
    if 'signal_direction' in data:
        segment.signal_direction = data['signal_direction']
    if 'station_name' in data:
        segment.station_name = data['station_name']
    
    network.add_track(segment)
    return jsonify(segment.to_dict()), 201
```

### 3.2 Update PATCH /api/tracks/<x>/<y>

```python
@app.route('/api/tracks/<int:x>/<int:y>', methods=['PATCH'])
def update_track(x, y):
    segment = network.get_track(x, y)
    if not segment:
        return jsonify({'error': 'Track not found'}), 404
    
    data = request.json
    
    # Update track_bits
    if 'track_bits' in data:
        new_bits = data['track_bits']
        if new_bits in FORBIDDEN_TRACK_BITS:
            return jsonify({'error': 'Forbidden track configuration'}), 400
        segment.track_bits = new_bits
        segment._update_switch_states()
    
    # Update dead_end_ports
    if 'dead_end_ports' in data:
        segment.dead_end_ports = set(data['dead_end_ports'])
    
    # Update switch_states
    if 'switch_states' in data:
        segment.switch_states = data['switch_states']
    
    # Toggle specific track bit
    if 'toggle_bit' in data:
        bit = data['toggle_bit']
        if not segment.toggle_track(bit):
            return jsonify({'error': 'Would create forbidden configuration'}), 400
    
    # Toggle switch
    if 'toggle_switch' in data:
        edge = data['toggle_switch']
        segment.toggle_switch(edge)
    
    return jsonify(segment.to_dict())
```

### 3.3 Update GET /api/tracks

Response now includes track_bits:

```python
@app.route('/api/tracks', methods=['GET'])
def get_tracks():
    tracks = [segment.to_dict() for segment in network.tracks.values()]
    return jsonify(tracks)
```

### 3.4 Update POST /api/tracks/bulk

```python
@app.route('/api/tracks/bulk', methods=['POST'])
def bulk_add_tracks():
    data = request.json
    tracks_data = data.get('tracks', [])
    
    results = []
    for track in tracks_data:
        x, y = track['x'], track['y']
        track_bits = track.get('track_bits', 0)
        
        if track_bits in FORBIDDEN_TRACK_BITS:
            results.append({'x': x, 'y': y, 'error': 'Forbidden configuration'})
            continue
        
        segment = network.get_track(x, y)
        if segment:
            # Merge with existing
            new_bits = segment.track_bits | track_bits
            if new_bits in FORBIDDEN_TRACK_BITS:
                results.append({'x': x, 'y': y, 'error': 'Merge would be forbidden'})
                continue
            segment.track_bits = new_bits
            segment._update_switch_states()
        else:
            segment = TrackSegment(x=x, y=y, track_bits=track_bits)
            network.add_track(segment)
        
        results.append(segment.to_dict())
    
    return jsonify(results)
```

---

## 4. Phase 3: static/js/editor.js

### 4.1 Add Track Bit Constants

```javascript
const TRACK_BITS = {
    NS: 1,   // 0b000001
    EW: 2,   // 0b000010
    NE: 4,   // 0b000100
    NW: 8,   // 0b001000
    SE: 16,  // 0b010000
    SW: 32   // 0b100000
};

// Forbidden configurations (23 total)
const FORBIDDEN_TRACK_BITS = new Set([
    13, 22, 42, 49,           // 3-way switches (4)
    15, 23, 43, 51,           // Double slips (4)
    29, 30, 45, 46, 53, 54, 57, 58,  // Complex 4-path (8)
    31, 47, 55, 59, 61, 62,   // 5-path (6)
    63                         // 6-path (1)
]);
```

### 4.2 Update Ribi Editor Canvas

Instead of toggling edges, toggle track bits:

```javascript
function drawRibiEditor(canvas, trackBits, deadEndPorts) {
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    
    ctx.clearRect(0, 0, w, h);
    
    // Draw background
    ctx.fillStyle = '#333';
    ctx.fillRect(0, 0, w, h);
    
    // Draw each possible track segment
    const segments = [
        { bit: 1, name: 'NS', from: [cx, 0], to: [cx, h] },
        { bit: 2, name: 'EW', from: [0, cy], to: [w, cy] },
        { bit: 4, name: 'NE', from: [cx, 0], to: [w, cy] },
        { bit: 8, name: 'NW', from: [cx, 0], to: [0, cy] },
        { bit: 16, name: 'SE', from: [cx, h], to: [w, cy] },
        { bit: 32, name: 'SW', from: [cx, h], to: [0, cy] }
    ];
    
    for (const seg of segments) {
        const active = (trackBits & seg.bit) !== 0;
        ctx.strokeStyle = active ? '#00ff00' : '#666';
        ctx.lineWidth = active ? 4 : 2;
        ctx.beginPath();
        ctx.moveTo(seg.from[0], seg.from[1]);
        ctx.lineTo(seg.to[0], seg.to[1]);
        ctx.stroke();
    }
    
    // Draw dead-end markers
    const edgePositions = {
        'N': [cx, 10], 'E': [w-10, cy], 'S': [cx, h-10], 'W': [10, cy]
    };
    for (const port of deadEndPorts) {
        const [x, y] = edgePositions[port];
        ctx.fillStyle = '#ff0000';
        ctx.beginPath();
        ctx.arc(x, y, 6, 0, Math.PI * 2);
        ctx.fill();
    }
}
```

### 4.3 Update Click Handler

```javascript
function handleRibiEditorClick(e, canvas, track) {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    
    // Determine which track bit was clicked
    // Based on position relative to center
    let clickedBit = null;
    
    // Check click regions for each track
    if (isNearLine([cx, 0], [cx, canvas.height], x, y)) clickedBit = 1;  // NS
    if (isNearLine([0, cy], [canvas.width, cy], x, y)) clickedBit = 2;   // EW
    if (isNearLine([cx, 0], [canvas.width, cy], x, y)) clickedBit = 4;   // NE
    if (isNearLine([cx, 0], [0, cy], x, y)) clickedBit = 8;              // NW
    if (isNearLine([cx, canvas.height], [canvas.width, cy], x, y)) clickedBit = 16; // SE
    if (isNearLine([cx, canvas.height], [0, cy], x, y)) clickedBit = 32; // SW
    
    if (clickedBit !== null) {
        const newBits = track.track_bits ^ clickedBit;  // XOR to toggle
        
        if (FORBIDDEN_TRACK_BITS.has(newBits)) {
            showError('This configuration is not allowed (would create 3-way switch)');
            return;
        }
        
        // Update via API
        api.updateTrack(track.x, track.y, { track_bits: newBits });
    }
}
```

### 4.4 Update Properties Panel

```javascript
function updatePropertiesPanel(track) {
    const panel = document.getElementById('properties-panel');
    
    panel.innerHTML = `
        <h3>Track (${track.x}, ${track.y})</h3>
        <p><strong>Type:</strong> ${track.type}</p>
        <p><strong>Track Bits:</strong> ${track.track_bits} (0b${track.track_bits.toString(2).padStart(6, '0')})</p>
        <p><strong>RIBI:</strong> ${track.ribi}</p>
        <p><strong>Paths:</strong> ${track.num_paths}</p>
        <p><strong>Edges:</strong> ${track.edges.join(', ')}</p>
        ${track.dead_end_ports.length ? `<p><strong>Dead Ends:</strong> ${track.dead_end_ports.join(', ')}</p>` : ''}
        ${Object.keys(track.switch_points).length ? `
            <p><strong>Switches:</strong></p>
            <ul>
                ${Object.entries(track.switch_points).map(([edge, exits]) => 
                    `<li>${edge} → [${exits.join(', ')}] (active: ${track.switch_states[edge] || 0})</li>`
                ).join('')}
            </ul>
        ` : ''}
    `;
}
```

---

## 5. Phase 4: demo_main.py

### 5.1 Update Track Creation Functions

```python
def create_demo_network():
    network = Network()
    
    # Dead-end at (0,0) facing East
    network.add_track(TrackSegment(
        x=0, y=0,
        track_bits=0,
        dead_end_ports={'E'}
    ))
    
    # Horizontal straight (E-W) from (1,0) to (9,0)
    for x in range(1, 10):
        if x == 3:
            continue  # Junction at (3,0)
        network.add_track(TrackSegment(
            x=x, y=0,
            track_bits=TRACK_EW  # 2
        ))
    
    # Junction at (3,0) - E-W + N-W (switch at W)
    network.add_track(TrackSegment(
        x=3, y=0,
        track_bits=TRACK_EW | TRACK_NW,  # 2 + 8 = 10
        switch_states={'W': 0}  # Main route: W→E
    ))
    
    # Vertical branch (N-S) from (3,1) to (3,2)
    for y in range(1, 3):
        network.add_track(TrackSegment(
            x=3, y=y,
            track_bits=TRACK_NS  # 1
        ))
    
    # Dead-end at (3,3) facing South
    network.add_track(TrackSegment(
        x=3, y=3,
        track_bits=0,
        dead_end_ports={'S'}
    ))
    
    # Dead-end at (10,0) facing West
    network.add_track(TrackSegment(
        x=10, y=0,
        track_bits=0,
        dead_end_ports={'W'}
    ))
    
    # Add signals
    network.get_track(1, 0).signal_state = SignalState.GREEN
    network.get_track(1, 0).signal_direction = 'E'
    
    network.get_track(4, 0).signal_state = SignalState.GREEN
    network.get_track(4, 0).signal_direction = 'W'
    
    # Add stations
    network.get_track(5, 0).station_name = 'Main'
    network.get_track(3, 3).station_name = 'Branch'
    
    return network
```

### 5.2 Helper Constants

```python
# For demo convenience
def straight_ew():
    return TRACK_EW  # 2

def straight_ns():
    return TRACK_NS  # 1

def curve_ne():
    return TRACK_NE  # 4

def curve_nw():
    return TRACK_NW  # 8

def curve_se():
    return TRACK_SE  # 16

def curve_sw():
    return TRACK_SW  # 32

def switch_w_to_e_or_n():
    """W splits to E (main) or N (diverge)"""
    return TRACK_EW | TRACK_NW  # 10

def switch_n_to_s_or_e():
    """N splits to S (main) or E (diverge)"""
    return TRACK_NS | TRACK_NE  # 5
```

---

## 5. Phase 5: network.py - Propagation System

### 5.1 Add Neighbor Lookup

```python
def get_neighbor(self, x: int, y: int, edge: str) -> Optional[TrackSegment]:
    """Get neighbor tile in the specified direction."""
    offsets = {
        'N': (0, -1),
        'E': (1, 0),
        'S': (0, 1),
        'W': (-1, 0)
    }

    if edge not in offsets:
        return None

    dx, dy = offsets[edge]
    return self.get_track(x + dx, y + dy)
```

### 5.2 Add Propagation Trigger

```python
def propagate_from_tile(self, x: int, y: int) -> None:
    """Propagate updates from a tile to its neighbors (DNS-style wave).

    This is called when:
    - A tile's track_bits changes
    - A tile's switch_states changes
    - A tile is added or removed
    """
    queue = [(x, y)]
    visited = set()

    while queue:
        tx, ty = queue.pop(0)
        if (tx, ty) in visited:
            continue
        visited.add((tx, ty))

        tile = self.get_track(tx, ty)
        if not tile:
            continue

        # Update this tile's derived data
        tile.propagate_update(self)

        # Enqueue neighbors
        for edge in ['N', 'E', 'S', 'W']:
            neighbor = self.get_neighbor(tx, ty, edge)
            if neighbor and (neighbor.x, neighbor.y) not in visited:
                queue.append((neighbor.x, neighbor.y))
```

### 5.3 Call Propagation on Changes

Update existing methods:

```python
def add_track(self, segment: TrackSegment) -> None:
    """Add track segment and propagate updates."""
    key = (segment.x, segment.y)
    self.tracks[key] = segment
    self.propagate_from_tile(segment.x, segment.y)

def remove_track(self, x: int, y: int) -> None:
    """Remove track segment and propagate updates."""
    key = (x, y)
    if key in self.tracks:
        del self.tracks[key]
        # Propagate to neighbors since this tile is now gone
        for edge in ['N', 'E', 'S', 'W']:
            offsets = {'N': (0, -1), 'E': (1, 0), 'S': (0, 1), 'W': (-1, 0)}
            dx, dy = offsets[edge]
            neighbor_key = (x + dx, y + dy)
            if neighbor_key in self.tracks:
                self.propagate_from_tile(x + dx, y + dy)
```

---

## 6. Testing Checklist

### 6.1 Track Creation
- [ ] Create empty track (track_bits=0)
- [ ] Create single path tracks (1, 2, 4, 8, 16, 32)
- [ ] Create diamond crossings (3, 24, 36)
- [ ] Create Y-switches (5, 6, 9, 10, 12, 17, 18, 20, 33, 34, 40, 48)
- [ ] Create WYE junctions (14, 21, 41, 50)
- [ ] Create complex junctions (7, 11, 19, 25, etc.)
- [ ] Create scissors (27, 39)
- [ ] Create double cross (60)

### 6.1.5 Complex 3-Path Junctions (VALID - 12 configs)
- [ ] Create complex junction (7) - NS+EW+NE
- [ ] Create complex junction (11) - NS+EW+NW
- [ ] Create complex junction (19) - NS+EW+SE
- [ ] Create complex junction (25) - NS+NW+SE
- [ ] Create complex junction (26) - EW+NW+SE
- [ ] Create complex junction (28) - NE+NW+SE
- [ ] Create complex junction (35) - NS+EW+SW
- [ ] Create complex junction (37) - NS+NE+SW
- [ ] Create complex junction (38) - EW+NE+SW
- [ ] Create complex junction (44) - NE+NW+SW
- [ ] Create complex junction (52) - NE+SE+SW
- [ ] Create complex junction (56) - NW+SE+SW
- [ ] Verify all have exactly 2 switch points
- [ ] Verify no edge has 3 exits

### 6.1.6 Double Cross (VALID - config 60)
- [ ] Create double cross (60) - NE+NW+SE+SW
- [ ] Verify all 4 edges have exactly 2 exits
- [ ] Verify switches work at N, E, S, W edges

### 6.2 Forbidden Validation
- [ ] Reject 3-way switch (13, 22, 42, 49)
- [ ] Reject double slip (15, 23, 43, 51)
- [ ] Reject complex 4-path with 3-way (29, 30, etc.)
- [ ] Reject 5-path configurations (31, 47, etc.)
- [ ] Reject full junction (63)
- [ ] Show user-friendly error message

### 6.3 Dead-Ends
- [ ] Create track with dead-end only
- [ ] Add dead-end to existing track
- [ ] Validate dead-end cannot be on edge with path
- [ ] RIBI computed correctly with dead-ends

### 6.4 Switches
- [ ] Switch points auto-detected
- [ ] switch_states initialized for new switches
- [ ] switch_states cleaned up when paths removed
- [ ] Toggle switch changes active exit
- [ ] get_active_exit returns correct exit

### 6.5 API
- [ ] POST /api/tracks creates track with track_bits
- [ ] GET /api/tracks returns track_bits
- [ ] PATCH /api/tracks updates track_bits
- [ ] PATCH /api/tracks/toggle_bit toggles single bit
- [ ] Bulk endpoint merges track_bits correctly

### 6.6 Editor
- [ ] Ribi editor displays all 6 track segments
- [ ] Click toggles correct bit
- [ ] Forbidden configurations blocked with feedback
- [ ] Properties panel shows track_bits, type, switches

### 6.7 Demo
- [ ] Demo network creates successfully
- [ ] All tracks have correct track_bits
- [ ] Switches work (can toggle)
- [ ] Trains can traverse network
- [ ] Signals and stations functional

---

## 7. Rollback Plan

If critical issues are found:

### 7.1 Git Revert
```bash
git checkout HEAD~1 -- track.py server.py static/js/editor.js demo_main.py
```

### 7.2 Keep Reference Doc
The `Reference_6Bit_Track_System.md` document is documentation-only and can remain even if code is reverted.

### 7.3 Partial Rollback
If only specific phases fail:
- Phase 1 (track.py): Core issue - full rollback needed
- Phase 2 (server.py): Can keep track.py, revert API
- Phase 3 (editor.js): Can keep backend, revert frontend
- Phase 4 (demo_main.py): Can update demo separately

---

## 8. Migration Order

1. **track.py** - Core data model (must be first)
2. **server.py** - API changes (depends on track.py)
3. **demo_main.py** - Demo updates (can test with API)
4. **editor.js** - Frontend (last, can use API directly)

Each phase should be tested before proceeding to the next.
