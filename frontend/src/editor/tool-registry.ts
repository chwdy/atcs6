// tool-registry.ts — the data model for the editor's tools.
//
// Tools are declared as data (ToolContribution[]). Each contribution names the
// tool, how picking it dispatches (its `kind`), its group, and the editor
// modes it is available in. The core editor contributes the layout tools;
// plugins (e.g. SimEditorPlugin) contribute their own — so a layout-only host
// gets only the layout tools by simply not registering the sim plugin.
// The Editor screen's tool bar (frontend/src/next/editor/tools.ts) picks them
// by id.

export type ToolMode = 'edit' | 'sim';

export type ToolKind =
  // A stateful build/pick tool: clicking sets the active tool and the canvas
  // handlers read `editor.selectedTool` (track, signal, block, platform, inspect…).
  | { type: 'tool' }
  // A mode tool: clicking enters an editor mode built by `factory()`
  // (selection tools, path tools).
  | { type: 'mode'; factory: () => any }
  // A panel/dialog action: clicking runs `run()` and leaves the active tool
  // unchanged (switches list, add-train dialog, route runs…).
  | { type: 'action'; run: () => void };

export interface ToolContribution {
  /** Stable id; matches the canvas handler checks and the tool bar's variants. */
  id: string;
  /** Button text. */
  label: string;
  /** Tabler icon class, e.g. `ti ti-plus`. */
  icon: string;
  /** Long tooltip. */
  title: string;
  /** The family the tool belongs to (Selection, Network, Path, …); null for
   *  the default Inspect tool. */
  group: string | null;
  /** Editor modes the tool may be the active tool in. */
  availableIn: ToolMode[];
  /** How a click on the tool dispatches. */
  kind: ToolKind;
}
