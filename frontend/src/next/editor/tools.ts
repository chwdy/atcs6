/**
 * The Editor screen's tool bar, as data (docs/ui-layout-rewrite-plan.md §4).
 *
 * One flat bar of the things you place. Each bar tool has a fixed number key
 * (its place in the bar) and the variants that the options row shows while
 * it is picked. A variant names one of today's editor tools by its id
 * (Editor.coreTools, or the two this screen adds in modes.ts), so picking a
 * variant runs the same canvas behavior today's tool rail runs.
 */

export type BarToolId =
  | 'select'
  | 'track'
  | 'switch'
  | 'signal'
  | 'tunnel'
  | 'platform'
  | 'segment'
  | 'block';

export interface ToolVariant {
  /** Today's editor tool id the variant picks. */
  toolId: string;
  label: string;
  icon: string;
  /** Tooltip: what the canvas does with this variant. */
  title: string;
}

export interface BarTool {
  id: BarToolId;
  label: string;
  icon: string;
  /** Number key, '1'…'8'. */
  key: string;
  /** Picked in order; the first is what picking the bar tool selects. */
  variants: ToolVariant[];
  /** For a tool with one fixed action: the plain line the options row shows
   *  instead of variant buttons. */
  hint?: string;
}

const SELECT_HINT = 'Space clears the selection; Delete removes the selected track.';

export const BAR_TOOLS: BarTool[] = [
  {
    id: 'select',
    label: 'Select',
    icon: 'ti ti-pointer',
    key: '1',
    variants: [
      { toolId: 'inspect', label: 'Click', icon: 'ti ti-pointer', title: 'Click a piece of track to edit its segment. Drag to move the view.' },
      { toolId: 'box-select', label: 'Box', icon: 'ti ti-box-multiple', title: `Drag a rectangle to select track; Shift+drag removes. ${SELECT_HINT}` },
      { toolId: 'lasso-select', label: 'Lasso', icon: 'ti ti-lasso', title: `Draw a shape around track to select it; Shift removes. ${SELECT_HINT}` },
      { toolId: 'paint-select', label: 'Brush', icon: 'ti ti-brush', title: `Click or drag over track to add it to the selection; Shift removes. ${SELECT_HINT}` },
      { toolId: 'trace', label: 'Trace', icon: 'ti ti-line', title: `Click two pieces of track to select the track between them. ${SELECT_HINT}` },
      { toolId: 'entity-select', label: 'Entity', icon: 'ti ti-click', title: `Click a tile, then pick a block, platform or station to select all its track. ${SELECT_HINT}` },
    ],
  },
  {
    id: 'track',
    label: 'Track',
    icon: 'ti ti-track',
    key: '2',
    variants: [
      { toolId: 'track', label: 'Draw', icon: 'ti ti-pencil', title: 'Drag to lay track. Starting or ending on existing track joins it; hold Shift to keep clear of nearby track. R and T change the first and last piece.' },
      { toolId: 'remove-track', label: 'Remove', icon: 'ti ti-eraser', title: 'Click a piece of track to remove it. Removing a switch, signal or platform asks first.' },
    ],
  },
  {
    id: 'switch',
    label: 'Switch',
    icon: 'ti ti-arrows-split-2',
    key: '3',
    variants: [{ toolId: 'switch-pick', label: 'Edit', icon: 'ti ti-arrows-split-2', title: 'Click a switch to edit it.' }],
    hint: 'Click a switch to edit it. To make a switch, draw a branch off existing track with Track.',
  },
  {
    id: 'signal',
    label: 'Signal',
    icon: 'ti ti-traffic-lights',
    key: '4',
    variants: [{ toolId: 'signal', label: 'Place', icon: 'ti ti-traffic-lights', title: 'Click a piece of track to place a signal.' }],
    hint: 'Click a piece of track to place a signal. Right-click while pointing at it to turn the direction it watches.',
  },
  {
    id: 'tunnel',
    label: 'Tunnel',
    icon: 'ti ti-arrows-join',
    key: '5',
    variants: [{ toolId: 'tunnel', label: 'Pair', icon: 'ti ti-arrows-join', title: 'Click two dead-end track edges to join them as a tunnel.' }],
    hint: 'Click two dead-end track edges to join them as a tunnel.',
  },
  {
    id: 'platform',
    label: 'Platform',
    icon: 'ti ti-building-arch',
    key: '6',
    variants: [
      { toolId: 'platform', label: 'New', icon: 'ti ti-plus', title: 'Click one piece of track, or drag across several, to place a platform.' },
      { toolId: 'extend-platform', label: 'Extend', icon: 'ti ti-arrows-horizontal', title: 'Drag from a platform along the track to make it longer.' },
      { toolId: 'remove-platform-tile', label: 'Remove', icon: 'ti ti-minus', title: 'Click or drag over platform tiles to remove them.' },
    ],
  },
  {
    id: 'segment',
    label: 'Segment',
    icon: 'ti ti-square-dashed',
    key: '7',
    variants: [
      { toolId: 'segment', label: 'New', icon: 'ti ti-plus', title: 'Drag along track to make a new segment.' },
      { toolId: 'extend-segment', label: 'Extend', icon: 'ti ti-arrows-horizontal', title: 'Drag from a segment along the track to make it longer.' },
      { toolId: 'remove-segment-tile', label: 'Remove', icon: 'ti ti-minus', title: 'Click or drag over segment tiles to take them out of their segment.' },
    ],
  },
  {
    id: 'block',
    label: 'Block',
    icon: 'ti ti-stack-2',
    key: '8',
    variants: [
      { toolId: 'block-assign', label: 'Assign', icon: 'ti ti-click', title: 'With a block open, click a segment to add it to the block; click a member again to take it out.' },
    ],
  },
];

/** The bar tool a given editor tool id belongs to, or null for a tool this
 *  screen does not show (path, train and route tools, which belong to Operate). */
export function barToolFor(toolId: string): BarTool | null {
  return BAR_TOOLS.find((t) => t.variants.some((v) => v.toolId === toolId)) ?? null;
}

/** The bar tool a number key picks, or null. */
export function barToolForKey(key: string): BarTool | null {
  return BAR_TOOLS.find((t) => t.key === key) ?? null;
}
