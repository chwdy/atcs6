/**
 * The selected block, in the Operate screen's right panel
 * (docs/ui-layout-rewrite-plan.md §5): what a click on track opens here.
 *
 * It shows who occupies the block, its voltage and its speed limit, and the
 * two controls an operator changes while running: availability (Normal /
 * Manual / Disabled, the `disabled` and `manual` flags) and the allowed
 * travel direction. Both are config writes the server accepts while the
 * session runs (design_new.md "Direct-commit exceptions"); they are the same
 * requests the segment panel sends today. The block's F / R direction arrows
 * show on the map while it is open, as they do in the segment panel.
 */
import { useEffect } from 'preact/hooks';

import { api } from '../../lib/api';
import {
  availabilityStateToFlags,
  deriveAvailabilityState,
  deriveDirectionState,
  directionStateToPayload,
  formatDuration,
} from '../../lib/helpers';
import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';
import { liveVersion } from './live';
import type { OperateHost } from './operate-state';
import { CopyId } from './train-panel';

/** The block states (design_new.md "Block") and their status-chip colours. */
const STATE_CHIP: Record<string, string> = {
  free: 'chip-info',
  reserved: 'chip-success',
  occupied: 'chip-danger',
  caution: 'chip-warn',
  disabled: 'chip-muted',
  manual: 'chip-warn',
};

/** A segmented control of one value: the design-language `switch-group`. */
export function Segment({
  label,
  options,
  value,
  onPick,
}: {
  label: string;
  options: Array<{ value: string; label: string; title: string }>;
  value: string;
  onPick: (value: string) => void;
}) {
  return (
    <div class="input-large-container mb-8">
      <label class="input-large-label">{label}</label>
      <div class="switch-group fluid" role="radiogroup" aria-label={label}>
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={o.value === value}
            class={o.value === value ? 'switch-btn active' : 'switch-btn'}
            title={o.title}
            onClick={() => o.value !== value && onPick(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export const AVAILABILITY_OPTIONS = [
  { value: 'available', label: 'Normal', title: 'Normal — works under automatic routing and ATO' },
  {
    value: 'manual',
    label: 'Manual',
    title: 'Manual — automatic routing skips it; an operator route with override can still use it',
  },
  { value: 'disabled', label: 'Disabled', title: 'Disabled — off-limits to all trains; treated as missing track' },
];

const DIRECTION_OPTIONS = [
  { value: 'both', label: 'Both', title: 'Trains may travel either way through this block' },
  { value: 'forward', label: 'Forward', title: 'Only in the direction of the F arrows on the map' },
  { value: 'reverse', label: 'Reverse', title: 'Only in the direction of the R arrows on the map' },
];

/** Send a block or switch change and report a refusal the way today's
 *  panels do: the server's reason in a toast, the panel back to its value. */
export async function sendChange(what: string, send: () => Promise<any>, host: OperateHost): Promise<void> {
  try {
    const result = await send();
    if (result && result.error) showToast(`Cannot change ${what}: ${result.error}`, 'error', 4000);
  } catch (err: any) {
    showToast(`Cannot change ${what}: ${err?.message ?? err}`, 'error', 4000);
  }
  await host.refreshNetwork();
}

export function BlockPanel({ host, blockId }: { host: OperateHost; blockId: string }) {
  liveVersion.value;
  const block: any = stateManager.get('blocks', blockId);

  useEffect(() => {
    host.editor.renderer?.setBlockDirectionFocus?.(blockId);
    return () => host.editor.renderer?.clearBlockDirectionFocus?.();
  }, [blockId]);

  if (!block) return <div class="op-panel-empty">Block {blockId} is no longer on the layout.</div>;

  const state = String(block.state || 'free');
  const occupant = block.occupied_by as string | null;
  const reservedFor = block.reserved_for_train as string | null;
  const voltage = Number(block.current_voltage || 0);
  const limit = block.speed_limit_kmh;
  const name = String(block.name || '').trim();
  const trainLink = (id: string) => (
    <button type="button" class="rr-flat-btn font-mono" onClick={() => host.selectTrain(id)}>
      {id}
    </button>
  );
  return (
    <div class="op-entity-panel">
      <div class="status-header">
        <div class="status-header-row">
          <div class="id-copy-group">
            <div class="status-header-id font-mono" title={blockId}>
              {name || blockId}
            </div>
            <CopyId id={blockId} label="Block ID" />
          </div>
          <span class={`chip chip-state ${STATE_CHIP[state] ?? 'chip-muted'}`}>{state.toUpperCase()}</span>
        </div>
        <div class="status-header-ident">
          <span class="status-header-role">Block</span>
          {name && <span class="status-sep">·</span>}
          {name && <span class="font-mono">{blockId}</span>}
        </div>
      </div>

      <div class="control-section">
        <div class="control-section-header">Occupancy</div>
        <dl class="op-fields">
          <dt>Occupied by</dt>
          <dd>{occupant ? trainLink(occupant) : <span class="text-muted">No train</span>}</dd>
          <dt>Reserved for</dt>
          <dd>{reservedFor ? trainLink(reservedFor) : <span class="text-muted">No train</span>}</dd>
          <dt>Voltage</dt>
          <dd class="font-mono">{voltage.toFixed(1)} V</dd>
          <dt>Speed limit</dt>
          <dd class="font-mono">{limit != null ? `${limit} km/h` : 'No limit'}</dd>
          {block.is_holding_point && (
            <>
              <dt>Holding point</dt>
              <dd>Yes</dd>
            </>
          )}
          <dt>Usage time</dt>
          <dd class="font-mono">{formatDuration(block.occupied_seconds || 0)}</dd>
        </dl>
      </div>

      <div class="control-section">
        <div class="control-section-header">Operation</div>
        <Segment
          label="Availability"
          options={AVAILABILITY_OPTIONS}
          value={deriveAvailabilityState(block)}
          onPick={(v) =>
            void sendChange('availability', () => api.updateBlock(blockId, availabilityStateToFlags(v)), host)
          }
        />
        <Segment
          label="Allowed travel direction"
          options={DIRECTION_OPTIONS}
          value={deriveDirectionState(block)}
          onPick={(v) =>
            void sendChange('travel direction', () => api.updateBlock(blockId, directionStateToPayload(v)), host)
          }
        />
        <div class="text-xxs text-muted">
          Automatic routing obeys both; an operator route with override (Quick Route) can ignore them.
        </div>
      </div>
    </div>
  );
}
