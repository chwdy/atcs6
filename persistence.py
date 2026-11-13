"""
Persistence Manager for ATCS6 Railway Simulation

Handles saving and loading of track layouts to/from JSON files.
Saves snapshot, steps, and hardware configuration in a single file:
- Snapshot: Primary format for fast loading
- Steps: Fallback for recovery if snapshot fails
- Hardware: The whole hardware configuration (design_v2.md
  "Hardware setup", Saved with the layout), so a saved layout carries the wiring its
  logical ids are bound to
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from network import RailwayNetwork


class PersistenceManager:
    """Manages persistence of railway network layouts to JSON files."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, storage_dir: str = "layouts"):
        """Initialize the PersistenceManager."""
        base_dir = Path(__file__).parent
        self.storage_dir = base_dir / storage_dir
        self.storage_dir.mkdir(exist_ok=True)

    def save_merged_layout(
        self, name: str, network: "RailwayNetwork", description: str = ""
    ) -> dict:
        """
        Save a layout with both snapshot and steps in a single file.

        Args:
            name: Name of the layout.
            network: The RailwayNetwork to save.
            description: Optional description.

        Returns:
            Dict with: path, steps_generated, steps_error (if any)
        """
        import hardware_config
        from step_recorder import StepRecorder

        if not name or not name.strip():
            raise ValueError("Layout name cannot be empty")

        safe_name = self._sanitize_filename(name)
        if not safe_name:
            raise ValueError(f"Invalid layout name: {name}")

        # Layout-only snapshot — runtime state is not persisted (see
        # design_v2.md "Out of scope", Saving trains). The frontend payload still uses `network.to_dict()`.
        snapshot_data = network.to_layout_dict()

        steps_data = []
        steps_error = None
        try:
            recorder = StepRecorder()
            steps_data = recorder.generate_steps_list(network)
        except Exception as e:
            steps_error = str(e)
            print(f"[Persistence] Error generating steps: {e}")

        # The binding maps are keyed by `block_id` / `switch_id`, so the wiring
        # only means anything alongside the layout those ids come from — save it
        # with the layout (design_v2.md "Hardware setup",
        # Saved with the layout).
        hardware_data = hardware_config.persisted_dict()

        # Build merged file
        wrapper = {
            "version": self.SCHEMA_VERSION,
            "name": name,
            "description": description,
            "modified": datetime.now().isoformat(),
            "snapshot": snapshot_data,
            "steps": steps_data,
            "hardware": hardware_data,
        }

        file_path = self.storage_dir / f"{safe_name}.json"

        # Atomic write with compact JSON
        temp_path = file_path.with_suffix(".json.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(wrapper, f, separators=(",", ":"), ensure_ascii=False)
            temp_path.replace(file_path)
        except Exception as e:
            if temp_path.exists():
                temp_path.unlink()
            raise IOError(f"Failed to save layout: {e}") from e

        # Cleanup old _steps files
        self._cleanup_steps_files(safe_name)

        return {
            "path": str(file_path),
            "steps_generated": len(steps_data) > 0,
            "steps_count": len(steps_data),
            "steps_error": steps_error,
        }

    def load_layout_with_fallback(self, name: str, network: "RailwayNetwork") -> dict:
        """
        Load a layout, falling back to step replay if snapshot fails.

        Returns:
            Dict with load result info: {success, method, error}
        """
        from step_player import StepPlayer

        safe_name = self._sanitize_filename(name)
        file_path = self.storage_dir / f"{safe_name}.json"

        if not file_path.exists():
            return {
                "success": False,
                "method": None,
                "error": f"Layout not found: {name}",
            }

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                wrapper = json.load(f)
        except json.JSONDecodeError as e:
            return {"success": False, "method": None, "error": f"Invalid JSON: {e}"}

        snapshot_error = None
        if "snapshot" in wrapper:
            try:
                network.from_dict(wrapper["snapshot"])
                return {
                    "success": True,
                    "method": "snapshot",
                    "error": None,
                    **self._restore_hardware(wrapper),
                }
            except Exception as e:
                snapshot_error = str(e)
                print(f"[Persistence] Snapshot load failed: {e}, trying steps...")

        # Fall back to steps
        if "steps" in wrapper and wrapper["steps"]:
            try:
                player = StepPlayer()
                result = player.play_steps(wrapper["steps"], network)
                if result.get("success"):
                    return {
                        "success": True,
                        "method": "steps",
                        "error": None,
                        "recovery": True,
                        **self._restore_hardware(wrapper),
                    }
                return {
                    "success": False,
                    "method": "steps",
                    "error": result.get("errors"),
                }
            except Exception as e:
                return {
                    "success": False,
                    "method": "steps",
                    "error": f"Steps replay failed: {e}",
                }

        return {
            "success": False,
            "method": None,
            "error": snapshot_error or "No snapshot or steps found",
        }

    def _restore_hardware(self, wrapper: Dict) -> Dict:
        """Adopt the layout's `hardware` section onto the hardware-configuration
        store, writing it to the app-managed config file so it applies on the
        next session build (design_v2.md "Hardware setup", Loading a layout's
        setup).

        Returns the `hardware` result keys to merge into the load result:
        `absent` for a layout saved before hardware was carried (the running
        configuration is left alone), `applied` on success, or `error` plus a
        `hardware_error` message.

        Fail-safe: a malformed or invalid section is reported and skipped, never
        raised — a layout still loads on a machine whose wiring the file does not
        describe, the same way a corrupt config file falls back instead of
        booting a broken real mode.
        """
        import hardware_config

        data = wrapper.get("hardware")
        if not isinstance(data, dict):
            return {"hardware": "absent"}

        # Legacy lift: rail polarity used to live on the Block
        # (`polarity_reversed` in the snapshot's block dicts) and now lives on
        # the `block_track` binding (design_v2.md "Hardware setup", A block's
        # rails). A layout saved under the old schema would
        # otherwise load a corrected block back to normal wiring — silently
        # reversing it on the rails. Only ever sets the flag: a new-schema save
        # carries `reversed` in the binding itself and has no block-level key.
        try:
            snapshot_blocks = (wrapper.get("snapshot") or {}).get("blocks") or {}
            track_map = data.get("block_track")
            if isinstance(track_map, dict):
                for block_id, block_data in snapshot_blocks.items():
                    if not isinstance(block_data, dict):
                        continue
                    if not block_data.get("polarity_reversed"):
                        continue
                    binding = track_map.get(block_id)
                    if isinstance(binding, dict) and "reversed" not in binding:
                        binding["reversed"] = True
        except Exception as e:
            print(f"[Persistence] Legacy polarity lift skipped: {e}")

        try:
            hardware_config.apply_snapshot(data)
        except Exception as e:
            print(f"[Persistence] Hardware configuration not applied: {e}")
            return {"hardware": "error", "hardware_error": str(e)}
        return {"hardware": "applied"}

    def list_layouts(self) -> List[Dict]:
        """List all available layouts with metadata."""
        layouts = []
        for file_path in self.storage_dir.glob("*.json"):
            try:
                stat = file_path.stat()
                with open(file_path, "r", encoding="utf-8") as f:
                    wrapper = json.load(f)

                # Get name from various possible locations
                name = (
                    wrapper.get("name")
                    or wrapper.get("meta", {}).get("name")
                    or file_path.stem
                )
                description = wrapper.get("description") or wrapper.get("meta", {}).get(
                    "description", ""
                )

                layouts.append(
                    {
                        "name": name,
                        "filename": file_path.stem,
                        "description": description,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "size_bytes": stat.st_size,
                        "has_snapshot": "snapshot" in wrapper,
                        "has_steps": "steps" in wrapper
                        and len(wrapper.get("steps", [])) > 0,
                        "has_hardware": isinstance(wrapper.get("hardware"), dict),
                    }
                )
            except (json.JSONDecodeError, IOError):
                continue

        layouts.sort(key=lambda x: x["modified"], reverse=True)
        return layouts

    def delete_layout(self, name: str) -> bool:
        """Delete a layout file."""
        safe_name = self._sanitize_filename(name)
        file_path = self.storage_dir / f"{safe_name}.json"

        if file_path.exists():
            file_path.unlink()
            # Also cleanup any _steps file
            self._cleanup_steps_files(safe_name)
            return True
        return False

    def _cleanup_steps_files(self, base_name: str) -> None:
        """Remove orphaned _steps files for a given layout."""
        steps_file = self.storage_dir / f"{base_name}_steps.json"
        if steps_file.exists():
            try:
                steps_file.unlink()
                print(f"[Persistence] Cleaned up old steps file: {steps_file.name}")
            except Exception as e:
                print(f"[Persistence] Failed to cleanup {steps_file.name}: {e}")

    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a layout name for use as a filename."""
        unsafe_chars = '<>:"/\\|?*'
        result = name.strip()
        for char in unsafe_chars:
            result = result.replace(char, "_")
        while "__" in result:
            result = result.replace("__", "_")
        result = result.strip("_")
        return result[:100] if result else ""

