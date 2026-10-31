"""`POST /api/server/restart` hands the port to the new process image.

The route replaces the process with `os.execv`, which keeps the pid and every
inheritable fd. Werkzeug marks its listening socket inheritable and records its
fd in WERKZEUG_SERVER_FD:

- Under the reloader (WERKZEUG_RUN_MAIN=true) the new image is a reloader child
  and serves on that inherited fd, so the socket must survive the exec.
- With `--no-reload` the new image binds the port itself, after
  `shutdown.ensure_port_available` checks it. An inherited socket makes that
  check find the port held by our own pid, and the new image exits -- the page
  then shows "Lost connection" forever.

The exec itself is stubbed here; a child process stands in for the new image,
since inheritable fds cross a fork+exec the same way they cross `execv`.
"""

import os
import socket
import subprocess
import sys
import threading
from datetime import datetime

import pytest

from app_factory import create_app
from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


class _RunNow:
    """threading.Timer that runs its function at start(), inside the request."""

    def __init__(self, _interval, function, *args, **kwargs):
        self._call = lambda: function(*args, **kwargs)

    def start(self):
        self._call()


def _port_of_listener_in_new_image(fd: int) -> int | None:
    """The port a fresh process image finds on `fd`, or None if it has none."""
    probe = (
        "import socket, sys\n"
        "try:\n"
        "    print(socket.socket(fileno=int(sys.argv[1])).getsockname()[1])\n"
        "except OSError:\n"
        "    print('none')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, str(fd)],
        close_fds=False,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    return None if out == "none" else int(out)


@pytest.fixture
def client(tmp_path):
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    state = AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=SimulationController(network, path_dispatcher),
        persistence_manager=PersistenceManager(storage_dir=str(tmp_path)),
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )
    app = create_app(state)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def werkzeug_listener():
    """A listening socket prepared the way `run_simple` prepares its own."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.set_inheritable(True)
    yield sock
    sock.close()


@pytest.mark.parametrize(
    "reloader_child, socket_survives_exec",
    [(False, False), (True, True)],
    ids=["no-reload", "reloader-child"],
)
def test_restart_passes_the_listening_socket_on_only_to_a_reloader_child(
    client, werkzeug_listener, monkeypatch, reloader_child, socket_survives_exec
):
    fd = werkzeug_listener.fileno()
    port = werkzeug_listener.getsockname()[1]
    monkeypatch.setenv("WERKZEUG_SERVER_FD", str(fd))
    if reloader_child:
        monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    else:
        monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    execs = []
    monkeypatch.setattr(threading, "Timer", _RunNow)
    monkeypatch.setattr(os, "execv", lambda path, argv: execs.append((path, argv)))

    response = client.post("/api/server/restart")

    assert response.status_code == 200
    assert response.get_json()["status"] == "restarting"
    assert execs == [(sys.executable, [sys.executable] + sys.argv)]
    if socket_survives_exec:
        assert _port_of_listener_in_new_image(fd) == port
        assert os.environ["WERKZEUG_SERVER_FD"] == str(fd)
    else:
        assert _port_of_listener_in_new_image(fd) is None
        assert "WERKZEUG_SERVER_FD" not in os.environ


def test_restart_without_a_werkzeug_socket_still_execs(client, monkeypatch):
    """Under a server that is not werkzeug's (or a test client) there is no
    listening fd to hand over; the restart must not trip over its absence."""
    monkeypatch.delenv("WERKZEUG_SERVER_FD", raising=False)
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    execs = []
    monkeypatch.setattr(threading, "Timer", _RunNow)
    monkeypatch.setattr(os, "execv", lambda path, argv: execs.append((path, argv)))

    assert client.post("/api/server/restart").status_code == 200
    assert len(execs) == 1
