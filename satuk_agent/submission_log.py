#!/usr/bin/env python3
"""Persistent submission log + successful-vote counter for agent.py.

- Counter lives in .vote_counter.json (atomic write, survives restarts).
- Every attempt is appended to submission_log.csv with a uuid attempt_id;
  successes also carry the persistent vote_number + server-returned id.
- Log-write failures never raise (observability must not break submissions).

Counter counts server-confirmed successful votes only (not already-voted,
not failed). Nominations are logged but never increment the vote counter.
"""
import csv
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from run_coordination import file_lock

BASE_DIR = Path(__file__).resolve().parent
COUNTER_FILE = BASE_DIR / ".vote_counter.json"
LOG_FILE = BASE_DIR / "submission_log.csv"
COUNTER_LOCK_FILE = BASE_DIR / ".vote_counter.lock"
LOG_LOCK_FILE = BASE_DIR / ".submission_log.lock"

# How long to wait for a shared lock before giving up.  Generous enough for
# six concurrent workers to serialise on the counter without stalling.
COUNTER_LOCK_TIMEOUT = 60.0
LOG_LOCK_TIMEOUT = 60.0

LOG_HEADER = ["timestamp_utc", "vote_number", "attempt_id", "kind",
              "status", "file", "voter_name", "email",
              "category_id", "nominee_id", "nominee_name",
              "server_id", "error"]


def new_attempt_id():
    return uuid.uuid4().hex


def _read_counter():
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data.get("successful_votes", 0))
    except (FileNotFoundError, ValueError, TypeError,
            json.JSONDecodeError, AttributeError):
        return 0


def get_vote_count():
    """Current number of successful votes recorded to date."""
    return _read_counter()


def next_vote_number():
    """Increment the persistent success counter and return the new number.

    Call ONLY after submit_vote()/submit_nomination() succeeded.
    Atomic (temp file + os.replace) so Ctrl+C mid-write can't corrupt it.
    The read-modify-write is also guarded by a cross-process lock so two
    concurrent runs can never mint the same vote number.

    If the lock cannot be taken we retry and then raise: minting a number
    without the lock would hand the same number to two runs, which is far
    worse than losing the vote number for one attempt.
    """
    with file_lock(COUNTER_LOCK_FILE, timeout=COUNTER_LOCK_TIMEOUT) as locked:
        if not locked:
            raise RuntimeError(
                "could not acquire the vote-counter lock; refusing to "
                "allocate a vote number without it")
        return _next_vote_number_unlocked()


def _next_vote_number_unlocked():
    n = _read_counter() + 1
    dest_dir = COUNTER_FILE.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest_dir), prefix=".vote_counter",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"successful_votes": n}, fh)
        os.replace(tmp, COUNTER_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n


def recount_from_log():
    """Recompute successes from the CSV (recovery if counter file is lost)."""
    n = 0
    try:
        with open(LOG_FILE, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") == "success" and row.get("kind") == "vote":
                    n += 1
    except FileNotFoundError:
        pass
    return n


def log_submission(kind, status, file="", voter_name="", email="",
                   category_id="", nominee_id="", nominee_name="",
                   vote_number="", server_id="", error="", attempt_id=None):
    """Append one row to submission_log.csv. Never raises; returns the row dict."""
    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vote_number": vote_number if vote_number != "" else "",
        "attempt_id": attempt_id or new_attempt_id(),
        "kind": kind,
        "status": status,
        "file": file,
        "voter_name": voter_name,
        "email": str(email or "").strip().lower(),
        "category_id": category_id,
        "nominee_id": nominee_id,
        "nominee_name": nominee_name,
        "server_id": server_id,
        "error": str(error or "")[:300],
    }
    try:
        # Guard the append with a cross-process lock: without it, two
        # concurrent runs can interleave bytes and corrupt a CSV row.
        # If the lock cannot be taken we drop the row rather than write it
        # unlocked -- a torn row would corrupt the whole log for everyone.
        with file_lock(LOG_LOCK_FILE, timeout=LOG_LOCK_TIMEOUT) as locked:
            if not locked:
                print("    WARNING - submission log lock unavailable, "
                      "row not written")
            else:
                _append_row(row)
    except Exception as e:  # noqa: BLE001 - log must never break submissions
        print(f"    WARNING - could not write submission log: {e}")
    return row


def _append_row(row):
    """Append one dict row to the CSV (caller holds the log lock)."""
    need_header = (not LOG_FILE.is_file()
                   or LOG_FILE.stat().st_size == 0)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_HEADER)
        if need_header:
            w.writeheader()
        w.writerow(row)
