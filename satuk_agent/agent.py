#!/usr/bin/env python3
"""SATUK Awards 2026 nomination + voting agent.

Usage:
  python agent.py check-phase
  python agent.py list-categories
  python agent.py list-nominees --category-id <id>
  python agent.py fill nomination.yaml
  python agent.py fill --category-id <id> --nominee-name "..." ...
  python agent.py fill-all nominations_folder
  python agent.py vote votes_folder/aecas_jane.yaml
  python agent.py vote --voter-email jane@example.com [--voter-name "Jane"]
  python agent.py vote-all votes_folder
"""
import argparse, math, os, random, re, sys, time
from pathlib import Path
import requests, yaml

from submission_log import (get_vote_count, log_submission, new_attempt_id,
                            next_vote_number)
from run_coordination import ClaimManager

SUPABASE_URL = "https://dibwcoklnyyboaoldmyw.supabase.co"
API_KEY = "sb_publishable_s03_z2aK8etC6yC2iK44uw_XL_OsFx8"
HEADERS = {
    "apikey": API_KEY,
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def rpc(name, payload=None):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        headers=HEADERS, json=payload or {}, timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} from {name}: {r.text[:300]}")
    return r.json()


def check_phase():
    data = rpc("get_phase_status")
    row = data[0] if isinstance(data, list) and data else (data or {})
    nom = row.get("nomination_status", "CLOSED")
    vote = row.get("voting_status", "?")
    print(f"Nomination phase: {nom}")
    print(f"Voting phase:     {vote}")
    if nom == "OPEN":
        print("Nominations are OPEN - you can submit.")
    else:
        print("Nominations are not open yet.")
    return nom


def list_categories():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/categories",
        headers={**HEADERS, "Accept": "application/json"},
        params={"select": "id,code,name", "order": "code"}, timeout=30,
    )
    r.raise_for_status()
    cats = r.json()
    for c in cats:
        print(f"  [{c['code']:>2}] id={c['id']}  {c['name']}")
    return cats


