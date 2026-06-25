# JMRI Panel Editor — Technical Reference

**Source**: JMRI open-source project (`jmri/jmrit/display/`)
**Version**: JMRI 5.x (analyzed from master branch, June 2026)
**Purpose**: Technical reference for understanding JMRI's icon-based panel editor system, including the Editor base class, all positionable types, bean binding, persistence, and the ControlPanelEditor variant.

---

## 1. Overview

The Panel Editor system is JMRI's **free-form control panel builder**. Users place icons representing hardware objects (turnouts, sensors, signals, memory values) onto a blank canvas. Icons both display current hardware state and — where applicable — send commands when clicked.

Unlike the Layout Editor, Panel Editor has **no track geometry or connectivity enforcement**. It is a pure icon canvas: each icon is independently positioned and linked to a JMRI bean by name.

### Three Editor Variants

| Editor | Class | Use Case |
|--------|-------|---------|
| **PanelEditor** | `jmri.jmrit.display.panelEditor.PanelEditor` | Simple panels, quick control displays |
| **ControlPanelEditor** | `jmri.jmrit.display.controlPanelEditor.ControlPanelEditor` | Complex panels with circuit builder and shape drawing |
| **LayoutEditor** | `jmri.jmrit.display.layoutEditor.LayoutEditor` | Track-geometry diagram (separate document) |

All three extend the same `Editor` base class.

---

## 2. Class Hierarchy

```
Editor (abstract — jmri/jmrit/display/Editor.java)
  ├── PanelEditor
  └── ControlPanelEditor
        └── (includes CircuitBuilder, ShapeDrawer, PortalIcon support)

Positionable (interface — jmri/jmrit/display/Positionable.java)
  └── Implemented by all icons placed on a panel
```

---

## 3. Editor Base Class (`Editor.java`)

`Editor` owns all shared state and behavior across all panel editor types.

### 3.1 Core State

| Field | Type | Purpose |
|-------|------|---------|
| `_contents` | `List<Positionable>` | All positionable objects on the panel |
| `_idContents` | `HashMap<String, Positionable>` | Objects keyed by their ID attribute |
| `_classContents` | `HashMap<String, List<Positionable>>` | Objects grouped by CSS class name |
| `_targetPanel` | `JLayeredPane` | The visual canvas surface |
| `_paintScale` | `double` | Current zoom factor |
| `_selectionGroup` | `List<Positionable>` | Currently selected objects (multi-select) |
| `_currentSelection` | `Positionable` | Single selected object |

### 3.2 Global Flags

These flags control behavior for all objects on the panel:

| Flag | Constant | Effect when true |
|------|----------|-----------------|
| Editable | `OPTION_EDITABLE` | Objects can be moved, resized, deleted |
| Positionable | `OPTION_POSITION` | Objects can be repositioned |
| Controlling | `OPTION_CONTROLS` | Click events send commands to hardware |
| Show Hidden | `OPTION_HIDE` | Hidden objects become visible |
| Show Tooltips | `OPTION_TOOLTIP` | Tooltips appear on hover |

Individual objects can override these flags locally.

### 3.3 Display Levels (Z-Order)

Objects are placed in a `JLayeredPane` at one of 10 display levels. **Higher numeric level = further behind** (JLayeredPane convention):

| Constant | Value | Typical Use |
|----------|-------|-------------|
| `BKG` | 1 | Background images (frontmost) |
| `TEMP` | 2 | Temporary objects |
| `ICONS` | 3 | Generic icons |
| `LABELS` | 4 | Text labels |
| `MEMORIES` / `REPORTERS` | 5 | Memory/Reporter displays |
| `SECURITY` | 6 | Security-related icons |
| `TURNOUTS` | 7 | Turnout icons |
| `LIGHTS` | 8 | Light icons |
| `SIGNALS` | 9 | Signal icons |
| `SENSORS` / `CLOCK` / `MARKERS` | 10 | Sensors, clock, markers (furthest back) |

> Note: "BKG=1" appears **in front of** "SENSORS=10" — lower number = closer to the viewer.

