"""
Sort the compound records inside NIST .msp file(s) by an id field.

Each record block is kept verbatim (all fields preserved) -- only the order
of records changes. Default key is NIST#; pass --key db to sort by DB#.

Non-destructive by default: writes <name>_sorted.msp. Use --inplace to
overwrite the original.
"""
import re
import argparse
from pathlib import Path

KEY_FIELDS = {"nist": "NIST#", "db": "DB#"}


def _split_records(text):
    """Yield raw record blocks, each starting at a 'Name:' line."""
    for block in re.split(r"(?m)^(?=Name:)", text):
        block = block.strip()
        if block.startswith("Name:"):
            yield block


def _record_id(block, field):
    m = re.search(rf"(?m)^{re.escape(field)}:\s*(\d+)", block)
    return int(m.group(1)) if m else None


def sort_msp_text(text, field):
    """Return (sorted_text, n_records, n_missing). Records lacking the key
    are placed last, keeping their original relative order."""
    records = list(_split_records(text))
    n_missing = sum(1 for r in records if _record_id(r, field) is None)
    # stable sort: (has_no_id, id) -> ones with ids first, in ascending order
    records.sort(key=lambda r: (_record_id(r, field) is None,
                                _record_id(r, field) or 0))
    return "\n\n".join(records) + "\n", len(records), n_missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help=".msp file or folder of .msp files")
    ap.add_argument("--key", choices=KEY_FIELDS, default="nist",
                    help="id field to sort by (default: nist -> NIST#)")
    ap.add_argument("--inplace", action="store_true",
                    help="overwrite the original instead of writing *_sorted.msp")
    args = ap.parse_args()

    field = KEY_FIELDS[args.key]
    inp = Path(args.input)
    files = [inp] if inp.is_file() else sorted(inp.glob("*.msp"))
    if not files:
        raise FileNotFoundError(f"No .msp files found at {args.input}")

    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        sorted_text, n, n_missing = sort_msp_text(text, field)
        out = f if args.inplace else f.with_name(f.stem + "_sorted.msp")
        out.write_text(sorted_text, encoding="utf-8")
        note = f"  ({n_missing} without {field} placed last)" if n_missing else ""
        print(f"{f.name}: {n} records sorted by {field} -> {out.name}{note}")


if __name__ == "__main__":
    main()
