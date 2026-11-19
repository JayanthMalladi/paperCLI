"""Detached worker launch (Phase 6, spec sections 4/5/12).

Launches the Phase-6 :class:`~papercli.jobs.worker.RunWorker` as a detached
child process through NATIVE process APIs: an argument array plus
``CREATE_NEW_PROCESS_GROUP`` on Windows / ``start_new_session`` on POSIX --
never a shell string, never shell interpolation (spec section 12). Closing
the originating terminal therefore does not kill the job.

Durable coordination around one run (all under the run dir):

* **Per-run lock** -- ``run.lock`` marker (pid + start ts). Advisory:
  :func:`acquire_run_lock` reclaims it when the holder PID is dead or its
  heartbeat age exceeds :data:`STALE_AFTER_SECONDS` -- a lock is
  detectable-stale, and a resume can reclaim it (spec sections 11/12).
* **Handshake** -- the child writes ``handshake.json`` (accepted + start +
  its PID) once it holds the lock. A launch counts as "accepted" ONLY when
  the handshake names the launched child (spec section 6).
* **Heartbeat** -- the child refreshes ``heartbeat.json`` periodically
  (wall + monotonic + PID). Status derives RUNNING vs
  STALE-possibly-interrupted from the heartbeat AGE, NEVER from a PID
  alone (spec section 12).

Windows-safe throughout (pathlib, arg arrays, no ``os.fork``). Secrets are
never serialized here: the child reconstructs its config from the run's
saved config snapshot + the inherited process environment (spec section 13).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from papercli.storage.db import PaperStore, utc_now_iso

__all__ = [
    "STALE_AFTER_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "TERMINAL_STATES",
    "STARTABLE_STATES",
    "LaunchError",
    "RunNotStartable",
    "RunLockHeld",
    "BriefChangedError",
    "HandshakeTimeout",
    "LaunchResult",
    "RunLockGuard",
    "acquire_run_lock",
    "launch_worker",
    "run_child",
    "lock_held",
    "is_lock_stale",
    "heartbeat_path",
    "heartbeat_age_seconds",
    "pid_alive",
]

#: A run whose heartbeat is older than this is STALE-possibly-interrupted
#: (spec section 12). Measured against the heartbeat WALL timestamp.
STALE_AFTER_SECONDS = 60.0

#: How often the child's background heartbeat refreshes ``heartbeat.json``.
DEFAULT_HEARTBEAT_INTERVAL = 10.0

#: Run states from which no new dispatch is possible.
TERMINAL_STATES = ("completed", "partial", "failed", "cancelled")

#: States a fresh launch may start from.
STARTABLE_STATES = ("planned", "running", "queued")


# ---------------------------------------------------------------------------
# Typed launch errors (the CLI maps these to the spec-6 exit codes)
# ---------------------------------------------------------------------------

class LaunchError(Exception):
    """Base for launch failures: the CLI maps them to exit 2 (a run that
    cannot be started / a preflight failure, spec section 6)."""

    code = "LAUNCH_FAILED"

    def __init__(self, message: str,
                 details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class RunNotStartable(LaunchError):
    """The run does not exist or is in a terminal state (exit 2)."""

    code = "RUN_NOT_STARTABLE"


class RunLockHeld(LaunchError):
    """Another LIVE worker holds this run's lock (exit 2)."""

    code = "RUN_LOCK_HELD"


class BriefChangedError(LaunchError):
    """The brief/config hash no longer matches the run's protocol hash
    (spec section 5: a changed brief or config is a NEW revision, never a
    silent resume). The CLI maps it to exit 2 with new-revision guidance."""

    code = "BRIEF_CHANGED"


class HandshakeTimeout(LaunchError):
    """The child never handshaked within the bound: startup is NOT
    confirmed, so a launch must not be reported accepted (spec 6/12)."""

    code = "HANDSHAKE_TIMEOUT"


