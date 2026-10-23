#!/usr/bin/env python3
"""Replay a loadtest schedule against Supabase with realistic pacing.

Replays manifest_s{SID}.csv offsets (compressed-time NHPP) using a worker
pool + token-bucket cap + AIMD backoff on 429/5xx. Records per-vote latency
to results_s{SID}.csv for downtime analysis.

RESUME: results file is the checkpoint. Restarting the same command skips
emails already logged as ok/already-voted and continues from the rest
(--resume/--no-resume, --retry-failed to also retry failures).

SAFETY: only sends emails LIKE 'loadtest-%' unless --allow-real given.
Always run --dry-run first, then a small --limit pilot, then full --count.

Usage:
  python loadtest_run.py --server-id 1 --dry-run
  python loadtest_run.py --server-id 1 --limit 5 --workers 4 --live
  python loadtest_run.py --server-id 1 --workers 8 --max-rps 3 --live
"""
import argparse, csv, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import threading

STOP = threading.Event()  # set on Ctrl+C; workers abort between requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from agent import submit_vote, rpc  # reuse live backend calls + validation

def load_manifest(sid, folder, limit=None):
    mp = Path(folder) / f"manifest_s{sid}.csv"
    rows = list(csv.DictReader(mp.open(encoding="utf-8")))
    if limit:
        rows = rows[:limit]
    return rows

class Bucket:
    """Token-bucket cap: smooths micro-bursts, still allows scheduled humps."""
    def __init__(self, rate):
        self.rate = rate; self.tokens = rate; self.t = time.monotonic()
        self.lock = threading.Lock()
    def take(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now-self.t)*self.rate)
                self.t = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                need = (1.0 - self.tokens) / self.rate
            time.sleep(need)
    def halve(self):
        with self.lock:
            self.rate = max(0.2, self.rate/2.0); self.tokens = min(self.tokens, self.rate)
            print(f"  [backoff] cap -> {self.rate:.2f} rps")

def health(label="preflight"):
    try:
        d = rpc("get_phase_status")
        row = d[0] if isinstance(d, list) and d else (d or {})
        vs = row.get("voting_status", "?")
        print(f"  [{label}] voting_status={vs}")
        return vs == "OPEN"
    except Exception as e:
        print(f"  [{label}] health check failed: {e}")
        return False

def one_vote(row, live, allow_real):
    data = {"category_id": row["category_id"], "nominee_id": row["nominee_id"],
            "voter_name": row["voter_name"], "voter_email": row["voter_email"]}
    em = row["voter_email"]
    if live and not em.startswith("loadtest-") and not allow_real:
        return "blocked", "not a loadtest email (use --allow-real)"
    if not live:
        time.sleep(0.005)
        return "dry-run", "not sent"
    t0 = time.monotonic()
    try:
        rid = submit_vote(data)
        return "ok", f"{rid} {(time.monotonic()-t0)*1000:.0f}ms"
    except Exception as e:
        return "failed", str(e)[:200]

def is_done_status(status, detail):
    """Terminal states that must NOT be resent on resume."""
    s = (status or "").strip().lower()
    if s == "ok":
        return True
    if s in ("failed", "blocked", "already-voted"):
        d = (detail or "").lower()
        return ("already voted" in d or "23505" in d or "duplicate" in d
                or "already exists" in d)
    return False

def load_checkpoint(outp):
    done = {}
    p = Path(outp)
    if p.exists():
        try:
            for r in csv.DictReader(p.open(encoding="utf-8")):
                em = (r.get("email") or "").strip().lower()
                if em and is_done_status(r.get("status"), r.get("detail")):
                    done[em] = (r.get("status"), r.get("detail"))
        except Exception as e:
            print(f"  WARNING - could not read checkpoint {p.name}: {e}")
    return done

def classify_vote_error(detail):
    """duplicate/already-voted -> recorded before (skip); else transient (retry)."""
    d = (detail or "").lower()
    if ("already voted" in d or "23505" in d or "duplicate" in d
            or "already exists" in d):
        return "recorded"
    return "transient"

