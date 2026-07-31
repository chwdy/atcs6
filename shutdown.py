"""Shutdown reporting and the loss-of-control fail-safe.

design_new.md "Power -> Hardware interface boundary" requires that a process
shutdown drive outputs to their safe state (block track voltages 0, switch
motors de-energized). `server.py` used to express that with a bare
`atexit.register(sim_controller.enter_safe_state)`, which has a hole:
**atexit does not run when the process is killed by a signal**, and a signal is
how this server normally dies.

The hole is not hypothetical. With the Werkzeug reloader active (the default),
`python server.py` is two processes:

    parent  -- werkzeug's reloader supervisor, blocked in `subprocess.call`
    child   -- WERKZEUG_RUN_MAIN=true; owns the simulation and the hardware

A SIGTERM to the parent (an IDE stop button, `kill <pid>`, a supervisor) trips
werkzeug's own handler (`_reloader.py`: `signal.signal(SIGTERM, sys.exit)`),
which raises SystemExit inside `subprocess.call`. That function is:

    with Popen(...) as p:
        try:    return p.wait(...)
        except: p.kill(); raise          # <-- SIGKILL, unconditionally

so the child -- the process actually driving the track -- is SIGKILLed. It runs
no handler, no atexit, and no `enter_safe_state()`. The only outward sign is a
`resource_tracker: There appear to be 1 leaked semaphore objects` warning,
emitted because werkzeug's debug PIN counter (`Value("B")`) was never released.

This module closes that hole from three sides:

1. `supervise_reloader_child()` replaces werkzeug's reloader parent with a
   supervisor that forwards SIGTERM/SIGINT/SIGHUP to the child and *waits* for
   it, escalating to SIGKILL only after a grace period. The child then dies the
   way a file-change reload already died -- cleanly, with atexit running.
2. `install_shutdown_reporting()` owns the safe-state call for every exit path
   (normal return, SystemExit, unhandled exception, caught signal) and prints
   exactly what happened and whether the outputs were actually safed.
3. A run marker catches what no handler can catch -- a real SIGKILL, a host
   crash, a power loss. It is written at boot and removed on a clean exit; a
   marker still present at the *next* boot proves the previous run died with
   its outputs in an unknown state, which is reported loudly to the console and
   handed to the UI via `/api/server/shutdown-report`.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# Runtime bookkeeping lives beside the process, not in the layout tree. Both
# files are gitignored -- they describe one machine's last run, not the project.
RUNTIME_DIR = Path(__file__).resolve().parent / ".runtime"
RUN_MARKER_PATH = RUNTIME_DIR / "server_run.json"
LAST_SHUTDOWN_PATH = RUNTIME_DIR / "last_shutdown.json"

# How often the owning process refreshes `last_seen` in the run marker. On an
# unclean kill this is the resolution of "when did it die" -- the only such
# evidence available, since the process itself got no chance to write anything.
HEARTBEAT_INTERVAL_S = 5.0

# How long the supervisor waits for the session process to finish its own
# shutdown (safe state + record write) before escalating to SIGKILL.
CHILD_GRACE_S = 10.0

# Identifies this process to the browser. The UI compares it across a reconnect
# to tell "the server restarted" from "the connection blipped".
BOOT_ID = uuid.uuid4().hex[:12]

_BAR = "=" * 70

_SIGNAL_NAMES = {
    signal.SIGTERM: "SIGTERM",
    signal.SIGINT: "SIGINT",
}
if hasattr(signal, "SIGHUP"):
    _SIGNAL_NAMES[signal.SIGHUP] = "SIGHUP"

# Set by our own signal handlers so the atexit reporter can name the cause.
# Stays None on a plain return, a reload (`sys.exit(3)`), or a SystemExit
# raised by a handler we do not own (werkzeug's, in the reloader child).
_caught_signal: Optional[int] = None
_started_at = time.time()
_reported = False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via tmp+rename so a kill mid-write can never leave a
    half-written file that the next boot would fail to parse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Run marker -- the only detector for kills no handler can survive
# ---------------------------------------------------------------------------


def claim_run_marker(port: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Take ownership of the run marker for this process.

    Returns a shutdown record describing the *previous* run if that run left
    its marker behind -- i.e. it died without ever reaching its shutdown path,
    so its outputs were never driven to the safe state. Returns None when the
    previous run exited cleanly (or there was no previous run).

    Only the process that actually owns the hardware calls this. The reloader
    supervisor does not bootstrap a runtime and so never claims a marker.
    """
    stale = _read_json(RUN_MARKER_PATH)

    _write_json(
        RUN_MARKER_PATH,
        {
            "boot_id": BOOT_ID,
            "pid": os.getpid(),
            "port": port,
            "argv": sys.argv,
            "started_at": _now_iso(),
            "started_at_epoch": _started_at,
            "last_seen": _now_iso(),
            "last_seen_epoch": _started_at,
        },
    )
    _start_heartbeat(port)

    if not stale:
        return None

    # A marker whose owner is still alive is not a crash record — it is a
    # second server running against the same checkout. Reporting that as
    # "the previous run was killed" would be a lie, and a scary one.
    if _pid_alive(stale.get("pid")):
        print(
            f"[Shutdown] Note: pid {stale.get('pid')} is also serving this "
            f"checkout. Shutdown reporting tracks the most recent process only.",
            flush=True,
        )
        return None

    last_seen = float(stale.get("last_seen_epoch") or 0.0)
    started = float(stale.get("started_at_epoch") or 0.0)
    record = {
        "reason": "abnormal_termination",
        "clean": False,
        "safe_state_applied": False,
        "signal_name": None,
        "boot_id": stale.get("boot_id"),
        "pid": stale.get("pid"),
        "started_at": stale.get("started_at"),
        "ended_at": stale.get("last_seen"),
        "ran_for_seconds": max(last_seen - started, 0.0) if last_seen and started else None,
        "detail": (
            "The process was killed without running its shutdown handler "
            "(SIGKILL, a host crash, or a power loss). Track voltages and "
            "switch coils kept whatever value they held at that instant."
        ),
    }
    _write_json(LAST_SHUTDOWN_PATH, record)
    _print_abnormal_report(record)
    return record


