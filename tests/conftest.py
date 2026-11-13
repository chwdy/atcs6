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

import pytest

import bootstrap


# --------------------------------------------------------------------------- #
# Per-test scene selection (`@pytest.mark.scene(...)`)
# --------------------------------------------------------------------------- #
#
# A test tied to a specific demo scene marks itself with
# `pytestmark = pytest.mark.scene("demo7_1")` (module level) instead of skipping
# when the process default scene does not match. Before each marked test we
# switch the active scene to the marked value via `bootstrap.set_current_scene`
# (the same call `server.py --scene ...` makes), so the test builds its own
# scene and *runs* rather than being skipped. Tests with no marker run under the
# process default (`ATCS_SCENE`, falling back to bootstrap's `_DEFAULT_SCENE`).
#
# Because `set_current_scene` also mirrors the choice into the `ATCS_SCENE`
# environment variable, a scene-marked test that builds via an implicit
# `bootstrap_runtime()` (no explicit `scene=`) still gets the right scene. We
# re-assert the correct scene in *every* test's setup (marked → its scene,
# unmarked → the captured default), so a preceding marked test can never leak
# its scene into a later one regardless of collection order or xdist worker.

# The process default scene, captured once at import (before any test mutates
# it). Unmarked tests run under this; it honours an `ATCS_SCENE` override.
_DEFAULT_SCENE = bootstrap.get_current_scene_id()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "scene(scene_id): build this test under the named demo scene "
        "(demo7_1 / demo7_2), overriding the process "
        "default for the test's duration so the test runs instead of being "
        "skipped. Replaces the old module-level get_current_scene_id() gate.",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Activate the test's scene before any fixture or test body builds a
    network. Runs first so an implicit `bootstrap_runtime()` sees the right
    scene. A `scene(...)` marker wins; otherwise we restore the process
    default so a preceding marked test never leaks its scene."""
    marker = item.get_closest_marker("scene")
    if marker is not None and not marker.args:
        raise pytest.UsageError(
            f"{item.nodeid}: @pytest.mark.scene requires a scene id, "
            "e.g. @pytest.mark.scene('demo7_1')."
        )
    target = marker.args[0] if marker is not None else _DEFAULT_SCENE
    bootstrap.set_current_scene(target)


@pytest.fixture(autouse=True)
def _locomotive_file_off_after_each_test():
    """The locomotive file is module state that `bootstrap_runtime` turns on
    when given a path. Turn it off after every test so one test that enables it
    (at a temp path) never makes a later test write a file."""
    yield
    import locomotive_file

    locomotive_file.disable()


@pytest.fixture(autouse=True)
def _hardware_config_file_off_limits(tmp_path, monkeypatch):
    """Point the hardware-configuration store's file at a per-test temp path, so
    no test can write the developer's real `hardware_config.json`.

    Three code paths write that file — `PUT /api/hardware/config`, `PUT
    /api/hardware/binding`, and loading a layout (a layout save file carries its
    own hardware configuration, see `PersistenceManager._restore_hardware`) — and
    the last one fires from any test that loads a layout, which is not obviously
    a hardware test at all. Redirecting the path here means a test never has to
    know that. Tests needing their own isolation still monkeypatch `CONFIG_PATH`
    themselves; this fixture only guarantees the default is never the real file.
    """
    import hardware_config

    monkeypatch.setattr(
        hardware_config, "CONFIG_PATH", str(tmp_path / "hardware_config.json")
    )


@pytest.fixture(autouse=True)
def _selected_environment_starts_at_the_default(tmp_path, monkeypatch):
    """Give every test the default environment (Virtual Hardware) and a
    per-test file.

    The selected environment is module-global and `bootstrap` loads it from disk,
    so without this a developer who leaves Real Hardware selected in their own
    `operation_environment.json` would run the suite against the rig's
    selection. Redirecting the path also keeps a test that calls `select(...)`
    from overwriting the developer's real selection. A test that is about the
    environment sets the one it wants; this only fixes the starting point.
    """
    import operation_environment

    monkeypatch.setattr(
        operation_environment,
        "ENVIRONMENT_PATH",
        str(tmp_path / "operation_environment.json"),
    )
    monkeypatch.setattr(
        operation_environment, "_selected", operation_environment.DEFAULT_ENVIRONMENT
    )


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
