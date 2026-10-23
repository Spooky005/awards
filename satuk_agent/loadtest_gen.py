#!/usr/bin/env python3
"""Generate a 1k-voter loadtest dataset for one server (of 4).

Each server makes 1000 unique synthetic voters:
  loadtest-s{SID}-NNNN@satukawards2026.co.ke
Unique per category (1-vote-per-email rule), clearly prefixed for cleanup:
  DELETE FROM votes WHERE voter_email LIKE 'loadtest-%';

Traffic model: compressed-time Non-Homogeneous Poisson Process (NHPP).
Real peak ~4000 votes/day across 4 servers, ~70% in 3 humps. We compress
one server's 1k into T minutes preserving hump shape, sampled by thinning
so inter-arrivals are Exponential-like (human), not metronomic 60s.

Usage:
  python loadtest_gen.py --server-id 1 --count 1000 --minutes 60 --seed 11
"""
import argparse, csv, random, math, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "votes_folder"
CLUB_CAT = "697391ad-baf1-4ba9-b0dd-e32f771fe915"
NOMINEES = [
    ("6845e30a-71fe-4626-b4bc-f21229b51663", "AECAS", 0.50),
    ("3fce36f5-8933-483b-b98c-35f94338863a", "CSA-TUK", 0.30),
    ("7e59bf05-6c92-4960-baf2-1b04181118fd", "TU-K CU", 0.20),
]
FIRST = ["Wanjiku","Brian","Achieng","Otieno","Kamau","Njeri","Ochieng","Atieno",
         "Mwangi","Chebet","Kiprop","Njoroge","Auma","Odhiambo","Wafula","Mutiso",
         "Kerubo","Onyango","Faith","Derrick","Wema","Weyne","Whitney","Yvonne"]
LAST = ["Kamau","Otieno","Aduogo","Osiemo","Kagwiria","Magara","Osano","Nyanchera",
        "Kiptoo","Mutua","Wanjiru","Omondi","Kiplagat","Cherono","Barasa","Wekesa"]

def rate_shape(u, spike=1.0):
    def g(c, w): return math.exp(-((u - c) / w) ** 2)
    w = 0.30 + 2.0*g(0.25,0.08) + 1.5*g(0.55,0.10) + 2.5*g(0.85,0.07)
    if spike != 1.0:
        w = 0.30 + (w - 0.30) * spike
    return max(w, 0.05)

def sample_arrivals(n, total_sec, spike, rng):
    wmax = rate_shape(0.85, spike)
    out = []
    while len(out) < n:
        for _ in range((n - len(out)) * 3 + 10):
            u = rng.random()
            if rng.random() * wmax <= rate_shape(u, spike):
                out.append(u * total_sec)
                if len(out) >= n:
                    break
    out.sort()
    return out

def lookup_nominee_name(nid, rng):
    for _id, _name, _w in NOMINEES:
        if _id == nid:
            return _name
    return pick_nominee(rng)[1]

def pick_nominee(rng):
    r = rng.random(); acc = 0.0
    for nid, name, w in NOMINEES:
        acc += w
        if r <= acc:
            return nid, name
    return NOMINEES[0][0], NOMINEES[0][1]

def norm_header(h):
    return (h or "").strip().lower().replace("_", " ").replace("-", " ").strip()

HEADER_ALIASES = {  # normalised header -> canonical field
    "voter email": "voter_email", "email": "voter_email", "voter mail": "voter_email",
    "email address": "voter_email", "e mail": "voter_email",
    "mail": "voter_email",
    "voter name": "voter_name", "name": "voter_name", "voter": "voter_name",
    "full name": "voter_name",
    "category id": "category_id", "category": "category_id",
    "nominee id": "nominee_id", "nominee": "nominee_id",
    "nominee name": "nominee_name",
}

