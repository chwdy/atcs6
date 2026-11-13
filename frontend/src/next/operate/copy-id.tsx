/** A small button that copies an entity's id (lib/copy-id.ts does the copy). */
import { installCopyIdHandler } from '../../lib/copy-id';

export function CopyId({ id, label }: { id: string; label: string }) {
  installCopyIdHandler();
  return (
    <button
      type="button"
      class="copy-id-btn"
      data-copy-id={id}
      title={`Copy ${label}`}
      aria-label={`Copy ${label}: ${id}`}
    >
      <i class="ti ti-copy" aria-hidden="true" />
    </button>
  );
}