def fetch_category_ids():
    """Return the set of live category IDs from the categories table."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/categories",
        headers={**HEADERS, "Accept": "application/json"},
        params={"select": "id"}, timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} fetching categories: {r.text[:300]}")
    return {c["id"] for c in r.json()}


REQUIRED = ["category_id", "nominee_name", "reason", "nominator_name", "nominator_email"]
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# --- Voting (live phase) ---
# Club or Society of the Year category + published AECAS nominee row.
AECAS_CLUB_CATEGORY_ID = "697391ad-baf1-4ba9-b0dd-e32f771fe915"
AECAS_NOMINEE_ID = "6845e30a-71fe-4626-b4bc-f21229b51663"
AECAS_NOMINEE_NAME = "AECAS"

VOTE_REQUIRED = ["category_id", "nominee_id", "voter_email"]


def submit_nomination(data):
    """Validate and submit one nomination. Returns the reference number.
    Raises ValueError with a user-friendly message on validation failure."""
    data = {k: v for k, v in (data or {}).items() if v not in (None, "")}

    missing = [k for k in REQUIRED if not data.get(k)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
    if not EMAIL_RE.fullmatch(str(data["nominator_email"])):
        raise ValueError("nominator_email is not a valid email address")
    if len(str(data["reason"])) < 40:
        raise ValueError("reason must be at least 40 characters (form validation rule)")

    payload = {
        "_category_id": data["category_id"],
        "_nominee_name": data["nominee_name"],
        "_reason": data["reason"],
        "_nominator_name": data["nominator_name"],
        "_nominator_email": data["nominator_email"],
        "_nominee_email": data.get("nominee_email") or None,
        "_nominee_phone": data.get("nominee_phone") or None,
        "_nominee_details": data.get("nominee_details") or None,
        "_supporting_info": data.get("supporting_info") or None,
        "_nominator_phone": data.get("nominator_phone") or None,
        "_nominee_photo_url": None,  # photo upload intentionally unsupported
    }
    return rpc("submit_nomination", payload)


def fill(args):
    data = vars(args).copy()
    path = data.pop("file", None)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data.update(yaml.safe_load(f) or {})
    attempt = new_attempt_id()
    try:
        ref = submit_nomination(data)
    except Exception as e:
        log_submission("nomination", "failed",
                       file=Path(path).name if path else "",
                       voter_name=data.get("nominator_name", ""),
                       email=data.get("nominator_email", ""),
                       category_id=data.get("category_id", ""),
                       nominee_name=data.get("nominee_name", ""),
                       error=e, attempt_id=attempt)
        raise
    log_submission("nomination", "success",
                   file=Path(path).name if path else "",
                   voter_name=data.get("nominator_name", ""),
                   email=data.get("nominator_email", ""),
                   category_id=data.get("category_id", ""),
                   nominee_name=data.get("nominee_name", ""),
                   server_id=ref, attempt_id=attempt)
    print("Nomination submitted successfully!")
    print(f"Reference number: {ref}")
    return ref


def build_bounded_schedule(n, window_s):
    """Return ``n`` absolute timestamps (0 … window_s) spread by a time-bounded
    Poisson process.

    Draws ``n-1`` i.i.d. exponential gaps with mean ``window_s / (n-1)`` —
    the Poisson shape (short gaps cluster, long gaps are rare) — then
    linearly warps the cumulative times onto ``[0, window_s]`` so the last
    vote lands exactly on the deadline.  No gap cap; the global scale is
    what is fitted to the deadline.

    Returns a list of ``n`` floats: ``t[0] == 0.0`` and ``t[-1] == window_s``.
    ``n <= 1`` returns ``[0.0]``; ``window_s <= 0`` returns ``[0.0] * n``.
    """
    if n <= 1:
        return [0.0]
    if window_s <= 0:
        return [0.0] * n
    mean = window_s / (n - 1)
    gaps = [random.expovariate(1.0 / mean) for _ in range(n - 1)]
    total = sum(gaps)
    scale = window_s / total
    times = [0.0]
    acc = 0.0
    for g in gaps:
        acc += g
        times.append(acc * scale)
    return times


def poisson_stagger_delay(min_delay, max_delay):
    """Draw one inter-arrival delay from a truncated exponential distribution.

    Kept for ``fill-all`` (nominations), which still uses the fixed 0–25s
    stagger.  ``vote-all`` now uses ``build_bounded_schedule`` instead.

    A Poisson process with rate ``lambd`` has ``Exp(lambd)`` gaps between
    events, so exponential gaps are what make the submission cadence
    Poisson-distributed: short gaps cluster, long gaps are rare.  A plain
    exponential is unbounded, but the CLI promises delays inside
    ``[min_delay, max_delay]``, so we sample the exact truncated
    exponential via inverse-CDF in a single draw (no clamp pile-up at
    ``max_delay``, no unbounded rejection loop).

    The rate is ``lambd = 2 / (max_delay - min_delay)``, i.e. the
    underlying (untruncated) exponential has mean ``(max-min)/2`` — the
    same mean the old uniform stagger had.  Truncation pulls the
    observed mean down a little (for 0–25s: ~8.6s observed vs 12.5s
    uniform) while giving the Poisson shape: short gaps cluster,
    long gaps are rare::

        mean_excess = (max_delay - min_delay) / 2
        lambd = 1 / mean_excess   (rate of the underlying exponential)
        delay = min_delay + Exp(lambd) truncated above at max_delay

    Edge cases: ``max_delay <= 0`` -> ``0.0`` (no wait);
    ``min_delay >= max_delay`` -> ``float(min_delay)`` (deterministic).
    Raises ``ValueError`` on negative bounds or ``min_delay > max_delay``.
    """
    if min_delay < 0 or max_delay < 0:
        raise ValueError(
            f"delays must be >= 0 (got min_delay={min_delay}, max_delay={max_delay})"
        )
    if min_delay > max_delay:
        raise ValueError(
            f"min_delay ({min_delay}) must not exceed max_delay ({max_delay})"
        )
    if max_delay <= 0:
        return 0.0
    if min_delay >= max_delay:
        return float(min_delay)
    width = max_delay - min_delay
    mean_excess = width / 2.0  # mean of the underlying (untruncated) exponential
    # Inverse-CDF of Exp(rate=1/mean_excess) truncated to [0, width]:
    #   x = -mean_excess * ln(1 - u * (1 - exp(-width / mean_excess)))
    u = random.random()  # in [0.0, 1.0); u == 1.0 has probability 0
    cap = 1.0 - math.exp(-width / mean_excess)  # == 1 - e^-2, ~0.8647
    return min_delay - mean_excess * math.log(1.0 - u * cap)


def fill_all(args):
    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    files = [*folder.glob("*.yaml"), *folder.glob("*.yml")]
    files = [f for f in files if not f.name.startswith(".")]
    random.shuffle(files)
    if not files:
        sys.exit(f"No .yaml/.yml files found in {folder}")

    print(f"Found {len(files)} nomination file(s) in {folder}\n")
    print("Shuffled files into random order.\n")

    # Pre-flight: check every file's category_id against the live categories
    # table so unknown IDs fail fast with a clear message instead of a bare
    # 400 from the server, and so no staggered delay is spent on a submission
    # that cannot succeed.
    valid_ids = None
    try:
        valid_ids = fetch_category_ids()
    except Exception as e:
        print(f"    WARNING - could not fetch categories, pre-flight check skipped: {e}\n")

    ok, failed, submittable = [], [], []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            cid = str(data.get("category_id") or "")
            if valid_ids is not None and cid not in valid_ids:
                raise ValueError(f"unknown category_id {cid!r} (not in the live categories table)")
            submittable.append((f, data))
        except Exception as e:
            print(f"--- {f.name}")
            print(f"    FAILED - {e}")
            failed.append((f.name, str(e)))

    if args.no_delay or len(submittable) <= 1:
        min_delay = max_delay = 0.0
    else:
        min_delay = args.min_delay
        max_delay = args.max_delay

    def wait_before_next():
        """Sleep a Poisson (truncated-exponential) interval between submissions."""
        if max_delay <= 0:
            return
        delay = poisson_stagger_delay(min_delay, max_delay)
        mins, secs = divmod(int(delay), 60)
        print(f"    Waiting {mins}m {secs:02d}s before next submission...")
        time.sleep(delay)

    for i, (f, data) in enumerate(submittable):
        if i > 0:
            wait_before_next()
        print(f"--- {f.name}")
        attempt = new_attempt_id()
        try:
            print(f"    Nominee: {data.get('nominee_name', '?')} | Category: {data.get('category_id', '?')}")
            ref = submit_nomination(data)
            print(f"    OK - Reference: {ref}")
            ok.append((f.name, ref))
            log_submission("nomination", "success", file=f.name,
                           voter_name=data.get("nominator_name", ""),
                           email=data.get("nominator_email", ""),
                           category_id=data.get("category_id", ""),
                           nominee_name=data.get("nominee_name", ""),
                           server_id=ref, attempt_id=attempt)
        except Exception as e:
            print(f"    FAILED - {e}")
            failed.append((f.name, str(e)))
            log_submission("nomination", "failed", file=f.name,
                           voter_name=data.get("nominator_name", ""),
                           email=data.get("nominator_email", ""),
                           category_id=data.get("category_id", ""),
                           nominee_name=data.get("nominee_name", ""),
                           error=e, attempt_id=attempt)

    print(f"\n=== Summary: {len(ok)} submitted, {len(failed)} failed ===")
    for name, ref in ok:
        print(f"  [OK]     {name} -> {ref}")
    for name, err in failed:
        print(f"  [FAILED] {name} -> {err}")
    return ok, failed


def list_nominees(args):
    """List published nominees (optionally filtered to one category)."""
    params = {"select": "id,category_id,name,bio,is_published",
              "is_published": "eq.true", "order": "name", "limit": "100"}
    if getattr(args, "category_id", None):
        params["category_id"] = f"eq.{args.category_id}"
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/nominees",
        headers={**HEADERS, "Accept": "application/json"},
        params=params, timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    for n in rows:
        print(f"  {n['id']}  [{n['category_id']}]  {n['name']}")
    return rows


def submit_vote(data):
    """Validate and cast one vote. Returns the created row id or 'recorded'.

    Mirrors the web vote form (assets/vote-*.js):
      votes.insert({category_id, nominee_id, voter_name|null,
                    voter_email lower-cased})
    NOTE: no `Prefer: return=representation` header — supabase-js does not
    send one, and adding it flips the RLS evaluation context on the votes
    table (HTTP 401 / code 42501 on every insert).
    """
    data = {k: v for k, v in (data or {}).items() if v not in (None, "")}

    missing = [k for k in VOTE_REQUIRED if not data.get(k)]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
    email = str(data["voter_email"]).strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("voter_email is not a valid email address")

    payload = {
        "category_id": str(data["category_id"]),
        "nominee_id": str(data["nominee_id"]),
        "voter_name": str(data.get("voter_name") or "").strip() or None,
        "voter_email": email,
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/votes",
        headers={**HEADERS, "Accept": "application/json"},
        json=payload, timeout=30,
    )
    if r.status_code in (409, 400):
        text = r.text or ""
        low = text.lower()
        if "23505" in text or "duplicate" in low or "already" in low:
            raise ValueError(
                f"This email has already voted in this category ({email})")
        raise ValueError(f"Vote rejected (HTTP {r.status_code}): {text[:300]}")
    if r.status_code >= 400:
        raise ValueError(f"Vote rejected (HTTP {r.status_code}): {r.text[:300]}")
    try:
        rows = r.json()
    except Exception:
        rows = None
    if isinstance(rows, list) and rows:
        return rows[0].get("id") or "recorded"
    if isinstance(rows, dict) and rows.get("id"):
        return rows["id"]
    return "recorded"


def vote(args):
    data = vars(args).copy()
    path = data.pop("file", None)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data.update(yaml.safe_load(f) or {})
    # Default every vote to AECAS for Club/Society of the Year unless the
    # file/flags explicitly override it.
    data.setdefault("category_id", AECAS_CLUB_CATEGORY_ID)
    data.setdefault("nominee_id", AECAS_NOMINEE_ID)
    data.setdefault("nominee_name", AECAS_NOMINEE_NAME)
    data = {k: v for k, v in data.items() if v not in (None, "")}
    attempt = new_attempt_id()
    try:
        row_id = submit_vote(data)
    except Exception as e:
        status = "already-voted" if is_already_voted(e) else "failed"
        log_submission("vote", status,
                       file=Path(path).name if path else "",
                       voter_name=data.get("voter_name", ""),
                       email=data.get("voter_email", ""),
                       category_id=data.get("category_id", ""),
                       nominee_id=data.get("nominee_id", ""),
                       nominee_name=data.get("nominee_name", ""),
                       error=e, attempt_id=attempt)
        raise
    n = next_vote_number()
    log_submission("vote", "success",
                   file=Path(path).name if path else "",
                   voter_name=data.get("voter_name", ""),
                   email=data.get("voter_email", ""),
                   category_id=data.get("category_id", ""),
                   nominee_id=data.get("nominee_id", ""),
                   nominee_name=data.get("nominee_name", ""),
                   vote_number=n, server_id=row_id, attempt_id=attempt)
    print(f"Vote {n} for {data.get('nominee_name', 'AECAS')} recorded successfully!")
    print(f"Voter: {str(data.get('voter_email', '')).lower()}  Vote id: {row_id}")
    return row_id


def is_already_voted(err):
    """True if this failure means the email already voted (safe to delete)."""
    low = str(err).lower()
    return ("already voted" in low or "23505" in low
            or "duplicate" in low or "already exists" in low)


def _resolve_folder(raw):
    """Resolve a folder argument relative to the CWD, then the script dir.

    Lets ``vote-all votes_folder`` work no matter where the script is
    invoked from.
    """
    p = Path(raw)
    if p.is_dir():
        return p.resolve()
    alt = Path(__file__).resolve().parent / p
    if alt.is_dir():
        return alt.resolve()
    return p.resolve()


def vote_all(args):
    folder = _resolve_folder(args.folder)
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    return _vote_all_claimed(args, folder)


def _vote_all_claimed(args, folder):
    """Cast every vote file in *folder*.

    Several runs may point at the same folder at the same time: each run
    claims the files it works on atomically, so a file is only ever
    processed by one run.
    """
    window_hours = getattr(args, "window_hours", 6)
    stale_seconds = max(7200, int(float(window_hours) * 3600.0 * 1.5))
    run_id = getattr(args, "run_id", None) or f"run-{os.getpid()}"
    claims = ClaimManager(folder, run_id, stale_seconds=stale_seconds)

    # Recover files abandoned by a previous run that died mid-flight.
    recovered = claims.recover_stale()
    if recovered:
        print(f"Recovered {recovered} file(s) left by a finished run.\n")

    return _vote_all_locked(args, folder, claims)


def _vote_all_locked(args, folder, claims=None):
    files = [*folder.glob("*.yaml"), *folder.glob("*.yml")]
    files = [f for f in files if not f.name.startswith(".")]
    seed = getattr(args, "seed", None)
    if seed is not None:
        random.seed(seed)
        print(f"Using fixed shuffle seed {seed} (reproducible order).\n")
    random.shuffle(files)
    if not files:
        sys.exit(f"No .yaml/.yml files found in {folder}")

    print(f"Found {len(files)} vote file(s) in {folder}\n")
    print("Shuffled files into random order.\n")
    print(f"Successful votes to date: {get_vote_count()}\n")

    # Pre-flight: voting must be OPEN, else abort before any POST.
    try:
        phase = rpc("get_phase_status")
        row = phase[0] if isinstance(phase, list) and phase else (phase or {})
        if row.get("voting_status") != "OPEN":
            sys.exit(f"Voting phase is {row.get('voting_status', '?')} - vote-all aborted.")
    except SystemExit:
        raise
    except Exception as e:
        print(f"    WARNING - could not check voting phase, continuing: {e}\n")

    votable_ids = None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/categories",
            headers={**HEADERS, "Accept": "application/json"},
            params={"select": "id,votable"}, timeout=30,
        )
        r.raise_for_status()
        votable_ids = {c["id"] for c in r.json() if c.get("votable")}
    except Exception as e:
        print(f"    WARNING - categories check skipped: {e}\n")

    published = None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/nominees",
            headers={**HEADERS, "Accept": "application/json"},
            params={"select": "id,category_id", "is_published": "eq.true",
                    "limit": "1000"}, timeout=30,
        )
        r.raise_for_status()
        published = {(n["category_id"], n["id"]) for n in r.json()}
    except Exception as e:
        print(f"    WARNING - nominees check skipped: {e}\n")

    ok, failed = [], []
    # Files are claimed one at a time, at submit time, so that several runs
    # can share the folder and each file is handled by exactly one run.
    skipped_claimed = 0
    skipped_handled = 0

    vote_count = getattr(args, "vote_count", 3000)
    window_hours = getattr(args, "window_hours", 6)
    window_s = float(window_hours) * 3600.0
    simulate = getattr(args, "simulate", False)
    no_delay = bool(getattr(args, "no_delay", False))

    delete_voted = getattr(args, "delete_voted", False)
    quarantine_failed = getattr(args, "quarantine_failed", False)
    quarantine_dir = Path(getattr(args, "quarantine_dir", "") or (folder / "failed"))
    if quarantine_failed:
        quarantine_dir.mkdir(parents=True, exist_ok=True)

    def maybe_delete(path, reason):
        """Finalise a claimed file after its vote was recorded.

        With ``--delete-voted`` the file is deleted; otherwise it is put
        back into the folder so a later run can pick it up.
        """
        try:
            if claims is not None:
                if delete_voted:
                    claims.release(path, restore=False)
                else:
                    claims.release(path, restore=True)
            elif delete_voted:
                Path(path).unlink()
            if delete_voted:
                print(f"    Deleted {Path(path).name} ({reason})", flush=True)
                return True
            return False
        except Exception as e:
            print(f"    WARNING - could not finalise {Path(path).name}: {e}",
                  flush=True)
            return False

    def maybe_quarantine(path):
        """Move a claimed file out of the pool for inspection.

        Without ``--quarantine-failed`` the file is simply returned to
        the folder.
        """
        try:
            if quarantine_failed:
                if claims is not None:
                    dest = claims.quarantine(path, quarantine_dir)
                    if dest is None:
                        raise OSError("could not move file to quarantine")
                else:
                    dest = quarantine_dir / Path(path).name
                    Path(path).rename(dest)
                print(f"    Moved {Path(path).name} -> {quarantine_dir.name}/",
                      flush=True)
                return True
            if claims is not None:
                claims.release(path, restore=True)
            return False
        except Exception as e:
            print(f"    WARNING - could not quarantine {Path(path).name}: {e}",
                  flush=True)
            return False

    def sleep_until(window_start, absolute_s):
        """Sleep until ``absolute_s`` seconds after ``window_start``."""
        if simulate or no_delay:
            return
        now = time.time()
        target = window_start + absolute_s
        remaining_s = target - now
        if remaining_s > 0:
            mins, secs = divmod(int(remaining_s), 60)
            print(f"    Waiting {mins}m {secs:02d}s until scheduled time...", flush=True)
            time.sleep(remaining_s)

    prev_failed = False
    deleted, already_voted, quarantined = 0, 0, 0
    votes_in_window = 0
    window_index = 0
    window_start = time.time()
    total = len(files)
    batch_size = min(vote_count, total)
    schedule = build_bounded_schedule(batch_size, window_s)
    print(f"\n=== Window {window_index + 1}: voting up to {vote_count} file(s) over "
          f"{window_hours} h (of {total} total) ===\n", flush=True)
    for i, f in enumerate(files):
        position = i % vote_count
        if position == 0 and i > 0:
            # Previous window complete; start a new window immediately.
            elapsed = time.time() - window_start
            print(f"\n    Window {window_index} complete: {votes_in_window} vote(s) cast in "
                  f"{elapsed / 60:.1f} min. Starting next 6 h window.\n", flush=True)
            window_index += 1
            window_start = time.time()
            remaining = total - i
            batch_size = min(vote_count, remaining)
            schedule = build_bounded_schedule(batch_size, window_s)
            votes_in_window = 0
            print(f"\n=== Window {window_index + 1}: voting up to {vote_count} file(s) over "
                  f"{window_hours} h (of {total} total) ===\n", flush=True)
        sleep_until(window_start, schedule[position])

        # --- Claim the file (atomic): exactly one run can win it. -------
        claim_path = None
        if claims is not None:
            claim_path = claims.claim(f)
            if claim_path is None:
                # Either another run owns it, or it was already voted and
                # deleted by another run. Either way it is not ours.
                if f.exists():
                    skipped_claimed += 1
                else:
                    skipped_handled += 1
                continue
            work_file = claim_path
        else:
            work_file = f

        votes_in_window += 1
        print(f"--- {f.name}", flush=True)
        attempt = new_attempt_id()
        data = {}
        try:
            with open(work_file, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            data.setdefault("category_id", AECAS_CLUB_CATEGORY_ID)
            data.setdefault("nominee_id", AECAS_NOMINEE_ID)
            data.setdefault("nominee_name", AECAS_NOMINEE_NAME)
            cid = str(data.get("category_id") or "")
            nid = str(data.get("nominee_id") or "")
            if votable_ids is not None and cid not in votable_ids:
                raise ValueError(f"unknown or non-votable category_id {cid!r}")
            if published is not None and (cid, nid) not in published:
                raise ValueError(f"nominee {nid!r} not published in {cid!r}")
        except Exception as e:
            print(f"    FAILED - {e}", flush=True)
            failed.append((f.name, str(e)))
            log_submission("vote", "failed", file=f.name,
                           voter_name=data.get("voter_name", ""),
                           email=data.get("voter_email", ""),
                           category_id=data.get("category_id", ""),
                           nominee_id=data.get("nominee_id", ""),
                           nominee_name=data.get("nominee_name", ""),
                           error=e, attempt_id=attempt)
            if maybe_quarantine(work_file):
                quarantined += 1
            prev_failed = True
            continue
        try:
            print(f"    Voter: {data.get('voter_email', '?')} -> "
                  f"{data.get('nominee_name', 'AECAS')}", flush=True)
            if simulate:
                print("    [SIMULATE] would POST vote", flush=True)
                # No real vote was cast, so no vote number is consumed and
                # the shared counter is left untouched. Leaving the number
                # blank keeps the shared log's vote_number space clean.
                print("    OK - Vote (simulated, no number allocated)",
                      flush=True)
                ok.append((f.name, "simulated"))
                prev_failed = False
                log_submission("vote", "success", file=f.name,
                               voter_name=data.get("voter_name", ""),
                               email=data.get("voter_email", ""),
                               category_id=data.get("category_id", ""),
                               nominee_id=data.get("nominee_id", ""),
                               nominee_name=data.get("nominee_name", ""),
                               server_id="simulated",
                               attempt_id=attempt)
                if maybe_delete(work_file, "vote recorded"):
                    deleted += 1
                continue
            row_id = submit_vote(data)
            n = next_vote_number()
            print(f"    OK - Vote {n} (id: {row_id})", flush=True)
            ok.append((f.name, row_id))
            prev_failed = False
            log_submission("vote", "success", file=f.name,
                           voter_name=data.get("voter_name", ""),
                           email=data.get("voter_email", ""),
                           category_id=data.get("category_id", ""),
                           nominee_id=data.get("nominee_id", ""),
                           nominee_name=data.get("nominee_name", ""),
                           vote_number=n, server_id=row_id,
                           attempt_id=attempt)
            if maybe_delete(work_file, "vote recorded"):
                deleted += 1
        except Exception as e:
            if is_already_voted(e):
                print(f"    ALREADY VOTED - {e}", flush=True)
                failed.append((f.name, str(e)))
                already_voted += 1
                log_submission("vote", "already-voted", file=f.name,
                               voter_name=data.get("voter_name", ""),
                               email=data.get("voter_email", ""),
                               category_id=data.get("category_id", ""),
                               nominee_id=data.get("nominee_id", ""),
                               nominee_name=data.get("nominee_name", ""),
                               error=e, attempt_id=attempt)
                if maybe_delete(work_file, "already voted"):
                    deleted += 1
            else:
                print(f"    FAILED - {e}", flush=True)
                failed.append((f.name, str(e)))
                log_submission("vote", "failed", file=f.name,
                               voter_name=data.get("voter_name", ""),
                               email=data.get("voter_email", ""),
                               category_id=data.get("category_id", ""),
                               nominee_id=data.get("nominee_id", ""),
                               nominee_name=data.get("nominee_name", ""),
                               error=e, attempt_id=attempt)
                if maybe_quarantine(work_file):
                    quarantined += 1
            prev_failed = True

    print(f"\n=== Summary: {len(ok)} voted, {len(failed)} failed "
          f"({already_voted} already-voted, {deleted} deleted"
          f"{f', {quarantined} quarantined' if quarantine_failed else ''}"
          f"{f', {skipped_claimed} claimed by other runs' if skipped_claimed else ''}"
          f"{f', {skipped_handled} handled by other runs' if skipped_handled else ''}) ===", flush=True)
    print(f"=== Total successful votes to date: {get_vote_count()} "
          f"(logged in submission_log.csv) ===", flush=True)
    for name, ref in ok:
        print(f"  [OK]     {name} -> {ref}", flush=True)
    for name, err in failed:
        print(f"  [FAILED] {name} -> {err}", flush=True)
    return ok, failed


def main():
    p = argparse.ArgumentParser(description="SATUK Awards 2026 nomination + voting agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-phase")
    sub.add_parser("list-categories")
    ln = sub.add_parser("list-nominees", help="List published nominees")
    ln.add_argument("--category-id", default=None,
                    help="Only show nominees in this category")
    f = sub.add_parser("fill")
    f.add_argument("file", nargs="?", help="YAML file with nomination data")
    f.add_argument("--category-id")
    f.add_argument("--nominee-name")
    f.add_argument("--nominee-email")
    f.add_argument("--nominee-phone")
    f.add_argument("--nominee-details")
    f.add_argument("--reason")
    f.add_argument("--supporting-info")
    f.add_argument("--nominator-name")
    f.add_argument("--nominator-email")
    f.add_argument("--nominator-phone")
    fa = sub.add_parser("fill-all")
    fa.add_argument("folder", help="Folder containing .yaml/.yml nomination files")
    fa.add_argument("--no-delay", action="store_true",
                    help="Submit all files immediately without staggered delays")
    fa.add_argument("--min-delay", type=float, default=0.0,
                    help="Minimum delay between submissions in seconds; bounds of the "
                         "Poisson (truncated-exponential) stagger (default: 0)")
    fa.add_argument("--max-delay", type=float, default=25.0,
                    help="Maximum delay between submissions in seconds; bounds of the "
                         "Poisson (truncated-exponential) stagger (default: 25)")
    v = sub.add_parser("vote", help="Cast one vote (defaults to AECAS Best Club)")
    v.add_argument("file", nargs="?", help="YAML file with vote data")
    v.add_argument("--category-id")
    v.add_argument("--nominee-id")
    v.add_argument("--nominee-name")
    v.add_argument("--voter-name")
    v.add_argument("--voter-email")
    va = sub.add_parser("vote-all", help="Cast every vote file in a folder")
    va.add_argument("folder", help="Folder containing .yaml/.yml vote files")
    va.add_argument("--no-delay", action="store_true",
                    help="Cast all votes immediately without staggered delays")
    va.add_argument("--vote-count", type=int, default=3000,
                    help="Number of votes to spread across each time-bounded "
                         "window (default: 3000). When a window closes, a new "
                         "window starts for the next batch until all files are "
                         "voted.")
    va.add_argument("--window-hours", type=float, default=6.0,
                    help="Window length in hours over which the first "
                         "--vote-count votes are spread (default: 6).")
    va.add_argument("--simulate", action="store_true",
                    help="Print the schedule without actually POSTing votes.")
    va.add_argument("--delete-voted", action="store_true",
                    help="Delete each YAML file after its vote is recorded or already-voted")
    va.add_argument("--quarantine-failed", action="store_true",
                    help="Move other failed vote files out of the folder for inspection")
    va.add_argument("--quarantine-dir", default=None,
                    help="Where to move quarantined failures (default: <folder>/failed)")
    va.add_argument("--seed", type=int, default=None,
                    help="Fix the shuffle seed for a reproducible order. "
                         "Omit it (default) for a fresh random order every run.")
    va.add_argument("--run-id", default=None,
                    help="Identifier for this run's claim directory "
                         "(default: run-<pid>)")
    args = p.parse_args()
    {"check-phase": check_phase,
     "list-categories": list_categories,
     "list-nominees": lambda: list_nominees(args),
     "fill": lambda: fill(args),
     "fill-all": lambda: fill_all(args),
     "vote": lambda: vote(args),
     "vote-all": lambda: vote_all(args)}[args.cmd]()


if __name__ == "__main__":
    main()