@dataclass
class LaunchResult:
    """What ``launch_worker`` hands back (all paths under the run dir)."""

    run_id: str
    pid: int
    executable: str
    args: list[str] = field(default_factory=list)
    start_ts: str = ""
    lock_file: Path | None = None
    handshake_file: Path | None = None
    work_dir: Path | None = None
    process: subprocess.Popen | None = None
    handshake: dict[str, Any] | None = None
    exit_code: int | None = None


# ---------------------------------------------------------------------------
# PID liveness (a PID alone is never trusted -- it is combined with the
# heartbeat age, spec section 12)
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """Best-effort liveness check for ``pid`` on this host."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_INFORMATION = 0x0400
        # Still-active exit codes: 255 is documented; 259 is the
        # undocumented value Windows reports for some running processes.
        _ACTIVE_EXIT_CODES = (255, 259)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION, False, int(pid))
        if handle == 0:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot read the state: assume alive (safe)
            # An exited process keeps its object alive while any handle
            # (e.g. Popen's) stays open: the exit code, not OpenProcess
            # success, is the signal.
            return code.value in _ACTIVE_EXIT_CODES
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Run-dir coordination helpers
# ---------------------------------------------------------------------------

def _work_dir_for(store: PaperStore, run_id: str) -> Path:
    from papercli.exports.run_summary import run_dir
    return run_dir(store, run_id)


def heartbeat_path(store: PaperStore, run_id: str) -> Path:
    return _work_dir_for(store, run_id) / "heartbeat.json"


def lock_file_path(store: PaperStore, run_id: str) -> Path:
    return _work_dir_for(store, run_id) / "run.lock"


def _parse_wall(value: Any) -> float | None:
    """A heartbeat wall timestamp -> epoch seconds (None if unreadable)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def heartbeat_age_seconds(store: PaperStore, run_id: str,
                          now_fn: Callable[[], Any] | None = None) -> float | None:
    """Age of the run's heartbeat in seconds (None when there is none)."""
    path = heartbeat_path(store, run_id)
    if not path.exists():
        return None
    try:
        hb = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    ts = _parse_wall(hb.get("wall"))
    if ts is None:
        return None
    now = now_fn() if now_fn is not None else time.time()
    now_ts = _parse_wall(now)
    if now_ts is None:
        return None
    return max(0.0, now_ts - ts)