def load_voters(args):
    """Load voter rows from --csv or synthesize.
    Returns [(name,email,cat_id,nom_id,nom_name)] honouring per-row
    category/nominee columns when present (else campaign defaults)."""
    import re
    pat = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
    if args.csv:
        src = args.csv
        if src.lower().endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
            try:
                import openpyxl
            except ImportError:
                sys.exit("openpyxl not installed: pip install openpyxl")
            wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
            ws = wb.active
            it = ws.iter_rows(values_only=True)
            header = [(c if c is not None else "") for c in next(it)]
            raw = [dict(zip(header, [(c if c is not None else "") for c in row]))
                   for row in it]
            # drop fully-empty rows (trailing blanks in Excel)
            raw = [r for r in raw if any(str(v).strip() for v in r.values())]
        else:
            raw = list(csv.DictReader(open(src, encoding="utf-8-sig")))
        if not raw:
            sys.exit("input file is empty")
        rows = [{HEADER_ALIASES.get(norm_header(k), k): (v or "") for k, v in r.items()}
                for r in raw]
        if "voter_email" not in rows[0]:
            sys.exit(f"CSV must have a voter-email column; found: {list(raw[0].keys())}")
        voters = []
        seen = set()
        for r in rows:
            em = (r.get("voter_email") or "").strip().lower()
            if not pat.fullmatch(em):
                print(f"  skip bad email: {em!r}")
                continue
            if em in seen:
                print(f"  skip duplicate: {em}")
                continue
            seen.add(em)
            voters.append((
                (r.get("voter_name") or "").strip() or em.split("@")[0],
                em,
                (r.get("category_id") or "").strip() or CLUB_CAT,
                (r.get("nominee_id") or "").strip() or None,  # None = weighted pick
                (r.get("nominee_name") or "").strip() or None,
            ))
        if not voters:
            sys.exit("no valid unique emails in CSV")
        print(f"  {len(voters)} unique valid emails from {args.csv}")
        return voters
    rng0 = random.Random(args.seed if args.seed is not None else 1000 + sid * 97)
    return [(f"{rng0.choice(FIRST)} {rng0.choice(LAST)}",
             f"loadtest-s{sid}-{i:04d}@satukawards2026.co.ke",
             CLUB_CAT, None, None)
            for i in range(1, args.count + 1)]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-id", type=int, default=1)
    ap.add_argument("--servers", type=int, default=4)
    ap.add_argument("--count", type=int, default=1000)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--spike", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--csv", default=None,
                    help="your CSV or XLSX file with voter_email (+optional voter_name, "
                         "category_id, nominee_id, nominee_name); "
                         "deletes old s{SID}-*.yaml first, --count ignored")
    ap.add_argument("--clear", action="store_true",
                    help="delete old s{SID}-*.yaml + manifest/schedule/results before gen")
    ap.add_argument("--shuffle-voters", dest="shuffle_voters", action="store_true",
                    default=True,
                    help="shuffle voter order before assigning NHPP slots (default: on); "
                         "breaks alphabetical runs so sends jump across emails")
    ap.add_argument("--no-shuffle-voters", dest="shuffle_voters", action="store_false",
                    help="keep input file order")
    ap.add_argument("--nominee", default=None,
                    help="force all votes to one nominee: AECAS, CSA-TUK, TU-K-CU, "
                         "or a nominee UUID (overrides file columns + 50/30/20 split)")
    ap.add_argument("--out", default=str(OUT_DIR))
    a = ap.parse_args()
    global sid
    sid = a.server_id
    rng = random.Random(a.seed if a.seed is not None else 1000 + sid*97)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.csv or a.clear:  # wipe this server's old yamls + manifests first
        old = list(out.glob(f"s{sid}-*.yaml")) + [out/f"manifest_s{sid}.csv",
            out/f"schedule_s{sid}.csv", out/f"results_s{sid}.csv"]
        n = 0
        for p in old:
            if p.exists():
                p.unlink(); n += 1
        print(f"  cleared {n} old file(s) for s{sid}")
    voters = load_voters(a)
    if a.nominee:  # force single nominee (e.g. all AECAS)
        key = a.nominee.strip().lower()
        by_name = {"aecas": NOMINEES[0], "csa-tuk": NOMINEES[1], "csa": NOMINEES[1],
                   "tu-k cu": NOMINEES[2], "tu-k-cu": NOMINEES[2], "tuk-cu": NOMINEES[2]}
        if key in by_name:
            nid_f, nname_f = by_name[key][0], by_name[key][1]
        else:  # raw UUID
            nid_f, nname_f = a.nominee.strip(), lookup_nominee_name(a.nominee.strip(), rng)
        voters = [(vn, ve, CLUB_CAT, nid_f, nname_f) for (vn, ve, _c, _n, _nn) in voters]
        print(f"  nominee override: all {nname_f} ({nid_f})")
    if a.shuffle_voters:  # break alphabetical runs: sends jump across emails
        rng.shuffle(voters)
        print(f"  shuffled voter order (seed={a.seed})")
    n = len(voters)
    total = a.minutes * 60.0
    offs = sample_arrivals(n, total, a.spike, rng)
    shift = (sid-1) * (total / a.servers / 8.0)  # decorrelate 4 servers at t=0
    offs = sorted(((o + shift) % total) for o in offs)
    mean_rps = n / total
    peak = mean_rps * (rate_shape(0.85, a.spike) / 1.8)
    src = f"csv {a.csv}" if a.csv else f"synthetic seed={a.seed}"
    print(f"server s{sid}: N={n} in {a.minutes:g}min ({src})")
    print(f"  mean {mean_rps*60:.1f}/min  est peak ~{peak*60:.0f}/min")
    print(f"  vs old 60s fixed: {n*60/3600:.1f}h -> {a.minutes:g}min")
    mw = open(out/f"manifest_s{sid}.csv","w",newline="",encoding="utf-8")
    sw = open(out/f"schedule_s{sid}.csv","w",newline="",encoding="utf-8")
    import csv as _csv
    W = _csv.writer(mw); S = _csv.writer(sw)
    W.writerow(["seq","file","voter_name","voter_email","category_id",
                "nominee_id","nominee_name","offset_sec"])
    S.writerow(["seq","offset_sec","inter_arrival_sec"])
    prev = 0.0
    for i, (off, (vn, ve, cat, nid0, nname0)) in enumerate(zip(offs, voters), 1):
        fn = f"s{sid}-{i:04d}.yaml"
        if nid0:
            nid, nname = nid0, (nname0 or lookup_nominee_name(nid0, rng))
        else:
            nid, nname = pick_nominee(rng)
            if nname0:
                nname = nname0
        (out/fn).write_text(
            f'category_id: "{cat}"\nnominee_id: "{nid}"\n'
            f'nominee_name: "{nname}"\nvoter_name: "{vn}"\n'
            f'voter_email: "{ve}"\n', encoding="utf-8")
        W.writerow([i,fn,vn,ve,cat,nid,nname,f"{off:.3f}"])
        S.writerow([i,f"{off:.3f}",f"{(off-prev if i>1 else 0):.3f}"])
        prev = off
    mw.close(); sw.close()
    print(f"  wrote {n} yaml -> {out}")
    if not a.csv:
        print(f"  CLEANUP: DELETE FROM votes WHERE voter_email LIKE 'loadtest-s{sid}-%';")

if __name__ == "__main__":
    main()