### 3.4 Key Methods

| Method | Purpose |
|--------|---------|
| `putItem(Positionable)` | Add a positionable to the panel (contents list + visual layer) |
| `addToTarget(Positionable)` | Place object in the JLayeredPane at its display level |
| `removeFromContents(Positionable)` | Remove object from panel |
| `getContents()` | Return all positionables |
| `setFlag(int option, boolean set)` | Set a global flag |
| `getFlag(int option, boolean localFlag)` | Resolve effective flag (global vs. local) |
| `mousePressed/Released/Clicked/Dragged` | Dispatch mouse events to appropriate positionable |
| `highLight(Positionable, Color)` | Draw selection highlight border |
| `copyItem(Positionable)` | Deep-clone a positionable for copy-paste |

---

## 4. The Positionable Interface

`Positionable` is the contract every icon must fulfill to live on a panel.

### 4.1 Core Properties

| Property | Methods | Description |
|----------|---------|-------------|
| Position | `getX()`, `getY()`, `setLocation(x,y)` | Canvas coordinates |
| Size | `maxWidth()`, `maxHeight()` | Bounding box |
| Visibility | `isVisible()`, `setVisible(bool)` | Show/hide |
| Hidden | `isHidden()`, `setHidden(bool)` | Hidden from non-edit views |
| Editable | `isEditable()`, `setEditable(bool)` | Local override of global flag |
| Positionable | `isPositionable()`, `setPositionable(bool)` | Can be dragged |
| Controlling | `isControlling()`, `setControlling(bool)` | Click sends commands |
| Display Level | `getDisplayLevel()`, `setDisplayLevel(int)` | Z-order layer |
| Rotation | `getDegrees()`, `rotate(int)` | Visual rotation |
| Scale | `getScale()`, `scale(double)` | Visual scale |
| Tooltip | `getTooltip()`, `setTooltip(ToolTip)` | Hover tooltip |
| ID | `getId()`, `setId(String)` | Unique identifier |
| CSS Class | `getClasses()`, `addClass(String)` | Class grouping |

### 4.2 Mouse Event Methods

Each positionable can intercept mouse events:

```java
void doMousePressed(JmriMouseEvent e)
void doMouseReleased(JmriMouseEvent e)
void doMouseClicked(JmriMouseEvent e)
void doMouseDragged(JmriMouseEvent e)
void doMouseMoved(JmriMouseEvent e)
void doMouseEntered(JmriMouseEvent e)
void doMouseExited(JmriMouseEvent e)
```

### 4.3 Bean Reference

```java
NamedBean getNamedBean()   // returns null if this icon has no hardware bean
```

---

## 5. Positionable Class Hierarchy

```
Positionable (interface)
  │
  ├── PositionableLabel (extends JLabel)
  │     ├── PositionableIcon (extends PositionableLabel)
  │     │     ├── TurnoutIcon
  │     │     ├── SensorIcon
  │     │     ├── SignalHeadIcon
  │     │     ├── SignalMastIcon
  │     │     ├── LightIcon
  │     │     ├── SlipTurnoutIcon
  │     │     ├── IndicatorTurnoutIcon
  │     │     └── ReporterIcon
  │     ├── LocoIcon
  │     ├── TrainIcon
  │     ├── MemoryOrGVIcon
  │     │     ├── MemoryIcon
  │     │     │     ├── MemorySpinnerIcon
  │     │     │     ├── MemoryComboIcon
  │     │     │     ├── MemoryInputIcon
  │     │     │     └── BlockContentsIcon
  │     │     ├── GlobalVariableIcon
  │     │     │     ├── GlobalVariableSpinnerIcon
  │     │     │     ├── GlobalVariableComboIcon
  │     │     │     └── GlobalVariableInputIcon
  │     │     └── MemoryOrGVComboIcon
  │     ├── AudioIcon
  │     ├── RpsPositionIcon
  │     ├── LogixNGIcon
  │     └── LogixNGTableIcon
  │
  ├── PositionableJPanel (extends JPanel)
  └── PositionableJComponent (extends JComponent)
```

