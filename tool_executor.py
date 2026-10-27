"""
tool_executor.py
================
Runs one tool call under the policy in its ToolSpec and returns a normalized,
classified result.

Per call:
  1. unknown tool            -> unavailable
  2. argument validation     -> permanent_failure (the tool never runs)
  3. cancellation            -> cancelled
  4. per-run tool budget     -> permanent_failure once exhausted
  5. identical failed call   -> not re-run; the earlier failure is returned
  6. cooldown                -> short ones are waited out, long ones refused
  7. run (isolated tools on a worker thread with a hard timeout)
  8. classify: success | retryable_failure | permanent_failure |
               approval_required | cancelled | unavailable
  9. retry ONLY retryable failures of retryable tools, bounded, with backoff

Tools that touch COM/UI Automation handles run inline on the calling thread
(their handles are apartment-bound); only tools marked isolate=True run on a
worker thread. A timed-out isolated tool may still finish in the background,
so a mutating tool that times out is reported as a permanent failure with an
"outcome unknown - check before retrying" note, never retried blindly.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

import privacy
from tool_registry import ToolRegistry, validate_args


def _waiting_on_user() -> bool:
    """A card a running tool showed (Web Search's query preview or fallback question) is open."""
    try:
        import prompt_prep
        return prompt_prep.gate.pending() is not None
    except Exception:
        return False


SUCCESS = "success"
RETRYABLE = "retryable_failure"
PERMANENT = "permanent_failure"
APPROVAL = "approval_required"
CANCELLED = "cancelled"
UNAVAILABLE = "unavailable"

# Message patterns for tools that report failure as {"ok": False, "message"}.
_RETRYABLE_RE = re.compile(r"timed? ?out|temporar|try again|busy|not ready|still loading|connection (?:reset|refused|error|aborted)|"
                           r"rate.?limit|429|503|502|unreachable|no response|bridge is not connected", re.I)
_UNAVAILABLE_RE = re.compile(r"not installed|windows only|is unavailable|not available|unknown tool|no (?:messaging )?provider|"
                             r"not configured|not supported", re.I)
_APPROVAL_RE = re.compile(r"\bREJECTED\b|requires approval|set to DENY|was not approved", re.I)
_BLOCKED_RE = re.compile(r"CURRENT-TURN GATE|INTENT GUARD|already executed recently|blocked as a duplicate", re.I)


@dataclass
class ToolResult:
    status: str
    tool: str
    message: str = ""
    raw: dict = field(default_factory=dict)
    attempts: int = 1
    duration_ms: int = 0
    tool_call_id: str | None = None
    task_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS

    def to_dict(self) -> dict:
        """The legacy result dict (ok/message/verified/image_b64/...) that the
        agent loop and UI already consume, plus the classification."""
        out = dict(self.raw)
        out.update(ok=self.ok, status=self.status, attempts=self.attempts, duration_ms=self.duration_ms)
        out["message"] = self.message
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.task_id:
            out["task_id"] = self.task_id
        return out


def classify(raw: dict) -> str:
    """Status for a raw tool result dict."""
    if not isinstance(raw, dict):
        return PERMANENT
    status = raw.get("status")
    if status in (SUCCESS, RETRYABLE, PERMANENT, APPROVAL, CANCELLED, UNAVAILABLE):
        return status
    if raw.get("ok"):
        return SUCCESS
    msg = str(raw.get("message") or "")
    if raw.get("blocked") or _BLOCKED_RE.search(msg):
        return PERMANENT
    if _APPROVAL_RE.search(msg):
        return APPROVAL
    if _UNAVAILABLE_RE.search(msg) or raw.get("fallback") == "unavailable":
        return UNAVAILABLE
    if raw.get("retryable") or _RETRYABLE_RE.search(msg):
        return RETRYABLE
    return PERMANENT


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return RETRYABLE
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return PERMANENT
    return RETRYABLE if _RETRYABLE_RE.search(str(exc)) else PERMANENT


_TARGET_KEYS = ("filename", "path", "destination", "output_zip", "source", "zip_path")


def _summary(name: str, args: dict) -> str:
    """One line for the undo trail, e.g. 'write_file C:\\x\\a.txt'."""
    target = next((str(args[k]) for k in _TARGET_KEYS if isinstance(args, dict) and args.get(k)), "")
    return f"{name} {target}".strip()[:200]


def _call_key(name: str, args: dict) -> str:
    clean = {k: v for k, v in (args or {}).items() if k not in ("justification", "force")}
    return name + ":" + json.dumps(clean, sort_keys=True, default=str)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, run_fn, *, is_cancelled=lambda: False,
                 checkpoint=None, clock=time.monotonic, sleep=time.sleep,
                 max_retries: int = 2, backoff_base: float = 0.5, backoff_cap: float = 4.0,
                 max_wait_cooldown: float = 2.0, on_result=None, undo=None):
        self.registry = registry
        self.run_fn = run_fn
        self.is_cancelled = is_cancelled
        self.checkpoint = checkpoint
        self.clock = clock
        self.sleep = sleep
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_wait_cooldown = max_wait_cooldown
        self.on_result = on_result
        self.undo = undo                      # undo_store.UndoStore, or None
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}
        self._task_id: str | None = None
        self._failures: dict[str, ToolResult] = {}
        self._history: list[dict] = []
        self._calls = 0
        self._budget: int | None = None

    # ------------------------------------------------------------ task scope
    def begin_task(self, task_id: str | None, tool_budget: int | None = None):
        """Start a fresh scope: failure memory, history and budget are per task."""
        with self._lock:
            self._task_id = task_id
            self._failures.clear()
            self._history = []
            self._calls = 0
            self._budget = tool_budget

    def set_budget(self, tool_budget: int | None):
        """Change the running task's tool budget (an effort change mid-run);
        calls already made still count."""
        with self._lock:
            self._budget = tool_budget

    def history(self) -> list:
        with self._lock:
            return list(self._history)

    @property
    def calls(self) -> int:
        return self._calls

    # ---------------------------------------------------------------- run
    def _run_once(self, spec, args, runner=None) -> dict:
        call = runner or (lambda: self.run_fn(spec.name, args))
        if not spec.isolate:
            return call()
        box = {}

        def _target():
            try:
                box["raw"] = call()
            except BaseException as e:      # noqa: BLE001 - reported below
                box["exc"] = e
        # privacy.wrap: the worker carries the turn's context - a temporary chat's marker, the
        # network layer's scopes - or a temporary chat's search would fill the shared caches.
        worker = threading.Thread(target=privacy.wrap(_target), daemon=True, name=f"tool-{spec.name}")
        worker.start()
        deadline = self.clock() + spec.timeout_s
        while worker.is_alive():
            worker.join(0.05)
            if self.is_cancelled():
                return {"status": CANCELLED, "ok": False, "message": f"{spec.name} was cancelled."}
            if _waiting_on_user():
                deadline = max(deadline, self.clock() + 5)     # a card the tool showed (search preview): the user's time
            if self.clock() >= deadline:
                if spec.read_only:
                    return {"status": PERMANENT, "ok": False,
                            "message": f"{spec.name} timed out after {spec.timeout_s:.0f}s; its worker may still be running. "
                                       "Automatic retry was suppressed to avoid overlapping work."}
                return {"status": PERMANENT, "ok": False,
                        "message": f"{spec.name} timed out after {spec.timeout_s:.0f}s; its outcome is unknown - "
                                   "check the actual state before trying again."}
        if "exc" in box:
            raise box["exc"]
        return box.get("raw") or {"ok": False, "message": f"{spec.name} returned nothing."}

    def execute(self, name: str, args: dict, *, tool_call_id: str | None = None, runner=None) -> dict:
        """`runner` (internal tools only): a zero-argument callable that performs
        an already-resolved action, instead of looking the tool up in run_fn.
        Everything else - validation, budget, classification, undo - is the same."""
        started = self.clock()

        def done(status, message, raw=None, attempts=1):
            res = ToolResult(status=status, tool=name, message=message, raw=raw or {}, attempts=attempts,
                             duration_ms=int((self.clock() - started) * 1000), tool_call_id=tool_call_id,
                             task_id=self._task_id)
            entry = res.to_dict()
            spec_ = self.registry.get(name)
            with self._lock:
                self._history.append({"tool": name, "args": args, "mutating": bool(spec_ and spec_.mutating),
                                      "internal": runner is not None, "verified": entry.get("verified"),
                                      **{k: entry[k] for k in ("status", "ok", "message", "attempts", "duration_ms") if k in entry}})
            if self.on_result:
                self.on_result(res)
            return res.to_dict()

        spec = self.registry.get(name)
        if spec is None:
            return done(UNAVAILABLE, f"Unknown tool '{name}'.")
        args, errors = validate_args(spec.input_schema, args)
        if errors:
            return done(PERMANENT, f"Invalid arguments for {name}: " + "; ".join(errors)
                        + ". Fix the arguments and call it again.")
        if self.checkpoint:
            self.checkpoint()                 # may raise RunStopped - that is the point
        if self.is_cancelled():
            return done(CANCELLED, f"{name} was not run: the task was cancelled.")

        with self._lock:
            # Internal routes (runner given) were approved as one fixed step by
            # the user and are not model work: they don't use the model's budget.
            if runner is not None:
                budget_hit = False
            elif self._budget is not None and self._calls >= self._budget:
                budget_hit = True
            else:
                budget_hit = False
                self._calls += 1
        if budget_hit:
            return done(PERMANENT, f"Tool budget for this task ({self._budget} calls) is used up. "
                                   "Give the user your answer with what you have.")

        key = _call_key(name, args)
        previous = self._failures.get(key)
        if previous is not None:
            return done(previous.status, f"Not repeated: this exact {name} call already failed in this task "
                                         f"({previous.message[:200]}). Change the approach or the arguments.")

        # Cooldown: wait out a short one, refuse a long one.
        if spec.cooldown_s:
            last = self._last_call.get(name)
            if last is not None:
                remaining = spec.cooldown_s - (self.clock() - last)
                if remaining > self.max_wait_cooldown:
                    return done(RETRYABLE, f"{name} is cooling down; it can run again in {remaining:.0f}s.")
                if remaining > 0:
                    self.sleep(remaining)

        # Undoable tools run inside a journal: everything they change is backed
        # up first. Success keeps the journal (the change can be undone later);
        # any failure, cancellation or crash replays it in reverse at once, so
        # no half-applied state is left behind.
        change = None
        if spec.undoable and self.undo is not None:
            try:
                change = self.undo.begin(name, _summary(name, args))
            except Exception as e:           # never run an undoable tool without its journal
                return done(PERMANENT, f"{name} was not run: the undo backup could not be prepared ({e}).")

        attempts, raw, status = 0, {}, PERMANENT
        max_attempts = 1 + (self.max_retries if spec.retryable else 0)
        try:
            while attempts < max_attempts:
                attempts += 1
                self._last_call[name] = self.clock()
                try:
                    raw = self._run_once(spec, args, runner)
                    status = classify(raw)
                except Exception as e:           # tool raised instead of returning a result
                    raw = {"ok": False, "message": f"{name} failed: {e}"}
                    status = _classify_exception(e)
                if status != RETRYABLE or not spec.retryable or attempts >= max_attempts:
                    break
                if self.is_cancelled():
                    status = CANCELLED
                    break
                self.sleep(min(self.backoff_cap, self.backoff_base * (2 ** (attempts - 1))))
        except BaseException:
            if change is not None:
                self.undo.rollback(change)       # e.g. RunStopped mid-tool
            raise
        if change is not None:
            if status == SUCCESS:
                undo_id = self.undo.commit(change)
                if undo_id:
                    raw = {**raw, "undo_id": undo_id}
            else:
                rolled = self.undo.rollback(change)
                if change.ops:
                    raw = {**raw, "message": str(raw.get("message") or "") + (
                        "\n[Any partial changes were rolled back.]" if rolled
                        else "\n[Partial changes could NOT be fully rolled back - the backup is kept in the undo trail.]")}

        message = str(raw.get("message") or ("ok" if status == SUCCESS else f"{name} failed."))
        if status == SUCCESS and spec.mutating:
            # The machine changed, so an earlier failure (e.g. "no such window"
            # before the app was opened) may succeed now - forget them.
            self._failures.clear()
        if status not in (SUCCESS,):
            suffix = {RETRYABLE: "temporary failure", PERMANENT: "failed", APPROVAL: "needs approval",
                      CANCELLED: "cancelled", UNAVAILABLE: "unavailable"}[status]
            message += f"\n[status: {status} - {suffix}{f', after {attempts} attempts' if attempts > 1 else ''}]"
            if status in (PERMANENT, RETRYABLE, UNAVAILABLE) and runner is None:
                # (internal routes are re-resolved each time, so a failure is not cached)
                self._failures[key] = ToolResult(status=status, tool=name, message=str(raw.get("message") or ""))
        return done(status, message, raw, attempts)
