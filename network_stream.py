"""
Network stream — pushes the network snapshot to every open browser tab.

Per design_new.md "Data Flow To UI", **Network stream**: `GET
/api/network/stream` is a server-sent event stream. Each message is the reply
`GET /api/network` would give at that moment, and it leaves the layout part
out when the connection already holds the current layout (the same rule as
the poll, see `network_poll.py`).

The server sends a message every `NETWORK_STREAM_INTERVAL_SECONDS`, and
right after a request that can change state. It builds each snapshot once
and hands the same one to every open connection, so a second tab adds no
second build. The build holds `AppState.simulation_lock`; waiting for the
next send does not.

Each connection runs in its own request thread of the threaded dev server.
Nothing here keeps a list of connections: a connection is just a thread
waiting in `NetworkStreamHub.next_snapshot`, and when the browser goes away
the next write fails and the thread ends.
"""

import threading
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from network_poll import (
    full_reply,
    layout_version,
    live_only_reply,
    split_network_payload,
)

# How often the server sends a message when nothing asks for one sooner.
NETWORK_STREAM_INTERVAL_SECONDS = 0.2

# Methods of a request that can change state. When one finishes, the next
# message goes out at once instead of at the end of the interval.
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class StreamSnapshot:
    """One built snapshot, shared by every connection that sends it.

    The split into layout and live parts, the version hash and the two JSON
    texts are worked out once, on first use, whichever connection asks first.
    """

    seq: int
    # `time.monotonic()` when the build started.
    built_at: float
    # The hub's count of state-changing requests when the build started. A
    # request that finishes during the build makes this snapshot out of date.
    change_count: int
    payload: Dict[str, Any]
    encode: Callable[[Any], str] = field(repr=False)

    @cached_property
    def _parts(self) -> Tuple[Dict[str, Any], str]:
        layout_part, live_part = split_network_payload(self.payload)
        return live_part, layout_version(layout_part)

    @property
    def version(self) -> str:
        return self._parts[1]

    @cached_property
    def full_text(self) -> str:
        return self.encode(full_reply(self.payload, self.version))

    @cached_property
    def live_text(self) -> str:
        live_part, version = self._parts
        return self.encode(live_only_reply(self.payload, live_part, version))

    def message(self, held_version: Optional[str]) -> str:
        """The server-sent event for a connection whose browser holds
        `held_version`: the live fields only when that is the current
        version, otherwise the whole snapshot."""
        text = self.live_text if held_version == self.version else self.full_text
        return f"data: {text}\n\n"


class NetworkStreamHub:
    """Builds snapshots for the open streams, one build per send.

    `build_payload` returns the network payload as `GET /api/network` builds
    it, taking `AppState.simulation_lock` itself. `encode` turns a reply into
    one line of JSON.
    """

    def __init__(
        self,
        build_payload: Callable[[], Dict[str, Any]],
        encode: Callable[[Any], str],
        interval_seconds: float = NETWORK_STREAM_INTERVAL_SECONDS,
    ) -> None:
        self._build_payload = build_payload
        self._encode = encode
        self._interval = interval_seconds
        self._cond = threading.Condition()
        self._change_count = 0
        self._latest: Optional[StreamSnapshot] = None
        self._building = False
        self._next_seq = 1

    def note_state_changed(self) -> None:
        """A request that can change state finished: every open stream sends
        its next message now."""
        with self._cond:
            self._change_count += 1
            self._cond.notify_all()

    def _out_of_date(self, snapshot: StreamSnapshot, now: float) -> bool:
        return (
            snapshot.change_count < self._change_count
            or now - snapshot.built_at >= self._interval
        )

    def next_snapshot(self, after_seq: Optional[int]) -> StreamSnapshot:
        """The snapshot a connection sends next.

        `after_seq` is the `seq` of the snapshot this connection sent last, or
        None for its first message. Returns the latest snapshot when it is up
        to date and newer than that one. Otherwise waits until it is out of
        date, then builds a new one, unless another connection is already
        building it, in which case it waits for that build.
        """
        with self._cond:
            while True:
                latest = self._latest
                now = time.monotonic()
                if latest is not None and not self._out_of_date(latest, now):
                    if after_seq is None or latest.seq > after_seq:
                        return latest
                    # This connection already sent it. Wait until it goes
                    # out of date; a state-changing request wakes us sooner.
                    self._cond.wait(latest.built_at + self._interval - now)
                    continue
                if self._building:
                    self._cond.wait()
                    continue
                self._building = True
                seq = self._next_seq
                self._next_seq += 1
                change_count = self._change_count
                break

        built: Optional[StreamSnapshot] = None
        try:
            built_at = time.monotonic()
            built = StreamSnapshot(
                seq=seq,
                built_at=built_at,
                change_count=change_count,
                payload=self._build_payload(),
                encode=self._encode,
            )
        finally:
            with self._cond:
                self._building = False
                if built is not None:
                    self._latest = built
                self._cond.notify_all()
        return built


def stream_messages(
    hub: NetworkStreamHub, client_layout_version: Optional[str]
) -> Iterator[str]:
    """The server-sent events for one connection, forever.

    The first message leaves the layout out only when the browser named the
    current version when it connected; each later one leaves it out when the
    version equals the one the previous message carried.
    """
    held_version = client_layout_version
    seq: Optional[int] = None
    while True:
        snapshot = hub.next_snapshot(seq)
        seq = snapshot.seq
        yield snapshot.message(held_version)
        held_version = snapshot.version
