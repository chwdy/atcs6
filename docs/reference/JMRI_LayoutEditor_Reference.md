# JMRI Layout Editor — Technical Reference

**Source**: JMRI open-source project (`jmri/jmrit/display/layoutEditor/`)
**Version**: JMRI 5.x (analyzed from master branch, June 2026)
**Purpose**: Technical reference for understanding JMRI's track-geometry editor, its data model, rendering pipeline, hardware integration, and persistence format.

---

## 1. Overview

The Layout Editor is a **track-geometry editor** that serves dual purpose:

1. **Design tool** — draw the physical track arrangement of a model railroad layout, define blocks, assign signals and sensors
2. **Live control panel** — in operation mode, click turnouts to command them; block colors update in real time as trains move

It is distinct from PanelEditor: instead of free-placed icons, it uses **geometric track objects** with enforced connectivity. The connections between track objects form a traversable graph used by the Dispatcher, signal setup tools, and block routing.

---

## 2. Relationship to Other Editors

```
Editor (abstract base — jmri/jmrit/display/Editor.java)
  └── PanelEditor (free-form icon canvas)
        └── LayoutEditor (track-geometry aware, adds topology enforcement)
```

LayoutEditor inherits PanelEditor's icon-placement capability (sensors, labels, memory icons can still be placed freely) but adds a parallel layer of track objects with strict geometric connectivity rules.

---

## 3. Core Data Model

### 3.1 Two Parallel Hierarchies

The model is split into **topology** (what connects to what) and **view** (how it looks):

```
LayoutTrack          ←→    LayoutTrackView
(connectivity/topology)     (geometry/rendering)
        │                          │
   stored as                  stored as
  LayoutTrack                LayoutTrackView
  subclass                   subclass (parallel)
```

This is an explicit MVC separation: topology logic never contains pixel coordinates; view logic never contains connectivity logic.

### 3.2 LayoutTrack Subclasses (Topology Layer)

| Class | Description | Connection Points |
|-------|-------------|-------------------|
| `TrackSegment` | Straight or curved track between two nodes | connect1, connect2 |
| `LayoutTurnout` | Base for all turnout types | A (throat), B (straight), C (diverging) |
| `LayoutRHTurnout` | Right-hand turnout | A, B, C |
| `LayoutLHTurnout` | Left-hand turnout | A, B, C |
| `LayoutWye` | Wye turnout | A, B, C |
| `LayoutDoubleXOver` | Double crossover | A, B, C, D |
| `LayoutXOver` | Single crossover | A, B, C, D |
| `LayoutSlip` | Single or double slip | A, B, C, D |
| `PositionablePoint` | Anchor node, end bumper, or edge connector | up to 2 |
| `LevelXing` | Two tracks crossing at grade | A, B, C, D |
| `LayoutTurntable` | Turntable with up to 64 track rays | CENTER + RAY_0…RAY_63 |
| `LayoutTraverser` | Linear transfer table with up to 64 slots | CENTER + SLOT_0…SLOT_63 |

### 3.3 LayoutTrackView Subclasses (View Layer)

Each `LayoutTrack` subclass has a corresponding `LayoutTrackView` subclass (e.g., `TrackSegmentView`, `LayoutTurnoutView`). The view stores:
- Center position (`Point2D`)
- Displacement vectors for connection point offsets
- Rotation angle
- Colors, line widths, dashed/solid style
- Bezier curve control points (up to 9, for curved TrackSegments)
- Decoration flags (arrows, tunnels, bridges, bumpers)
- Hidden/selected status

### 3.4 LayoutBlock (Block Abstraction)

`LayoutBlock` bridges the Layout Editor to the JMRI core block system:

```
LayoutBlock
  ├── name (user-defined, unique per layout)
  ├── Block bean (jmri.Block) — core JMRI circuit object
  ├── Sensor bean (optional) — occupancy detector
  ├── Memory bean (optional) — stores train name for display
  ├── blockTrackColor — color when unoccupied
  ├── blockOccupiedColor — color when occupied
  ├── blockExtraColor — color for alternate state
  └── useCount — how many track objects reference this block
```

Multiple TrackSegments and LayoutTurnout legs can share one LayoutBlock, representing a contiguous electrical circuit.

