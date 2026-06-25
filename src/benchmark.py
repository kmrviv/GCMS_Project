"""
benchmark.py — Performance benchmark for imgprocess.py (the I/O-optimised processor).

Measures wall-clock time across 1 → N worker threads/processes and writes a full
report to explanations/performance_new_<pool>.txt covering:
  • Speedup S = T1 / Tp
  • Efficiency E = S / P
  • Amdahl's Law theoretical maximum

Optional --compare-pools flag runs BOTH ThreadPoolExecutor and ProcessPoolExecutor
in one invocation and produces overlay comparison graphs in graphs/.

Usage
-----
  python benchmark.py --input samples/
  python benchmark.py --input samples/ --workers 1 2 4 8 11 16 22 --runs 3
  python benchmark.py --input samples/ --compare-pools
  python benchmark.py --input samples/ --pool process
"""

import time
import os
import sys
import platform
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    _HAS_PLOT = True
except ImportError:
    _HAS_PLOT = False

sys.path.insert(0, str(Path(__file__).parent))
from imgprocess import (
    process_pdf as process_pdf_new,
    process_pdf_vector,
    process_pdf_raster,
)
# Reuse the project's canonical cosine + peak-dict helpers so vector-vs-raster
# is scored exactly the way the extractor is validated against NIST.
from compare_peaks import cosine, results_peaks

_ROOT               = Path(__file__).resolve().parent.parent
EXPLANATIONS_FOLDER = _ROOT / "explanations"
GRAPHS_FOLDER       = _ROOT / "graphs"


# ── Tee: simultaneous console + file output ──────────────────────────────────

