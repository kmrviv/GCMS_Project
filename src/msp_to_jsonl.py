"""
Convert a (large) NIST .msp library into JSONL, one compound per line, in the
same record shape imgprocess.py emits:

    {"name": ..., "bar_count": <n peaks>, "peaks": [{"mz": ..., "intensity": ...}, ...]}

Values are kept RAW exactly as written in the .msp -- no m/z division, no
intensity normalization. Scale later.

Streams the file line-by-line so a 267k-record library never loads fully into
memory.
"""
import re
import json
import argparse
from pathlib import Path

_PAIR = re.compile(r"(\d+)\s+(\d+)")


def iter_blocks(path):
    """Yield raw record blocks, each starting at a 'Name:' line."""
    block = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("Name:") and block:
                yield "".join(block)
                block = [line]
            else:
                block.append(line)
    if block:
        yield "".join(block)


def block_to_record(block):
    """Parse one block into the imgprocess record form, or None if it isn't a
    real compound block."""
    nm = re.search(r"(?m)^Name:\s*(.+)$", block)
    npk = re.search(r"(?m)^Num Peaks:\s*(\d+)", block)
    if nm is None or npk is None:
        return None

    name = nm.group(1).strip()
    declared = int(npk.group(1))

    pairs = _PAIR.findall(block[npk.end():])
    if declared and len(pairs) > declared:      # guard against stray trailing numbers
        pairs = pairs[:declared]

    peaks = [{"mz": int(mz), "intensity": int(it)} for mz, it in pairs]
    return {"name": name, "bar_count": len(peaks), "peaks": peaks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="NIST .msp file")
    ap.add_argument("-o", "--output", help="output JSONL (default: <input>.jsonl)")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output) if args.output else inp.with_suffix(".jsonl")

    n = skipped = 0
    with open(out, "w", encoding="utf-8") as f:
        for block in iter_blocks(inp):
            rec = block_to_record(block)
            if rec is None:
                skipped += 1
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            if n % 25000 == 0:
                print(f"  ...{n} records")

    print(f"done: {n} records written to {out.name}" +
          (f" ({skipped} non-compound blocks skipped)" if skipped else ""))


if __name__ == "__main__":
    main()
