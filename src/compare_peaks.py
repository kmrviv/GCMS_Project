import json
import re
import math
import csv
import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------
# Canonical name normalization
# ---------------------------------------------------------------------

_GLYPH = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu",
    "ξ": "xi", "π": "pi", "ρ": "rho", "σ": "sigma",
    "ς": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "χ": "chi", "ψ": "psi", "ω": "omega"
}

_GLYPH_RE = re.compile("|".join(map(re.escape, _GLYPH)))

_NIST_RE = re.compile(
    r"\.(alpha|beta|gamma|delta|epsilon|zeta|eta|theta|kappa|"
    r"lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega)\."
)


def canon(name):
    if name is None:
        return None

    s = name.lower()
    s = _NIST_RE.sub(lambda m: m.group(1), s)
    s = _GLYPH_RE.sub(lambda m: _GLYPH[m.group(0)], s)

    return re.sub(r"\s+", " ", s).strip().rstrip(" -.")


# ---------------------------------------------------------------------
# Peak conversion
# ---------------------------------------------------------------------

def results_peaks(peaks):
    d = {}

    for p in peaks:
        mz = int(round(p["mz"]))
        intensity = float(p["intensity"])

        if mz not in d or intensity > d[mz]:
            d[mz] = intensity

    return d


def reference_peaks(peaks):
    mzs = [p["mz"] for p in peaks]

    div = 10 if mzs and all(m % 10 == 0 for m in mzs) else 1

    d = {}

    for p in peaks:
        mz = p["mz"] // div
        intensity = float(p["intensity"])

        if mz not in d or intensity > d[mz]:
            d[mz] = intensity

    if d:
        max_int = max(d.values())
        if max_int > 0:
            d = {mz: round(v / max_int * 100.0, 2) for mz, v in d.items()}

    return d


# ---------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------

def cosine(a, b):

    shared = set(a) & set(b)

    if not shared:
        return 0.0

    dot = sum(a[m] * b[m] for m in shared)

    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))

    if na == 0 or nb == 0:
        return 0.0

    return dot / (na * nb)


# ---------------------------------------------------------------------
# Difference reporting
# ---------------------------------------------------------------------