### 3.5 PositionablePoint Types

| Type | Purpose |
|------|---------|
| `ANCHOR` | Mid-track node connecting two TrackSegments |
| `END_BUMPER` | Track endpoint (dead end) |
| `EDGE_CONNECTOR` | Links to a matching point on a different Layout Editor panel |

### 3.6 The Connectivity Graph

The layout is represented as a **typed directed graph**:

- **Vertices** = `LayoutTrack` objects
- **Edges** = connections, each encoded as `(LayoutTrack reference, HitPointType endpoint)`

Example: a TrackSegment stores `connect1 = turnoutA, connect1Type = TURNOUT_B` meaning "my start connects to the B leg of turnoutA."

---

## 4. HitPointType — Connection Point Taxonomy

`HitPointType` is an enum with ~178 values that identifies exactly which point on a track object is being referenced, either for click detection or for storing connectivity.

### Common Values by Category

**General**

| Value | Meaning |
|-------|---------|
| `NONE` | No hit / unset |
| `TRACK` | Body of a TrackSegment |
| `POS_POINT` | A PositionablePoint node |

**Turnout Endpoints** (RH, LH, Wye turnouts)

| Value | Meaning |
|-------|---------|
| `TURNOUT_A` | Throat — the single-track entry end |
| `TURNOUT_B` | Continuing (straight) route |
| `TURNOUT_C` | Diverging route |
| `TURNOUT_D` | 4th route — crossovers only |
| `TURNOUT_CENTER` | Body center — click to toggle / show popup |

**Level Crossing**

| Value | Meaning |
|-------|---------|
| `LEVEL_XING_A/B/C/D` | Four endpoints (two crossing tracks) |
| `LEVEL_XING_CENTER` | Center body |

**Slip Switch**

| Value | Meaning |
|-------|---------|
| `SLIP_A/B/C/D` | Four track endpoints |
| `SLIP_LEFT / SLIP_RIGHT` | Route selectors |
| `SLIP_CENTER` | Body center |

**Turntable** (indexed range)

| Value | Meaning |
|-------|---------|
| `TURNTABLE_CENTER` | Turntable body center |
| `TURNTABLE_RAY_0` … `TURNTABLE_RAY_63` | Individual track stall connections (up to 64) |

**Traverser** (indexed range)

| Value | Meaning |
|-------|---------|
| `TRAVERSER_CENTER` | Traverser body center |
| `TRAVERSER_SLOT_0` … `TRAVERSER_SLOT_63` | Individual track slots (up to 64) |

**Bezier Control Points** (indexed range)

| Value | Meaning |
|-------|---------|
| `BEZIER_CONTROL_POINT_0` … `BEZIER_CONTROL_POINT_8` | Drag handles for curved TrackSegments (up to 9) |

**Decorative Shapes**

| Value | Meaning |
|-------|---------|
| `SHAPE_CENTER` | Center of a drawn shape |
| `SHAPE_POINT_0` … `SHAPE_POINT_9` | Corner/edge control points (up to 10) |

**UI Icons (placed on canvas)**

| Value | Meaning |
|-------|---------|
| `LAYOUT_POS_LABEL` | Text label |
| `LAYOUT_POS_JCOMP` | Swing component (sensor icon, turnout icon, etc.) |
| `MULTI_SENSOR` | Multi-sensor icon |
| `MARKER` | Train marker icon |
| `TRACK_CIRCLE_CENTRE` | Circle center for circular track display |

> **For JMRI XML export**: the connectivity-critical values are `TURNOUT_A/B/C/D`, `LEVEL_XING_A/B/C/D`, `SLIP_A/B/C/D`, `POS_POINT`, and `TRACK`. These are stored as `connect1type` / `connect2type` attributes and must be correct for the Dispatcher and signal tools to work.

---

## 5. UI Architecture

### 5.1 Window Structure