def _write_json_atomic(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def lock_held(store: PaperStore, run_id: str,
              now_fn: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Inspect the run's lock marker without touching it.

    Returns ``{"held": bool, "holder_pid": int | None, "stale": bool,
    "heartbeat_age": float | None, "reason": str}``. ``stale`` is True
    when the holder is dead OR the heartbeat is too old -- i.e. the lock
    is RECLAIMABLE (spec sections 11/12). A live holder with a fresh
    heartbeat is held and NOT stale.
    """
    path = lock_file_path(store, run_id)
    if not path.exists():
        return {"held": False, "holder_pid": None, "stale": False,
                "heartbeat_age": None, "reason": "no lock"}
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        marker = {}
    pid = int(marker.get("pid") or 0)
    age = heartbeat_age_seconds(store, run_id, now_fn=now_fn)
    if not pid_alive(pid):
        return {"held": True, "holder_pid": pid or None, "stale": True,
                "heartbeat_age": age,
                "reason": "holder pid is not running"}
    if age is not None and age > STALE_AFTER_SECONDS:
        return {"held": True, "holder_pid": pid, "stale": True,
                "heartbeat_age": age,
                "reason": (f"heartbeat stale ({age:.0f}s > "
                            f"{STALE_AFTER_SECONDS:.0f}s)")
                }
    return {"held": True, "holder_pid": pid, "stale": False,
            "heartbeat_age": age, "reason": "live worker holds the lock"}


def is_lock_stale(store: PaperStore, run_id: str,
                  now_fn: Callable[[], Any] | None = None) -> bool:
    """True when the lock is held but RECLAIMABLE (dead holder or stale
    heartbeat, spec section 12)."""
    info = lock_held(store, run_id, now_fn=now_fn)
    return bool(info.get("held") and info.get("stale"))


# ---------------------------------------------------------------------------
# The per-run lock (advisory holder marker, reclaimed when stale)
# ---------------------------------------------------------------------------

class RunLockGuard:
    """Advisory per-run lock: a ``run.lock`` marker (pid + start ts) plus,
    on POSIX, a flock on the same file so two live acquirers cannot
    corrupt the marker. The OS release-on-exit of the flock handle is what
    makes it safe to hold across the child's whole life; on Windows the
    marker + holder-PID liveness check carries the semantics (no flock,
    no ``os.fork``).

    The guard optionally runs a background heartbeat thread (the child's
    periodic ``heartbeat.json`` refresh); release deletes the marker
    (normal close path). A child that is SIGKILLed never releases -- its
    marker then names a dead PID and/or a stale heartbeat, which
    :func:`acquire_run_lock` reclaims (spec section 12).
    """

    def __init__(self, store: PaperStore, run_id: str,
                 *, interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                 background_heartbeat: bool = True,
                 now_fn: Callable[[], Any] | None = None):
        self.store = store
        self.run_id = run_id
        self.work_dir = _work_dir_for(store, run_id)
        self.lock_file = self.work_dir / "run.lock"
        self.heartbeat_file = self.work_dir / "heartbeat.json"
        self.interval = max(0.2, float(interval))
        self._now_fn = now_fn
        self._fh: Any = None
        self._thread: threading.Thread | None = None
        self._stop = False
        self._pid = os.getpid()
        self._released = False
        if background_heartbeat:
            self._start_heartbeat_thread()

    # -- acquire -----------------------------------------------------------
    @staticmethod
    def _check_and_reclaim(store: PaperStore, run_id: str,
                           now_fn: Callable[[], Any] | None) -> dict[str, Any]:
        info = lock_held(store, run_id, now_fn=now_fn)
        if not info.get("held"):
            return info
        if not info.get("stale"):
            return info
        # Reclaim the stale marker: dead holder or stale heartbeat. The
        # crashed holder's stale heartbeat is removed with it, otherwise
        # the NEW holder (fresh, alive) would read its predecessor's
        # heartbeat age and be flagged stale in turn.
        for p in (lock_file_path(store, run_id),
                  heartbeat_path(store, run_id)):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        return {"held": False, "holder_pid": None, "stale": False,
                "heartbeat_age": None, "reason": "reclaimed stale lock"}

    def acquire(self) -> None:
        self._check_and_reclaim(self.store, self.run_id, self._now_fn)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        marker = {"pid": self._pid, "start_ts": utc_now_iso(), "ts": utc_now_iso()}
        # Exclusive-creation marker (O_EXCL via mode "xb"): only ONE racing
        # child can create a FRESH lock marker; a loser re-checks and, if the
        # holder is live, is rejected by the double-check below. This closes
        # the write->check->write->check TOCTOU window on Windows (D-P6-3).
        # A torn/invalid marker self-heals: it reads as pid 0 -> stale ->
        # reclaimable.
        # "x" = exclusive creation (O_EXCL): only ONE racing child can create
        # a FRESH marker; a loser self-heals below.
        try:
            with open(self.lock_file, "x", encoding="utf-8") as fh:
                fh.write(json.dumps(marker))
        except FileExistsError:
            info = self._check_and_reclaim(self.store, self.run_id, self._now_fn)
            if info.get("reason") == "reclaimed stale lock":
                with open(self.lock_file, "x", encoding="utf-8") as fh:
                    fh.write(json.dumps(marker))
        if os.name == "posix":
            import fcntl
            self._fh = open(str(self.lock_file), "a+")
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # A live acquirer holds the flock: we lost the race.
                self._fh.close()
                self._fh = None
                raise RunLockHeld(
                    f"run {self.run_id}: another live worker holds the "
                    f"lock (advisory flock)", {"run_id": self.run_id})
        # Double-check after the marker write (the flock check, where
        # available, already serialized it).
        info = lock_held(self.store, self.run_id, self._now_fn)
        if (info.get("holder_pid") is not None
                and info["holder_pid"] != self._pid
                and not info.get("stale")):
            self._release_quiet()
            raise RunLockHeld(
                f"run {self.run_id}: another live worker holds the lock "
                f"(pid {info['holder_pid']}); cancel or wait, then resume",
                {"run_id": self.run_id,
                 "holder_pid": info["holder_pid"]})

    def _start_heartbeat_thread(self) -> None:
        def loop():
            while not self._stop:
                try:
                    _write_json_atomic(self.heartbeat_file, {
                        "monotonic": time.monotonic(),
                        "wall": time.time(),
                        "wall_iso": utc_now_iso(),
                        "pid": self._pid,
                        "ts": utc_now_iso(),
                        "source": "lock_guard",
                    })
                except OSError:
                    break
                time.sleep(self.interval)

        self._thread = threading.Thread(target=loop, daemon=True,
                                        name=f"lock-hb-{self.run_id}")
        self._thread.start()

    # -- release -----------------------------------------------------------
    def _release_quiet(self) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self._fh.close()
            self._fh = None
        try:
            if self.lock_file.exists() and _read_marker_pid(self.lock_file) == self._pid:
                self.lock_file.unlink()
        except OSError:
            pass

    def __enter__(self) -> "RunLockGuard":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    # release() is idempotent: the worker closes its own guard AND the
    # child's run_child closes it again on the normal path.
    def release(self) -> None:
        """Delete the marker on the normal close path (the child's lock is
        RELEASED by the child on normal close, spec 12). Idempotent."""
        if getattr(self, "_released", False):
            return
        self._released = True
        self._release_quiet()


def _read_marker_pid(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("pid") or 0)
    except (ValueError, OSError):
        return 0


def acquire_run_lock(store: PaperStore, run_id: str, *,
                     interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                     background_heartbeat: bool = True,
                     now_fn: Callable[[], Any] | None = None) -> RunLockGuard:
    """Acquire the run's advisory lock, reclaiming STALE locks only.

    A live holder with a fresh heartbeat raises :class:`RunLockHeld`
    (the CLI maps it to exit 2); a stale lock (dead holder / heartbeat
    age past :data:`STALE_AFTER_SECONDS`) is reclaimed (spec 11/12).
    """
    guard = RunLockGuard(store, run_id, interval=interval,
                         background_heartbeat=background_heartbeat,
                         now_fn=now_fn)
    guard.acquire()
    return guard


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def _child_arg_array(store: PaperStore, run_id: str,
                     executable: str | None = None,
                     heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
                     handshake_token: str | None = None) -> list[str]:
    """The child's argument array (never a shell string, spec section 12).

    ``PAPERS_WORKER_MODULE`` selects the child module: the production
    worker or the offline test fake (``papercli.jobs.fake_worker_child``
    -- a documented test seam, never the production default)."""
    exe = str(executable) if executable else sys.executable
    module = os.environ.get("PAPERS_WORKER_MODULE", "papercli.jobs.worker")
    # Absolute db path: the child's CWD is the run dir, so a relative
    # path would resolve to the wrong database.
    db_path = str(Path(store.db_path).resolve())
    args = [
        exe,
        "-m",
        module,
        "--db-path", db_path,
        "--run-id", run_id,
        "--heartbeat-interval", f"{heartbeat_interval:g}",
    ]
    if handshake_token:
        args.extend(["--handshake-token", handshake_token])
    return args


def _spawn_default_child(store: PaperStore, run_id: str,
                         heartbeat_interval: float,
                         handshake_token: str | None = None) -> subprocess.Popen:
    """The production child: the project's own interpreter (never a bare
    'python' on PATH, never a shell) runs the worker entry.

    The child's stdout/stderr are redirected to ``worker-output.log`` in
    the run dir (crash diagnostics) so the detached worker NEVER holds the
    launcher's output stream: closing the originating terminal (or a piped
    caller waiting on EOF) does not kill the job, and a piped launch
    returns as soon as the worker handshakes (spec section 12)."""
    args = _child_arg_array(store, run_id,
                            heartbeat_interval=heartbeat_interval,
                            handshake_token=handshake_token)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP: closing the originating terminal must
        # not kill the detached job (spec section 12).
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    kwargs["cwd"] = str(_work_dir_for(store, run_id))
    # Redirect the child's stdout/stderr to a run-dir log file (the
    # structured worker log is worker.log.jsonl; this captures a crash
    # traceback). Kept open on the Popen handle so the child's inherited
    # handle stays valid for its whole life.
    out_path = _work_dir_for(store, run_id) / "worker-output.log"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_fh = open(str(out_path), "ab")
    kwargs["stdout"] = out_fh
    kwargs["stderr"] = out_fh
    proc = subprocess.Popen(args, **kwargs)
    proc._papercli_out = out_fh  # type: ignore[attr-defined]
    return proc


def _wait_for_handshake(handshake_file: Path, run_id: str,
                       timeout: float,
                       handshake_token: str | None = None) -> dict[str, Any]:
    """Boundedly wait for the child's durable handshake (spec 12: startup
    is confirmed by the HANDSHAKE file, never by a PID alone).

    The stale handshake is deleted before the child is spawned, and the
    child stamps it with a launch NONCE this launcher generated, so a
    handshake left behind by an earlier launch can never be mistaken for
    this one. (We do NOT require the handshake's pid to equal the
    launcher's Popen pid: on some hosts the process handle's pid differs
    from the child's own os.getpid().)"""
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        if handshake_file.exists():
            try:
                hs = json.loads(handshake_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                time.sleep(0.05)
                continue
            if hs.get("accepted") is False:
                raise LaunchError(
                    f"the worker rejected the launch: {hs.get('error')}",
                    {"run_id": hs.get("run_id"), "error": hs.get("error")})
            hs_id = hs.get("run_id")
            if hs_id not in (None, "", run_id):
                # A handshake for a different run -- keep waiting.
                time.sleep(0.05)
                continue
            if handshake_token is None or hs.get("token") == handshake_token:
                return hs
            # A handshake from another launch (token mismatch) -- keep
            # waiting for this launch's own handshake.
        time.sleep(0.05)
    raise HandshakeTimeout(
        "the worker did not handshake within the bound; the launch is NOT "
        "accepted (spec section 6)",
        {"run_id": run_id, "timeout_seconds": timeout})


def launch_worker(
    store: PaperStore,
    run_id: str,
    *,
    cfg: Any = None,
    detach: bool = True,
    detach_timeout_seconds: float = 30.0,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    now_fn: Callable[[], Any] | None = None,
    child_fn: Callable[..., subprocess.Popen | None] | None = None,
    wait_deadline_seconds: float = 600.0,
) -> LaunchResult:
    """Start this run's detached worker and WAIT for its handshake.

    A launch is "accepted" ONLY when the child handshaked + acquired its
    lock -- not when the review completed (spec section 6). Steps: verify
    the run is startable -> check/reclaim the per-run lock -> spawn the
    child via native process APIs -> wait for the handshake.

    ``child_fn`` is an injection point for tests (a fake/tiny worker); by
    default it spawns ``<project interpreter> -m papercli.jobs.worker``.
    """
    row = store.run_row(run_id)
    if row is None:
        raise RunNotStartable(f"run not found: {run_id}", {"run_id": run_id})
    if row.get("state") in TERMINAL_STATES:
        raise RunNotStartable(
            f"run {run_id} is in terminal state '{row.get('state')}' and "
            f"cannot be launched; use `papers review resume` for an "
            f"interrupted run, or start a new revision "
            f"(`papers review plan`)",
            {"run_id": run_id, "state": row.get("state")})
    if row.get("state") not in STARTABLE_STATES:
        raise RunNotStartable(
            f"run {run_id} state '{row.get('state')}' is not startable",
            {"run_id": run_id, "state": row.get("state")})

    # Per-run lock: a live holder with a fresh heartbeat rejects a second
    # launch (exit 2); a stale lock is reclaimed (spec 12).
    info = RunLockGuard._check_and_reclaim(store, run_id, now_fn)
    if info.get("held") and not info.get("stale"):
        raise RunLockHeld(
            f"run {run_id}: a live worker holds the lock "
            f"(pid {info.get('holder_pid')}); cancel it or wait, then "
            f"resume",
            {"run_id": run_id, "holder_pid": info.get("holder_pid"),
             "reason": info.get("reason")})

    work_dir = _work_dir_for(store, run_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    handshake_file = work_dir / "handshake.json"
    # A previous launch's handshake must not be mistaken for THIS launch's.
    if handshake_file.exists():
        try:
            handshake_file.unlink()
        except OSError:
            pass
    # The launch nonce: the child stamps its handshake with it (double
    # safety on top of the deletion above).
    import uuid
    handshake_token = uuid.uuid4().hex

    process: subprocess.Popen | None
    if child_fn is not None:
        process = child_fn(store=store, run_id=run_id, work_dir=work_dir,
                           cfg=cfg,
                           heartbeat_interval=heartbeat_interval,
                           handshake_token=handshake_token)
    else:
        process = _spawn_default_child(store, run_id, heartbeat_interval,
                                       handshake_token=handshake_token)
    pid = int(process.pid) if process is not None else os.getpid()

    handshake = _wait_for_handshake(handshake_file, run_id,
                                     detach_timeout_seconds,
                                    handshake_token=handshake_token)
    executable = (str(list(process.args)[0]) if process is not None
                   and getattr(process, "args", None) else
                  "inline")
    args = list(process.args) if process is not None and getattr(
        process, "args", None) else []
    result = LaunchResult(
        run_id=run_id, pid=pid, executable=executable, args=args,
        start_ts=utc_now_iso(),
        lock_file=lock_file_path(store, run_id),
        handshake_file=handshake_file, work_dir=work_dir,
        process=process, handshake=handshake,
    )
    if not detach:
        if process is not None:
            result.exit_code = int(process.wait(timeout=wait_deadline_seconds))
        # Foreground: the lock is released by the child on close; nothing
        # held here (the parent only checked it above).
    return result


# ---------------------------------------------------------------------------
# The detached child's main (invoked by ``papercli.jobs.worker``)
# ---------------------------------------------------------------------------

def run_child(store: PaperStore, run_id: str, *,
              worker_factory: Callable[[PaperStore, str, Any], Any] | None = None,
              heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
              handshake_token: str | None = None) -> int:
    """The child's main: hold the lock, write the durable handshake, run
    the worker, release the lock on every normal path.

    Returns the child exit code: 0 completed, 6 partial/failed/cancelled,
    1 internal failure. A hard-killed child never reaches the release --
    its lock is then detected stale (dead holder PID + heartbeat age) and
    reclaimed by a resume (spec sections 11/12).
    """
    from papercli.jobs.worker import RunWorker

    work_dir = _work_dir_for(store, run_id)
    guard = acquire_run_lock(store, run_id, interval=heartbeat_interval)
    try:
        handshake = {
            "accepted": True, "pid": os.getpid(), "run_id": run_id,
            "start_ts": utc_now_iso(),
        }
        if handshake_token:
            handshake["token"] = handshake_token
        _write_json_atomic(work_dir / "handshake.json", handshake)
        factory = worker_factory or (
            lambda s, rid, g: RunWorker(s, rid, lock_guard=g))
        worker = factory(store, run_id, guard)
        summary = worker.execute()
        state = str(summary.get("state"))
        if state == "completed":
            return 0
        if state in ("partial", "failed", "cancelled"):
            return 6
        return 0
    except Exception as e:  # noqa: BLE001 -- a child crash is never silent
        try:
            _write_json_atomic(work_dir / "handshake.json", {
                "accepted": False, "pid": os.getpid(), "run_id": run_id,
                "error": f"{type(e).__name__}: {e}",
            })
        except OSError:
            pass
        return 1
    finally:
        # The lock is RELEASED by the child on normal close (spec 12).
        guard.release()
