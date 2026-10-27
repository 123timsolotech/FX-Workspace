"""One process-wide reservation for llama-server inference and lifecycle changes.

Acquisition order is runtime -> build/config locks. Reentrant so a completion
can select a build or load a model without deadlocking its own reservation.
"""
from contextlib import contextmanager
from functools import wraps
import threading

lock = threading.RLock()


@contextmanager
def reserve(wait=True, stop=None):
    """stop: called every quarter second while waiting - it raises to give up the wait (the run's Stop), so a reply
    queued behind a model load or another call can still be stopped."""
    if not wait:
        if not lock.acquire(blocking=False):
            raise RuntimeError('The local model is busy; retry when its current operation finishes.')
    elif stop is None:
        lock.acquire()
    else:
        while not lock.acquire(timeout=0.25):
            stop()
    try:
        yield
    finally:
        lock.release()


def exclusive(fn=None, *, stop=None):
    """Decorator: the call holds the reservation. @exclusive, or @exclusive(stop=...) for a wait Stop can end."""
    def wrap(f):
        @wraps(f)
        def run(*args, **kwargs):
            with reserve(stop=stop):
                return f(*args, **kwargs)
        return run
    return wrap(fn) if fn is not None else wrap


def busy():
    if not lock.acquire(blocking=False):
        return True
    lock.release()
    return False