```
LayoutEditor (JFrame, extends PanelEditor)
├── MenuBar
│   ├── File (store/delete panel)
│   ├── Options (edit mode, grid, zoom, tooltips, track style)
│   └── Tools (signal setup, block assignment, dispatcher launch)
├── LayoutEditorToolBarPanel (top / left / bottom / right / floating)
│   ├── Track type radio buttons (turnout type, segment, level xing, turntable, traverser)
│   ├── Turnout name ComboBox
│   ├── Block ID ComboBox
│   └── Icon placement controls (sensor, turnout icon, signal head/mast, memory)
├── LayoutEditorComponent (JComponent — main canvas)
│   └── _targetPanel (JLayeredPane — icons, labels, shapes, track objects)
└── Help Bar (optional, context-sensitive)
```

### 5.2 Toolbar Variants

| Class | Layout |
|-------|--------|
| `LayoutEditorHorizontalToolBarPanel` | Horizontal bar (top/bottom) |
| `LayoutEditorVerticalToolBarPanel` | Vertical bar (left/right) |
| `LayoutEditorFloatingToolBarPanel` | Detached floating palette |

### 5.3 Edit Mode vs. Operation Mode

| Mode | Behavior |
|------|----------|
| **Edit** | Click and drag to place/move track objects; grid snapping active; control points visible |
| **Operation** | Click turnouts to command them; block colors show occupancy; editing disabled |

Toggle via Options menu or keyboard shortcut.

---

## 6. Rendering Pipeline

The canvas renders in a fixed order (back to front):

1. Grid lines (edit mode only)
2. Dashed track segments
3. Track ballast (fill between rails)
4. Track ties
5. Track rails
6. Block boundary indicators
7. PositionablePoints (non-selected)
8. PositionablePoints (selected)
9. Shapes (background layer)
10. Track decorations (arrows, tunnel portals, bridges, bumpers)
11. Shape foreground layer
12. Edit control points (edit mode only)
13. Selection rectangle (drag select)

Each `LayoutTrackView` subclass implements its own drawing method, called by `LayoutEditorComponent.paintComponent()`.

---

## 7. Hardware Integration

### 7.1 Bean System

Hardware interaction is mediated by JMRI **beans** — named interface objects with system-specific implementations:

| Bean Interface | Represents | System example |
|----------------|-----------|----------------|
| `jmri.Turnout` | Track switch motor / decoder | `NceTurnout`, `LocoNetTurnout` |
| `jmri.Sensor` | Occupancy detector / input | `NceSensor`, `LocoNetSensor` |
| `jmri.SignalHead` | Signal light | `SE8cSignalHead` |
| `jmri.SignalMast` | Multi-aspect signal mast | `DccSignalMast` |
| `jmri.Block` | Electrical detection circuit | `DefaultBlock` |

### 7.2 Sending a Command: Click → DCC Packet

```
User clicks turnout on Layout Editor canvas
  │
  ▼
LayoutEditor.mouseReleased()
  └── hit detection → HitPointType.TURNOUT_CENTER
  │
  ▼
LayoutTurnout.toggleTurnout()
  └── getTurnout().setCommandedState(THROWN or CLOSED)
  │
  ▼
Turnout bean fires PropertyChangeEvent(PROPERTY_COMMANDED_STATE)
  │
  ▼
System-specific Turnout.forwardCommandChangeToLayout()
  └── e.g. NceTurnout → constructs NMRA DCC accessory packet
      → sends via NceTrafficController over serial/USB
      → command station broadcasts on DCC bus
      → turnout decoder executes
```

### 7.3 Receiving Occupancy: Sensor → Block Color

```
Physical occupancy detector activates
  │
  ▼
System manager receives hardware message
  └── e.g. NceSensorManager.handleSensorMessage()
      → NceAIU.sensorChange() → sensor.setKnownState(ACTIVE)
  │
  ▼
Sensor bean fires PropertyChangeEvent
  │
  ▼
LayoutBlock.handleBlockChange()
  └── calls redrawLayoutBlockPanels()
  │
  ▼
LayoutEditor.redrawPanel()
  └── LayoutBlock.getBlockColor()
      → if getOccupancy() == OCCUPIED: return blockOccupiedColor
      → else: return blockTrackColor
  │
  ▼
Canvas repaints — block segments render in occupied color
```

### 7.4 Bean References: NamedBeanHandle

All bean references are stored as `NamedBeanHandle<T>`, not direct object references. This allows beans to be renamed without breaking panel references.

```
LayoutTurnout stores:
  NamedBeanHandle<Turnout> namedTurnout
    → manager.getNamedBeanHandle("IT42", turnoutObject)
```