def seek_resume(rows, live, allow_real, step=100, refine=(20, 5, 1), confirm=3):
    """Find first unsent row with decreasing-interval probes.

    Jumps forward by `step`, probing ONE email per landing: recorded ->
    keep jumping; transient/ok(new) -> step back and divide interval by
    `refine` steps until step 1 locates the boundary. Confirms with
    `confirm` consecutive recorded probes before declaring the prefix done
    (workers may have finished out of order near the kill point).
    Returns (start_index, n_probes, log_lines[(seq,email,outcome)]).
    """
    n = len(rows)
    log = []
    def probe(i):
        st, de = one_vote(rows[i], live, allow_real)
        if st == "ok":
            # brand-new vote just recorded; treat as boundary found
            out = "new"
        elif st == "failed" and classify_vote_error(de) == "recorded":
            out = "recorded"
        else:
            out = f"transient: {de[:120]}"
        log.append((rows[i]["seq"], rows[i]["voter_email"], out))
        return out
    idx = 0
    n_probes = 0
    stages = [step, *refine]
    for si, s in enumerate(stages):
        if idx >= n:
            break
        while idx < n:
            nxt = idx + s if si > 0 else idx  # first stage probes idx itself
            if nxt >= n:
                nxt = n - 1
            out = probe(nxt); n_probes += 1
            print(f"  [seek] probe seq={rows[nxt]['seq']} -> {out}")
            if out == "recorded":
                idx = nxt + 1
                if si == 0:
                    continue  # keep striding
                break  # overshoot corrected at finer level below
            elif out == "new":
                # boundary is at or before nxt; refine below (or stop at step 1)
                idx = max(idx, nxt - s) if si else nxt
                break
            else:  # transient: retry once, then treat as boundary (safe: will resend)
                print(f"  [seek] transient, retrying once...")
                out2 = probe(nxt); n_probes += 1
                print(f"  [seek] retry seq={rows[nxt]['seq']} -> {out2}")
                if out2 == "recorded":
                    idx = nxt + 1
                    if si == 0:
                        continue
                    break
                idx = max(0, nxt - s) if si else nxt
                break
    # confirm: next `confirm` rows must all be recorded, else pull back
    while idx < n:
        ok_run = True
        for c in range(confirm):
            if idx + c >= n:
                break
            out = probe(idx + c); n_probes += 1
            print(f"  [seek-confirm] seq={rows[idx+c]['seq']} -> {out}")
            if out != "recorded":
                idx = max(0, idx + c - 1)
                ok_run = False
                break
        if ok_run:
            idx = min(n, idx + confirm)
            break
    return min(idx, n), n_probes, log

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-id", type=int, default=1)
    ap.add_argument("--folder", default=str(BASE/"votes_folder"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-rps", type=float, default=5.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-real", action="store_true")
    ap.add_argument("--resume", dest="resume", action="store_true", default=True,
                    help="skip emails already ok/already-voted in results file (default: on)")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    help="resend everything, ignore checkpoint")
    ap.add_argument("--retry-failed", action="store_true",
                    help="also retry rows logged as failed (non-duplicate) last run")
    ap.add_argument("--seek", dest="seek", action="store_true", default=True,
                    help="decreasing-interval probe to find first unsent row (default: on)")
    ap.add_argument("--no-seek", dest="seek", action="store_false",
                    help="send sequentially from start of remaining rows")
    ap.add_argument("--seek-step", type=int, default=100,
                    help="initial probe stride (default 100)")
    ap.add_argument("--start-from", type=int, default=None,
                    help="resume from manifest seq N (1-based): skips rows with "
                         "seq < N without sending. e.g. --start-from 1301")
    a = ap.parse_args()
    live = a.live and not a.dry_run
    mode = "LIVE -> Supabase" if live else "DRY-RUN (no POSTs)"
    print(f"loadtest replay s{a.server_id}: {mode} workers={a.workers} cap={a.max_rps} rps")
    STOP.clear()
    rows = load_manifest(a.server_id, a.folder, None)
    print(f"  {len(rows)} vote(s) in manifest")
    if live and not health():
        sys.exit("aborted: voting not OPEN or backend unreachable")
    outp = Path(a.folder)/(f"results_s{a.server_id}.csv" if live
                             else f"results_s{a.server_id}.dryrun.csv")
    # --- --start-from: jump straight to manifest seq N, no probes, no replay ---
    skipped = []
    if a.start_from is not None:
        before = len(rows)
        skipped = [r for r in rows if int(r.get("seq", 0)) < a.start_from]
        rows = [r for r in rows if int(r.get("seq", 0)) >= a.start_from]
        print(f"  start-from seq>={a.start_from}: skipping {len(skipped)} "
              f"({len(rows)} remaining of {before})")
    if live and a.resume and not a.retry_failed:
        done = load_checkpoint(outp)
        if done:
            keep = []
            for r in rows:
                if (r.get("voter_email") or "").strip().lower() in done:
                    skipped.append(r)
                else:
                    keep.append(r)
            rows = keep
            print(f"  resume: skipping {len(skipped)} already-recorded "
                  f"({len(rows)} remaining)")
    elif live and a.resume and a.retry_failed:
        # retry everything except ok/already-voted (non-duplicate failures resent)
        done = load_checkpoint(outp)
        if done:
            keep = []
            for r in rows:
                if (r.get("voter_email") or "").strip().lower() in done:
                    skipped.append(r)
                else:
                    keep.append(r)
            rows = keep
            print(f"  resume+retry-failed: skipping {len(skipped)} done "
                  f"({len(rows)} to send, incl. past failures)")
    if not rows:
        print("  nothing to send - all rows already recorded. "
              "Use --no-resume to resend or --retry-failed to retry failures.")
        return
    if a.limit is not None:  # applied AFTER start-from/resume filters
        rows = rows[:a.limit]
        print(f"  limit: sending first {len(rows)} of remaining")
    # append mode on resume so the checkpoint is never truncated
    fmode = "a" if (live and a.resume and outp.exists()) else "w"
    need_header = not (live and a.resume and outp.exists() and outp.stat().st_size > 0)
    t_start = time.monotonic()
    sched0 = float(rows[0]["offset_sec"]) if rows else 0.0
    bucket = Bucket(a.max_rps)
    ok = fail = dry = 0
    skipped_n = len(skipped)
    seek_log = []
    with open(outp, fmode, newline="", encoding="utf-8") as fh:
        W = csv.writer(fh)
        if need_header:
            W.writerow(["seq","email","scheduled_s","sent_at_s","status","detail"])
        # --- decreasing-interval seek: jump to first unsent row, skip the rest ---
        # skipped when --start-from given (explicit position wins, no probes needed)
        if live and a.seek and a.start_from is None and len(rows) > a.seek_step:
            print(f"  seek: probing every ~{a.seek_step} to find first unsent "
                  f"(--no-seek to send sequentially)...")
            start, n_probes, seek_log = seek_resume(rows, live, a.allow_real,
                                                    step=a.seek_step)
            for seq, em, outcome in seek_log:
                el = time.monotonic() - t_start
                if outcome == "new":
                    W.writerow([seq, em, "", f"{el:.2f}", "ok",
                                "seek probe: first-time record"]); fh.flush()
                    ok += 1
                elif outcome == "recorded":
                    W.writerow([seq, em, "", f"{el:.2f}", "failed",
                                "seek probe: This email has already voted in "
                                "this category"]); fh.flush()
                    skipped_n += 1
                else:
                    W.writerow([seq, em, "", f"{el:.2f}", "failed",
                                f"seek probe {outcome}"]); fh.flush()
                    fail += 1
            skipped_n += max(0, start - len(seek_log))
            print(f"  seek: {n_probes} probes -> resuming at seq={rows[start]['seq'] if start < len(rows) else 'END'} "
                  f"(skipped {start} recorded, {len(rows)-start} to send)")
            rows = rows[start:]
            sched0 = float(rows[0]["offset_sec"]) if rows else sched0
            if not rows:
                el = time.monotonic() - t_start
                print(f"=== done: {ok} ok, {fail} failed, {dry} dry-run, "
                      f"{skipped_n} skipped-resumed in {el:.1f}s -> {outp.name} ===")
                return
        def run(r):
            off = float(r["offset_sec"]) - sched0
            wait = (t_start + off) - time.monotonic()
            while wait > 0:
                if STOP.is_set():
                    return r, "skipped", "stopped by user (Ctrl+C)"
                time.sleep(min(wait, 0.2))
                wait = (t_start + off) - time.monotonic()
            if STOP.is_set():
                return r, "skipped", "stopped by user (Ctrl+C)"
            bucket.take()
            if STOP.is_set():
                return r, "skipped", "stopped by user (Ctrl+C)"
            st, de = one_vote(r, live, a.allow_real)
            if st == "failed" and ("429" in de or "500" in de or "503" in de):
                bucket.halve()
            return r, st, de
        try:
            with ThreadPoolExecutor(max_workers=a.workers) as ex:
                futs = {ex.submit(run, r): r for r in rows}
                done_n = 0
                for f in as_completed(futs):
                    r, st, de = f.result()
                    done_n += 1
                    el = time.monotonic() - t_start
                    W.writerow([r["seq"], r["voter_email"], f"{float(r['offset_sec'])-sched0:.2f}",
                                f"{el:.2f}", st, de]); fh.flush()
                    if st == "ok": ok += 1
                    elif st == "dry-run": dry += 1
                    elif st == "skipped": pass
                    else: fail += 1
                    tag = {"ok":"[OK]"}.get(st, "[..]")
                    print(f"  {tag} {done_n}/{len(rows)} seq={r['seq']} {r['voter_email']} -> {st} {de}",
                          flush=True)
                    if live and done_n % 100 == 0:
                        health(label=f"health@{done_n}")
                    if STOP.is_set():
                        print("  stopping... (in-flight requests finish, queued are skipped)")
                        for fu in futs:
                            fu.cancel()
                        break
        except KeyboardInterrupt:
            STOP.set()
            print("\n  Ctrl+C - stopping after in-flight requests... restart with --start-from <seq>")
    el = time.monotonic() - t_start
    print(f"=== done: {ok} ok, {fail} failed, {dry} dry-run, "
          f"{skipped_n} skipped-resumed in {el:.1f}s "
          f"({len(rows)/el:.2f} rps avg) -> {outp.name} ===")
    if live and ok:
        print(f"CLEANUP: DELETE FROM votes WHERE voter_email LIKE 'loadtest-s{a.server_id}-%'; "
              f"({ok} row(s) inserted)")

if __name__ == "__main__":
    main()