def _pid_alive(pid: Any) -> bool:
    """Is this pid still running? `signal 0` probes without delivering."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _start_heartbeat(port: Optional[int]) -> None:
    """Refresh `last_seen` in the marker so an unclean kill still leaves a
    usable time of death. Daemon thread: it must never hold up an exit."""

    def beat() -> None:
        while True:
            time.sleep(HEARTBEAT_INTERVAL_S)
            marker = _read_json(RUN_MARKER_PATH)
            # Another process claimed the marker (e.g. a reload spawned a new
            # session). Ours is no longer the live run -- stop touching it.
            if not marker or marker.get("boot_id") != BOOT_ID:
                return
            marker["last_seen"] = _now_iso()
            marker["last_seen_epoch"] = time.time()
            try:
                _write_json(RUN_MARKER_PATH, marker)
            except OSError:
                return

    threading.Thread(target=beat, name="shutdown-heartbeat", daemon=True).start()


def _release_run_marker() -> None:
    """Drop our marker so the next boot knows this run ended on its own feet."""
    marker = _read_json(RUN_MARKER_PATH)
    if marker and marker.get("boot_id") != BOOT_ID:
        return  # not ours anymore
    try:
        RUN_MARKER_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def read_last_shutdown() -> Optional[dict[str, Any]]:
    """The most recent shutdown record, for `/api/server/shutdown-report`."""
    return _read_json(LAST_SHUTDOWN_PATH)


# ---------------------------------------------------------------------------
# Console reports
# ---------------------------------------------------------------------------


def _print_abnormal_report(record: dict[str, Any]) -> None:
    ran = record.get("ran_for_seconds")
    print(f"\n{_BAR}")
    print("!!  PREVIOUS RUN DID NOT SHUT DOWN CLEANLY")
    print(_BAR)
    print(f"  previous pid   {record.get('pid')}")
    print(f"  started        {record.get('started_at')}")
    print(
        f"  last seen      {record.get('ended_at')}   "
        f"(heartbeat, {HEARTBEAT_INTERVAL_S:.0f}s resolution)"
    )
    if ran is not None:
        print(f"  ran for        {_fmt_duration(ran)}")
    print()
    print("  OUTPUTS WERE NOT DRIVEN TO THE SAFE STATE by that process.")
    print("  Block track voltages and switch coils kept whatever value they")
    print("  held at the instant it died. Verify the layout is de-energized")
    print("  before starting a session.")
    print()
    print("  Most likely cause on this project: a SIGTERM delivered to the")
    print("  Werkzeug reloader PARENT. subprocess.call() SIGKILLs the reloader")
    print("  child, so the child never runs enter_safe_state(). The supervisor")
    print("  in shutdown.py prevents that -- if you saw this after a plain")
    print("  `kill`, the run predates the fix or was killed with -9.")
    print(f"{_BAR}\n", flush=True)


def _print_shutdown_report(record: dict[str, Any]) -> None:
    signal_name = record.get("signal_name")
    header = (
        f"SERVER SHUTDOWN -- {signal_name} (signal {record.get('signal_number')})"
        if signal_name
        else "SERVER SHUTDOWN"
    )
    print(f"\n{_BAR}")
    print(header)
    print(_BAR)
    print(f"  process        pid {record.get('pid')} ({record.get('role')})")
    print(
        f"  uptime         {_fmt_duration(record.get('ran_for_seconds') or 0)}"
        f"   (started {record.get('started_at')})"
    )
    sim = record.get("simulation") or {}
    if sim:
        print(
            f"  simulation     {'running' if sim.get('running') else 'stopped'}, "
            f"{sim.get('train_count', 0)} train(s)"
        )
    if record.get("safe_state_applied"):
        print(
            f"  safe state     OK -- block voltages 0, switch coils de-energized "
            f"({record.get('safe_state_ms', 0):.0f} ms)"
        )
    else:
        print(f"  safe state     FAILED -- {record.get('safe_state_error')}")
        print("                 OUTPUTS MAY STILL BE ENERGIZED. Check the layout.")
    print(f"  record         {LAST_SHUTDOWN_PATH}")
    print(f"{_BAR}\n", flush=True)


# ---------------------------------------------------------------------------
# The session process: signal handlers + atexit fail-safe
# ---------------------------------------------------------------------------


def install_shutdown_reporting(
    sim_controller: Any,
    *,
    state: Any = None,
    port: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Make this process report and safe itself on every exit path it can.

    Replaces `atexit.register(sim_controller.enter_safe_state)`: this owns the
    safe-state call so it can also say, in the console and in the record,
    whether it actually succeeded.

    Returns the previous run's shutdown record when that run died unclean
    (see `claim_run_marker`), otherwise None.
    """
    previous = claim_run_marker(port=port)
    _install_signal_handlers()
    atexit.register(_finalize, sim_controller, state)
    return previous