---

## 6. Icon Types Reference

### 6.1 Hardware Control Icons

These icons both display state and send commands when clicked.

| Icon | Bean | States Displayed | Click Action |
|------|------|-----------------|--------------|
| `TurnoutIcon` | `Turnout` | CLOSED, THROWN, UNKNOWN, INCONSISTENT | Toggle CLOSED/THROWN |
| `SensorIcon` | `Sensor` | ACTIVE, INACTIVE, UNKNOWN, INCONSISTENT | (display only by default) |
| `SignalHeadIcon` | `SignalHead` | Per-aspect icons (RED, YELLOW, GREEN, DARK, FLASHYELLOW, etc.) | Cycle aspects (if controlling) |
| `SignalMastIcon` | `SignalMast` | Per-aspect icons (CLEAR, APPROACH, STOP, etc.) | Cycle aspects |
| `LightIcon` | `Light` | ON, OFF, UNKNOWN | Toggle ON/OFF |
| `SlipTurnoutIcon` | `Turnout` (×2) | Slip route states | Toggle slip routes |
| `IndicatorTurnoutIcon` | `Turnout` + `OBlock` | Turnout state + block occupancy color coding | Toggle turnout |

### 6.2 Display-Only Icons

| Icon | Bean | What it displays |
|------|------|-----------------|
| `MemoryIcon` | `Memory` | Current memory value as text |
| `MemorySpinnerIcon` | `Memory` | Memory value as a spinner widget |
| `MemoryComboIcon` | `Memory` | Memory value as a dropdown |
| `MemoryInputIcon` | `Memory` | Memory value as an editable text field |
| `ReporterIcon` | `Reporter` | Current reporter value (e.g., RFID tag, transponder ID) |
| `BlockContentsIcon` | `Block` | What the block currently contains (train name) |
| `GlobalVariableIcon` | LogixNG `GlobalVariable` | Variable value as text |
| `GlobalVariableSpinnerIcon` | LogixNG `GlobalVariable` | Variable value as spinner |
| `GlobalVariableComboIcon` | LogixNG `GlobalVariable` | Variable value as dropdown |
| `GlobalVariableInputIcon` | LogixNG `GlobalVariable` | Variable value as editable field |
| `RpsPositionIcon` | RPS Block | RPS-tracked locomotive position |
| `LogixNGIcon` | LogixNG element | LogixNG display element |
| `LogixNGTableIcon` | LogixNG table | LogixNG table display |

### 6.3 Decorative / Non-Bean Icons

| Icon | Purpose |
|------|---------|
| `PositionableLabel` | Static text label or static image |
| `LocoIcon` | Movable locomotive marker (no hardware bean; manually positioned) |
| `TrainIcon` | Movable train marker (similar to LocoIcon) |
| `AudioIcon` | Triggers JMRI audio playback on click |
| Background image | Full-panel background image or color fill |
| `PositionableJPanel` | Embeds an arbitrary Swing JPanel |

### 6.4 Fast Clock

The analog fast clock is added via a special action and renders as a clock face showing the current JMRI fast-clock time. It listens to `Timebase` property changes.

---

## 7. Bean Binding

Every icon that represents hardware binds to a JMRI bean via `NamedBeanHandle<T>`.

### 7.1 How Binding Works

```java
// Example: TurnoutIcon

private NamedBeanHandle<Turnout> namedTurnout;

public void setTurnout(String pName) {
    Turnout t = InstanceManager.turnoutManagerInstance().provideTurnout(pName);
    namedTurnout = InstanceManager.getDefault(NamedBeanHandleManager.class)
                                  .getNamedBeanHandle(pName, t);
    // Register listener — icon will auto-update on state change
    t.addPropertyChangeListener(this, namedTurnout.getName(), "TurnoutIcon update");
}

public Turnout getTurnout() {
    return namedTurnout.getBean();
}
```

`NamedBeanHandle<T>` is used instead of a direct reference so that bean renames don't break the binding. The handle manager maintains the mapping.

