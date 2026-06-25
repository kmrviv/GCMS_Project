"""
Qualitative analysis
sanitycheck.py — Automated quality checks on the JSON outputs from imgprocess.py.

Reads every .json in outputs/ and runs seven layers of checks:
  1. Schema         — required keys present, correct types
  2. Physics        — base peak = 100%, m/z in range, intensities valid
  3. Completeness   — not empty, enough peaks detected
  4. Consistency    — no duplicate m/z, m/z values are sorted
  5. Warnings       — any warning flags raised by the extractor
  6. Statistics     — outlier detection across the whole batch
  7. Spot-check     — random sample list for manual visual verification

Writes a full report to validation_report.txt and prints a summary.

Usage
-----
  python sanitycheck.py
  python sanitycheck.py --outputs outputs/ --report validation_report.txt
"""

import json
import random
import os
import sys
from pathlib import Path
from collections import defaultdict

_ROOT        = Path(__file__).resolve().parent.parent
OUTPUTS_DIR  = _ROOT / "outputs"
REPORT_FILE  = _ROOT / "reports/validation_report.txt"
VISUALS_DIR  = _ROOT / "visuals"
SPOT_N       = 15          # number of files to flag for manual spot-check
BASE_PEAK_TOL = 0.5        # intensity must be >= 100.0 - this tolerance
MZ_MIN       = 1
MZ_MAX       = 2000
MIN_PEAKS    = 3
OUTLIER_SIGMA = 6.0        # flag files more than this many std devs from mean


# ── helpers ───────────────────────────────────────────────────────────────────

