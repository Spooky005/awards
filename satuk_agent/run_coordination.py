#!/usr/bin/env python3
"""Cross-process coordination helpers for parallel agent runs.

Two problems this module solves on Windows:

1. **Liveness probing.** ``os.kill(pid, 0)`` is *not* a safe liveness probe
   on Windows -- it raises ``OSError [WinError 11]`` for a process that is
   alive but owned by a different integrity level, which made
   ``agent.py`` believe a live run was dead and steal its lock.  We use
   ``OpenProcess`` + ``GetExitCodeProcess`` instead (POSIX keeps the
   ``os.kill`` probe).

2. **Mutual exclusion for shared files.** The vote counter and the
   submission log are shared by every run.  ``file_lock`` provides a
   cross-process mutex so concurrent runs cannot interleave a CSV row
   or mint the same vote number.

File claiming is also handled here: a run "claims" a vote file by
atomically renaming it into a private per-run directory, so two runs can
never work on the same file.
"""
import errno
import json
import os
import time
from pathlib import Path

__all__ = [
    "process_alive",
    "process_start_time",
    "file_lock",
    "ClaimManager",
    "CLAIM_DIR_NAME",
]

CLAIM_DIR_NAME = ".claims"

# Windows constant: STILL_ACTIVE (259) is the exit code of a running process.
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# --------------------------------------------------------------------------
# Process liveness / identity
# --------------------------------------------------------------------------

def _windows_process_info(pid):
    """Return ``(alive, create_time)`` for *pid* on Windows, else ``None``."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - non-Windows
        return None

    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return (False, None)
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return (False, None)
        alive = code.value == _STILL_ACTIVE
        # GetProcessTimes -> creation time (used to detect PID reuse).
        created = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        start = None
        if k32.GetProcessTimes(handle, ctypes.byref(created),
                               ctypes.byref(exit_time), ctypes.byref(kernel),
                               ctypes.byref(user)):
            # wintypes.FILETIME is a struct: combine its two 32-bit halves.
            start = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return (alive, start)
    finally:
        k32.CloseHandle(handle)


def process_alive(pid):
    """True if a process with *pid* is currently running.

    Windows-safe: uses the Win32 API rather than ``os.kill(pid, 0)``,
    which reports false negatives for processes owned by another
    integrity level.  Returns ``False`` for PIDs that do not exist.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        info = _windows_process_info(pid)
        if info is not None:
            return bool(info[0])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not signal it.
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        if e.errno == errno.EPERM:
            return True
        return False
    return True


def process_start_time(pid):
    """Best-effort process creation marker, used to detect PID reuse.

    Returns an int on Windows (the Win32 creation FILETIME) or a float
    epoch seconds on POSIX, or ``None`` when it cannot be determined.
    """
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        info = _windows_process_info(pid)
        if info is not None and info[1] is not None:
            return int(info[1])
        return None
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            fields = fh.read().rsplit(") ", 1)[1].split()
        starttime = int(fields[19])
        hz = os.sysconf("SC_CLK_TCK")
        return starttime / float(hz)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Cross-process file lock
# --------------------------------------------------------------------------

class _LockHandle:
    """Context manager yielding True when the lock was acquired."""

    def __init__(self, fh, acquired):
        self.fh = fh
        self.acquired = acquired

    def __enter__(self):
        return self.acquired

    def __exit__(self, *exc):
        self.release()
        return False

    def release(self):
        if self.fh is None:
            return
        try:
            if self.acquired:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.fh.close()
        except Exception:
            pass
        self.fh = None


