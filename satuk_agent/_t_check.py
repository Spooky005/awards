import csv
import collections
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = list(csv.DictReader(open(HERE / "submission_log.csv", newline="", encoding="utf-8")))
start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
live = [r for r in rows if r.get("vote_number", "").isdigit() and int(r["vote_number"]) >= start]
c = collections.Counter(r["file"] for r in live)
nums = [int(r["vote_number"]) for r in live]
print("rows:", len(live))
print("unique files:", len(c))
print("double-voted files (must be 0):", len([k for k, v in c.items() if v > 1]))
print("vote_numbers unique:", len(set(nums)) == len(nums))
print("malformed (must be 0):", len([r for r in live if not r.get("kind") or not r.get("status")]))
print("counter:", json.load(open(HERE / ".vote_counter.json"))["successful_votes"])
print("max in log:", max(nums) if nums else 0)
print("statuses:", dict(collections.Counter(r["status"] for r in live)))
