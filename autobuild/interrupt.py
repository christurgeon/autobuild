"""Cooperative Ctrl-C / SIGTERM handling for `run`.

The handler only sets a flag; the supervisor polls it at points where its in-memory
state is consistent. Raising from the handler instead could fire between `Popen` and
`running.append` (dropping a live session) or between a merge and `reaped.json`.

Ladder: first signal -> graceful shutdown (`requested`); second -> skip the kill grace
(`forced`) and restore SIG_DFL, so a third signal kills the harness outright.

A leaf module (imports nothing from autobuild), so reaper, report, and supervisor can
all import it without a cycle."""

from __future__ import annotations

import contextlib
import os
import signal
import threading

_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_state: dict = {"requested": False, "forced": False, "signum": None}


class RunInterrupted(RuntimeError):
    """`run` stopped on an operator signal after a graceful shutdown. The CLI exits
    128 + signum (130 for SIGINT, 143 for SIGTERM)."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"run interrupted by {signal.Signals(signum).name}")


def reset() -> None:
    _state.update(requested=False, forced=False, signum=None)


def requested() -> bool:
    return _state["requested"]


def forced() -> bool:
    return _state["forced"]


def signum() -> int | None:
    return _state["signum"]


def _say(msg: str) -> None:
    # os.write, not print: print() inside a signal handler can hit a reentrant-call
    # RuntimeError if the main thread was itself mid-print on the same stream.
    with contextlib.suppress(OSError):
        os.write(2, f"\033[1;33m[warn]\033[0m {msg}\n".encode())


def _on_signal(signum: int, frame) -> None:
    if not _state["requested"]:
        _state.update(requested=True, signum=signum)
        _say("interrupt received: stopping running sessions... "
             "(press Ctrl-C again to kill immediately)")
        return
    _state["forced"] = True
    for sig in _SIGNALS:
        signal.signal(sig, signal.SIG_DFL)
    _say("second interrupt: killing sessions now (a third Ctrl-C kills autobuild)")


@contextlib.contextmanager
def install():
    """Install the handler for SIGINT and SIGTERM for the block's duration; restore the
    previous handlers on exit. State is reset on entry and exit. No-op off the main
    thread (signal.signal raises ValueError there)."""
    reset()
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in _SIGNALS}
    for sig in _SIGNALS:
        signal.signal(sig, _on_signal)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            if handler is not None:  # None = installed outside Python; cannot restore
                signal.signal(sig, handler)
        reset()
