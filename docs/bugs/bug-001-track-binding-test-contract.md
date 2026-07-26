# Bug 001: Track-binding tests used the retired single-port contract

- **Status:** Fixed
- **Test references:** `tests/test_hardware_config.py`, `tests/test_hardware_develop.py`, `tests/test_hardware_drivers.py`

## Symptom

The hardware test group failed while parsing configuration payloads with
`track binding 'B1' needs an integer forward`, and driver tests failed while
unpacking two-value track bindings into the required three values.

## Root cause

Track power now binds two independently selected PWM channels (`forward` and
`reverse`). Several fixtures and direction-sweep tests still used the retired
single `port` field and `(chip_id, port)` tuple. Debug readback also derived an
adjacent channel pair from that retired field.

## Relevant code locations

- `hardware_config.py`: `TrackBinding` and track-binding parsing
- `hardware/interfaces.py`: `Ch347TrackOutputInterface`
- `routes/hardware.py`: develop-mode track-voltage readback
- `tests/test_hardware_config.py`
- `tests/test_hardware_develop.py`
- `tests/test_hardware_drivers.py`

## Fix

Use explicit `forward` and `reverse` channels in configuration fixtures and
three-value driver bindings, exercise direction sweeps through the block-level
track-test API, and read the exact bound channels in develop-mode diagnostics.