def spectrum_differences(extracted, reference):

    mz_e = set(extracted)
    mz_r = set(reference)

    only_extracted = sorted(mz_e - mz_r)
    only_reference = sorted(mz_r - mz_e)

    intensity_diff = []

    for mz in sorted(mz_e & mz_r):

        if extracted[mz] != reference[mz]:
            intensity_diff.append({
                "mz": mz,
                "extracted": extracted[mz],
                "reference": reference[mz],
                "difference": extracted[mz] - reference[mz]
            })

    return only_extracted, only_reference, intensity_diff


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():

    ap = argparse.ArgumentParser()

    ap.add_argument("--results",   default=str(_ROOT / "data/processed/results.jsonl"))
    ap.add_argument("--reference", default=str(_ROOT / "data/processed/NISTds.jsonl"))
    ap.add_argument("--out",       default=str(_ROOT / "reports/compare_results.csv"))
    ap.add_argument("--report",    default=str(_ROOT / "reports/compare_report.txt"))

    args = ap.parse_args()

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    _report = open(args.report, "w", encoding="utf-8")
    _orig_stdout = sys.stdout
    sys.stdout = _report

    # ---------------------------------------------------------------
    # Load extracted spectra
    # ---------------------------------------------------------------

    extracted = {}
    dup_keys = 0

    with open(args.results, encoding="utf-8") as f:

        for line in f:

            line = line.strip()

            if not line:
                continue

            r = json.loads(line)

            if not r.get("ok", True):
                continue

            key = canon(r["name"])

            if key in extracted:
                dup_keys += 1
                continue

            extracted[key] = (
                r["name"],
                results_peaks(r["peaks"])
            )

    print(
        f"loaded {len(extracted)} extracted spectra "
        f"({dup_keys} duplicate names skipped)"
    )

    # ---------------------------------------------------------------
    # Compare against NIST
    # ---------------------------------------------------------------

    best = {}

    with open(args.reference, encoding="utf-8") as f:

        for line in f:

            line = line.strip()

            if not line:
                continue

            n = json.loads(line)

            key = canon(n["name"])

            if key not in extracted:
                continue

            extracted_name, extracted_peaks_dict = extracted[key]

            reference_peaks_dict = reference_peaks(n["peaks"])

            score = round(
                cosine(
                    extracted_peaks_dict,
                    reference_peaks_dict
                ),
                6
            )

            only_e, only_r, diff_i = spectrum_differences(
                extracted_peaks_dict,
                reference_peaks_dict
            )

            if key not in best or score > best[key]["cosine"]:

                best[key] = {
                    "name": extracted_name,
                    "cosine": score,
                    "n_extracted": len(extracted_peaks_dict),
                    "n_reference": len(reference_peaks_dict),
                    "base_peak_match":
                        bool(extracted_peaks_dict)
                        and bool(reference_peaks_dict)
                        and (
                            max(
                                extracted_peaks_dict,
                                key=extracted_peaks_dict.get
                            )
                            ==
                            max(
                                reference_peaks_dict,
                                key=reference_peaks_dict.get
                            )
                        ),
                    "only_extracted": only_e,
                    "only_reference": only_r,
                    "intensity_diff": diff_i
                }

    # ---------------------------------------------------------------
    # CSV
    # ---------------------------------------------------------------

    rows = sorted(best.values(), key=lambda r: r["cosine"])

    with open(
        args.out,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=[
                "name",
                "cosine",
                "n_extracted",
                "n_reference",
                "base_peak_match"
            ],
            extrasaction="ignore"
        )

        writer.writeheader()
        writer.writerows(rows)

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------

    n = len(rows)

    if not n:
        print("No matching compounds found.")
        sys.stdout = _orig_stdout
        _report.close()
        print(f"Report written to {args.report}")
        return

    print(f"\ncompared {n} compounds -> {args.out}")

    buckets = [
        (0.99, ">=0.99"),
        (0.95, "0.95-0.99"),
        (0.90, "0.90-0.95"),
        (0.00, "<0.90")
    ]

    prev = 1.01

    for lo, label in buckets:

        count = sum(
            1
            for r in rows
            if lo <= r["cosine"] < prev
        )

        print(
            f"{label:>10}: "
            f"{count:6d} "
            f"({100 * count / n:5.1f}%)"
        )

        prev = lo

    bp = sum(
        1
        for r in rows
        if r["base_peak_match"]
    )

    print(
        f"\nbase-peak m/z agrees: "
        f"{bp}/{n} "
        f"({100*bp/n:.1f}%)"
    )

    # ---------------------------------------------------------------
    # Peak count mismatches
    # ---------------------------------------------------------------

    count_diff = sorted(
        [r for r in rows if r["n_extracted"] != r["n_reference"]],
        key=lambda r: abs(r["n_extracted"] - r["n_reference"]),
        reverse=True
    )

    print(f"\n{len(count_diff)} compounds have different peak counts:\n")
    print(f"  {'extracted':>10}  {'reference':>10}  {'diff':>6}  name")
    print(f"  {'-'*10}  {'-'*10}  {'-'*6}  {'-'*40}")

    for r in count_diff:
        diff = r["n_extracted"] - r["n_reference"]
        print(
            f"  {r['n_extracted']:>10}  {r['n_reference']:>10}  {diff:>+6}  {r['name']}"
        )

    # ---------------------------------------------------------------
    # Worst matches
    # ---------------------------------------------------------------

    print("\nworst 15 matches:\n")

    for r in rows[:15]:

        print(
            f"{r['cosine']:.4f}  "
            f"e={r['n_extracted']:<4} "
            f"r={r['n_reference']:<4} "
            f"{'BP+' if r['base_peak_match'] else 'BP-'}  "
            f"{r['name']}"
        )

    # ---------------------------------------------------------------
    # Detailed differences
    # ---------------------------------------------------------------

    imperfect = [r for r in rows if r["cosine"] != 1.0]

    print("\n")
    print("=" * 100)
    print(f"DETAILED PEAK DIFFERENCES  ({len(imperfect)} compounds with cosine < 1.0)")
    print("=" * 100)

    for r in imperfect:

        key = canon(r["name"])
        d = best[key]

        if not d["only_extracted"] and not d["only_reference"] and not d["intensity_diff"]:
            continue

        print("\n" + "=" * 100)
        print(f"COMPOUND : {d['name']}")
        print(f"COSINE   : {d['cosine']:.6f}")
        print("=" * 100)

        if d["only_extracted"]:
            print("\nPeaks ONLY in extracted (m/z):")
            print(d["only_extracted"])

        if d["only_reference"]:
            print("\nPeaks ONLY in reference (m/z):")
            print(d["only_reference"])

        if d["intensity_diff"]:

            print("\nShared m/z with different intensities:\n")

            for item in d["intensity_diff"]:

                print(
                    f"m/z {item['mz']:>4} | "
                    f"Extracted={item['extracted']:>8.2f} | "
                    f"Reference={item['reference']:>8.2f} | "
                    f"Diff={item['difference']:+8.2f}"
                )

    print("\nDone.")
    sys.stdout = _orig_stdout
    _report.close()
    print(f"Report written to {args.report}")


if __name__ == "__main__":
    main()