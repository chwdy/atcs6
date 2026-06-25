"""A2 — `Route.manual_override` lets a route serve `manual` resources.

design_new.md line 1208 / line 640: `RouteExecutor` applies the assigned
route's `manual_override` to the target resolver's `manual` filtering. With the
flag set, a `manual` block/platform/station resolves as a target; without it
(the default — every automatic route), a `manual` resource is filtered out like
a disabled one. `disabled` is never relaxed by the override.

These tests drive `RouteTargetResolver.resolve_task_target_block_detailed(...)`
directly over a stub executor that exposes `assigned_route_id_for` +
`route_registry`, the two reads the resolver uses to find the train's route and
read its `manual_override` flag.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from route_target_resolver import RouteTargetResolver
from train_route import BlockTarget, Route, RouteTask


@dataclass
class _StubBlock:
    disabled: bool = False
    manual: bool = False
    occupied_by: Optional[str] = None


class _StubNetwork:
    def __init__(self, blocks: Dict[str, _StubBlock]) -> None:
        self.blocks = blocks
        self.platforms: Dict[str, object] = {}
        self.station_groups: Dict[str, object] = {}
        self.trains = {"T1": _StubTrain()}

    def get_track(self, x: int, y: int):  # block targets never call this
        return None


@dataclass
class _StubTrain:
    train_id: str = "T1"


class _StubExecutor:
    """Minimal executor exposing the two reads `_route_manual_override` uses."""

    def __init__(self, network: _StubNetwork, route: Optional[Route]) -> None:
        self.network = network
        self.route_registry = {route.route_id: route} if route else {}
        self._route_id = route.route_id if route else None

    def assigned_route_id_for(self, train_id: str) -> Optional[str]:
        return self._route_id


def _resolver_for(block: _StubBlock, *, manual_override: bool):
    network = _StubNetwork({"BM": block})
    route = Route(route_id="R1", manual_override=manual_override)
    return RouteTargetResolver(_StubExecutor(network, route))


def _stop_task(block_id: str = "BM") -> RouteTask:
    return RouteTask(task_kind="stop", task_location=BlockTarget(block_id=block_id))


def test_manual_block_filtered_without_override():
    """Default route (manual_override=False) treats a manual block like disabled."""
    resolver = _resolver_for(_StubBlock(manual=True), manual_override=False)
    block_id, reason = resolver.resolve_task_target_block_detailed(
        _stop_task(), train=_StubTrain()
    )
    assert block_id is None
    assert "manual" in (reason or "").lower()


def test_manual_block_targetable_with_override():
    """An operator route (manual_override=True) resolves the manual block."""
    resolver = _resolver_for(_StubBlock(manual=True), manual_override=True)
    block_id, reason = resolver.resolve_task_target_block_detailed(
        _stop_task(), train=_StubTrain()
    )
    assert block_id == "BM"
    assert reason is None


def test_disabled_block_never_targetable_even_with_override():
    """`disabled` is absolute: manual_override never relaxes it."""
    resolver = _resolver_for(
        _StubBlock(disabled=True, manual=True), manual_override=True
    )
    block_id, reason = resolver.resolve_task_target_block_detailed(
        _stop_task(), train=_StubTrain()
    )
    assert block_id is None
    assert "disabled" in (reason or "").lower()


def test_normal_block_resolves_regardless_of_override():
    """A non-manual block is unaffected by the flag in either state."""
    for override in (False, True):
        resolver = _resolver_for(_StubBlock(), manual_override=override)
        block_id, reason = resolver.resolve_task_target_block_detailed(
            _stop_task(), train=_StubTrain()
        )
        assert block_id == "BM"
        assert reason is None