### 7.2 State → Icon Mapping

State-based icons maintain a map from state value to `NamedIcon` (an image):

```java
// TurnoutIcon internal state map (simplified)
HashMap<Integer, NamedIcon> _iconStateMap = {
    Turnout.CLOSED  → NamedIcon("closedsignal.gif"),
    Turnout.THROWN  → NamedIcon("thrownsignal.gif"),
    Turnout.UNKNOWN → NamedIcon("unknownturnout.gif"),
    Turnout.INCONSISTENT → NamedIcon("inconsistentTurnout.gif")
}
```

Icons are user-configurable via right-click → "Edit Icon". Default icons ship in JMRI's `resources/icons/` directory.

### 7.3 Reactive Update Flow

```
Hardware state changes
  │
  ▼
System-specific bean (e.g., NceTurnout) updates knownState
  │
  ▼
Bean fires PropertyChangeEvent(PROPERTY_KNOWN_STATE, oldValue, newValue)
  │
  ▼
Icon.propertyChange(event) called (icon registered as listener)
  │
  ▼
Icon.displayState(newState)
  └── looks up new icon image in _iconStateMap
  └── calls setIcon(newImage)
  └── JComponent repaints automatically
```

---

## 8. Mouse Interaction Flow

### 8.1 Event Routing (Editor → Positionable)

```
User clicks on canvas (_targetPanel)
  │
  ▼
Editor.mouseClicked(JmriMouseEvent e)
  └── Find topmost Positionable at (x, y) via component hit testing
  └── If editable mode: handle selection, dragging, popup menus
  └── If operation mode: call positionable.doMouseClicked(e)
```

### 8.2 TurnoutIcon Click (Toggle)

```java
public void doMouseClicked(JmriMouseEvent e) {
    if (!editor.getFlag(OPTION_CONTROLS, isControlling())) return;
    if (e.isMetaDown() || e.isAltDown()) return;  // right-click or alt = popup, not command

    if (getDirectControl() && !isEditable()) {
        getTurnout().setCommandedState(Turnout.CLOSED);  // momentary direct
    } else {
        // toggle: CLOSED → THROWN, THROWN → CLOSED
        if (getTurnout().getKnownState() == Turnout.CLOSED)
            getTurnout().setCommandedState(Turnout.THROWN);
        else
            getTurnout().setCommandedState(Turnout.CLOSED);
    }
}
```

### 8.3 Momentary Icons (press/release)

Some icons support momentary behavior (active while mouse held):
- `doMousePressed` → `bean.setCommandedState(ACTIVE / THROWN)`
- `doMouseReleased` → `bean.setCommandedState(INACTIVE / CLOSED)`

### 8.4 Context Menu (Right-Click)

Right-click in edit mode opens a popup with:
- Edit Icon (change image)
- Edit Item (change bean binding)
- Set X/Y position
- Set Level
- Rotate / Scale
- Duplicate
- Remove

---

## 9. PanelEditor UI

`PanelEditor` provides a simple editing panel alongside the canvas:

```
PanelEditor window
├── "Add item" ComboBox (select type: Turnout, Sensor, Signal, Memory, etc.)
├── Text field (for label text)
├── X / Y coordinate fields (explicit positioning)
├── Checkboxes:
│     ├── All Editable
│     ├── All Positionable
│     ├── All Controlling
│     ├── Show tooltips
│     └── Show hidden
├── Panel width / height fields
└── Scroll bar configuration
```

When the user selects an item type from the ComboBox and clicks "Add", the appropriate icon editor opens (e.g., `TurnoutIconEditor`), where the user picks a bean name. The icon is then placed at the specified X/Y.

### Menus (on canvas window)

| Menu | Contains |
|------|---------|
| Edit | Undo, Redo, Cut, Copy, Paste, Select All, Deselect |
| Marker | Add LocoIcon, Add TrainIcon |
| Warrant | Warrant-related panel actions |

---

## 10. ControlPanelEditor