def _install_signal_handlers() -> None:
    """Record the signal, then exit normally so atexit still runs.

    Only meaningful when nothing overwrites us. In the reloader child, werkzeug
    installs its own SIGTERM handler after this (it also just exits, so the
    fail-safe still fires -- we lose only the signal's name, which the
    supervisor reports instead).
    """

    def handler(signum: int, _frame: Any) -> None:
        global _caught_signal
        _caught_signal = signum
        sys.exit(128 + signum)

    for signum in _SIGNAL_NAMES:
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            # Not the main thread, or the platform disallows it. The atexit
            # fail-safe still covers every non-signal exit path.
            pass


def _finalize(sim_controller: Any, state: Any) -> None:
    """atexit: drive outputs safe, write the record, release the marker."""
    global _reported
    if _reported:
        return
    _reported = True

    safe_ok, safe_ms, safe_err = _apply_safe_state(sim_controller)

    signum = _caught_signal
    record: dict[str, Any] = {
        "reason": "signal" if signum else "clean_exit",
        "clean": True,
        "safe_state_applied": safe_ok,
        "safe_state_ms": safe_ms,
        "safe_state_error": safe_err,
        "signal_name": _SIGNAL_NAMES.get(signum) if signum else None,
        "signal_number": signum,
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        "role": (
            "reloader child"
            if os.environ.get("WERKZEUG_RUN_MAIN") == "true"
            else "server"
        ),
        "started_at": datetime.fromtimestamp(_started_at).isoformat(timespec="seconds"),
        "ended_at": _now_iso(),
        "ran_for_seconds": time.time() - _started_at,
        "simulation": _simulation_summary(state),
    }

    try:
        _write_json(LAST_SHUTDOWN_PATH, record)
    except OSError as exc:
        print(f"[Shutdown] Could not write shutdown record: {exc}", flush=True)

    if signum or not safe_ok:
        _print_shutdown_report(record)
    else:
        # Reloads go through here on every file change -- keep them to one line.
        print(
            "[Shutdown] Clean exit -- outputs driven to safe state "
            "(block voltages 0, switch coils de-energized).",
            flush=True,
        )

    _release_run_marker()


