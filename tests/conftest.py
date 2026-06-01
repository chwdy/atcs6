"""Collection ordering for fast parallel (pytest-xdist) runs.

After the pathfinding-walk cache landed, the suite's wall-clock is set almost
entirely by two sim-journey tests; every other test finishes in well under
their combined runtime. Under xdist's default ``load`` scheduler each worker is
handed a chunk of *consecutive* collected tests, so two heavyweight tests that
sit next to each other in the collection -- both live in ``test_demo_5_*`` and
collect adjacently -- land on the same worker and run back-to-back. That is the
worst case for wall-clock: it serializes the two longest tests on one core
while the other cores go idle, so ``-n`` buys almost nothing.

We reshape the collection so the two heavyweights cannot share a worker and the
longest one starts immediately:

  * the longest test is hoisted to the very front, so the first worker picks it
    up at t=0 -- its runtime becomes the wall-clock floor instead of floor plus
    the small tests that would otherwise be queued ahead of it on that worker;
  * the next-longest is pushed to the very back, so it is handed out on demand
    to whichever *other* worker frees up first. The front worker is still busy
    on the longest test and will not request more work until it finishes, by
    which point the second test has already been claimed elsewhere.

This hook only reorders; it never selects, skips, or drops a test, and it is
deterministic, so every xdist worker computes the same order and their
collections still match. The markers below are matched as substrings of the
test node id and are ordering *hints* only -- if a test is renamed, it simply
stops being repositioned (a wall-clock regression at worst, never a correctness
problem). Refresh them from ``pytest --durations`` if the heavy set changes.
"""

# Longest and next-longest tests, by node-id substring. Keep longest first.
_LONGEST = "test_demo_5_no_path_revisit.py::test_t3_s_line_reassignment"
_SECOND_LONGEST = (
    "test_demo_5_crossover_blocks.py::test_best_effort_crossover_block_states"
)


def pytest_collection_modifyitems(config, items):
    """Hoist the longest test to the front and defer the second-longest to the
    back so xdist runs them on different workers (see module docstring)."""
    front, back, middle = [], [], []
    for item in items:
        if _LONGEST in item.nodeid:
            front.append(item)
        elif _SECOND_LONGEST in item.nodeid:
            back.append(item)
        else:
            middle.append(item)
    # Only reshuffle when both heavyweights are actually present (e.g. a full
    # run); a filtered selection that contains neither is left untouched.
    if front or back:
        items[:] = front + middle + back
