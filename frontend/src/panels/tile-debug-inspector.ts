/**
 * Tile debug inspector — extracted from app.ts. Renders the hovered tile's
 * track bits / ribi / active edges / dead-ends / switch states / forbidden-set
 * check / occupancy into the debug panel. Pure: takes the live network + grid
 * coords, no App state.
 */
export function updateTileDetails(gridX: number, gridY: number, network: any): void {
  const detailsDiv = document.getElementById('tile-details');
  if (!detailsDiv) return;

  if (!network || !network.tracks) {
    detailsDiv.innerHTML = '<p class="debug-hint">No network loaded</p>';
    return;
  }

  const track = network.tracks.find((t: any) => t.x === gridX && t.y === gridY);

  if (!track) {
    detailsDiv.innerHTML = '<p class="debug-hint">No track at this position</p>';
    return;
  }

  // Calculate ribi for display
  const RIBI: Record<string, number> = { NORTH: 1, EAST: 2, SOUTH: 4, WEST: 8 };
  let ribi = 0;
  if (track.directed_paths) {
    for (const p of track.directed_paths) {
      if (p.port1 === 'NORTH' || p.port2 === 'NORTH') ribi |= RIBI.NORTH;
      if (p.port1 === 'EAST' || p.port2 === 'EAST') ribi |= RIBI.EAST;
      if (p.port1 === 'SOUTH' || p.port2 === 'SOUTH') ribi |= RIBI.SOUTH;
      if (p.port1 === 'WEST' || p.port2 === 'WEST') ribi |= RIBI.WEST;
    }
  }

  let html = `
            <h4 class="debug-tile-header">Tile (${gridX}, ${gridY})</h4>
            <table class="debug-table">
                <tr><td class="debug-label">Type:</td><td class="debug-value">${track.type || 'TRACK'}</td></tr>
                <tr><td class="debug-label">Track Bits:</td><td class="debug-value">${track.track_bits || 0} (0b${(track.track_bits || 0).toString(2).padStart(6, '0')})</td></tr>
                <tr><td class="debug-label">Ribi:</td><td class="debug-value">${ribi}</td></tr>
        `;

  // Show active bits
  if (track.track_bits) {
    const bits = [];
    if (track.track_bits & 1) bits.push('NS');
    if (track.track_bits & 2) bits.push('EW');
    if (track.track_bits & 4) bits.push('NE');
    if (track.track_bits & 8) bits.push('NW');
    if (track.track_bits & 16) bits.push('SE');
    if (track.track_bits & 32) bits.push('SW');
    html += `<tr><td class="debug-label">Active:</td><td class="debug-value">${bits.join(', ') || 'None'}</td></tr>`;
  }

  // Dead-ends
  if (track.dead_end_ports && track.dead_end_ports.length > 0) {
    html += `<tr><td class="debug-label">Dead-Ends:</td><td class="debug-value-deadend">${track.dead_end_ports.join(', ')}</td></tr>`;
  }

  // Switch states
  if (track.switch_states && Object.keys(track.switch_states).length > 0) {
    const switches = Object.entries(track.switch_states)
      .map(([edge, pos]) => `${edge}:${pos}`)
      .join(', ');
    html += `<tr><td class="debug-label">Switches:</td><td class="debug-value-switches">${switches}</td></tr>`;
  }

  // Validation - check if forbidden
  const FORBIDDEN_TRACK_BITS = new Set([
    13, 22, 42, 49, 15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58,
    31, 47, 55, 59, 61, 62, 63
  ]);
  const isForbidden = FORBIDDEN_TRACK_BITS.has(track.track_bits);
  html += `<tr><td class="debug-label">Valid:</td><td class="${isForbidden ? 'debug-value-invalid' : 'debug-value-valid'}">${isForbidden ? 'FORBIDDEN' : 'Valid'}</td></tr>`;

  // Occupation
  if (track.occupied_by) {
    html += `<tr><td class="debug-label">Occupied:</td><td class="debug-value-occupied">${track.occupied_by}</td></tr>`;
  }
  if (track.reserved_by) {
    html += `<tr><td class="debug-label">Reserved:</td><td class="debug-value-reserved">${track.reserved_by}</td></tr>`;
  }

  html += '</table>';
  detailsDiv.innerHTML = html;
}