def _apply_safe_state(sim_controller: Any) -> tuple[bool, float, Optional[str]]:
    if sim_controller is None:
        return False, 0.0, "no simulation controller"
    started = time.time()
    try:
        sim_controller.enter_safe_state()
    except Exception as exc:  # a failure here is exactly what must be reported
        return False, (time.time() - started) * 1000.0, f"{type(exc).__name__}: {exc}"
    return True, (time.time() - started) * 1000.0, None


def _simulation_summary(state: Any) -> dict[str, Any]:
    if state is None:
        return {}
    try:
        network = getattr(state, "network", None)
        return {
            "running": bool(getattr(state, "simulation_running", False)),
            "train_count": len(getattr(network, "trains", {}) or {}),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# The supervisor: replaces werkzeug's reloader parent
# ---------------------------------------------------------------------------


def _child_args() -> list[str]:
    """Argv that relaunches this server. Mirrors werkzeug's own helper so
    `python -m ...` invocations relaunch correctly; falls back to plain argv."""
    try:
        from werkzeug._reloader import _get_args_for_reloading

        return _get_args_for_reloading()
    except Exception:
        return [sys.executable, *sys.argv]


def _bind_listen_socket(host: str, port: int):
    """Bind the server's listening socket in the supervisor and hand it down.

    This is not an optimization -- it is required. A process with
    WERKZEUG_RUN_MAIN=true takes the `fd = int(os.environ["WERKZEUG_SERVER_FD"])`
    branch in `run_simple` and never binds for itself, because werkzeug's own
    reloader parent binds first and passes the fd down (inheritable fd +
    `close_fds=False`). Standing in for that parent means standing in for that
    too. It also means the port is held by the supervisor across reloads, so a
    restart cannot lose the port to a race.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)  # werkzeug's LISTEN_QUEUE
    sock.set_inheritable(True)
    return sock


def supervise_reloader_child(
    host: str = "127.0.0.1",
    port: int = 5001,
    log: Callable[[str], None] = print,
) -> int:
    """Run the reload supervisor loop. Never returns to the caller's flow.

    This is werkzeug's `restart_with_reloader` with the one behaviour the
    fail-safe needs: on SIGTERM/SIGINT/SIGHUP the child is asked to stop and is
    given `CHILD_GRACE_S` to run its own shutdown (safe state + record) before
    any escalation to SIGKILL. Werkzeug's version SIGKILLs it immediately.

    The child is a normal `server.py` run with WERKZEUG_RUN_MAIN=true, so
    werkzeug's in-process file watcher still drives reloads exactly as before:
    a file change makes the child exit 3 and we start a fresh one.
    """
    try:
        sock = _bind_listen_socket(host, port)
    except OSError as exc:
        log(f"Could not bind {host}:{port} — {exc}")
        return 1

    env = os.environ.copy()
    env["WERKZEUG_RUN_MAIN"] = "true"
    env["WERKZEUG_SERVER_FD"] = str(sock.fileno())

    log(f" * Serving on http://{host}:{port}")

    child: Optional[subprocess.Popen] = None
    stopping: dict[str, Any] = {"signum": None}

    def request_stop(signum: int, _frame: Any) -> None:
        # Re-entrant: a second Ctrl-C while we are already draining should not
        # restart the countdown, it should fall through to the escalation.
        if stopping["signum"] is not None:
            return
        stopping["signum"] = signum
        name = _SIGNAL_NAMES.get(signum, str(signum))
        log(f"\n{_BAR}")
        log(f"SERVER SHUTDOWN -- {name} (signal {signum}) received by supervisor")
        log(_BAR)
        if child and child.poll() is None:
            log(f"  forwarding {name} to session process pid {child.pid}")
            log(f"  waiting up to {CHILD_GRACE_S:.0f}s for it to safe its outputs")
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass
        else:
            log("  no session process running")

    for signum in _SIGNAL_NAMES:
        try:
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass

    try:
        while True:
            log(" * Restarting with supervisor (graceful shutdown enabled)")
            child = subprocess.Popen(_child_args(), env=env, close_fds=False)

            exit_code: Optional[int] = None
            while exit_code is None:
                try:
                    exit_code = child.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
                if stopping["signum"] is not None:
                    exit_code = _drain_child(child, log)
                    _annotate_record(stopping["signum"], exit_code)
                    log(f"{_BAR}\n")
                    return 0 if exit_code in (0, None) else exit_code

            # Werkzeug's file watcher exits the child with 3 to ask for a
            # restart; anything else is a real exit we should propagate.
            if exit_code != 3:
                return exit_code
    finally:
        if child and child.poll() is None:
            _drain_child(child, log)


def _drain_child(child: subprocess.Popen, log: Callable[[str], None]) -> Optional[int]:
    """Wait out the child's own shutdown; escalate to SIGKILL only if it hangs."""
    try:
        code = child.wait(timeout=CHILD_GRACE_S)
        log(f"  session process exited cleanly (code {code}) -- outputs safed")
        return code
    except subprocess.TimeoutExpired:
        log(
            f"  session process did not exit within {CHILD_GRACE_S:.0f}s -- "
            "escalating to SIGKILL"
        )
        log("  OUTPUTS MAY STILL BE ENERGIZED: enter_safe_state() did not finish.")
        child.kill()
        try:
            return child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return None


def _annotate_record(signum: int, exit_code: Optional[int]) -> None:
    """Stamp the signal onto the record the child just wrote.

    The child cannot name the signal itself: werkzeug replaces our SIGTERM
    handler in the reloader child, so from inside it the exit looks ordinary.
    The supervisor knows, so it fills the gap here -- this is what makes the UI
    say "ended on SIGTERM" rather than just "stopped".
    """
    record = _read_json(LAST_SHUTDOWN_PATH)
    if not record:
        return
    record["reason"] = "signal"
    record["signal_name"] = _SIGNAL_NAMES.get(signum)
    record["signal_number"] = signum
    record["initiator"] = "supervisor"
    record["child_exit_code"] = exit_code
    try:
        _write_json(LAST_SHUTDOWN_PATH, record)
    except OSError:
        pass
