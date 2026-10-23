#!/usr/bin/env python3
"""Rebuild results_s{SID}.csv checkpoint from what's actually in Supabase.

Compares every email in manifest_s{SID}.csv against the live votes table
(paginated, 1000/req) and writes a fresh results file:
  already in DB  -> status=ok, detail=rebuilt: present in votes
  not in DB     -> NOT written (will be sent on next resume run)

This recovers from a lost/truncated results file (e.g. killed terminal,
accidental dry-run overwrite) without resending 1300 votes.

Usage:
  python loadtest_rebuild.py --server-id 1
  python loadtest_rebuild.py --server-id 1 --category-id <uuid>  # narrow check
"""
import argparse, csv, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from agent import SUPABASE_URL, HEADERS
import requests

def fetch_voted_emails(category_id=None):
    """Return set of lower-cased voter_emails present in votes."""
    import urllib.parse
    emails = set()
    page_size = 1000
    offset = 0
    while True:
        params = {"select": "voter_email", "limit": str(page_size),
                  "offset": str(offset)}
        if category_id:
            params["category_id"] = f"eq.{category_id}"
        r = requests.get(f"{SUPABASE_URL}/rest/v1/votes",
                         headers={**HEADERS, "Accept": "application/json"},
                         params=params, timeout=60)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            em = (row.get("voter_email") or "").strip().lower()
            if em:
                emails.add(em)
        print(f"  fetched {len(emails)} emails so far (page @{offset})...")
        if len(rows) < page_size:
            break
        offset += page_size
    return emails

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-id", type=int, default=1)
    ap.add_argument("--folder", default=str(BASE / "votes_folder"))
    ap.add_argument("--category-id", default=None)
    a = ap.parse_args()
    folder = Path(a.folder)
    man = folder / f"manifest_s{a.server_id}.csv"
    outp = folder / f"results_s{a.server_id}.csv"
    rows = list(csv.DictReader(man.open(encoding="utf-8")))
    print(f"manifest: {len(rows)} rows; checking Supabase votes table...")
    voted = fetch_voted_emails(a.category_id)
    print(f"  votes table holds {len(voted)} unique emails")
    present = [r for r in rows
               if (r.get("voter_email") or "").strip().lower() in voted]
    missing = [r for r in rows
               if (r.get("voter_email") or "").strip().lower() not in voted]
    print(f"  {len(present)} already recorded, {len(missing)} still to send")
    bak = outp.with_suffix(".csv.bak")
    if outp.exists():
        outp.replace(bak)
        print(f"  old {outp.name} -> {bak.name}")
    with open(outp, "w", newline="", encoding="utf-8") as fh:
        W = csv.writer(fh)
        W.writerow(["seq", "email", "scheduled_s", "sent_at_s", "status", "detail"])
        for r in present:
            W.writerow([r["seq"], r["voter_email"], r["offset_sec"], "",
                        "ok", "rebuilt: present in votes"])
    print(f"  wrote {len(present)} checkpoint rows -> {outp.name}")
    print(f"  resume with: python satuk_agent/loadtest_run.py --server-id "
          f"{a.server_id} --workers 8 --max-rps 5 --live --allow-real")

if __name__ == "__main__":
    main()
