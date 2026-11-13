/**
 * The Editor screen's tool bar and options row.
 *
 * The top row is one flat bar of the things you place, each with its fixed
 * number key. The row under it shows only the picked tool's variants, plus
 * one plain line saying what the canvas does with the variant in hand.
 * Picking runs today's tool through ToolbarHandler.selectTool, so the canvas
 * behaves as it does with today's tool rail.
 */
import { BAR_TOOLS, barToolFor, type BarTool } from './tools';
import { activeToolId } from './state';

export interface ToolBarActions {
  /** Pick one of today's editor tools by id. */
  pick(toolId: string): void;
  /** Open the list of all blocks in the right panel. */
  showBlockList(): void;
}

export function EditorToolBar({ actions }: { actions: ToolBarActions }) {
  const current = barToolFor(activeToolId.value);
  return (
    <div class="ned-bar">
      <div class="ned-tools" role="toolbar" aria-label="Editor tools">
        {BAR_TOOLS.map((tool) => (
          <button
            key={tool.id}
            type="button"
            class={tool === current ? 'ned-tool active' : 'ned-tool'}
            aria-pressed={tool === current}
            title={`${tool.label} (${tool.key})`}
            onClick={() => pickBarTool(tool, current, actions)}
          >
            <span class="ned-tool-key" aria-hidden="true">{tool.key}</span>
            <i class={tool.icon} aria-hidden="true" />
            <span>{tool.label}</span>
          </button>
        ))}
      </div>
      {current && <OptionsRow tool={current} actions={actions} />}
    </div>
  );
}

/** Picking the tool already in hand keeps its variant; picking another
 *  starts on its first variant. */
export function pickBarTool(tool: BarTool, current: BarTool | null, actions: ToolBarActions): void {
  if (tool === current) return;
  actions.pick(tool.variants[0].toolId);
}

function OptionsRow({ tool, actions }: { tool: BarTool; actions: ToolBarActions }) {
  const activeId = activeToolId.value;
  const variant = tool.variants.find((v) => v.toolId === activeId) ?? tool.variants[0];
  // A tool with one fixed action (Switch, Tunnel) carries a hint
  // instead of variant buttons.
  const showVariants = !tool.hint;
  return (
    <div class="ned-options" aria-label={`${tool.label} options`}>
      <span class="ned-options-label">{tool.label}</span>
      {showVariants && (
        <div class="switch-group ned-variants" role="radiogroup" aria-label={`${tool.label} variant`}>
          {tool.variants.map((v) => (
            <button
              key={v.toolId}
              type="button"
              role="radio"
              aria-checked={v === variant}
              class={v === variant ? 'switch-btn active' : 'switch-btn'}
              title={v.title}
              onClick={() => v !== variant && actions.pick(v.toolId)}
            >
              <i class={v.icon} aria-hidden="true" /> {v.label}
            </button>
          ))}
        </div>
      )}
      {tool.id === 'block' && (
        <button type="button" class="btn ned-options-btn" title="List every block" onClick={actions.showBlockList}>
          <i class="ti ti-list" aria-hidden="true" /> Block list
        </button>
      )}
      <span class="ned-hint">{tool.hint ?? variant.title}</span>
    </div>
  );
}