`ControlPanelEditor` extends `PanelEditor` with advanced features for complex operational panels.

### 10.1 Additional Capabilities

| Feature | Description |
|---------|-------------|
| **Circuit Builder** | `CircuitBuilder` — tools to define OBlock circuits visually, link track segments to OBlocks, create portal icons |
| **Shape Drawing** | `ShapeDrawer` — draw lines, rectangles, circles, polygons as decoration |
| **Portal Icons** | `PortalIcon` — represents entry/exit points between OBlock circuits |
| **Indicator Track Icons** | `IndicatorTrack` — visual track segments that color-code based on OBlock occupancy |
| **Drag and Drop** | `DropTargetListener` — drag items from a palette onto the canvas |
| **Item Palette** | More sophisticated `ItemPalette` for browsing and selecting icons |

### 10.2 Additional Menus

| Menu | Purpose |
|------|---------|
| Editor | Global flags, zoom, scrollbar settings |
| Options | View coordinates, disable shape selection |
| Icon | Add specific icon types |
| Zoom | Zoom in/out presets |
| Draw | Enter shape drawing mode, select shape type |
| Circuit | Circuit Builder tools (create/edit OBlocks, portals) |
| Warrant | Warrant and train tracking operations |

### 10.3 When to Use ControlPanelEditor vs PanelEditor

| Situation | Recommended |
|-----------|-------------|
| Simple status display board | PanelEditor |
| Turnout control panel without block tracking | PanelEditor |
| Block occupancy visualization | ControlPanelEditor |
| Warrant-based automatic train running | ControlPanelEditor |
| Complex mimic panel with track shapes | ControlPanelEditor |
| Needs shape/line drawing tools | ControlPanelEditor |

---

## 11. Persistence: XML Format

### 11.1 Serializer Classes

| Serializer | Handles |
|-----------|---------|
| `PanelEditorXml` | Root panel element; iterates all contents |
| `TurnoutIconXml` | TurnoutIcon save/load |
| `SensorIconXml` | SensorIcon save/load |
| `SignalHeadIconXml` | SignalHeadIcon save/load |
| `SignalMastIconXml` | SignalMastIcon save/load |
| `MemoryIconXml` | MemoryIcon and variants |
| `LightIconXml` | LightIcon |
| `PositionableLabelXml` | Static labels and images |
| `LocoIconXml` | Locomotive markers |
| `SlipTurnoutIconXml` | Slip turnout icons |
| `IndicatorTurnoutIconXml` | Indicator turnout icons |
| `AudioIconXml` | Audio icons |
| `LogixNGIconXml` | LogixNG icons |

### 11.2 XML Structure

```xml
<paneleditor name="Control Panel" x="100" y="50" width="900" height="600"
             editable="yes" positionable="yes" controlling="yes"
             showtooltips="yes" hidden="no" >

  <turnouticon turnout="IT42" x="200" y="150" level="7"
               forcecontroloff="false" momentary="false"
               class="jmri.jmrit.display.configurexml.TurnoutIconXml">
    <icons>
      <closed url="resources/icons/smallschematics/tracksegments/os-righthand-closed.gif" />
      <thrown url="resources/icons/smallschematics/tracksegments/os-righthand-thrown.gif" />
      <unknown url="resources/icons/smallschematics/tracksegments/os-unknown.gif" />
      <inconsistent url="resources/icons/smallschematics/tracksegments/os-inconsistent.gif" />
    </icons>
  </turnouticon>

  <sensoricon sensor="IS01" x="300" y="150" level="10" ... >
    <icons>
      <active   url="resources/icons/smallschematics/..." />
      <inactive url="resources/icons/smallschematics/..." />
      <unknown  url="..." />
    </icons>
  </sensoricon>

  <positionablelabel x="50" y="20" level="4" text="Main Yard" ... />

  <memoryicon memory="IM:AUTO:0001" x="400" y="20" level="5" ... />

</paneleditor>
```

### 11.3 Load Sequence