---

## 8. Persistence: XML Format

### 8.1 Serializer Classes

| Serializer | Handles |
|-----------|---------|
| `LayoutEditorXml` | Root panel element, window/panel attributes |
| `TrackSegmentViewXml` | TrackSegment topology + view |
| `LayoutTurnoutViewXml` (and subclasses) | Turnout topology + view |
| `PositionablePointViewXml` | Anchor/bumper/edge connector |
| `LevelXingViewXml` | Level crossing |
| `LayoutTurntableViewXml` | Turntable + rays |
| `LayoutTraverserViewXml` | Traverser + slots |
| `LayoutBlockManagerXml` | All LayoutBlocks (saved separately, cross-panel) |

### 8.2 XML Structure

```xml
<LayoutEditor name="Main Panel" x="0" y="0" width="1200" height="800"
              editable="yes" positionable="yes" controlling="yes"
              animating="yes" showgrid="no" gridSize="10" ...>

  <!-- Track objects (topology + view fused) -->
  <layoutturnout ident="TO1" type="RH" turnoutname="IT42"
                 xcen="240" ycen="180" xdisB="..." ydisB="..."
                 blockname="BLOCK-1" blockbname="BLOCK-2" blockdname="BLOCK-2"
                 connectaname="TS3" connectatype="TRACK"
                 connectbname="TS4" connectbtype="TRACK"
                 connectcname="TS5" connectctype="TRACK"
                 signalaname="" signalbname="" signalcname="" ... />

  <tracksegment ident="TS3"
                connect1name="TO1" type1="TURNOUT_A"
                connect2name="P1"  type2="POS_POINT"
                blockname="BLOCK-1" mainline="yes" arc="no" ... />

  <positionablepoint ident="P1" type="ANCHOR"
                     x="120" y="180"
                     connect1name="TS3" connect1type="TRACK"
                     connect2name="TS6" connect2type="TRACK" />

  <!-- Freely placed icons (inherited from PanelEditor) -->
  <sensoricon sensor="IS01" x="300" y="50" level="10" ... />
  <memoryicon memory="IM01" x="400" y="50" level="5" ... />

</LayoutEditor>
```

### 8.3 Save/Load Order

**Store** (all track objects saved in this sequence):
1. LayoutTurnouts (except LayoutSlips)
2. TrackSegments
3. PositionablePoints
4. LevelXings
5. LayoutSlips
6. LayoutTurntables
7. LayoutTraversers
8. LayoutShapes
9. All Positionable icons (inherited from PanelEditor)

**LayoutBlocks** are saved by `LayoutBlockManagerXml` in a **separate section** of the config file (they are system-wide resources, not panel-specific).

---

## 9. Interaction with Other JMRI Subsystems

### 9.1 Manager Dependencies

| Manager | Used for |
|---------|---------|
| `EditorManager` | Registration and lifecycle of all editor panels |
| `LayoutBlockManager` | Creates and manages LayoutBlock objects |
| `TurnoutManager` | Resolves turnout names to bean objects |
| `SensorManager` | Resolves sensor names to bean objects |
| `SignalHeadManager` / `SignalMastManager` | Signal assignment |
| `BlockManager` | Core JMRI block beans |
| `MemoryManager` | Memory beans for train name display |
| `TransitManager` | Transit creation from layout connectivity |

### 9.2 Dispatcher Integration

The Dispatcher uses Layout Editor data for:
- **Turnout routing** — `AutoTurnouts` queries `LayoutBlockConnectivityTools` to find which turnouts affect a Section and what position they need
- **Dynamic transit creation** — Dispatcher can auto-build a Transit between two blocks using `LayoutBlockConnectivityTools.getLayoutBlocks()`
- **Connectivity validation** — `ConnectivityUtil` validates that a train path is contiguous

### 9.3 Signal Setup Tools

The **Set Signals** tools (in the Tools menu) use layout connectivity to:
- Find which blocks adjoin a turnout
- Determine the facing/protecting block relationship
- Auto-assign signal heads/masts to the correct legs
- Configure signal logic based on block boundaries

### 9.4 Cross-Panel Edge Connectors