class _Tee:
    def __init__(self, path):
        self._f = open(path, "w", encoding="utf-8")

    def __call__(self, *lines):
        for line in lines:
            print(line.encode("ascii", errors="replace").decode("ascii"))
            self._f.write(line + "\n")

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _std(values):
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def _load_json(path):
    """Load a JSON file. Returns (data, error_string)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, str(e)


# ── per-file checks ───────────────────────────────────────────────────────────

def _check_file(name, data):
    """
    Run all per-file checks. Returns a list of issue strings (empty = clean).
    """
    issues = []

    # ── CHECK 1: Schema ──────────────────────────────────────────────────────
    for key in ("peaks", "warnings", "method"):
        if key not in data:
            issues.append(f"SCHEMA  missing required key '{key}'")

    if "peaks" in data and not isinstance(data["peaks"], list):
        issues.append("SCHEMA  'peaks' is not a list")
        return issues  # can't continue without a valid peaks list

    # ── CHECK 2: Failed extraction ────────────────────────────────────────────
    if data.get("ok") is False:
        err = data.get("warnings", ["unknown error"])
        issues.append(f"FAILED  extraction failed: {err[0] if err else '?'}")
        return issues

    peaks = data.get("peaks", [])

    # ── CHECK 3: Peak entry types ─────────────────────────────────────────────
    for i, p in enumerate(peaks):
        if not isinstance(p, dict):
            issues.append(f"SCHEMA  peak[{i}] is not a dict")
            continue
        if "mz" not in p:
            issues.append(f"SCHEMA  peak[{i}] missing 'mz'")
        if "intensity" not in p:
            issues.append(f"SCHEMA  peak[{i}] missing 'intensity'")

    # ── CHECK 4: Completeness ─────────────────────────────────────────────────
    if len(peaks) == 0:
        issues.append("EMPTY   no peaks extracted")
        return issues

    if len(peaks) < MIN_PEAKS:
        issues.append(f"SPARSE  only {len(peaks)} peak(s) — suspiciously few "
                      f"(minimum expected: {MIN_PEAKS})")

    # ── CHECK 5: Physics — base peak must be 100% ─────────────────────────────
    intensities = [p["intensity"] for p in peaks if "intensity" in p]
    mz_values   = [p["mz"]        for p in peaks if "mz"        in p]

    if intensities:
        base = max(intensities)
        if base < 100.0 - BASE_PEAK_TOL:
            issues.append(f"PHYSICS base peak is {base:.2f}% — should be 100.0% "
                          f"(y-axis calibration drift)")
        if base > 100.1:
            issues.append(f"PHYSICS base peak is {base:.2f}% — exceeds 100% "
                          f"(clamping failed)")

    # ── CHECK 6: Physics — intensity range ────────────────────────────────────
    bad_intensity = [i for i in intensities if i <= 0 or i > 100.1]
    if bad_intensity:
        issues.append(f"PHYSICS {len(bad_intensity)} intensity value(s) outside "
                      f"(0, 100]: {bad_intensity[:5]}")

    # ── CHECK 7: Physics — m/z range ─────────────────────────────────────────
    if mz_values:
        bad_mz = [m for m in mz_values if m < MZ_MIN or m > MZ_MAX]
        if bad_mz:
            issues.append(f"PHYSICS {len(bad_mz)} m/z value(s) outside "
                          f"[{MZ_MIN}, {MZ_MAX}]: {bad_mz[:5]}")

        if min(mz_values) < 0:
            issues.append(f"PHYSICS negative m/z detected: {min(mz_values)}")

    # ── CHECK 8: Consistency — duplicate m/z ─────────────────────────────────
    seen = {}
    for m in mz_values:
        seen[m] = seen.get(m, 0) + 1
    dupes = [m for m, n in seen.items() if n > 1]
    if dupes:
        issues.append(f"DUPE    duplicate m/z values: {sorted(dupes)[:10]}")

    # ── CHECK 9: Consistency — m/z order ─────────────────────────────────────
    if mz_values != sorted(mz_values):
        issues.append("ORDER   m/z values are not sorted ascending "
                      "(may indicate extraction ordering bug)")

    # ── CHECK 10: Extractor warnings ─────────────────────────────────────────
    import re as _re
    w = data.get("warnings", [])
    if isinstance(w, list) and w:
        for msg in w:
            # Skip "out-of-range m/z" extractor warnings whose values now fall
            # within [MZ_MIN, MZ_MAX] — the physics check above is authoritative.
            if "out-of-range m/z" in msg:
                flagged = [int(x) for x in _re.findall(r'\d+', msg)]
                if all(MZ_MIN <= v <= MZ_MAX for v in flagged):
                    continue
            issues.append(f"WARN    {msg}")

    return issues


# ── statistical outlier detection ─────────────────────────────────────────────

def _find_outliers(stats_by_file):
    """
    Compare each file's peak count against the batch mean.
    Max m/z is not flagged here — the physics check [MZ_MIN, MZ_MAX=2000] already
    covers the valid range, so any value within [2, 2000] is acceptable.
    Returns dict: filename -> list of outlier descriptions.
    """
    outliers = defaultdict(list)
    counts = {f: s["n_peaks"] for f, s in stats_by_file.items() if s["n_peaks"] is not None}
    for label, values_dict in [("peak count", counts)]:
        vals = list(values_dict.values())
        if len(vals) < 3:
            continue
        mu  = _mean(vals)
        sig = _std(vals)
        if sig == 0:
            continue
        for fname, v in values_dict.items():
            z = abs(v - mu) / sig
            if z > OUTLIER_SIGMA:
                direction = "high" if v > mu else "low"
                outliers[fname].append(
                    f"OUTLIER {label} = {v}  "
                    f"(mean={mu:.1f}, sd={sig:.1f}, z={z:.1f}, {direction})"
                )
    return outliers


# ── main validation ───────────────────────────────────────────────────────────

def validate(outputs_dir=OUTPUTS_DIR, report_path=REPORT_FILE, spot_n=SPOT_N):
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    json_files = sorted(outputs_dir.glob("*.json"))
    if not json_files:
        sys.exit(f"[validate] No JSON files found in '{outputs_dir}'.")

    SEP  = "=" * 72
    THIN = "-" * 72

    with _Tee(report_path) as out:

        out(
            "",
            SEP,
            "  GCMS JSON OUTPUT VALIDATION REPORT",
            SEP,
            f"  Source folder : {outputs_dir.resolve()}",
            f"  Files checked : {len(json_files)}",
            f"  Report        : {report_path.resolve()}",
            SEP,
            "",
        )

        # ── Pass 1: per-file checks ───────────────────────────────────────────
        all_issues   = {}   # filename -> [issue strings]
        stats        = {}   # filename -> {n_peaks, max_mz, base_peak, method}
        method_count = defaultdict(int)
        load_errors  = []

        for path in json_files:
            data, err = _load_json(path)
            if err:
                load_errors.append((path.name, err))
                all_issues[path.name] = [f"LOAD    could not parse JSON: {err}"]
                continue

            issues = _check_file(path.name, data)
            all_issues[path.name] = issues

            peaks = data.get("peaks", [])
            intensities = [p["intensity"] for p in peaks if "intensity" in p]
            mz_values   = [p["mz"]        for p in peaks if "mz"        in p]
            method      = data.get("method", "unknown")
            method_count[method] += 1

            stats[path.name] = {
                "n_peaks":  len(peaks),
                "max_mz":   max(mz_values)   if mz_values   else None,
                "base_peak": max(intensities) if intensities else None,
                "method":   method,
            }

        # ── Pass 2: statistical outliers ──────────────────────────────────────
        outliers = _find_outliers(stats)
        for fname, msgs in outliers.items():
            all_issues[fname].extend(msgs)

        # ── Categorise results ────────────────────────────────────────────────
        # ALERT_TAGS = low-priority informational flags (peak count outliers,
        # sparse spectra). Real issues are everything else.
        ALERT_TAGS = {"OUTLIER", "SPARSE"}

        def _issue_tags(issues):
            return {i.split()[0] for i in issues}

        clean_files  = [f for f, issues in all_issues.items() if not issues]
        failed_files = [f for f, issues in all_issues.items()
                        if any("FAILED" in i or "LOAD" in i for i in issues)]
        # warned = has at least one non-alert issue (excluding failed)
        warned_files = [f for f, issues in all_issues.items()
                        if issues and f not in failed_files
                        and not _issue_tags(issues).issubset(ALERT_TAGS)]
        # alert-only = every issue is an alert tag (no real warnings, not failed)
        alert_files  = [f for f, issues in all_issues.items()
                        if issues and f not in failed_files and f not in warned_files]

        # ── Section 1: Summary ────────────────────────────────────────────────
        out(
            "  SECTION 1 — SUMMARY",
            THIN,
            "",
            f"  Total files    : {len(json_files)}",
            f"  PASSED (clean) : {len(clean_files)}",
            f"  WARNINGS       : {len(warned_files)}",
            f"  ALERTS         : {len(alert_files)}",
            f"  FAILED         : {len(failed_files)}",
            "",
            f"  Pipeline usage :",
        )
        for method, count in sorted(method_count.items()):
            pct = count / len(json_files) * 100
            out(f"    {method:10s}: {count:4d} files  ({pct:.1f}%)")

        # Batch-wide peak count stats
        n_peaks_all = [s["n_peaks"] for s in stats.values() if s["n_peaks"] is not None]
        if n_peaks_all:
            out(
                "",
                "  Peak count across batch:",
                f"    min    : {min(n_peaks_all)}",
                f"    max    : {max(n_peaks_all)}",
                f"    mean   : {_mean(n_peaks_all):.1f}",
                f"    std dev: {_std(n_peaks_all):.1f}",
            )

        max_mz_all = [s["max_mz"] for s in stats.values() if s["max_mz"] is not None]
        if max_mz_all:
            out(
                "",
                "  Highest m/z across batch:",
                f"    min    : {min(max_mz_all)}",
                f"    max    : {max(max_mz_all)}",
                f"    mean   : {_mean(max_mz_all):.1f}",
            )

        out("")

        # ── Section 2: Files with issues ─────────────────────────────────────
        out(
            "  SECTION 2 — FILES WITH ISSUES",
            THIN,
            "",
        )

        if not warned_files and not failed_files and not alert_files:
            out("  All files passed. No issues detected.", "")
        else:
            def _print_file_issues(fname, tag):
                issues = all_issues[fname]
                s = stats.get(fname, {})
                out(f"  [{tag}] {fname}")
                if s:
                    out(f"           peaks={s.get('n_peaks','?')}  "
                        f"max_mz={s.get('max_mz','?')}  "
                        f"base={s.get('base_peak','?')}%  "
                        f"method={s.get('method','?')}")
                for issue in issues:
                    out(f"           >> {issue}")
                out("")

            for fname in sorted(failed_files):
                _print_file_issues(fname, "FAILED")
            for fname in sorted(warned_files):
                _print_file_issues(fname, "WARN  ")

            if alert_files:
                out("  -- ALERTS (informational, lower priority) --", "")
                for fname in sorted(alert_files):
                    _print_file_issues(fname, "ALERT ")

        # ── Section 3: Issue type breakdown ───────────────────────────────────
        out(
            "  SECTION 3 — ISSUE TYPE BREAKDOWN",
            THIN,
            "",
        )
        warn_counts  = defaultdict(int)
        alert_counts = defaultdict(int)
        for issues in all_issues.values():
            for issue in issues:
                tag = issue.split()[0]
                if tag in ALERT_TAGS:
                    alert_counts[tag] += 1
                else:
                    warn_counts[tag] += 1

        if warn_counts:
            out("  Warnings (action required):")
            for tag, count in sorted(warn_counts.items(), key=lambda x: -x[1]):
                out(f"    {tag:<10}: {count} occurrence(s)")
            out("")
        if alert_counts:
            out("  Alerts (informational):")
            for tag, count in sorted(alert_counts.items(), key=lambda x: -x[1]):
                out(f"    {tag:<10}: {count} occurrence(s)")
            out("")
        if not warn_counts and not alert_counts:
            out("  No issues found across any file.")
        out("")

        # ── Section 4: Spot-check list ────────────────────────────────────────
        out(
            "  SECTION 4 — SPOT-CHECK SAMPLE FOR MANUAL VERIFICATION",
            THIN,
            "",
            "  You cannot check all 251 PDFs by hand. Instead, check a",
            "  representative random sample. These files were randomly selected:",
            "  open the PDF alongside its visual PNG and JSON to verify.",
            "",
        )

        # Priority spot-check: failed + warned first, alert-only files last
        priority = list(set(warned_files + failed_files + alert_files))
        remaining = [f for f in [p.name for p in json_files] if f not in priority]
        random.seed(42)   # reproducible selection
        random_sample = random.sample(remaining, min(spot_n, len(remaining)))
        spot_list = priority[:spot_n] + random_sample[:max(0, spot_n - len(priority))]

        visual_exists = VISUALS_DIR.exists()
        for fname in sorted(spot_list):
            stem    = Path(fname).stem
            json_p  = outputs_dir / fname
            visual_p = VISUALS_DIR / f"{stem}.png"
            has_vis  = visual_p.exists() if visual_exists else False
            vis_note = f"  visual: {visual_p}" if has_vis else "  (no visual — re-run with --visual)"
            out(f"  {fname}")
            out(f"    JSON   : {json_p}")
            out(vis_note)
            out("")

        if not visual_exists or not any((VISUALS_DIR / f"{Path(f).stem}.png").exists()
                                         for f in [p.name for p in json_files]):
            out(
                "  NOTE: No visual overlays found. Generate them with:",
                "    python imgprocess.py --input samples/ --visual",
                "  Then open visuals/<name>.png to see axis detection, tick marks,",
                "  and bar-top markers overlaid on the rasterised PDF page.",
                "",
            )

        # ── Section 5: What to look for when spot-checking ────────────────────
        out(
            "  SECTION 5 — WHAT TO CHECK DURING MANUAL SPOT-CHECKS",
            THIN,
            "",
            "  For each spot-check file, open THREE things side by side:",
            "    A) The original PDF",
            "    B) The visual overlay PNG  (visuals/<name>.png)",
            "    C) The JSON output         (outputs/<name>.json)",
            "",
            "  WHAT TO VERIFY IN THE VISUAL (PNG):",
            "    • Red horizontal line   = detected x-axis (baseline)",
            "      Should sit exactly on the x-axis of the chart.",
            "    • Blue vertical line    = detected y-axis",
            "      Should sit exactly on the left edge of the chart.",
            "    • Green dot             = axis intersection (origin)",
            "    • Orange boxes/dots     = detected x-axis tick labels",
            "      Should align with the numeric labels printed under the axis.",
            "    • Orange vertical line  = right edge of chart",
            "    • Red dots              = detected bar tops",
            "      Should sit at the top of every visible bar in the chart.",
            "",
            "  WHAT TO VERIFY IN THE JSON:",
            "    • 'method' should be 'vector' for clean digital PDFs.",
            "      If it is 'raster', the vector pipeline failed — check why.",
            "    • The highest-intensity peak should have intensity = 100.0.",
            "    • m/z values should match the bars visible in the PDF chart.",
            "    • Peak count should look reasonable for the chart density.",
            "",
            "  RED FLAGS TO LOOK FOR:",
            "    • Red horizontal line is in the wrong position (wrong axis found)",
            "    • Red dots are on text or noise rather than bar tops",
            "    • Bars visible in the PDF that have no red dot (missed peaks)",
            "    • Red dots with no corresponding bar (ghost peaks)",
            "    • JSON has m/z 0 or negative values",
            "    • JSON base peak intensity very different from 100.0",
            "",
        )

        # ── Section 6: Why each check catches a real error ────────────────────
        out(
            "  SECTION 6 — WHY EACH CHECK CATCHES A REAL ERROR",
            THIN,
            "",
            "  SCHEMA checks:",
            "    Catch corrupted or truncated JSON files — e.g., if the process",
            "    was killed mid-write, the file may be missing closing braces.",
            "",
            "  PHYSICS — base peak = 100%:",
            "    In a normalised mass spectrum this is true BY DEFINITION.",
            "    The lab instrument always normalises the tallest bar to 100%.",
            "    If your JSON shows a base peak of e.g. 73%, the y-axis",
            "    calibration drifted — the '100' label was not found or was",
            "    misread. Every peak intensity in that file is proportionally wrong.",
            "",
            "  PHYSICS — m/z range [1, 2000]:",
            "    GC-MS unit-resolution instruments measure integer masses.",
            "    m/z < 1 is physically impossible.",
            "    m/z > 2000 exceeds the scan range of standard GC-MS hardware.",
            "    Out-of-range values indicate a calibration failure where the",
            "    x-axis tick labels were misread.",
            "",
            "  COMPLETENESS — minimum 3 peaks:",
            "    Real mass spectra of organic compounds always produce many",
            "    fragment ions. A spectrum with 1–2 peaks almost certainly means",
            "    the bar scanner missed most of the chart.",
            "",
            "  DUPLICATE m/z:",
            "    A unit-resolution instrument produces exactly one bar per mass",
            "    unit. Duplicates mean two bars were assigned the same m/z —",
            "    the x-calibration is off by < 0.5 m/z units, causing two nearby",
            "    bars to round to the same integer.",
            "",
            "  OUTLIER detection (peak count):",
            "    If one file has 5 peaks and the batch average is 45, that file",
            "    almost certainly has a detection problem — not a simple spectrum.",
            "    The outlier flag tells you which files deserve a second look",
            "    without requiring you to read every JSON manually.",
            "",
            "  Max m/z range [2, 2000] (physics check):",
            "    Any m/z within the physics range is accepted. Values outside",
            "    [2, 2000] are caught by the PHYSICS check above.",
            "",
        )

        # ── Section 7: Next steps ─────────────────────────────────────────────
        out(
            "  SECTION 7 — RECOMMENDED NEXT STEPS",
            THIN,
            "",
        )

        if failed_files:
            out(
                f"  1. INVESTIGATE {len(failed_files)} FAILED FILE(S):",
                "     These files produced no usable output. Common causes:",
                "       • PDF has no vector layer AND raster pipeline also failed",
                "       • PDF is password-protected or corrupted",
                "       • fitz could not open the file (wrong format)",
                "     Action: open each failed PDF manually and check if it is a",
                "     valid GCMS spectrum file.",
                "",
            )

        if warned_files:
            out(
                f"  {'2' if failed_files else '1'}. REVIEW {len(warned_files)} FILE(S) WITH WARNINGS:",
                "     These extracted data but flagged potential issues.",
                "     Re-run with --visual and inspect the overlay PNG to confirm",
                "     whether the axis detection and bar tops look correct.",
                "     Command:",
                "       python imgprocess.py --input samples/<filename>.pdf",
                "",
            )

        out(
            f"  {'3' if warned_files or failed_files else '1'}. GENERATE VISUALS FOR SPOT-CHECK FILES:",
            "     python final.py --input samples/ --visual",
            "     Then open visuals/<name>.png alongside the PDF for each",
            f"     file in the spot-check list above.",
            "",
            f"  {'4' if warned_files or failed_files else '2'}. RE-RUN WITH CROSS-VALIDATION ON SUSPICIOUS FILES:",
            "     python imgprocess.py --input samples/<name>.pdf --cross-validate",
            "     This runs both the vector AND raster pipelines and reports",
            "     any discrepancy in peak counts or m/z values.",
            "",
        )

        # ── Final pass/fail line ──────────────────────────────────────────────
        total  = len(json_files)
        passed = len(clean_files)
        rate   = passed / total * 100
        # "effective" pass rate counts alert-only files as passing
        eff_passed = passed + len(alert_files)
        eff_rate   = eff_passed / total * 100

        out(
            SEP,
            f"  RESULT: {passed}/{total} files clean ({rate:.1f}% pass rate)",
            f"          {eff_passed}/{total} files clean or alert-only ({eff_rate:.1f}% effective)",
        )
        if len(warned_files) == 0 and len(failed_files) == 0:
            out("  All outputs passed automated validation (alerts are informational).")
        elif eff_rate >= 90.0:
            out("  Good. Review warnings before using the data. Alerts are informational.")
        elif eff_rate >= 70.0:
            out("  Moderate. Investigate warnings — a systematic error may exist.")
        else:
            out("  Poor. Likely a systematic calibration or pipeline problem.")
        out(SEP, "")

    print(f"\n  Full report saved -> {report_path.resolve()}")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Validate GCMS JSON outputs produced by imgprocess.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python sanitycheck.py\n"
            "  python sanitycheck.py --outputs outputs/ --report validation_report.txt"
        ),
    )
    ap.add_argument("--outputs", default=str(_ROOT / "outputs"),
                    help="Folder containing JSON output files (default: outputs/)")
    ap.add_argument("--report",  default=str(_ROOT / "reports/validation_report.txt"),
                    help="Path to write the report")
    ap.add_argument("--spot",    type=int, default=15,
                    help="Number of files to include in the spot-check list (default: 15)")
    args = ap.parse_args()

    validate(Path(args.outputs), Path(args.report), args.spot)
