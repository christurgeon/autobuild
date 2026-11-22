"""Process control for session process groups: signal, kill-with-grace, and the
guarded SIGKILL of a crashed run's orphaned group."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

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
    """Terminate a session and reap its child. If already exited, just reap. Otherwise
    SIGTERM -> wait(grace) -> SIGKILL -> wait(), leaving no zombie."""
    proc = rs.proc
    if proc is None:
        return
    if proc.poll() is not None:
        return  # already exited: poll() reaped it
    _signal_session(rs, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return  # exited within grace -> reaped
    except subprocess.TimeoutExpired:
        pass
    _signal_session(rs, signal.SIGKILL)
    proc.wait()  # SIGKILL is uncatchable; wait unbounded so the child is always reaped
    #              (a bounded wait could time out and leak a zombie the loop won't reclaim)

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