def file_lock(path, timeout=30.0, poll=0.05):
    """Acquire an exclusive cross-process lock on *path*.

    Returns a context manager.  ``with file_lock(p) as ok:`` gives
    ``ok == True`` when the lock was taken.  On Windows this uses
    ``msvcrt.locking`` (a byte-range lock on the first byte); on POSIX
    it uses ``fcntl.flock``.  The lock file is never deleted, so the
    lock is released automatically when the process exits.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(lock_path, "a+b")
    except OSError:
        return _LockHandle(None, False)

    # Ensure at least one byte exists so the range lock is valid.
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
    except OSError:
        fh.close()
        return _LockHandle(None, False)

    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _LockHandle(fh, True)
        except OSError as e:
            # EACCES/EDEADLK -> locked by someone else; retry until timeout.
            if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                fh.close()
                return _LockHandle(None, False)
            if time.time() >= deadline:
                fh.close()
                return _LockHandle(None, False)
            time.sleep(poll)


# --------------------------------------------------------------------------
# Per-file claiming
# --------------------------------------------------------------------------

class ClaimManager:
    """Lets several runs work on one folder without ever sharing a file.

    A run "claims" a vote file by atomically renaming it into its own
    private directory (``<folder>/.claims/<run_id>/``).  ``os.rename``
    is atomic and fails when the source is already gone, so exactly one
    run can win a given file.

    Claims carry the owner PID and a timestamp so a crashed run's
    files can be recovered by the next run.
    """

    def __init__(self, folder, run_id, stale_seconds=7200):
        self.folder = Path(folder)
        self.run_id = str(run_id)
        self.stale_seconds = stale_seconds
        self.claims_dir = self.folder / CLAIM_DIR_NAME
        self.my_dir = self.claims_dir / self.run_id

    # -- internals --------------------------------------------------------

    def _meta_path(self, claim_file):
        return claim_file.with_suffix(claim_file.suffix + ".claim.json")

    def _write_meta(self, claim_file, original_name):
        meta = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "pid_start": process_start_time(os.getpid()),
            "started": time.time(),
            "original_name": original_name,
        }
        try:
            self._meta_path(claim_file).write_text(
                json.dumps(meta), encoding="utf-8")
        except Exception:
            pass

    def _read_meta(self, claim_file):
        try:
            return json.loads(self._meta_path(claim_file).read_text(
                encoding="utf-8"))
        except Exception:
            return {}

    # -- public API -------------------------------------------------------

    def claim(self, path):
        """Try to claim *path*.

        Returns the new claim path on success, or ``None`` if another
        run already owns the file (or it vanished).
        """
        path = Path(path)
        try:
            self.my_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        dest = self.my_dir / path.name
        try:
            os.rename(path, dest)
        except (FileNotFoundError, OSError):
            return None
        self._write_meta(dest, path.name)
        return dest

    def release(self, claim_file, restore=True):
        """Give a claimed file back to the pool (or delete it).

        ``restore=True`` moves the file back into the source folder;
        ``restore=False`` deletes it (used after a recorded vote when
        ``--delete-voted`` is on).
        """
        claim_file = Path(claim_file)
        meta = self._read_meta(claim_file)
        original = meta.get("original_name") or claim_file.name
        try:
            self._meta_path(claim_file).unlink()
        except OSError:
            pass
        if not restore:
            try:
                claim_file.unlink()
            except OSError:
                pass
            return None
        dest = self.folder / original
        try:
            os.rename(claim_file, dest)
        except OSError:
            return None
        return dest

    def quarantine(self, claim_file, dest_dir):
        """Move a claimed file into *dest_dir* (atomic rename).

        Used for failed votes so the file leaves the pool without ever
        re-appearing in the source folder (no window for another run to
        pick it up).
        """
        claim_file = Path(claim_file)
        meta = self._read_meta(claim_file)
        original = meta.get("original_name") or claim_file.name
        try:
            self._meta_path(claim_file).unlink()
        except OSError:
            pass
        dest_dir = Path(dest_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / original
            os.rename(claim_file, dest)
            return dest
        except OSError:
            return None

    def recover_stale(self):
        """Return claims owned by dead/expired runs to the pool.

        Returns the number of files recovered.  Claims owned by a live
        process are left alone, no matter how old they are: a worker may
        legitimately hold a claim for longer than ``stale_seconds`` (for
        example while waiting out a long scheduling gap), and taking it
        would let two runs vote for the same voter.

        The owner is considered dead only when the PID no longer exists
        *or* the PID has been reused by a different process (detected by
        comparing the recorded creation marker with the live one).
        """
        recovered = 0
        if not self.claims_dir.is_dir():
            return 0
        for run_dir in self.claims_dir.iterdir():
            if not run_dir.is_dir() or run_dir.name == self.run_id:
                continue
            for claim_file in list(run_dir.iterdir()):
                if claim_file.suffix == ".json":
                    continue
                meta = self._read_meta(claim_file)
                owner_pid = int(meta.get("pid", 0) or 0)
                if owner_pid and self._owner_is_alive(meta, owner_pid):
                    # Owner is alive and really owns this file -> leave it.
                    continue
                original = meta.get("original_name") or claim_file.name
                dest = self.folder / original
                try:
                    os.rename(claim_file, dest)
                    recovered += 1
                except OSError:
                    continue
                try:
                    self._meta_path(claim_file).unlink()
                except OSError:
                    pass
        return recovered

    @staticmethod
    def _owner_is_alive(meta, owner_pid):
        """True when *owner_pid* is still the process that made the claim.

        Guards against PID reuse: if the recorded creation marker differs
        from the live process's marker, the original owner is gone and the
        PID now belongs to somebody else.
        """
        if not process_alive(owner_pid):
            return False
        recorded_start = meta.get("pid_start")
        if recorded_start is None:
            # No marker recorded (older claim): fall back to liveness only.
            return True
        live_start = process_start_time(owner_pid)
        if live_start is None:
            return True
        try:
            return int(recorded_start) == int(live_start)
        except (TypeError, ValueError):
            return True

    def cleanup_claims_dir(self):
        """Remove empty claim directories (tidy-up, best effort)."""
        if not self.claims_dir.is_dir():
            return
        for run_dir in self.claims_dir.iterdir():
            if not run_dir.is_dir():
                continue
            try:
                if not any(run_dir.iterdir()):
                    run_dir.rmdir()
            except OSError:
                pass

