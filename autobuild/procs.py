"""Process control for session process groups: signal, kill-with-grace, and the
guarded SIGKILL of a crashed run's orphaned group."""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Callable

from .common import warn
from .session import RunningSession

def _signal_session(rs: RunningSession, sig: int) -> None:
    """Signal the session's whole process group (preferred) or the child directly.
    ESRCH — the group/child already exited — is suppressed so one dead session never
    aborts the harvest of the others."""
    with contextlib.suppress(ProcessLookupError):
        if rs.pgid is not None:
            os.killpg(rs.pgid, sig)
        elif rs.proc is not None:
            rs.proc.send_signal(sig)


def _kill_group(rs: RunningSession, grace: float) -> None:
    """Terminate one session and reap its child (see `_kill_groups`)."""
    _kill_groups([rs], grace)


def _kill_groups(sessions: list[RunningSession], grace: float,
                 should_stop: Callable[[], bool] = lambda: False) -> None:
    """Terminate sessions in parallel and reap their children: SIGTERM every live group,
    wait ONE shared grace (so N sessions cost 1 x grace, not N x grace), then SIGKILL the
    remainder and wait() each, leaving no zombie. Already-exited sessions are just reaped.
    `should_stop()` is polled during the grace; True skips the rest of it (a forced
    shutdown)."""
    live = [rs for rs in sessions if rs.proc is not None and rs.proc.poll() is None]
    for rs in live:
        _signal_session(rs, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while live and not should_stop():
        live = [rs for rs in live if rs.proc.poll() is None]  # poll() reaps the exited
        remaining = deadline - time.monotonic()
        if not live or remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
    for rs in live:
        if rs.proc.poll() is None:
            _signal_session(rs, signal.SIGKILL)
            rs.proc.wait()  # SIGKILL is uncatchable; wait unbounded so the child is always
            #                 reaped (a bounded wait could leak a zombie)

def _kill_orphan_group(pgid: object) -> bool:
    """SIGKILL an orphaned session's process group — a crashed run's detached `claude`
    subtree (reparented to init). Returns True if we signalled it.

    Heavily guarded, because os.killpg(pgid) maps to kill(-pgid): pgid 0 == OUR OWN group
    (would SIGKILL the running harness), pgid 1 == kill(-1) == a BROADCAST SIGKILL to every
    process we may signal, and the harness's own group must never be hit by a corrupted
    meta. We also suppress ESRCH (group already gone) AND EPERM (a recycled pgid we may not
    signal) so one un-killable orphan never aborts the whole reconcile sweep.

    CAVEAT — pgid reuse: a recorded pgid shares the pid number space, so after the original
    leader exited and was reaped the OS can recycle the number into an unrelated group (>1);
    worst case this signals that group. The window is bounded — reconcile runs only under
    the run lock, at startup shortly after a crash, on a host where autobuild is the only
    thing spawning detached groups, so a collision needs that exact number recycled since
    the crash. A portable leader-identity check isn't feasible without a new dependency (no
    /proc on macOS); a stronger guard is deferred as future hardening."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return False
    if pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False  # group already gone — nothing to reclaim
    except PermissionError:
        warn(f"cannot signal orphaned process group {pgid} (permission denied); skipping")
        return False
