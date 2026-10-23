#!/usr/bin/env python3
"""Generate AECAS vote YAMLs from a local Excel/CSV sheet into votes_folder.

The input sheet only needs TWO columns: voter_name and voter_email.
category_id, nominee_id and nominee_name are CONSTANT for AECAS
(Club or Society of the Year Award) and are injected into every generated
YAML.

Usage:
  python generate_votes.py --init-template     # create votes.xlsx (header + 2 samples)
  python generate_votes.py --dry-run           # preview without writing/deleting
  python generate_votes.py                     # generate + auto-delete populated rows
  python generate_votes.py --keep-rows         # generate but leave rows in the sheet
  python generate_votes.py --normalize-input   # rewrite the sheet to the 2-column layout
  python generate_votes.py --input votes_input/votes.csv   # CSV also accepted

Rows 2-3 are TEMPLATE samples: never generated, never deleted.
"""
import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_XLSX = BASE_DIR / "votes_input" / "votes.xlsx"
DEFAULT_OUT = BASE_DIR / "votes_folder"

# --- Constant vote target: AECAS (Club or Society of the Year Award) ---
CATEGORY_ID = "697391ad-baf1-4ba9-b0dd-e32f771fe915"
NOMINEE_ID = "6845e30a-71fe-4626-b4bc-f21229b51663"
NOMINEE_NAME = "AECAS"

# --- Input layout: only these two columns are required ---
HEADER = ["voter_name", "voter_email"]
TEMPLATE_ROWS = [
    ["Abdullahi Gitau", "abdullahig@gmail.com"],
    ["Adhiambo Wambui", "adhiawambui781@gmail.com"],
]
# Populated rows start after header (row 1) + template samples (rows 2-3).
FIRST_DATA_ROW = len(TEMPLATE_ROWS) + 2  # = 4 (1-based, xlsx + csv alike)