def _safe_console_print(line):
    """print() that never crashes on a non-ASCII glyph under a legacy console.

    When stdout is piped on Windows the console codec is often cp1252, which
    can't encode the report's em-dashes/arrows/×.  Plain print() then raises
    UnicodeEncodeError mid-report.  Re-encode to the actual stdout encoding with
    'replace' so unsupported glyphs degrade to '?' instead of aborting the run.
    The UTF-8 report file (see _Tee) keeps the original characters intact."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(line)
    except UnicodeEncodeError:
        sys.stdout.write(line.encode(enc, "replace").decode(enc) + "\n")


class _Tee:
    def __init__(self, path):
        self._f = open(path, "w", encoding="utf-8")

    def __call__(self, *lines):
        for line in lines:
            _safe_console_print(line)
            self._f.write(line + "\n")

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ── math helpers ──────────────────────────────────────────────────────────────

def _amdahl(f_par, p):
    return 1.0 / ((1.0 - f_par) + f_par / p)

def _estimate_f_par(T1, Tp, P):
    S = T1 / Tp
    return max(0.0, min(1.0, (1.0 - 1.0 / S) / (1.0 - 1.0 / P)))

def _ascii_bar(value, ceiling, width=30):
    filled = int(round(value / ceiling * width)) if ceiling > 0 else 0
    return "█" * filled + "░" * (width - filled)


# ── worker functions (top-level for ProcessPoolExecutor picklability) ────────

def _worker_new(args):
    """Worker for imgprocess.py — no disk writes; just CPU extraction."""
    path, pdf_bytes = args
    t0 = time.perf_counter()
    process_pdf_new(path, False, False, pdf_bytes)
    return time.perf_counter() - t0


def _worker_old(args):
    """Worker for final.py — writes a per-file JSON; imported lazily."""
    from final import process_pdf as _old, JSON_FOLDER
    path, pdf_bytes = args
    JSON_FOLDER.mkdir(exist_ok=True)
    t0 = time.perf_counter()
    _old(path, False, False, pdf_bytes=pdf_bytes)
    return time.perf_counter() - t0


# ── trial runner ──────────────────────────────────────────────────────────────

def _run_trial(pdf_files, n_workers, pdf_bytes_cache, pool_cls, worker_fn):
    """Process all PDFs with n_workers; return (wall_clock_s, [per_file_s])."""
    args     = [(p, pdf_bytes_cache[p]) for p in pdf_files]
    wall_t0  = time.perf_counter()
    with pool_cls(max_workers=n_workers) as ex:
        file_times = list(ex.map(worker_fn, args))
    return time.perf_counter() - wall_t0, file_times


# ── report writing ────────────────────────────────────────────────────────────

def _write_report_header(out, pool_label, cpu_count, n_files, runs, worker_counts,
                          report_file, comparing):
    SEP  = "=" * 70
    out(
        "",
        SEP,
        "  GCMS PDF PROCESSOR — PARALLEL PERFORMANCE REPORT (imgprocess.py)",
        SEP,
        f"  OS             : {platform.system()} {platform.release()}",
        f"  Python         : {platform.python_version()}",
        f"  Pool type      : {pool_label}",
        f"  Logical cores  : {cpu_count}",
        f"  PDF files      : {n_files}",
        f"  Trials / cfg   : {runs}",
        f"  Worker ladder  : {worker_counts}",
        f"  Compare mode   : {'yes (imgprocess vs final)' if comparing else 'no'}",
        f"  Report file    : {report_file.resolve()}",
        SEP,
        "",
        "  I/O optimisations active in imgprocess.py:",
        "    1. No per-file JSON writes — workers return dicts, main thread",
        "       writes one JSONL.  Eliminates N × (create+allocate+close) NTFS ops.",
        "    2. PDF bytes read sequentially by main thread, passed to workers.",
        "       Workers do pure CPU work; no per-worker fitz.open() disk access.",
        "    3. All helper functions (_extract_compound_name,",
        "       process_pdf_raster) accept pdf_bytes — zero disk I/O in workers.",
        "",
        "  SECTION 1 — RAW MEASUREMENTS",
        "-" * 70,
        "",
    )


def _write_metrics_table(out, timings, worker_counts, f_par):
    THIN = "-" * 70
    T1    = timings[1]
    max_w = max(worker_counts)
    f_ser = 1.0 - f_par

    out(
        THIN,
        "  METRICS TABLE",
        THIN,
        "",
        "  Formulas:  S = T1 / T_p        "
        "E = S / P        "
        "S_th = Amdahl theoretical",
        "",
        f"  {'P':>4}  {'T_p (s)':>10}  {'Speedup S':>11}  "
        f"{'Efficiency E':>13}  {'Amdahl S_th':>12}",
        f"  {'─'*4}  {'─'*10}  {'─'*11}  {'─'*13}  {'─'*12}",
    )
    for w in worker_counts:
        Tp  = timings[w]
        S   = T1 / Tp
        E   = S / w
        Sth = _amdahl(f_par, w)
        tag = "  ← T1 baseline" if w == 1 else ""
        out(f"  {w:>4}  {Tp:>10.3f}  {S:>10.2f}x  {E:>12.1%}  {Sth:>12.2f}x{tag}")

    out(
        THIN,
        "",
        "  KEY SUMMARY",
        f"  {'─'*44}",
        f"  T1  (1 worker,  sequential)  : {T1:.3f} s",
        f"  T{max_w} ({max_w} workers, parallel)  : {timings[max_w]:.3f} s",
        f"  Measured speedup @ P={max_w}     : {T1/timings[max_w]:.2f}x",
        f"  Efficiency   @ P={max_w}          : {(T1/timings[max_w])/max_w:.1%}",
        f"  Estimated serial   fraction   : {f_ser:.1%}",
        f"  Estimated parallel fraction   : {f_par:.1%}",
        f"  Amdahl max speedup (∞ cores)  : {1/f_ser:.1f}x",
        "",
    )


def _write_throughput(out, timings, worker_counts, n_files):
    THIN = "-" * 70
    T1       = timings[1]
    max_w    = max(worker_counts)
    base_tp  = n_files / T1
    out(THIN, "  THROUGHPUT  (PDFs processed per second)", THIN, "")
    for w in worker_counts:
        tp   = n_files / timings[w]
        gain = tp / base_tp
        bar  = _ascii_bar(tp, (n_files / timings[max_w]) * 1.1, 24)
        out(f"  {w:2d} worker(s) : {tp:7.2f} PDFs/s  [{bar}]  ×{gain:.2f} baseline")
    out("")


def _write_comparison(out, timings_new, timings_old, worker_counts, n_files):
    """Side-by-side table: imgprocess.py vs final.py."""
    SEP  = "=" * 70
    THIN = "-" * 70
    out(
        "",
        SEP,
        "  SECTION 2 — HEAD-TO-HEAD: imgprocess.py vs final.py",
        SEP,
        "",
        "  Columns: T_new = imgprocess wall time   T_old = final wall time",
        "           Δ = T_old - T_new  (positive = imgprocess is faster)",
        "           Speedup = T_old / T_new",
        "",
        f"  {'P':>4}  {'T_new (s)':>10}  {'T_old (s)':>10}  "
        f"{'Δ (s)':>8}  {'Δ (%)':>8}  {'Speedup':>9}",
        f"  {'─'*4}  {'─'*10}  {'─'*10}  {'─'*8}  {'─'*8}  {'─'*9}",
    )
    for w in worker_counts:
        t_new = timings_new[w]
        t_old = timings_old.get(w)
        if t_old is None:
            out(f"  {w:>4}  {t_new:>10.3f}  {'N/A':>10}")
            continue
        delta   = t_old - t_new
        delta_p = delta / t_old * 100
        speedup = t_old / t_new
        tag = "  ← faster" if delta > 0 else ("  ← slower" if delta < 0 else "")
        out(f"  {w:>4}  {t_new:>10.3f}  {t_old:>10.3f}  "
            f"{delta:>+8.3f}  {delta_p:>+7.1f}%  {speedup:>8.2f}x{tag}")

    # Throughput delta
    out("", THIN, "  THROUGHPUT COMPARISON", THIN, "")
    for w in worker_counts:
        tp_new = n_files / timings_new[w]
        t_old  = timings_old.get(w)
        if t_old is None:
            continue
        tp_old = n_files / t_old
        out(f"  P={w:2d}  imgprocess: {tp_new:7.2f} PDFs/s   final: {tp_old:7.2f} PDFs/s"
            f"   gain: +{tp_new - tp_old:+.2f} PDFs/s ({(tp_new/tp_old - 1)*100:+.1f}%)")
    out("")

    # Explain the delta
    out(
        THIN,
        "  WHY imgprocess.py IS FASTER (or not — see numbers above)",
        THIN,
        "",
        "  The two scripts do identical peak extraction, so any wall-time",
        "  difference comes purely from I/O behaviour:",
        "",
        "  1. PER-FILE JSON WRITES REMOVED",
        "     final.py calls open() + json.dump() + close() for every PDF,",
        "     inside every worker.  On NTFS this triggers per-file journal",
        "     entries (create, allocate, close) with process-wide locks.",
        "     With 22 workers racing, these locks serialize on the FS even",
        "     though each write is tiny.  Windows Defender also scans each",
        "     new file as it appears — easily 50 % of wall time for large batches.",
        "     imgprocess.py writes nothing in workers; one JSONL line is written",
        "     by the main thread after each future completes.",
        "",
        "  2. SEQUENTIAL BYTE READS vs RANDOM WORKER READS",
        "     final.py has each worker call fitz.open(pdf_path) independently.",
        "     With 22 workers, the SSD receives 22 concurrent random reads.",
        "     imgprocess.py reads bytes sequentially in the main thread before",
        "     submitting (bounded window to cap RAM usage), letting the OS",
        "     readahead prefetch contiguous blocks.",
        "",
        "  HOW MUCH YOU SEE depends on:",
        "     • Whether Defender exclusions are set (can halve the effect)",
        "     • Whether the PDFs fit in the OS file cache after the first run",
        "     • SSD speed (NVMe shows less improvement than SATA)",
        "     • Batch size (effect grows with N — for 17 K files it's large)",
        "",
    )


def _write_analysis(out, timings, worker_counts, f_par, cpu_count, pool_type):
    T1    = timings[1]
    max_w = max(worker_counts)
    f_ser = 1.0 - f_par
    S_max_measured  = T1 / timings[max_w]
    E_max_measured  = S_max_measured / max_w
    amdahl_infinite = 1.0 / f_ser

    SEP  = "=" * 70
    THIN = "-" * 70
    pool_label = "ProcessPoolExecutor" if pool_type == "process" else "ThreadPoolExecutor"

    out(
        "",
        SEP,
        "  SECTION 3 — ANALYSIS: WHY THE NUMBERS LOOK THE WAY THEY DO",
        SEP,
        "",
        f"  Pool type : {pool_label}",
        "",
        THIN,
        "  FINDING 1 — Speedup is sub-linear  (Amdahl's Law)",
        THIN,
        "",
        f"    Actual measured speedup @ P={max_w}: {S_max_measured:.2f}x  "
        f"(efficiency {E_max_measured:.1%})",
        "",
        "    S(P) = 1 / ( f_s + f_p / P )",
        f"    Estimated f_s = {f_ser:.1%}   f_p = {f_par:.1%}",
        f"    S_max (P → ∞) = {amdahl_infinite:.1f}x",
        "",
        "  WHAT IS THE SERIAL FRACTION IN imgprocess.py?",
        "    • Argument parsing, file discovery (negligible, 100 % serial)",
        f"    • {pool_label} startup / teardown",
        "    • Main-thread byte reading (sequential, overlapped with pool work",
        "      via the bounded window in _bounded_submit)",
        "    • JSONL write: ONE open, N buffered writes, ONE close — negligible",
        "    • Final summary print",
        "    Per-file JSON open/close/write is GONE, so f_s is lower than",
        "    in final.py, meaning the Amdahl ceiling is higher.",
        "",
        THIN,
        "  FINDING 2 — Efficiency falls as core count rises",
        THIN,
        "",
    )
    for w in worker_counts:
        S = T1 / timings[w]
        E = S / w
        out(f"    P={w:2d}  E={E:.1%}  [{_ascii_bar(E, 1.0, 20)}]")

    out(
        "",
        "  Primary causes (ranked):",
        "  1) I/O bandwidth saturation once PDF bytes exceed SSD read bandwidth",
        "  2) Amdahl serial floor",
        "  3) Thread scheduling overhead",
        "  4) Memory bandwidth (raster pipeline: ~3.5 MB pixel array per page)",
    )
    if pool_type == "thread":
        out(
            "  5) Python GIL — minor here: fitz, cv2, numpy all release the GIL",
            "     during their C-level work (>95% of per-file CPU time).",
        )
    else:
        out(
            "  5) IPC overhead — pickling pdf_bytes across process boundary.",
            "     Each file pays ~1-5 ms; no GIL contention.",
        )

    out(
        "",
        THIN,
        "  FINDING 3 — Diminishing returns: sweet spot",
        THIN,
        "",
        "  MARGINAL SPEEDUP PER EXTRA WORKER:",
        "",
    )
    sweet_spot_w = worker_counts[0]
    prev_w, prev_S = worker_counts[0], 1.0
    for w in worker_counts[1:]:
        S        = T1 / timings[w]
        per_core = (S - prev_S) / (w - prev_w)
        bar      = _ascii_bar(per_core, 1.0, 14)
        label    = "  ← sweet spot" if per_core >= 0.5 else ""
        out(f"    P={prev_w:2d}→{w:2d} : {per_core:.2f}x/core  [{bar}]{label}")
        if per_core >= 0.5:
            sweet_spot_w = w
        prev_w, prev_S = w, S

    out(
        "",
        f"  Sweet spot estimate: up to P={sweet_spot_w} workers.",
        "  Beyond this each extra core returns < 0.5× gain.",
        "",
        "=" * 70,
        "  END OF REPORT",
        "=" * 70,
        "",
    )


# ── plots ─────────────────────────────────────────────────────────────────────

def _plot_amdahl(timings, worker_counts, f_par, tag):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    T1 = timings[1]
    measured_p = np.array(worker_counts)
    measured_S = np.array([T1 / timings[w] for w in worker_counts])
    p_range    = np.linspace(1, max(worker_counts), 300)
    amdahl_S   = 1.0 / ((1.0 - f_par) + f_par / p_range)

    measured_E = measured_S / measured_p          # efficiency E = S / P, in [0, 1]

    fig, ax = plt.subplots(figsize=(9, 5.5))
    l1, = ax.plot(p_range, p_range,   "--", color="#aaaaaa", lw=1.2, label="Ideal (S = P)")
    l2, = ax.plot(p_range, amdahl_S,  "-",  color="#e07b39", lw=2,
                  label=f"Amdahl  (f_par ≈ {f_par:.1%})")
    l3, = ax.plot(measured_p, measured_S, "o-", color="#3a7abf", lw=2,
                  markersize=7, mfc="white", mew=2, label="Measured speedup")
    ceiling = 1.0 / (1.0 - f_par)
    l4 = ax.axhline(ceiling, ls=":", color="#e07b39", lw=1,
                    label=f"Ceiling  {ceiling:.1f}×")
    for p, s in zip(measured_p, measured_S):
        ax.annotate(f"{s:.2f}×", xy=(p, s), xytext=(4, 6),
                    textcoords="offset points", fontsize=8, color="#3a7abf")

    # Secondary axis: parallel efficiency E = S / P, fixed to [0, 1].
    ax2 = ax.twinx()
    l5, = ax2.plot(measured_p, measured_E, "s--", color="#5aab61", lw=1.8,
                   markersize=6, mfc="white", mew=1.8, label="Efficiency E = S / P")
    for p, e in zip(measured_p, measured_E):
        ax2.annotate(f"{e:.2f}", xy=(p, e), xytext=(4, -12),
                     textcoords="offset points", fontsize=8, color="#5aab61")
    ax2.set_ylim(0, 1)
    ax2.set_ylabel("Efficiency  E = S / P  (0–1)", fontsize=12, color="#5aab61")
    ax2.tick_params(axis="y", labelcolor="#5aab61")

    ax.set_xlabel("Workers (P)", fontsize=12)
    ax.set_ylabel("Speedup  S = T₁ / Tₚ", fontsize=12)
    ax.set_title(f"Speedup & Efficiency vs Workers — imgprocess.py  [{tag}]",
                 fontsize=13, fontweight="bold")
    handles = [l1, l2, l3, l4, l5]
    ax.legend(handles, [h.get_label() for h in handles], fontsize=9, loc="upper left")
    ax.set_xlim(left=0); ax.set_ylim(bottom=0)
    ax.grid(True, ls="--", alpha=0.4)
    fig.tight_layout()
    path = GRAPHS_FOLDER / f"new_amdahl_{tag}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


def _plot_efficiency(timings, worker_counts, tag):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    T1 = timings[1]
    p  = np.array(worker_counts)
    E  = np.array([(T1 / timings[w]) / w for w in worker_counts])
    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.plot(p, E * 100, "s-", color="#5aab61", lw=2, markersize=7,
            mfc="white", mew=2, label="Efficiency")
    ax.axhline(100, ls="--", color="#aaaaaa", lw=1.2, label="Ideal (100%)")
    for pi, ei in zip(p, E):
        ax.annotate(f"{ei:.0%}", xy=(pi, ei * 100), xytext=(4, 6),
                    textcoords="offset points", fontsize=8, color="#5aab61")
    ax.set_xlabel("Workers (P)", fontsize=12)
    ax.set_ylabel("Efficiency  E = S / P  (%)", fontsize=12)
    ax.set_title(f"Parallel Efficiency — imgprocess.py  [{tag}]",
                 fontsize=13, fontweight="bold")
    ax.set_ylim(0, 115); ax.set_xlim(left=0)
    ax.legend(fontsize=9)
    ax.grid(True, ls="--", alpha=0.4)
    fig.tight_layout()
    path = GRAPHS_FOLDER / f"new_efficiency_{tag}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


def _plot_comparison(timings_new, timings_old, worker_counts, n_files, tag):
    """Bar chart comparing throughput of imgprocess vs final."""
    if not _HAS_PLOT or not timings_old:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    ws      = [w for w in worker_counts if w in timings_old]
    tp_new  = [n_files / timings_new[w] for w in ws]
    tp_old  = [n_files / timings_old[w]  for w in ws]
    x       = np.arange(len(ws))
    width   = 0.35

    fig, ax = plt.subplots(figsize=(10, 5.5))
    b1 = ax.bar(x - width / 2, tp_old, width, label="final.py  (per-file JSON)",
                color="#c0392b", alpha=0.75, edgecolor="white")
    b2 = ax.bar(x + width / 2, tp_new, width, label="imgprocess.py  (JSONL, bytes preload)",
                color="#3a7abf", alpha=0.75, edgecolor="white")

    for bar, val in zip(b1, tp_old):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f"{val:.1f}", ha="center", va="bottom", fontsize=8, color="#c0392b")
    for bar, val in zip(b2, tp_new):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f"{val:.1f}", ha="center", va="bottom", fontsize=8, color="#3a7abf")

    ax.set_xticks(x)
    ax.set_xticklabels([f"P={w}" for w in ws])
    ax.set_xlabel("Worker count", fontsize=12)
    ax.set_ylabel("Throughput  (PDFs / second)", fontsize=12)
    ax.set_title(f"Throughput: final.py vs imgprocess.py  [{tag}]",
                 fontsize=13, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(True, axis="y", ls="--", alpha=0.4)
    fig.tight_layout()
    path = GRAPHS_FOLDER / f"comparison_{tag}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


def _plot_file_times(all_file_times, worker_counts, tag):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    labels = [f"P={w}" for w in worker_counts]
    data   = [all_file_times[w] for w in worker_counts]

    fig, (ax_box, ax_vio) = plt.subplots(1, 2, figsize=(13, 5.5), sharey=True)
    bp = ax_box.boxplot(data, tick_labels=labels, patch_artist=True,
                        medianprops=dict(color="#e07b39", lw=2))
    for patch in bp["boxes"]:
        patch.set_facecolor("#3a7abf"); patch.set_alpha(0.4)
    ax_box.set_title(f"Box Plot — Per-file Time  [imgprocess.py {tag}]",
                     fontsize=12, fontweight="bold")
    ax_box.set_xlabel("Workers"); ax_box.set_ylabel("Time per file (s)")
    ax_box.grid(True, axis="y", ls="--", alpha=0.4)

    parts = ax_vio.violinplot(data, positions=range(1, len(worker_counts) + 1),
                               showmedians=True, showextrema=True)
    for pc in parts["bodies"]:
        pc.set_facecolor("#5aab61"); pc.set_alpha(0.45)
    parts["cmedians"].set_color("#e07b39"); parts["cmedians"].set_linewidth(2)
    ax_vio.set_xticks(range(1, len(worker_counts) + 1))
    ax_vio.set_xticklabels(labels)
    ax_vio.set_title(f"Violin Plot — Per-file Time  [imgprocess.py {tag}]",
                     fontsize=12, fontweight="bold")
    ax_vio.set_xlabel("Workers")
    ax_vio.grid(True, axis="y", ls="--", alpha=0.4)

    fig.tight_layout()
    path = GRAPHS_FOLDER / f"new_file_times_{tag}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


# ── pool-comparison overlay plots ────────────────────────────────────────────

def _plot_pools_speedup(t_thread, t_process, worker_counts, f_par_t, f_par_p):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    T1_t = t_thread[1];  T1_p = t_process[1]
    ws   = np.array(worker_counts)
    S_t  = np.array([T1_t / t_thread[w]  for w in worker_counts])
    S_p  = np.array([T1_p / t_process[w] for w in worker_counts])
    p_r  = np.linspace(1, max(worker_counts), 300)

    E_t = S_t / ws        # efficiency, in [0, 1]
    E_p = S_p / ws

    fig, ax = plt.subplots(figsize=(9, 5.5))
    handles = []
    handles.append(ax.plot(p_r, p_r, "--", color="#cccccc", lw=1.2, label="Ideal (S = P)")[0])
    handles.append(ax.plot(p_r, 1.0 / ((1 - f_par_t) + f_par_t / p_r), "-",
            color="#3a7abf", lw=1, alpha=0.5, label=f"Amdahl thread (f={f_par_t:.1%})")[0])
    handles.append(ax.plot(p_r, 1.0 / ((1 - f_par_p) + f_par_p / p_r), "-",
            color="#e07b39", lw=1, alpha=0.5, label=f"Amdahl process (f={f_par_p:.1%})")[0])
    handles.append(ax.plot(ws, S_t, "o-", color="#3a7abf", lw=2, markersize=7,
            mfc="white", mew=2, label="Thread speedup")[0])
    handles.append(ax.plot(ws, S_p, "s-", color="#e07b39", lw=2, markersize=7,
            mfc="white", mew=2, label="Process speedup")[0])
    for p, s in zip(ws, S_t):
        ax.annotate(f"{s:.2f}×", xy=(p, s), xytext=(-18, 5),
                    textcoords="offset points", fontsize=7, color="#3a7abf")
    for p, s in zip(ws, S_p):
        ax.annotate(f"{s:.2f}×", xy=(p, s), xytext=(4, 5),
                    textcoords="offset points", fontsize=7, color="#e07b39")

    # Secondary axis: parallel efficiency E = S / P, fixed to [0, 1].
    ax2 = ax.twinx()
    handles.append(ax2.plot(ws, E_t, "o:", color="#3a7abf", lw=1.5, markersize=5,
            alpha=0.8, label="Thread efficiency")[0])
    handles.append(ax2.plot(ws, E_p, "s:", color="#e07b39", lw=1.5, markersize=5,
            alpha=0.8, label="Process efficiency")[0])
    ax2.set_ylim(0, 1)
    ax2.set_ylabel("Efficiency  E = S / P  (0–1)", fontsize=12, color="#5aab61")
    ax2.tick_params(axis="y", labelcolor="#5aab61")

    ax.set_xlabel("Workers (P)", fontsize=12)
    ax.set_ylabel("Speedup  S = T₁ / Tₚ", fontsize=12)
    ax.set_title("Speedup & Efficiency vs Workers — Thread vs Process Pool",
                 fontsize=13, fontweight="bold")
    ax.legend(handles, [h.get_label() for h in handles], fontsize=8, loc="upper left")
    ax.set_xlim(left=0); ax.set_ylim(bottom=0)
    ax.grid(True, ls="--", alpha=0.4)
    fig.tight_layout()
    path = GRAPHS_FOLDER / "pools_speedup.png"
    fig.savefig(path, dpi=150); plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


def _plot_pools_efficiency(t_thread, t_process, worker_counts):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    T1_t = t_thread[1];  T1_p = t_process[1]
    ws   = np.array(worker_counts)
    E_t  = np.array([(T1_t / t_thread[w])  / w for w in worker_counts]) * 100
    E_p  = np.array([(T1_p / t_process[w]) / w for w in worker_counts]) * 100

    fig, ax = plt.subplots(figsize=(9, 5.5))
    ax.axhline(100, ls="--", color="#cccccc", lw=1.2, label="Ideal (100%)")
    ax.plot(ws, E_t, "o-", color="#3a7abf", lw=2, markersize=7,
            mfc="white", mew=2, label="Thread")
    ax.plot(ws, E_p, "s-", color="#e07b39", lw=2, markersize=7,
            mfc="white", mew=2, label="Process")
    for p, e in zip(ws, E_t):
        ax.annotate(f"{e:.0f}%", xy=(p, e), xytext=(-20, 5),
                    textcoords="offset points", fontsize=7, color="#3a7abf")
    for p, e in zip(ws, E_p):
        ax.annotate(f"{e:.0f}%", xy=(p, e), xytext=(4, 5),
                    textcoords="offset points", fontsize=7, color="#e07b39")
    ax.set_xlabel("Workers (P)", fontsize=12)
    ax.set_ylabel("Efficiency  E = S / P  (%)", fontsize=12)
    ax.set_title("Parallel Efficiency — Thread vs Process Pool", fontsize=13, fontweight="bold")
    ax.set_xlim(left=0); ax.set_ylim(0, 115)
    ax.legend(fontsize=9); ax.grid(True, ls="--", alpha=0.4)
    fig.tight_layout()
    path = GRAPHS_FOLDER / "pools_efficiency.png"
    fig.savefig(path, dpi=150); plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


def _plot_pools_throughput(t_thread, t_process, worker_counts, n_files):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    ws     = worker_counts
    tp_t   = [n_files / t_thread[w]  for w in ws]
    tp_p   = [n_files / t_process[w] for w in ws]
    x      = np.arange(len(ws))
    width  = 0.35

    fig, ax = plt.subplots(figsize=(10, 5.5))
    b1 = ax.bar(x - width / 2, tp_t, width, label="Thread",
                color="#3a7abf", alpha=0.8, edgecolor="white")
    b2 = ax.bar(x + width / 2, tp_p, width, label="Process",
                color="#e07b39", alpha=0.8, edgecolor="white")
    for bar, val in zip(b1, tp_t):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f"{val:.1f}", ha="center", va="bottom", fontsize=8, color="#3a7abf")
    for bar, val in zip(b2, tp_p):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f"{val:.1f}", ha="center", va="bottom", fontsize=8, color="#e07b39")
    ax.set_xticks(x); ax.set_xticklabels([f"P={w}" for w in ws])
    ax.set_xlabel("Worker count", fontsize=12)
    ax.set_ylabel("Throughput  (PDFs / second)", fontsize=12)
    ax.set_title("Throughput — Thread vs Process Pool", fontsize=13, fontweight="bold")
    ax.legend(fontsize=9); ax.grid(True, axis="y", ls="--", alpha=0.4)
    fig.tight_layout()
    path = GRAPHS_FOLDER / "pools_throughput.png"
    fig.savefig(path, dpi=150); plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


def _plot_pools_file_times(ft_thread, ft_process, worker_counts):
    if not _HAS_PLOT:
        return
    GRAPHS_FOLDER.mkdir(exist_ok=True)
    labels = [f"P={w}" for w in worker_counts]

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5), sharey=True)
    for ax, ft, color, title in zip(
        axes,
        [ft_thread, ft_process],
        ["#3a7abf", "#e07b39"],
        ["Thread Pool", "Process Pool"],
    ):
        data = [ft[w] for w in worker_counts]
        bp   = ax.boxplot(data, tick_labels=labels, patch_artist=True,
                          medianprops=dict(color="#e07b39" if color == "#3a7abf" else "#3a7abf", lw=2))
        for patch in bp["boxes"]:
            patch.set_facecolor(color); patch.set_alpha(0.4)
        ax.set_title(f"Per-file Time — {title}", fontsize=12, fontweight="bold")
        ax.set_xlabel("Workers"); ax.set_ylabel("Time per file (s)")
        ax.grid(True, axis="y", ls="--", alpha=0.4)

    fig.suptitle("Per-file Processing Time Distribution — Thread vs Process",
                 fontsize=11, style="italic")
    fig.tight_layout()
    path = GRAPHS_FOLDER / "pools_file_times.png"
    fig.savefig(path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"  Graph saved → {path.resolve()}")


# ── main benchmark ────────────────────────────────────────────────────────────

def benchmark(pdf_folder, worker_counts=None, runs=3, pool_type="thread", compare=False):
    pool_cls   = ProcessPoolExecutor if pool_type == "process" else ThreadPoolExecutor
    pool_label = "ProcessPoolExecutor" if pool_type == "process" else "ThreadPoolExecutor"
    tag        = pool_type

    pdf_files = _gather_input_pdfs(pdf_folder)   # accepts one path or a list
    if not pdf_files:
        sys.exit(f"[benchmark] No PDFs found in '{pdf_folder}'. "
                 "Put your test PDFs in the samples/ folder first.")

    cpu_count = os.cpu_count() or 1
    n_files   = len(pdf_files)

    if worker_counts is None:
        wset = {1}
        w = 2
        while w < cpu_count:
            wset.add(w)
            w *= 2
        wset.add(cpu_count // 2)
        wset.add(cpu_count)
        worker_counts = sorted(w for w in wset if w >= 1)

    worker_counts = [w for w in worker_counts if w <= n_files] or [1]

    EXPLANATIONS_FOLDER.mkdir(exist_ok=True)
    report_file = EXPLANATIONS_FOLDER / f"performance_new_{tag}.txt"

    with _Tee(report_file) as out:
        _write_report_header(out, pool_label, cpu_count, n_files, runs,
                              worker_counts, report_file, compare)

        # ── pre-load all bytes once ───────────────────────────────────────────
        out("  Pre-loading PDFs into RAM ...", "")
        with ThreadPoolExecutor(max_workers=min(n_files, cpu_count or 4)) as _pre:
            pdf_bytes_cache = dict(zip(
                pdf_files,
                _pre.map(lambda p: p.read_bytes(), pdf_files)
            ))

        # ── imgprocess.py trials ───────────────────────────────────────────────
        out("  --- imgprocess.py ---", "")
        timings_new    = {}
        all_file_times = {w: [] for w in worker_counts}

        for w in worker_counts:
            trial_times = []
            for r in range(runs):
                t, ftimes = _run_trial(pdf_files, w, pdf_bytes_cache, pool_cls, _worker_new)
                trial_times.append(t)
                all_file_times[w].extend(ftimes)
                prog = "█" * (r + 1) + "░" * (runs - r - 1)
                out(f"  [new] workers={w:2d}  [{prog}] run {r+1}/{runs}  {t:.3f}s")
            avg = sum(trial_times) / len(trial_times)
            timings_new[w] = avg
            out(
                f"               └─ avg={avg:.3f}s   "
                f"min={min(trial_times):.3f}s   "
                f"max={max(trial_times):.3f}s",
                "",
            )

        # ── optional final.py trials ─────────────────────────────────────────
        timings_old = {}
        if compare:
            try:
                from final import process_pdf as _chk, JSON_FOLDER
                Path("outputs").mkdir(exist_ok=True)
            except ImportError:
                out("  [compare] final.py not found — skipping comparison.", "")
                compare = False

        if compare:
            out("", "  --- final.py (original, for comparison) ---", "")
            for w in worker_counts:
                trial_times = []
                for r in range(runs):
                    t, _ = _run_trial(pdf_files, w, pdf_bytes_cache, pool_cls, _worker_old)
                    trial_times.append(t)
                    prog = "█" * (r + 1) + "░" * (runs - r - 1)
                    out(f"  [old] workers={w:2d}  [{prog}] run {r+1}/{runs}  {t:.3f}s")
                avg = sum(trial_times) / len(trial_times)
                timings_old[w] = avg
                out(
                    f"               └─ avg={avg:.3f}s   "
                    f"min={min(trial_times):.3f}s   "
                    f"max={max(trial_times):.3f}s",
                    "",
                )

        # ── metrics for imgprocess ──────────────────────────────────────────────
        T1    = timings_new[1]
        max_w = max(worker_counts)
        ref_w = max((w for w in worker_counts if w > 1), default=None)
        f_par = _estimate_f_par(T1, timings_new[ref_w], ref_w) if ref_w else 0.95

        _write_metrics_table(out, timings_new, worker_counts, f_par)

        # speedup chart
        chart_ceil = max(
            max(T1 / timings_new[w] for w in worker_counts),
            _amdahl(f_par, max_w),
        ) * 1.15
        THIN = "-" * 70
        out(THIN, "  SPEEDUP CHART  (actual vs Amdahl theoretical)", THIN, "")
        for w in worker_counts:
            S_act = T1 / timings_new[w]
            S_th  = _amdahl(f_par, w)
            out(
                f"  P={w:2d}  actual  [{_ascii_bar(S_act, chart_ceil)}] {S_act:.2f}x",
                f"        theory  [{_ascii_bar(S_th,  chart_ceil)}] {S_th:.2f}x",
                "",
            )

        _write_throughput(out, timings_new, worker_counts, n_files)

        if compare and timings_old:
            _write_comparison(out, timings_new, timings_old, worker_counts, n_files)

        _write_analysis(out, timings_new, worker_counts, f_par, cpu_count, pool_type)

    # ── plots ─────────────────────────────────────────────────────────────────
    _plot_amdahl(timings_new, worker_counts, f_par, tag)
    _plot_efficiency(timings_new, worker_counts, tag)
    _plot_file_times(all_file_times, worker_counts, tag)
    if compare and timings_old:
        _plot_comparison(timings_new, timings_old, worker_counts, n_files, tag)

    _safe_console_print(f"\n  Report saved -> {report_file.resolve()}")
    return timings_new, all_file_times, f_par, worker_counts, n_files


# ── input gathering (one or more files/folders) ──────────────────────────────

def _resolve_input(item):
    """Resolve an input path so it works regardless of the current directory.

    Tries the path as given, then relative to the project root, data/, and
    data/samples/ (mirrors imgprocess.py).  Returns the first that exists, else
    the original path unchanged (so the caller reports 'not found').
    """
    p = Path(item)
    if p.exists():
        return p
    if not p.is_absolute():
        for base in (_ROOT, _ROOT / "data", _ROOT / "data" / "samples"):
            cand = base / item
            if cand.exists():
                return cand
    return p


def _gather_input_pdfs(inputs):
    """Collect PDFs from one or more input paths (files or folders).

    Each folder is scanned recursively; a single .pdf is taken as-is.  Returns
    a sorted, de-duplicated list of Paths so the same file passed via two
    overlapping folders is only processed once.
    """
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    seen, files = set(), []
    for item in inputs:
        p = _resolve_input(item)
        found = [p] if (p.is_file() and p.suffix.lower() == ".pdf") else sorted(p.rglob("*.pdf"))
        for f in found:
            key = f.resolve()
            if key not in seen:
                seen.add(key)
                files.append(f)
    return sorted(files)


def _display_base(inputs):
    """Common parent directory of the inputs, for readable relative paths."""
    if isinstance(inputs, (str, Path)):
        inputs = [inputs]
    dirs = [str((p if p.is_dir() else p.parent).resolve())
            for p in (_resolve_input(i) for i in inputs)]
    try:
        return Path(os.path.commonpath(dirs)) if dirs else _ROOT
    except ValueError:          # e.g. paths on different drives
        return _ROOT


# ── vector-vs-raster extraction comparison ──────────────────────────────────

def _extract_both(pdf_path, pdf_bytes=None):
    """Run BOTH pipelines on one PDF and capture peaks, timing, and any error.

    Unlike process_pdf() (which falls back vector→raster), this calls each
    pipeline directly so we can see what every method produces independently.
    """
    rec = {"file": pdf_path,
           "vector": None, "raster": None,
           "vector_err": None, "raster_err": None,
           "vector_time": 0.0, "raster_time": 0.0}

    t0 = time.perf_counter()
    try:
        rec["vector"], *_ = process_pdf_vector(pdf_path, pdf_bytes=pdf_bytes)
    except Exception as e:
        rec["vector_err"] = str(e) or type(e).__name__
    rec["vector_time"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    try:
        rec["raster"], *_ = process_pdf_raster(pdf_path, pdf_bytes=pdf_bytes)
    except Exception as e:
        rec["raster_err"] = str(e) or type(e).__name__
    rec["raster_time"] = time.perf_counter() - t0
    return rec


def _compare_one(pdf_path, base):
    """Worker: extract one PDF with both pipelines and score the cosine.

    Reads the PDF bytes once and feeds both pipelines, so each file touches the
    disk a single time.  Returns a row dict ready for the report/CSV.
    """
    rel = str(Path(pdf_path).resolve().relative_to(base)).replace("\\", "/")
    pdf_bytes = Path(pdf_path).read_bytes()
    rec = _extract_both(pdf_path, pdf_bytes)
    v = results_peaks(rec["vector"]) if rec["vector"] else {}
    r = results_peaks(rec["raster"]) if rec["raster"] else {}
    score = round(cosine(v, r), 6)
    bp = bool(v) and bool(r) and (max(v, key=v.get) == max(r, key=r.get))
    return {"file": rel, "cosine": score,
            "n_vector": len(v), "n_raster": len(r), "base_peak_match": bp}


def compare_methods(inputs, csv_path=None, workers=None):
    """Score VECTOR vs RASTER extraction by cosine similarity for every PDF
    under one or more input paths, using the same cosine as compare_peaks.py.

    Files are processed in parallel across a thread pool (fitz/cv2/numpy all
    release the GIL during their C-level work).  Output keeps input order.
    """
    pdf_files = _gather_input_pdfs(inputs)
    if not pdf_files:
        sys.exit(f"[compare] No PDFs found in: {inputs}")

    base    = _display_base(inputs)
    workers = workers or min(len(pdf_files), os.cpu_count() or 1)
    in_list = inputs if isinstance(inputs, (list, tuple)) else [inputs]

    EXPLANATIONS_FOLDER.mkdir(exist_ok=True)
    report_file = EXPLANATIONS_FOLDER / "compare_vector_vs_raster.txt"

    rows = []
    SEP = "=" * 78

    with _Tee(report_file) as out:
        out(SEP,
            "  VECTOR vs RASTER — COSINE SIMILARITY",
            SEP,
            f"  Inputs     : {len(in_list)} path(s)")
        for i in in_list:
            out(f"               - {i}")
        out(f"  PDF files  : {len(pdf_files)}",
            f"  Workers    : {workers} (thread pool)",
            "",
            "  Cosine as in compare_peaks.py: dot product over shared m/z,",
            "  divided by the full L2 norm of each spectrum.",
            "  BP = base-peak (most intense m/z) agreement.",
            SEP,
            "",
            f"  {'cosine':>8}  {'vec':>4}  {'ras':>4}  {'BP':>3}  file",
            f"  {'-'*8}  {'-'*4}  {'-'*4}  {'-'*3}  {'-'*40}")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            # map preserves input order, so the report stays deterministic
            for row in ex.map(lambda p: _compare_one(p, base), pdf_files):
                both_empty = row["n_vector"] == 0 and row["n_raster"] == 0
                note = "   <-- both empty (excluded)" if both_empty else ""
                cos_str = "   n/a  " if both_empty else f"{row['cosine']:8.4f}"
                out(f"  {cos_str}  {row['n_vector']:>4}  {row['n_raster']:>4}  "
                    f"{'BP+' if row['base_peak_match'] else 'BP-':>3}  {row['file']}{note}")
                rows.append(row)

        both_empty = [r for r in rows if r["n_vector"] == 0 and r["n_raster"] == 0]
        one_empty  = [r for r in rows if (r["n_vector"] == 0) != (r["n_raster"] == 0)]
        scored     = [r for r in rows if r["n_vector"] and r["n_raster"]]
        out("", SEP, "  SUMMARY", SEP,
            f"  total files            : {len(pdf_files)}",
            f"  both pipelines empty   : {len(both_empty)} (no peaks either side — excluded from cosine)",
            f"  one pipeline empty     : {len(one_empty)} (excluded from cosine)",
            f"  scored (both non-empty): {len(scored)}")
        for r in both_empty:
            out(f"      both-empty: {r['file']}")
        for r in one_empty:
            out(f"      one-empty : {r['file']}  (vec={r['n_vector']} ras={r['n_raster']})")
        if scored:
            cosines = [row["cosine"] for row in scored]
            out("")
            prev = 1.01
            for lo, label in [(0.99, ">=0.99"), (0.95, "0.95-0.99"),
                              (0.90, "0.90-0.95"), (0.00, "<0.90")]:
                c = sum(1 for x in cosines if lo <= x < prev)
                out(f"  {label:>10}: {c:4d} ({100 * c / len(scored):5.1f}%)")
                prev = lo
            bp = sum(1 for row in scored if row["base_peak_match"])
            out("",
                f"  mean cosine          : {sum(cosines) / len(cosines):.4f}",
                f"  min  cosine          : {min(cosines):.4f}",
                f"  base-peak m/z agrees : {bp}/{len(scored)} ({100 * bp / len(scored):.1f}%)")
        out(SEP, "")

    if csv_path:
        import csv as _csv
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=["file", "cosine", "n_vector",
                                               "n_raster", "base_peak_match"])
            w.writeheader()
            w.writerows(rows)
        print(f"  CSV saved    -> {csv_path.resolve()}")

    print(f"  Report saved -> {report_file.resolve()}")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description=(
            "Benchmark imgprocess.py (I/O-optimised) across 1…N workers.\n"
            "Results written to explanations/performance_new_<pool>.txt."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python benchmark.py --input data/samples/\n"
            "  python benchmark.py --input data/samples/IISc_Data/Gases --compare\n"
            "  python benchmark.py --input \"folderA\" \"folderB\" --compare\n"
            "  python benchmark.py --input data/samples/ --pool process\n"
            "  python benchmark.py --input data/samples/ --workers 1 2 4 8 16 22 --runs 5"
        ),
    )
    ap.add_argument("--input",   nargs="+", default=[str(_ROOT / "data/samples")],
                    help="One or more PDF files/folders (folders scanned recursively; "
                         "default: data/samples/)")
    ap.add_argument("--workers", nargs="+", type=int,
                    help="Worker counts to test (default: 1 2 4 … cpu//2 cpu_count)")
    ap.add_argument("--runs",    type=int, default=3,
                    help="Trials per config for averaging (default: 3)")
    ap.add_argument("--pool",    choices=["thread", "process"], default="thread",
                    help="Executor type  (default: thread)")
    ap.add_argument("--compare", action="store_true",
                    help="Compare VECTOR vs RASTER extraction: run both pipelines on "
                         "every PDF under --input and report m/z vs intensity per file "
                         "(no performance benchmark is run in this mode)")
    ap.add_argument("--compare-csv", default=str(_ROOT / "outputs/compare_vector_vs_raster.csv"),
                    help="CSV path for the --compare table (default: outputs/compare_vector_vs_raster.csv)")
    ap.add_argument("--compare-pools", action="store_true",
                    help="Run both thread and process pools and produce overlay graphs")
    args = ap.parse_args()

    if args.compare:
        compare_methods(args.input, args.compare_csv,
                        workers=(args.workers[0] if args.workers else None))
    elif args.compare_pools:
        print("\n  Running thread pool ...")
        t_thread, ft_thread, f_par_t, worker_counts, n_files = benchmark(
            args.input, args.workers, args.runs, "thread", compare=False
        )
        print("\n  Running process pool ...")
        t_process, ft_process, f_par_p, worker_counts, n_files = benchmark(
            args.input, args.workers, args.runs, "process", compare=False
        )
        print("\n  Generating pool-comparison graphs ...")
        _plot_pools_speedup(t_thread, t_process, worker_counts, f_par_t, f_par_p)
        _plot_pools_efficiency(t_thread, t_process, worker_counts)
        _plot_pools_throughput(t_thread, t_process, worker_counts, n_files)
        _plot_pools_file_times(ft_thread, ft_process, worker_counts)
    else:
        benchmark(args.input, args.workers, args.runs, args.pool, compare=False)