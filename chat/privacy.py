"""
privacy.py
==========
Temporary chats. A temporary chat is its own kind of chat ("tmp-" id,
"temporary": True) that lives in memory only. Instead of every caller
remembering not to save it, the stores refuse it:

  * chat_store.ChatStore.save refuses a temporary chat, and refuses to save
    anything while a temporary chat's work runs;
  * memory, usage, past runs, run recordings, the search and page caches, the
    saved connection log and awareness learning check active() and refuse
    (or skip, where skipping is the documented behaviour);
  * attachments and tool scratch files go to the chat's own folder under
    %TEMP%\\AgenticWorkspace-temp\\<id>, deleted when the chat closes, when
    the app exits and - after a crash - at the next start (sweep).

A turn of a temporary chat runs inside scope(chat_id). The marker is a
context variable, so it follows the turn's own thread; thread pools the turn
starts carry it with wrap(fn). Work outside the turn (a routine on its
schedule, the idle screen) is not affected.

  new_temp_id() / is_temp_id(id) / is_temporary(chat)
  scope(chat_id)          the turn is working for this temporary chat
  active() / current()    inside one? which?
  refuse(what)            raises TemporaryChatError inside one (stores call it before writing)
  wrap(fn)                fn carrying the caller's marker (thread pools)
  temp_dir(chat_id)       the chat's folder (created on use)
  on_discard(fn)          fn(chat_id) forgets what a module keeps in memory of one
  discard(chat_id)        the chat closed: every on_discard cleanup runs and its folder goes
  sweep()                 at start and exit: every leftover folder goes
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import shutil
import tempfile
import uuid

ROOT = os.path.join(tempfile.gettempdir(), "AgenticWorkspace-temp")
NOTE = "Temporary chats aren't saved to history or memory. Actions performed outside the chat may still persist."
REMOTE_NOTE = ("Messages sent to a cloud model are also subject to that provider's own retention policy, "
               "which this app can't change.")

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("temporary_chat", default=None)


class TemporaryChatError(RuntimeError):
    """A store refused to keep something from a temporary chat."""


def new_temp_id() -> str:
    return "tmp-" + uuid.uuid4().hex[:12]


def is_temp_id(chat_id) -> bool:
    return isinstance(chat_id, str) and chat_id.startswith("tmp-")


def is_temporary(chat) -> bool:
    return isinstance(chat, dict) and (chat.get("temporary") is True or is_temp_id(chat.get("id")))


@contextlib.contextmanager
def scope(chat_id: str | None):
    """Mark this thread's work as a temporary chat's (a no-op for None)."""
    token = _CURRENT.set(chat_id) if chat_id else None
    try:
        yield
    finally:
        if token is not None:
            _CURRENT.reset(token)


def current() -> str | None:
    return _CURRENT.get()


def active() -> bool:
    return _CURRENT.get() is not None


def refuse(what: str):
    if active():
        raise TemporaryChatError(f"{what} isn't saved from a temporary chat.")


def wrap(fn):
    """fn, run with the caller's context in whatever thread calls it - this
    marker and every other context variable (the network layer's component,
    offline and private scopes). Each call gets its own copy, so a pool can
    run several at once."""
    ctx = contextvars.copy_context()

    def run(*args, **kwargs):
        return ctx.copy().run(fn, *args, **kwargs)
    return run


def temp_dir(chat_id: str, create: bool = True) -> str:
    if not is_temp_id(chat_id) or not chat_id[4:].isalnum():
        raise ValueError(f"'{chat_id}' is not a temporary chat.")
    path = os.path.join(ROOT, chat_id)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


_cleanups: list = []


def on_discard(fn):
    """Register fn(chat_id): forget what a module holds in memory of a temporary chat when it closes."""
    _cleanups.append(fn)
    return fn


def discard(chat_id: str | None, forget: bool = True) -> bool:
    """The chat closed: run every module's cleanup (forget=False: it was saved
    as a normal chat, so its traces may stay) and delete its folder. True when
    there was a folder."""
    if not is_temp_id(chat_id):
        return False
    for fn in list(_cleanups) if forget else []:
        try:
            fn(chat_id)
        except Exception:
            pass
    path = temp_dir(chat_id, create=False)
    existed = os.path.isdir(path)
    shutil.rmtree(path, ignore_errors=True)
    if os.path.isdir(path):
        # A process that is still exiting (the chat's browser) holds a file: try again shortly.
        # Whatever is left then goes at the next sweep (app exit or start).
        import threading
        import time

        def retry():
            for _ in range(10):
                time.sleep(1.0)
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.isdir(path):
                    return
        threading.Thread(target=retry, daemon=True, name="temp-chat-cleanup").start()
    return existed


def sweep() -> int:
    """Delete every temporary chat folder (a crash leaves them behind). Returns how many."""
    try:
        names = os.listdir(ROOT)
    except OSError:
        return 0
    for name in names:
        shutil.rmtree(os.path.join(ROOT, name), ignore_errors=True)
    return len(names)