```
PanelEditorXml.load(Element e):
  1. Parse panel metadata (name, position, size, flags)
  2. Create PanelEditor instance
  3. Restore window location and size
  4. Set global flags BEFORE loading icons
     (preserves individual icon flag overrides)
  5. For each child element:
     a. Read "class" attribute → find serializer class by reflection
     b. Instantiate serializer
     c. Call serializer.load(element, editor)
     d. Serializer creates icon, binds bean, calls editor.putItem(icon)
```

---

## 12. Icon Editor Workflow (User Perspective)

When a user adds a new icon:

```
1. Select type from "Add item" ComboBox in PanelEditor
2. Icon Editor opens (e.g., "Add/Edit Turnout Icon")
3. User types bean name (e.g., "IT42") or picks from roster
4. User may customize icon images for each state
5. Click "Add to Panel" → icon placed at specified X/Y
6. Icon automatically registers as PropertyChangeListener on the bean
7. Any future state changes immediately update the icon display
```

---

## 13. Comparison: PanelEditor vs LayoutEditor

| Aspect | PanelEditor | LayoutEditor |
|--------|-------------|--------------|
| **Track representation** | Icons placed freely (no geometry) | Geometric track objects with connectivity |
| **Turnout placement** | Place a turnout icon at any pixel | Turnout has defined connection points A/B/C/D linked to track segments |
| **Block definition** | Not applicable | LayoutBlock groups track segments into circuits |
| **Connectivity graph** | None | Full traversable graph (used by Dispatcher, signal tools) |
| **Signal setup** | Manual — user assigns logic | Auto via "Set Signals at Turnout" tools |
| **Dispatcher integration** | Not applicable | Native — Dispatcher reads LayoutEditor topology |
| **Use case** | Custom control panels, status boards | Track diagram, routing, signal automation |
| **Learning curve** | Low | Higher |

---

## 14. Source File Map

| File | Responsibility |
|------|----------------|
| `Editor.java` | Abstract base: canvas, contents list, mouse routing, display levels, flags |
| `Positionable.java` | Interface for all canvas objects |
| `panelEditor/PanelEditor.java` | Simple panel editor UI |
| `panelEditor/configurexml/PanelEditorXml.java` | Root XML serializer for PanelEditor |
| `controlPanelEditor/ControlPanelEditor.java` | Advanced editor with circuits and shapes |
| `controlPanelEditor/CircuitBuilder.java` | OBlock circuit definition tools |
| `controlPanelEditor/ShapeDrawer.java` | Shape drawing tools |
| `TurnoutIcon.java` | Turnout control icon |
| `SensorIcon.java` | Sensor display icon |
| `SignalHeadIcon.java` | Signal head display icon |
| `SignalMastIcon.java` | Signal mast display icon |
| `LightIcon.java` | Light control icon |
| `MemoryIcon.java` | Memory value display |
| `MemoryInputIcon.java` | Editable memory value field |
| `MemorySpinnerIcon.java` | Spinner widget for memory |
| `MemoryComboIcon.java` | Dropdown widget for memory |
| `ReporterIcon.java` | Reporter value display |
| `BlockContentsIcon.java` | Block contents display |
| `LocoIcon.java` | Locomotive position marker |
| `SlipTurnoutIcon.java` | Slip turnout icon |
| `IndicatorTurnoutIcon.java` | Turnout + block status icon |
| `IndicatorTrack.java` | Track segment + OBlock status |
| `AudioIcon.java` | Audio playback trigger |
| `GlobalVariableIcon.java` | LogixNG global variable display |
| `PositionableLabel.java` | Base class: text labels, static images |
| `PositionableIcon.java` | Base class: state-mapped icons |
| `PositionableJPanel.java` | Embedded Swing panel |
| `configurexml/TurnoutIconXml.java` | TurnoutIcon XML serializer |
| `configurexml/SensorIconXml.java` | SensorIcon XML serializer |
| *(and corresponding Xml classes for each icon type)* | |

---

*Document generated from JMRI source analysis, June 2026.*
*JMRI is licensed under GNU GPL v2. See https://www.jmri.org*