A `PositionablePoint` of type `EDGE_CONNECTOR` can be linked to a matching `PositionablePoint` on a different Layout Editor panel. This allows representing a large layout across multiple panels while maintaining connectivity for routing.

---

## 10. Auxiliary Tools (Tools Menu)

| Tool | Function |
|------|---------|
| Set Signals at Turnout | Auto-configures signal logic at a turnout |
| Set Signals at Level Crossing | Signal logic at crossings |
| Set Signals at Block Boundary | Signal logic at block entry/exit points |
| Block Assignment | Assigns blocks to unassigned track segments |
| Layout Block Connectivity | Visualizes/validates block routing graph |
| Transit Creation | Builds Transits from layout connectivity for the Dispatcher |
| Launch Dispatcher | Opens DispatcherFrame |

---

## 11. Design Patterns

| Pattern | Where Used |
|---------|-----------|
| MVC | `LayoutTrack` (model) / `LayoutTrackView` (view) / `LayoutEditor` (controller) |
| Abstract Base + Subclasses | `LayoutTrack` → 7+ subclasses; `LayoutTrackView` → matching subclasses |
| Observer / PropertyChange | All bean-to-view updates; LayoutBlock fires REDRAW / ROUTING / OCCUPANCY events |
| Named Bean Handle | `NamedBeanHandle<T>` wraps all bean references for rename safety |
| Repository / Manager | `LayoutModels` interface provides collection access |
| Factory (via XML) | `ConfigXmlManager` uses reflection to instantiate serializers |
| Lazy Initialization | `LayoutBlockConnectivityTools` builds connectivity graph on first access |

---

## 12. Standalone Feasibility

| Layer | Independence |
|-------|-------------|
| `LayoutTrack` topology model | High — pure connectivity graph, no JMRI UI dependencies |
| `LayoutTrackView` rendering | Medium — uses JMRI color/prefs utilities but separable |
| XML persistence | Medium — uses JDOM2 (standard library), JMRI config manager hooks |
| `LayoutEditor` UI | Low — tightly coupled to `Editor` base, `InstanceManager`, all JMRI managers |

To extract just the data model + rendering for a new project, the minimum required interfaces are: `NamedBean`, `InstanceManager`, `LayoutBlock`, and the `LayoutTrack`/`LayoutTrackView` hierarchies.

---

## 13. Source File Map

| File | Responsibility |
|------|----------------|
| `LayoutEditor.java` | Main class (~5000 LOC): window, toolbar, mouse handling, rendering dispatch |
| `LayoutEditorAction.java` | Menu action to open a new Layout Editor |
| `LayoutTrack.java` | Abstract base for all topology objects |
| `LayoutTrackView.java` | Abstract base for all view objects |
| `TrackSegment.java` / `TrackSegmentView.java` | Straight/curved track segment |
| `LayoutTurnout.java` / `LayoutTurnoutView.java` | Turnout topology/view base |
| `PositionablePoint.java` / `PositionablePointView.java` | Anchor/bumper/edge connector |
| `LevelXing.java` / `LevelXingView.java` | Grade crossing |
| `LayoutSlip.java` / `LayoutSlipView.java` | Slip switch |
| `LayoutTurntable.java` / `LayoutTurntableView.java` | Turntable |
| `LayoutTraverser.java` / `LayoutTraverserView.java` | Transfer table |
| `LayoutBlock.java` | Block abstraction with occupancy and routing |
| `LayoutBlockManager.java` | Manager for all LayoutBlocks |
| `LayoutBlockConnectivityTools.java` | Path-finding between blocks |
| `HitPointType.java` | Enum of all connection/click point types (~178 values) |
| `LayoutEditorToolBarPanel.java` | Toolbar base class |
| `LayoutModels.java` | Interface for model collection access |
| `LayoutEditorAuxTools.java` | Block connectivity analysis helpers |
| `LayoutEditorFindItems.java` | Spatial search (hit detection) |
| `LayoutEditorTools.java` | Editing utility methods |
| `configurexml/LayoutEditorXml.java` | Root XML serializer |
| `configurexml/*ViewXml.java` | Per-type XML serializers |

---

*Document generated from JMRI source analysis, June 2026.*
*JMRI is licensed under GNU GPL v2. See https://www.jmri.org*