# Header names accepted for the two required columns.
NAME_KEYS = {"voter name", "name", "voter", "full name", "voter full name"}
EMAIL_KEYS = {"voter email", "email", "email address", "voter email address"}

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def sanitize_nominee(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def sanitize_voter(name):
    s = str(name).lower()
    s = re.sub(r"\s+", "_", s)
    return re.sub(r"[^a-z0-9_]", "", s)


def q(value):
    """Quote a YAML double-quoted scalar (fixes Apps Script bug with embedded quotes)."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def yaml_body(category_id, nominee_id, nominee_name, voter_name, voter_email):
    return (
        f'category_id: "{q(category_id)}"\n'
        f'nominee_id: "{q(nominee_id)}"\n'
        f'nominee_name: "{q(nominee_name)}"\n'
        f'voter_name: "{q(voter_name)}"\n'
        f'voter_email: "{q(voter_email)}"\n'
    )


# Stems already claimed during this run (so --dry-run detects collisions too).
RESERVED_STEMS = set()


def unique_path(out_dir, stem):
    """aecas_jane.yaml, then aecas_jane_2.yaml ... (Drive allowed dupes, disk must not)."""
    cand = out_dir / f"{stem}.yaml"
    n = 2
    while cand.exists() or cand.stem in RESERVED_STEMS:
        cand = out_dir / f"{stem}_{n}.yaml"
        n += 1
    RESERVED_STEMS.add(cand.stem)
    return cand


def normalize_header(value):
    """Header cell to comparable key ("voter_name" becomes "voter name")."""
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def resolve_columns(header_cells):
    """Return (name_idx, email_idx) for a header row.

    Works with the current 2-column layout and with the legacy 5-column
    layout: category_id / nominee_id / nominee_name are ignored, because the
    AECAS target is constant and is always injected by this script.
    """
    norm = {}
    for i, cell in enumerate(header_cells):
        key = normalize_header(cell)
        if key:
            norm.setdefault(key, i)
    name_idx = next((i for k, i in norm.items() if k in NAME_KEYS), None)
    email_idx = next((i for k, i in norm.items() if k in EMAIL_KEYS), None)
    if name_idx is None or email_idx is None:
        found = ", ".join(str(c).strip() for c in header_cells if str(c or "").strip())
        raise ValueError(
            "header row must contain 'voter_name' and 'voter_email' columns "
            f"(found: {found or 'nothing'})"
        )
    return name_idx, email_idx


def init_template(path):
    """Create votes.xlsx with header + 2 AECAS sample rows (2 columns only)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Votes"
    ws.append(HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in TEMPLATE_ROWS:
        ws.append(row)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 34
    ws.freeze_panes = f"A{FIRST_DATA_ROW}"
    wb.save(path)
    print(f"Template created: {path}")
    print("Rows 2-3 are TEMPLATE samples (never generated, never deleted).")
    print(f"Paste new voters from row {FIRST_DATA_ROW} down "
          "(only voter_name + voter_email needed).")
    print(f"AECAS is constant: category_id={CATEGORY_ID} nominee_id={NOMINEE_ID}")


def handle_row(voter_name, voter_email, out_dir, dry_run):
    """Build one AECAS YAML from a voter name + email."""
    voter_name = str(voter_name or "").strip()
    voter_email = str(voter_email or "").strip().lower()
    if not voter_name:
        return False, "blank voter_name (invalid row)"
    if not voter_email or not EMAIL_RE.fullmatch(voter_email):
        return False, f"bad voter_email {voter_email!r}"
    stem = f"{sanitize_nominee(NOMINEE_NAME)}_{sanitize_voter(voter_name)}"
    dest = unique_path(out_dir, stem)
    renamed = dest.stem != stem
    body = yaml_body(CATEGORY_ID, NOMINEE_ID, NOMINEE_NAME, voter_name, voter_email)
    # Prove agent.py vote-all can parse it.
    parsed = yaml.safe_load(body)
    assert parsed["voter_email"] == voter_email, "YAML round-trip mismatch"
    if not dry_run:
        dest.write_text(body, encoding="utf-8")
    label = dest.name + (" (collision renamed)" if renamed else "")
    return True, label


def is_template_sample(cells, name_idx=0, email_idx=1):
    """True if a row is one of the TEMPLATE_ROWS samples.

    Compares the RESOLVED voter_name / voter_email cells, so it works for
    the 2-column layout and for the legacy 5-column layout alike.
    """
    name = str(cells[name_idx] or "").strip() if name_idx < len(cells) else ""
    email = str(cells[email_idx] or "").strip().lower() if email_idx < len(cells) else ""
    if not name and not email:
        return False
    for tpl in TEMPLATE_ROWS:
        if (name == str(tpl[0]).strip()
                and email == str(tpl[1]).strip().lower()):
            return True
    return False


def data_start_index(rows):
    """0-based index where populated rows begin.

    Always row 2 (index 1): EVERY row after the header is treated as data
    and template samples are skipped INDIVIDUALLY by is_template_sample().
    A single global cut-off would silently drop a real voter that sits in
    row 2 or 3 of a sheet whose other sample row was left in place.
    """
    return 1


def collect_voters(rows):
    """Return (voters, invalid); voters = [(row_no, name, email)]."""
    name_idx, email_idx = resolve_columns(list(rows[0]))
    start = data_start_index(rows)
    voters, invalid = [], []
    for idx in range(start, len(rows)):
        cells = [str(c or "").strip() for c in rows[idx]]
        if not any(cells):
            continue
        if is_template_sample(cells, name_idx, email_idx):
            # Template sample row: never a vote, never deleted.
            continue
        name = cells[name_idx] if name_idx < len(cells) else ""
        email = cells[email_idx] if email_idx < len(cells) else ""
        if not name:
            invalid.append((idx + 1, "blank voter_name"))
            continue
        if not email or not EMAIL_RE.fullmatch(email.strip().lower()):
            invalid.append((idx + 1, f"bad voter_email {email!r}"))
            continue
        voters.append((idx + 1, name, email.strip().lower()))
    return voters, invalid


def process_csv(path, out_dir, dry_run, keep_rows, existing=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = existing if existing is not None else {}
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = [list(r) for r in csv.reader(f)]
    if not rows:
        print('Input sheet is empty.')
        return [], [], 0
    name_idx, email_idx = resolve_columns(rows[0])
    start = data_start_index(rows)
    generated, skipped, collisions = [], [], 0
    invalid_rows, sample_rows, dup_rows = [], set(), []
    for idx in range(start, len(rows)):
        cells = [(c or '').strip() for c in rows[idx]]
        if not any(cells):
            continue
        if is_template_sample(cells, name_idx, email_idx):
            # Template sample row: never a vote, never deleted.
            sample_rows.add(idx)
            continue
        # Dedup: this voter already has a generated vote file -> drop the row.
        email = (cells[email_idx].strip().lower()
                 if email_idx < len(cells) else '')
        if email and email in existing:
            dup_rows.append(idx)
            continue
        ok, msg = handle_row(cells[name_idx], cells[email_idx], out_dir, dry_run)
        if ok:
            generated.append(msg)
            if msg.endswith(' (collision renamed)'):
                collisions += 1
        else:
            skipped.append(f'row {idx + 1}: {msg}')
            invalid_rows.append(idx)
    if not dry_run and not keep_rows:
        backup = path.with_suffix(path.suffix + '.bak')
        shutil.copy2(path, backup)
        # Keep: header, template samples and invalid rows. Drop the consumed
        # ones and the rows deduped against already-generated vote files.
        keep = set(range(start)) | sample_rows | set(invalid_rows)
        kept = [rows[i] for i in range(len(rows)) if i in keep]
        with open(path, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerows(kept)
        print(f'Backup: {backup.name}')
    if dup_rows:
        print(f'Dedup: removed {len(dup_rows)} sheet row(s) whose vote file '
              f'already exists')
    return generated, skipped, collisions


def process_xlsx(path, out_dir, dry_run, keep_rows, existing=None):
    from openpyxl import load_workbook

    out_dir.mkdir(parents=True, exist_ok=True)
    existing = existing if existing is not None else {}
    wb = load_workbook(path)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not rows:
        wb.close()
        print("Input sheet is empty.")
        return [], [], 0
    name_idx, email_idx = resolve_columns(rows[0])
    start = data_start_index(rows)
    generated, skipped, collisions = [], [], 0
    dup_rows = []
    # Backwards so delete_rows() does not shift indices (same as Apps Script loop).
    for r in range(ws.max_row, start, -1):
        cells = [str(ws.cell(row=r, column=c).value or "").strip()
                 for c in range(1, ws.max_column + 1)]
        if not any(cells):
            if not dry_run and not keep_rows:
                ws.delete_rows(r)
            continue
        if is_template_sample(cells, name_idx, email_idx):
            # Template sample row: never a vote, never deleted.
            continue
        # Dedup: this voter already has a generated vote file -> drop the row.
        email = (cells[email_idx].strip().lower()
                 if email_idx < len(cells) else "")
        if email and email in existing:
            dup_rows.append(r)
            if not dry_run and not keep_rows:
                ws.delete_rows(r)
            continue
        ok, msg = handle_row(cells[name_idx], cells[email_idx], out_dir, dry_run)
        if ok:
            generated.append(msg)
            if msg.endswith(" (collision renamed)"):
                collisions += 1
            if not dry_run and not keep_rows:
                ws.delete_rows(r)
        else:
            skipped.append(f"row {r}: {msg}")
            # Invalid rows are KEPT so the data can be fixed and re-run.
    if not dry_run and not keep_rows:
        backup = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup)
        wb.save(path)
        print(f"Backup: {backup.name}")
    else:
        wb.close()
    if dup_rows:
        print(f"Dedup: removed {len(dup_rows)} sheet row(s) whose vote file "
              f"already exists")
    return generated, skipped, collisions


def index_existing_voter_emails(out_dir):
    """Map voter_email -> file name for the vote files that already exist.

    Used to drop sheet rows whose vote file was generated in an earlier run
    so the same voter is never queued (and voted) twice.

    Both the output folder and its ``failed/`` quarantine are scanned: a
    quarantined file was still generated, so its row must not be
    regenerated either.
    """
    import yaml as _yaml

    index = {}
    if not out_dir.is_dir():
        return index
    candidates = [*out_dir.glob("*.yaml"), *out_dir.glob("*.yml")]
    failed_dir = out_dir / "failed"
    if failed_dir.is_dir():
        candidates += [*failed_dir.glob("*.yaml"), *failed_dir.glob("*.yml")]
    for p in candidates:
        if p.name.startswith("."):
            continue
        try:
            data = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        email = str(data.get("voter_email", "") or "").strip().lower()
        if email:
            index.setdefault(email, p.name)
    return index


def index_voted_emails(log_file=None):
    """Return the set of voter emails that were already voted (or rejected
    as already-voted) according to ``submission_log.csv``.

    ``vote-all --delete-voted`` removes a YAML as soon as its vote is
    recorded, so the file index alone cannot tell that a sheet row was
    already processed. The submission log is the durable record of that.
    """
    if log_file is None:
        log_file = BASE_DIR / "submission_log.csv"
    log_file = Path(log_file)
    voted = set()
    if not log_file.is_file():
        return voted
    try:
        import csv as _csv
        with open(log_file, newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                if row.get("kind") != "vote":
                    continue
                if row.get("status") not in ("success", "already-voted"):
                    continue
                email = str(row.get("email") or "").strip().lower()
                if email:
                    voted.add(email)
    except Exception as e:
        print(f"  [WARN] could not read submission log for dedup: {e}")
    return voted


def dedup_output(out_dir, dry_run):
    """Remove duplicate vote YAMLs sharing the same voter_email (keep first by name)."""
    import yaml as _yaml
    files = sorted(p for p in out_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
                   and p.name != ".template.yaml")
    groups = {}
    for p in files:
        try:
            data = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        email = str(data.get("voter_email", "")).strip().lower()
        if not email:
            continue
        groups.setdefault(email, []).append(p)
    to_remove = []
    for paths in groups.values():
        if len(paths) > 1:
            to_remove.extend(paths[1:])
    dup_emails = sum(1 for v in groups.values() if len(v) > 1)
    if dry_run:
        print(f"Dedup (dry-run): would remove {len(to_remove)} duplicate file(s) "
              f"across {dup_emails} email(s)")
        for p in to_remove:
            print(f"  would delete {p.name}")
        return len(to_remove), dup_emails
    removed = 0
    for p in to_remove:
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            print(f"  [WARN] could not delete {p.name}: {e}")
    print(f"Dedup: removed {removed} duplicate file(s) across {dup_emails} email(s)")
    return removed, dup_emails

def normalize_csv(path):
    """Rewrite a CSV sheet to the 2-column layout (voter_name, voter_email)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [list(r) for r in csv.reader(f)]
    if not rows:
        print("Input sheet is empty - nothing to normalize.")
        return
    voters, invalid = collect_voters(rows)
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for row in TEMPLATE_ROWS:
            w.writerow(row)
        for _no, name, email in voters:
            w.writerow([name, email])
    print(f"Backup: {backup.name}")
    print(f"Normalized {len(voters)} row(s) into {path.name} "
          f"(columns: {', '.join(HEADER)})")
    for no, msg in invalid:
        print(f"  [SKIP] row {no}: {msg}")


def normalize_xlsx(path):
    """Rewrite an xlsx sheet to the 2-column layout (voter_name, voter_email)."""
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font

    wb = load_workbook(path)
    ws = wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    if not rows:
        print("Input sheet is empty - nothing to normalize.")
        return
    voters, invalid = collect_voters(rows)

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)

    out = Workbook()
    ows = out.active
    ows.title = "Votes"
    ows.append(HEADER)
    for cell in ows[1]:
        cell.font = Font(bold=True)
    for row in TEMPLATE_ROWS:
        ows.append(row)
    for _no, name, email in voters:
        ows.append([name, email])
    ows.column_dimensions["A"].width = 24
    ows.column_dimensions["B"].width = 34
    ows.freeze_panes = f"A{FIRST_DATA_ROW}"
    out.save(path)
    print(f"Backup: {backup.name}")
    print(f"Normalized {len(voters)} row(s) into {path.name} "
          f"(columns: {', '.join(HEADER)})")
    for no, msg in invalid:
        print(f"  [SKIP] row {no}: {msg}")


def normalize_input(path):
    if path.suffix.lower() == ".csv":
        normalize_csv(path)
    else:
        normalize_xlsx(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Excel sheet to votes_folder YAMLs (AECAS).")
    ap.add_argument("--input", default=str(DEFAULT_XLSX), help="votes.xlsx or .csv file")
    ap.add_argument("--output", default=str(DEFAULT_OUT), help="votes_folder destination")
    ap.add_argument("--init-template", action="store_true", help="create template xlsx and exit")
    ap.add_argument("--normalize-input", action="store_true",
                    help="rewrite the input sheet to the 2-column "
                         "(voter_name, voter_email) layout and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="preview only: no YAMLs written, rows kept")
    ap.add_argument("--keep-rows", action="store_true",
                    help="generate but leave rows in the sheet")
    ap.add_argument("--no-dedup", action="store_true",
                    help="skip end-of-run dedup of duplicate voter emails")
    ap.add_argument("--no-voted-dedup", action="store_true",
                    help="do not drop sheet rows whose voter already has a "
                         "generated vote file or was already voted (per the "
                         "submission log)")
    ap.add_argument("--log", default=str(BASE_DIR / "submission_log.csv"),
                    help="submission log used for the already-voted dedup "
                         "(default: submission_log.csv next to this script)")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    out_dir = Path(args.output)

    if args.init_template:
        target = in_path if in_path.suffix.lower() == ".xlsx" else in_path.with_suffix(".xlsx")
        init_template(target)
        return 0

    if not in_path.is_file():
        print(f"Input not found: {in_path}")
        print(f"Run: python {Path(__file__).name} --init-template")
        return 1

    if args.normalize_input:
        normalize_input(in_path)
        return 0

    RESERVED_STEMS.clear()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Index the vote files that already exist so sheet rows for voters that
    # were already generated are deleted from the sheet instead of being
    # queued (and voted) a second time.
    existing = index_existing_voter_emails(out_dir)
    if existing:
        print(f"Existing vote files: {len(existing)} voter(s) already generated "
              f"-> their sheet rows will be dropped.")

    # vote-all --delete-voted removes a YAML once its vote is recorded, so the
    # file index alone cannot spot rows that were already processed. The
    # submission log is the durable record of those.
    if not args.no_voted_dedup:
        voted = index_voted_emails(args.log)
        if voted:
            new = voted - set(existing)
            existing.update({e: "<already voted>" for e in new})
            print(f"Already voted (per submission log): {len(voted)} voter(s) "
                  f"-> their sheet rows will be dropped.")

    if in_path.suffix.lower() == ".csv":
        generated, skipped, collisions = process_csv(in_path, out_dir, args.dry_run, args.keep_rows, existing)
    else:
        generated, skipped, collisions = process_xlsx(in_path, out_dir, args.dry_run, args.keep_rows, existing)

    for name in generated:
        print(f"  [OK] {name}")
    for s in skipped:
        print(f"  [SKIP] {s}")
    mode = "DRY-RUN - nothing written, rows kept" if args.dry_run else (
        "rows kept (--keep-rows)" if args.keep_rows
        else f"populated rows deleted, template rows kept (1 header + {len(TEMPLATE_ROWS)} samples)")
    print(f"\n=== {len(generated)} generated ({collisions} renamed), "
          f"{len(skipped)} skipped -> {out_dir} | {mode} ===")
    if not args.no_dedup:
        dedup_output(out_dir, args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())