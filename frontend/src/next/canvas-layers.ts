/**
 * The canvas layers the ?ui=next View menu lists.
 *
 * Today each layer is a checkbox in the Settings dialog's Debug Info section,
 * and the renderer reads those checkboxes when it draws. Until the layers move
 * into their own state, the View menu reads and flips the same checkboxes, so
 * a layer has one on/off value whichever control you use, and a checkbox
 * added to Settings shows up in the menu with no extra code.
 */
export interface CanvasLayer {
  id: string;
  label: string;
  checked: boolean;
  /** Flip the checkbox and fire its change event, so the redraw it is wired
   *  to in App.setupDebugPanel runs. */
  toggle(): void;
}

export interface CanvasLayerGroup {
  title: string;
  layers: CanvasLayer[];
}

/** Every Debug Info group in the Settings dialog, in its order. */
export function readCanvasLayers(root: ParentNode = document): CanvasLayerGroup[] {
  return [...root.querySelectorAll('#settings-modal .debug-group')].map((group) => ({
    title: group.querySelector('.debug-group-title')?.textContent?.trim() ?? '',
    layers: [...group.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].map((box) => ({
      id: box.id,
      label: box.closest('label')?.querySelector('.checkbox-label')?.textContent?.trim() || box.id,
      checked: box.checked,
      toggle: () => {
        box.checked = !box.checked;
        box.dispatchEvent(new Event('change', { bubbles: true }));
      },
    })),
  }));
}
