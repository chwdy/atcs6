"""Tick-by-tick journey test harness.

Three layers:
  - Scene: builds the world (network + sim_controller + train_id).
  - Journey: declarative event list + continuous invariants for that scene.
  - Harness: drives ticks, snapshots state, fires event assertions, runs
    invariants, and reports failures with the surrounding tick history.
"""
