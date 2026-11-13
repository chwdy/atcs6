/**
 * The selected block, in the Operate screen's right panel: whether it reads
 * occupied, the train whose block it is, the train that claims it, and the
 * voltage on it (design_v2.md "Live fields", A block's live fields). The
 * block's F / R direction arrows show on the map while it is open.
 */
import { useEffect } from 'preact/hooks';

import { stateManager } from '../../lib/state-manager';
import { showToast } from '../../lib/toast';
import { CopyId } from './copy-id';
import { liveVersion } from './live';
import type { OperateHost } from './operate-state';

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

  if (!block) return <div class="empty-state empty-state-warn">Block {blockId} is no longer on the layout.</div>;

  const readsOccupied = !!block.reads_occupied;
  const trainId = (block.train_id as string | null) ?? null;
  const claimedBy = (block.claimed_by_train_id as string | null) ?? null;
  const voltage = Number(block.voltage ?? block.current_voltage ?? 0);
  const name = String(block.name || '').trim();
  const chip = readsOccupied ? ['OCCUPIED', 'chip-danger'] : claimedBy ? ['CLAIMED', 'chip-success'] : ['EMPTY', 'chip-info'];
  const train = (id: string | null) => (id ? <span class="font-mono">{id}</span> : <span class="text-muted">No train</span>);
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
          <span class={`chip chip-state ${chip[1]}`}>{chip[0]}</span>
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
          <dt>Reads</dt>
          <dd>{readsOccupied ? 'Occupied' : 'Empty'}</dd>
          <dt>Train</dt>
          <dd>{train(trainId)}</dd>
          <dt>Claimed by</dt>
          <dd>{train(claimedBy)}</dd>
          <dt>Voltage</dt>
          <dd class="font-mono">{voltage.toFixed(1)} V</dd>
        </dl>
      </div>
    </div>
  );
}
