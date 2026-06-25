"""
Optimized mass-spectrum PDF processor.

Two pipelines:
  - process_pdf_vector(): Tier 2 fast path. Reads vector drawings directly,
    no rasterization. ~10-50x faster than the raster pipeline.
  - process_pdf_raster(): Tier 1 tuned version of the original. Lower DPI,
    no visualization output, fallback when vector extraction fails.

Public entrypoint: process_pdf() tries vector first, falls back to raster.

I/O optimisations over final.py:
  1. One JSONL file written by the main process instead of N per-file JSONs.
     Workers return dicts; the main thread serialises them.  This eliminates
     the per-file create/allocate/close NTFS churn and Windows Defender scans.
  2. PDF bytes are read sequentially by the main thread and passed to workers,
     so workers do pure CPU work and the disk does one sequential stream.
  3. All fitz.open() calls accept an optional pdf_bytes argument so no worker
     ever touches the disk directly.
"""
import re
import fitz
import cv2
import numpy as np
import json
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
import time
import os
import argparse
from pathlib import Path
from tqdm import tqdm

# --- Config -----------------------------------------------------------------
_ROOT          = Path(__file__).resolve().parent.parent
JSON_FOLDER    = _ROOT / "outputs"     # kept for --per-file-json compat mode
VISUALS_FOLDER = _ROOT / "visuals"
RASTER_DPI     = 600
WRITE_VISUALS  = True


# --- m/z label-anchored calibration correction ------------------------------
# The x-axis tick calibration is coarse: ticks are spaced every 2 m/z and each
# carries sub-pixel raster error, so a small slope/intercept drift accumulates
# across the axis and pushes some bars over the .5 boundary — they then round to
# the wrong integer m/z (observed: a near-uniform +0.45 rightward drift on some
# instrument exports turns m/z 4 into 5, 17 into 18, 29 into 30).
#
# Every real peak, however, has its integer m/z printed as a text label directly
# above its bar (dx ~ 0).  Those labels are the instrument's own ground-truth
# assignment.  We match labels to bars, fit a robust (Theil-Sen) line
# bar_x = a*mz + b through the matched pairs, and re-round every bar against that
# clean line.  Unlabelled bars inherit the corrected slope+intercept.  When a
# page yields too few usable labels we leave the tick calibration untouched and
# emit a warning instead.

def _theil_sen(x, y):
    """Dependency-free Theil-Sen estimator for y = a*x + b.

    a = median of all pairwise slopes; b = median(y - a*x).  Deterministic,
    threshold-free, tolerates up to ~29% arbitrary outliers — more than enough
    once the anchor filter has removed tick labels and annotations.  Returns
    (a, b) or (None, None) if a slope cannot be formed.
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = len(x)
    if n < 2:
        return None, None
    slopes = []
    for i in range(n):
        dx = x[i + 1:] - x[i]
        dy = y[i + 1:] - y[i]
        good = dx != 0
        if np.any(good):
            slopes.append(dy[good] / dx[good])
    if not slopes:
        return None, None
    slopes = np.concatenate(slopes)
    a = float(np.median(slopes))
    b = float(np.median(y - a * x))
    return a, b


def _collect_mz_label_anchors(page, bar_xs, bar_tops_y, xaxis_y, ppu,
                              scale_x=1.0, scale_y=1.0):
    """Match printed numeric peak-labels to bars; return Nx2 array of (bar_x, mz).

    bar_xs / bar_tops_y / xaxis_y / ppu are in the CALLER's coordinate system
    (PDF units for the vector pipeline, image pixels for the raster pipeline).
    Text words always come from get_text() in PDF units, so they are multiplied
    by scale_x / scale_y to land in the caller's system.

    A numeric word is kept only when it is:
      * strictly an integer in (1, 2000],
      * ABOVE the x-axis  (axis-tick labels sit on/below the axis -> rejected),
      * horizontally over a bar (within 0.4*pitch -> rejects the y-axis '100'
        and the scan-intensity annotation such as '2.09e9'),
      * vertically just above that bar's tip (a second guard against far labels).
    """
    bar_xs = np.asarray(bar_xs, dtype=np.float64)
    if bar_xs.size == 0:
        return np.empty((0, 2))
    anchors = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        t = text.strip()
        if not re.fullmatch(r"\d+", t):
            continue
        val = int(t)
        if not (1 <= val <= 2000):
            continue
        lx = ((x0 + x1) / 2) * scale_x
        ly = ((y0 + y1) / 2) * scale_y
        if ly >= xaxis_y - 3 * scale_y:           # on/below axis -> tick label
            continue
        di = int(np.argmin(np.abs(bar_xs - lx)))
        if abs(bar_xs[di] - lx) >= ppu * 0.4:     # not over a bar -> annotation
            continue
        if ly <= bar_tops_y[di] - ppu * 4:        # too high above the tip
            continue
        anchors.append((bar_xs[di], float(val)))
    return np.array(anchors, dtype=np.float64) if anchors else np.empty((0, 2))


def _correct_mz_with_labels(page, bars, ppu, x_intercept, xaxis_y,
                            scale_x=1.0, scale_y=1.0, min_anchors=3):
    """Re-assign integer m/z to each bar using printed peak labels as ground truth.

    `bars` is a sequence of (bar_x, bar_top_y) in the caller's coordinate system.
    Returns (mz_map, info):
      * mz_map  : {bar_x: corrected_int_mz}
      * info    : dict with keys anchored(bool), n_anchors, warning(str|None),
                  and when anchored: slope, intercept, anchors (Nx2), max_resid_mz.

    If fewer than `min_anchors` usable labels are found (or the fit degenerates),
    the original tick-calibrated assignment is returned unchanged with a warning.
    """
    bar_xs = np.array([b[0] for b in bars], dtype=np.float64)
    bar_tops = np.array([b[1] for b in bars], dtype=np.float64)
    info = {"anchored": False, "n_anchors": 0, "warning": None}

    def _tick_calibrated():
        return {bx: int(round((bx - x_intercept) / ppu)) for bx in bar_xs}, info

    if bar_xs.size == 0:
        return _tick_calibrated()

    anchors = _collect_mz_label_anchors(page, bar_xs, bar_tops, xaxis_y, ppu,
                                        scale_x, scale_y)
    info["n_anchors"] = len(anchors)
    if len(anchors) < min_anchors:
        info["warning"] = (f"m/z label-correction skipped: only {len(anchors)} "
                           f"peak-label anchor(s) found (need {min_anchors}); "
                           f"kept tick calibration")
        return _tick_calibrated()

    a, b = _theil_sen(anchors[:, 1], anchors[:, 0])   # bar_x = a*mz + b
    if a is None or a == 0:
        info["warning"] = ("m/z label-correction skipped: degenerate label fit; "
                           "kept tick calibration")
        return _tick_calibrated()

    mz_map = {bx: int(round((bx - b) / a)) for bx in bar_xs}
    resid = (anchors[:, 0] - b) / a - np.round((anchors[:, 0] - b) / a)
    info.update(anchored=True, slope=a, intercept=b, anchors=anchors,
                max_resid_mz=float(np.abs(resid).max()) if resid.size else 0.0)
    return mz_map, info


def _save_calibration_graph(path, info, ppu, x_intercept, stem, calib_dir):
    """Write a 2-panel PNG: (top) bar_x-vs-m/z anchors with the Theil-Sen line and
    the old tick-calibration line; (bottom) per-peak rounding residual for both,
    with the +-0.5 'wrong integer' boundaries marked.  Only meaningful when the
    correction actually anchored.  matplotlib is imported lazily so the import is
    paid only when --calib-graph is requested."""
    if not info.get("anchored"):
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    anchors = info["anchors"]
    bx, mz = anchors[:, 0], anchors[:, 1]
    a, b = info["slope"], info["intercept"]
    grid = np.linspace(mz.min() - 1, mz.max() + 1, 100)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 8))

    ax1.scatter(mz, bx, s=55, c="#1f77b4", zorder=3, label="peak-label anchors")
    ax1.plot(grid, a * grid + b, "-", c="#d62728", lw=2,
             label=f"Theil-Sen: x = {a:.3f}*m/z + {b:.2f}")
    ax1.plot(grid, ppu * grid + x_intercept, "--", c="gray", lw=1.3,
             label=f"old tick cal: ppu = {ppu:.3f}")
    ax1.set_xlabel("true m/z (from text label)")
    ax1.set_ylabel("bar x-position")
    ax1.set_title(f"{stem}: label-anchored calibration  "
                  f"(max resid {info['max_resid_mz']:.3f} m/z)")
    ax1.legend(fontsize=8); ax1.grid(alpha=0.3)

    res_new = (bx - b) / a - mz
    res_old = (bx - x_intercept) / ppu - mz
    ax2.axhline(0, c="k", lw=0.8)
    ax2.axhline(0.5, c="r", ls=":", lw=0.8)
    ax2.axhline(-0.5, c="r", ls=":", lw=0.8)
    ax2.scatter(mz, res_old, s=40, c="gray", marker="x", label="old (tick cal)")
    ax2.scatter(mz, res_new, s=45, c="#2ca02c", zorder=3, label="new (anchored)")
    ax2.set_ylim(-0.7, 0.7)
    ax2.set_xlabel("m/z"); ax2.set_ylabel("rounding residual (m/z)")
    ax2.set_title("+-0.5 = rounds to the wrong integer")
    ax2.legend(fontsize=8); ax2.grid(alpha=0.3)

    fig.tight_layout()
    calib_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(calib_dir / f"{stem}_calib.png"), dpi=130)
    plt.close(fig)


# --- Tier 2: Vector extraction ---------------------------------------------

def _collect_line_segments(drawings):
    """Flatten all line items from every drawing into a list of segments.

    Returns list of (x0, y0, x1, y1) in PDF coordinates (y increases downward).
    Also includes vertical sides of filled rectangles in case bars are drawn
    as rects rather than lines.
    """
    segs = []
    for d in drawings:
        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                segs.append((p1.x, p1.y, p2.x, p2.y))
            elif kind == "re":
                r = item[1]
                # left and right edges of the rect as vertical segments
                segs.append((r.x0, r.y0, r.x0, r.y1))
                segs.append((r.x1, r.y0, r.x1, r.y1))
    return segs


def _find_baseline_and_yaxis(segs, page_w, page_h):
    """Identify the x-axis (a long horizontal line) and y-axis (a long
    vertical line near the left). Returns (xaxis_y, yaxis_x, xaxis_x_right) or (None, None, None)."""
    # Exclude page-border/frame lines that hug the left or right page edge.
    # Some PDFs (e.g. the IISc set) draw a full-page rectangle whose left edge
    # runs the entire page height — longer than the real plot axis — and would
    # otherwise be mistaken for the y-axis.
    edge_margin = page_w * 0.02

    # Horizontal segments sorted by length
    h_segs = [(x0, y0, x1, y1) for x0, y0, x1, y1 in segs
              if abs(y1 - y0) < 0.5 and abs(x1 - x0) > page_w * 0.3]
    v_segs = [(x0, y0, x1, y1) for x0, y0, x1, y1 in segs
              if abs(x1 - x0) < 0.5 and abs(y1 - y0) > page_h * 0.15
              and edge_margin < (x0 + x1) / 2 < page_w - edge_margin]

    if not h_segs or not v_segs:
        return None, None, None

    # x-axis: longest horizontal in the lower half
    h_segs_lower = [s for s in h_segs if (s[1] + s[3]) / 2 > page_h * 0.3] or h_segs
    xaxis = max(h_segs_lower, key=lambda s: abs(s[2] - s[0]))
    xaxis_y      = (xaxis[1] + xaxis[3]) / 2
    xaxis_x_right = max(xaxis[0], xaxis[2])

    # y-axis: the leftmost tall vertical in the left third. Leftmost (not
    # longest) because a base peak at 100% can be exactly as tall as the axis,
    # and the axis always sits to the left of every bar.
    v_segs_left = [s for s in v_segs if (s[0] + s[2]) / 2 < page_w * 0.4] or v_segs
    yaxis = min(v_segs_left, key=lambda s: (s[0] + s[2]) / 2)
    yaxis_x = (yaxis[0] + yaxis[2]) / 2

    return xaxis_y, yaxis_x, xaxis_x_right


def _calibrate(page, xaxis_y, yaxis_x):
    """Use vector text to find pixels-per-unit on x and pixels-per-percent on y.
    Returns (pixels_per_unit, x_intercept_pixel, pixels_per_pct) — any may be None."""
    words = page.get_text("words")

    # X-tick candidates: numeric words just below the x-axis, right of the y-axis
    x_ticks = []
    for x0, y0, x1, y1, text, *_ in words:
        # coarse vertical filter: word baseline should be just below the x-axis
        mid_y = (y0 + y1) / 2
        if not (mid_y > xaxis_y and mid_y < xaxis_y + 40 and x0 > yaxis_x - 5):
            continue
        # reject long text boxes (compound names or annotations)
        width = x1 - x0
        max_tick_width = page.rect.width * 0.08  # ticks are short labels
        if width > max_tick_width:
            continue
        # only accept strictly numeric labels (integers or decimals)
        if not re.match(r"^\d+(?:\.\d+)?$", text.strip()):
            continue
        try:
            val = float(text)
            # ensure the label sits close to the axis baseline
            if mid_y >= xaxis_y and mid_y <= xaxis_y + 12:
                x_ticks.append(((x0 + x1) / 2, val))
        except ValueError:
            pass

    pixels_per_unit = x_intercept = None
    if len(x_ticks) >= 2:
        xs = np.array([t[0] for t in x_ticks])
        vs = np.array([t[1] for t in x_ticks])
        pixels_per_unit, x_intercept = np.polyfit(vs, xs, 1)

    # Y-axis "100" label: should be just left of y-axis near the top
    pixels_per_pct = None
    for x0, y0, x1, y1, text, *_ in words:
        if text.strip() == "100" and x1 < yaxis_x + 5:
            mid_y = (y0 + y1) / 2
            pixels_per_pct = abs(xaxis_y - mid_y) / 100.0
            break

    return pixels_per_unit, x_intercept, pixels_per_pct


def _extract_bars(segs, xaxis_y, yaxis_x, xaxis_x_right):
    """Find vertical line segments whose bottom sits on the x-axis baseline
    and which are right of the y-axis. Returns list of (x_pdf, height_pdf)."""
    if not segs:
        return []
    arr    = np.array(segs, dtype=np.float64)
    x0, y0, x1, y1 = arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]
    x_mid  = (x0 + x1) / 2
    y_lo   = np.minimum(y0, y1)
    y_hi   = np.maximum(y0, y1)
    height = y_hi - y_lo
    mask = (
        (np.abs(x1 - x0) <= 0.5)       &  # vertical
        (x_mid > yaxis_x + 0.5)        &  # right of y-axis
        (y_hi <= xaxis_y + 3)          &  # bottom does not go far below x-axis
        (y_hi >= xaxis_y - 1)         &  # bottom is not floating too far above x-axis
        (y_lo < xaxis_y - 0.1)         &  # top extends above baseline
        (height >= -1)                    # noise floor
    )
    if xaxis_x_right is not None:
        mask &= x_mid < xaxis_x_right - 1
    x_mid  = x_mid[mask]
    height = height[mask]
    if len(x_mid) == 0:
        return []
    order     = np.argsort(x_mid)
    x_mid     = x_mid[order]
    height    = height[order]
    # dedup near-duplicates (some PDFs draw bars twice)
    gaps      = np.concatenate([[False], np.diff(x_mid) >= 0.01])
    group_ids = np.cumsum(gaps)
    return [
        (float(x_mid[group_ids == g][0]), float(height[group_ids == g].min()))
        for g in range(int(group_ids[-1]) + 1)
    ]


def process_pdf_vector(pdf_path, write_visual=False, stem=None, pdf_bytes=None, visuals_dir=None,
                       calib_graph=False, calib_dir=None):
    """Try to extract peaks directly from PDF vector layer.
    Returns (results, max_mz, mz_warnings). Raises on extraction failure."""
    doc  = fitz.open(stream=pdf_bytes, filetype="pdf") if pdf_bytes else fitz.open(pdf_path)
    page = doc[0]
    drawings = page.get_drawings()
    if not drawings:
        raise RuntimeError("no vector drawings on page")

    segs = _collect_line_segments(drawings)
    if len(segs) < 5:
        raise RuntimeError(f"too few line segments ({len(segs)})")

    page_w, page_h = page.rect.width, page.rect.height
    xaxis_y, yaxis_x, xaxis_x_right = _find_baseline_and_yaxis(segs, page_w, page_h)
    if xaxis_y is None:
        raise RuntimeError("could not locate axes")

    pixels_per_unit, x_intercept, pixels_per_pct = _calibrate(page, xaxis_y, yaxis_x)
    if pixels_per_unit is None or pixels_per_pct is None:
        raise RuntimeError("calibration failed (need ≥2 x-ticks and a '100' label)")

    raw_bars = _extract_bars(segs, xaxis_y, yaxis_x, xaxis_x_right)

    # Post-process: re-assign integer m/z from printed peak labels (ground truth),
    # correcting any sub-unit calibration drift that would otherwise misround.
    bar_tops = [(x_pdf, xaxis_y - h_pdf) for x_pdf, h_pdf in raw_bars]
    mz_map, calib_info = _correct_mz_with_labels(
        page, bar_tops, pixels_per_unit, x_intercept, xaxis_y
    )
    mz_warnings = [calib_info["warning"]] if calib_info["warning"] else []

    if calib_graph and stem:
        cdir = calib_dir or ((visuals_dir or VISUALS_FOLDER) / "calib")
        try:
            _save_calibration_graph(pdf_path, calib_info, pixels_per_unit,
                                    x_intercept, stem, cdir)
        except Exception as e:
            mz_warnings.append(f"calib-graph failed: {e}")

    seen_mz = {}
    for x_pdf, h_pdf in raw_bars:
        mz        = mz_map[x_pdf]
        intensity = round(min(h_pdf / pixels_per_pct, 100.0), 2)
        if mz not in seen_mz or intensity < seen_mz[mz]:
            seen_mz[mz] = intensity
    results = [{"mz": mz, "intensity": seen_mz[mz]} for mz in sorted(seen_mz)]

    if results:
        max_int = max(r["intensity"] for r in results)
        if 0 < max_int < 100.0:
            diff = 100.0 - max_int
            results = [
                {"mz": r["mz"], "intensity": round(min(r["intensity"] + diff, 100.0), 2)}
                for r in results
            ]

    max_mz = round((xaxis_x_right - x_intercept) / pixels_per_unit) \
             if xaxis_x_right is not None else None

    if write_visual and stem:
        pix = page.get_pixmap(dpi=RASTER_DPI)
        vis = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        out = cv2.cvtColor(vis, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
        vh, vw = out.shape[:2]
        sx, sy = vw / page_w, vh / page_h

        xr = int(xaxis_y * sy)
        yc = int(yaxis_x * sx)
        cv2.line(out, (0, xr), (vw - 1, xr), (0, 0, 255), 2)
        cv2.line(out, (yc, 0), (yc, vh - 1), (255, 0, 0), 2)
        cv2.circle(out, (yc, xr), 10, (0, 255, 0), -1)
        if xaxis_x_right is not None:
            xrr = int(xaxis_x_right * sx)
            cv2.line(out, (xrr, 0), (xrr, vh - 1), (0, 165, 255), 2)

        words = page.get_text("words")
        if x_intercept is not None and pixels_per_unit is not None:
            for x0, y0, x1, y1, text, *_ in words:
                if y0 > xaxis_y and y0 < xaxis_y + 40 and x0 > yaxis_x - 5:
                    try:
                        float(text)
                        px0, py0 = int(x0 * sx), int(y0 * sy)
                        px1, py1 = int(x1 * sx), int(y1 * sy)
                        midx = (px0 + px1) // 2
                        cv2.rectangle(out, (px0, py0), (px1, py1), (0, 165, 255), 2)
                        cv2.circle(out, (midx, xr), 6, (0, 165, 255), -1)
                    except ValueError:
                        pass

        if pixels_per_pct is not None:
            for x0, y0, x1, y1, text, *_ in words:
                if text.strip() == "100" and x1 < yaxis_x + 5:
                    mid_y = int(((y0 + y1) / 2) * sy)
                    cv2.circle(out, (yc, mid_y), 6, (0, 255, 100), -1)
                    cv2.line(out, (yc, mid_y), (yc, xr), (0, 255, 100), 2)
                    break

        for x_pdf, h_pdf in raw_bars:
            px = int(x_pdf * sx)
            py = int((xaxis_y - h_pdf) * sy)
            cv2.circle(out, (px, py), 2, (0, 0, 255), -1)

        vdir = visuals_dir or VISUALS_FOLDER
        vdir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(vdir / f"{stem}.png"), out)

    doc.close()
    return results, max_mz, mz_warnings


# --- Tier 1: Tuned raster fallback -----------------------------------------

def process_pdf_raster(pdf_path, write_visual=False, stem=None, pdf_bytes=None, visuals_dir=None,
                       calib_graph=False, calib_dir=None):
    """Original pipeline, with DPI lowered and visualization off by default.
    Returns (results, mz_warnings)."""
    pdf_path = Path(pdf_path)
    doc  = fitz.open(stream=pdf_bytes, filetype="pdf") if pdf_bytes else fitz.open(pdf_path)
    page = doc[0]
    pix  = page.get_pixmap(dpi=RASTER_DPI)
    img  = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    # Go straight to grayscale — skip the BGR detour
    if pix.n == 4:
        gray = cv2.cvtColor(img, cv2.COLOR_RGBA2GRAY)
    elif pix.n == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        gray = img.squeeze()
    h, w = gray.shape[:2]

    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    inv    = 255 - binary

    row_sums   = inv.sum(axis=1)
    row_region = row_sums[h // 50 : h - h // 50]
    candidates = np.where(row_region >= row_region.max() * 0.7)[0]
    xaxis_row  = int(candidates[-1]) + h // 50

    col_sums      = inv.sum(axis=0)
    search_region = col_sums[w // 50 : w // 2]
    threshold     = search_region.max() * 0.3
    cands_y       = np.where(search_region >= threshold)[0]
    yaxis_col     = int(cands_y[0]) + w // 50  # leftmost heavy column = y-axis

    scale_x = w / page.rect.width
    scale_y = h / page.rect.height
    words   = page.get_text("words")

    tick_candidates = []
    for x0, y0, x1, y1, text, *_ in words:
        px0, py0 = x0 * scale_x, y0 * scale_y
        px1, py1 = x1 * scale_x, y1 * scale_y
        # coarse vertical filter: text baseline just below the x-axis
        if not (py0 > xaxis_row and py0 < xaxis_row + 40 and px0 > yaxis_col + 10):
            continue
        # reject long text (likely compound name or annotation)
        box_width = px1 - px0
        if box_width > w * 0.08:
            continue
        # only accept strictly numeric labels
        if not re.match(r"^\d+(?:\.\d+)?$", text.strip()):
            continue
        tick_candidates.append((px0, py0, px1, py1, text))

    tick_candidates.sort(key=lambda t: t[0])
    if tick_candidates:
        min_y = min(c[1] for c in tick_candidates)
        tick_candidates = [c for c in tick_candidates if c[1] <= min_y + 30]

    out = None
    if write_visual:
        color = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)
        out = color.copy()
        cv2.line(out, (0, xaxis_row), (w - 1, xaxis_row), (0, 0, 255), 2)
        cv2.line(out, (yaxis_col, 0), (yaxis_col, h - 1), (255, 0, 0), 2)
        cv2.circle(out, (yaxis_col, xaxis_row), 10, (0, 255, 0), -1)

    numeric_ticks = []
    for px0, py0, px1, py1, text in tick_candidates:
        try:
            val  = float(text)
            numeric_ticks.append(((px0 + px1) / 2, val))
            if out is not None:
                cv2.rectangle(out, (int(px0), int(py0)), (int(px1), int(py1)), (0, 165, 255), 2)
                cv2.circle(out, (int((px0 + px1) / 2), xaxis_row), 6, (0, 165, 255), -1)
        except ValueError:
            pass

    pixels_per_unit = scale_intercept = mid_a = None
    if len(numeric_ticks) >= 2:
        tick_pixels = np.array([t[0] for t in numeric_ticks])
        tick_values = np.array([t[1] for t in numeric_ticks])
        pixels_per_unit, scale_intercept = np.polyfit(tick_values, tick_pixels, 1)
        mid_a = numeric_ticks[0][0]

    pixels_per_pct = None
    for x0, y0, x1, y1, text, *_ in words:
        if text.strip() == '100':
            px1_w = x1 * scale_x
            py0_w, py1_w = y0 * scale_y, y1 * scale_y
            if px1_w < yaxis_col + 20:
                mid_y_100      = (py0_w + py1_w) / 2
                pixels_per_pct = abs(xaxis_row - mid_y_100) / 100
                if out is not None:
                    cv2.circle(out, (yaxis_col, int(mid_y_100)), 6, (0, 255, 100), -1)
                    cv2.line(out, (yaxis_col, int(mid_y_100)), (yaxis_col, xaxis_row), (0, 255, 100), 2)
                break

    # Scan all the way to the right END OF THE X-AXIS LINE, not just the last
    # numeric tick — otherwise bars beyond the highest labelled m/z (which can
    # include the base peak) are silently dropped.  The baseline is one
    # continuous black run from the y-axis to its right end; walk along it and
    # stop where the run ends (small gaps tolerated for anti-aliasing).
    row_black = binary[xaxis_row, :] == 0
    gap_tol   = max(3, w // 500)
    xaxis_right_col = w
    gap = 0
    for c in range(yaxis_col, w):
        if row_black[c]:
            xaxis_right_col = c + 1
            gap = 0
        else:
            gap += 1
            if gap > gap_tol:
                break

    # Step the scan row above the x-axis line itself.  At higher DPI the baseline
    # is several pixels thick; scanning a row that is still on the line reads the
    # whole continuous baseline as one giant bar.  Walk up past any near-solid
    # rows so we sample just above the line, where only real bars cross.
    scan_row = xaxis_row
    while scan_row > 0 and (binary[scan_row, :] == 0).sum() > w * 0.5:
        scan_row -= 1
    # Start just right of the y-axis (not the first numeric tick) so peaks below
    # the first tick label — e.g. m/z < 20 — are scanned too.  Skip the y-axis
    # line's own columns at the scan row so the axis is not read as a bar.
    start_col = yaxis_col
    while start_col < xaxis_right_col and binary[scan_row, start_col] == 0:
        start_col += 1

    def _bar_height(col):
        """Pixels a bar rises above the baseline at image column col (0 if none)."""
        if col < 0 or col >= w or binary[scan_row, col] != 0:
            return 0
        top = scan_row
        while top > 0 and binary[top - 1, col] == 0:
            top -= 1
        return xaxis_row - top

    # Grid-aware bar detection: sample the bar height at each integer-m/z column
    # (col = intercept + (mz+shift)*ppu) instead of taking one peak per contiguous
    # black run.  When px/m/z is small (wide-range / high-m/z spectra) adjacent
    # bars touch, so the run method merges them — dropping peaks and mis-centring
    # others onto the wrong integer.  Sampling on the calibrated grid gives every
    # m/z its own reading.  The window is kept under half the m/z pitch so a
    # slightly off-centre bar is caught without bleeding into its neighbour.
    seen_mz = {}
    bar_px = []   # (col_pixel, tip_y_pixel) of each detected bar, for label anchoring
    if pixels_per_unit and scale_intercept is not None and pixels_per_pct:
        half  = max(0, int(round(pixels_per_unit / 2)) - 1)
        mz_lo = max(1, int(np.floor((start_col - scale_intercept) / pixels_per_unit)))
        mz_hi = int(np.ceil((xaxis_right_col - scale_intercept) / pixels_per_unit))
        for mz in range(mz_lo, mz_hi + 1):
            c = int(round(scale_intercept + mz * pixels_per_unit))
            lo = max(c - half, start_col)
            hi = min(c + half + 1, xaxis_right_col)
            # locate the actual column of the tallest bar in the window (not just c)
            best, best_c = 0, c
            for cc in range(lo, hi):
                hgt = _bar_height(cc)
                if hgt > best:
                    best, best_c = hgt, cc
            if best <= 0:
                continue
            intensity = round(min(best / pixels_per_pct, 100.0), 2)
            if intensity <= 0:
                continue
            seen_mz[mz] = intensity
            bar_px.append((best_c, xaxis_row - best))
            if out is not None:
                cv2.circle(out, (best_c, xaxis_row - best), 2, (0, 0, 255), -1)

    # Post-process: re-assign integer m/z from printed peak labels (ground truth).
    # Work in image-pixel coordinates; scale PDF-unit text words into pixels.
    mz_warnings = []
    if seen_mz and pixels_per_unit and scale_intercept is not None:
        prov_mz   = sorted(seen_mz)                       # provisional keys, ascending
        prov_int  = [seen_mz[m] for m in prov_mz]
        mz_map, calib_info = _correct_mz_with_labels(
            page, bar_px, pixels_per_unit, scale_intercept, xaxis_row,
            scale_x=scale_x, scale_y=scale_y,
        )
        if calib_info["warning"]:
            mz_warnings.append(calib_info["warning"])
        if calib_info.get("anchored"):
            # remap intensities onto corrected m/z (keep min on collision, as before)
            corrected = {}
            for (col, _tip), inten in zip(bar_px, prov_int):
                cm = mz_map[col]
                if cm not in corrected or inten < corrected[cm]:
                    corrected[cm] = inten
            seen_mz = corrected
        if calib_graph and stem:
            cdir = calib_dir or ((visuals_dir or VISUALS_FOLDER) / "calib")
            try:
                _save_calibration_graph(pdf_path, calib_info, pixels_per_unit,
                                        scale_intercept, stem, cdir)
            except Exception as e:
                mz_warnings.append(f"calib-graph failed: {e}")

    results = [{"mz": mz, "intensity": seen_mz[mz]} for mz in sorted(seen_mz)]

    # Normalize: the highest detected bar should be exactly 100%.
    # The "100" text label midpoint is slightly offset from the true 100% line,
    # so all heights are systematically under-reported by a small fixed amount.
    if results:
        max_int = max(r["intensity"] for r in results)
        if 0 < max_int < 100.0:
            diff = 100.0 - max_int
            results = [
                {"mz": r["mz"], "intensity": round(min(r["intensity"] + diff, 100.0), 2)}
                for r in results
            ]

    if out is not None and stem is not None:
        vdir = visuals_dir or VISUALS_FOLDER
        vdir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(vdir / f"{stem}.png"), out)

    doc.close()
    return results, mz_warnings


# --- Compound name extraction -----------------------------------------------

def _extract_compound_name(pdf_path, pdf_bytes=None):
    """Read the first text line of the PDF and strip the library prefix, e.g. '(mainlib) '."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf") if pdf_bytes else fitz.open(pdf_path)
    first_line = doc[0].get_text().split("\n")[0].strip()
    doc.close()
    # Standard format: "(mainlib) Compound Name"
    match = re.match(r"^\([^)]+\)\s*(.+)", first_line)
    if match:
        return match.group(1).strip()
    # NIST text format: "Name: Compound Name"
    match = re.match(r"^Name:\s*(.+)", first_line, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return first_line



# --- Validation helpers -----------------------------------------------------

def _validate_result(results):
    """Automated sanity checks on extracted peaks. Returns list of warning strings."""
    warnings = []
    if not results:
        warnings.append("no bars detected")
        return warnings

    intensities = [r["intensity"] for r in results]
    mz_values   = [r["mz"]        for r in results]

    if max(intensities) < 99.0:
        warnings.append(f"base peak is {max(intensities):.2f}% — expected 100.0 (y-calibration drift)")

    bad_mz = [mz for mz in mz_values if mz < 2 or mz > 2000]
    if bad_mz:
        warnings.append(f"out-of-range m/z: {bad_mz}")

    if any(i <= 0 or i > 100.1 for i in intensities):
        warnings.append("intensity values outside (0, 100] range")

    if len(results) < 3:
        warnings.append(f"only {len(results)} bar(s) detected — suspiciously few")

    seen = {}
    for mz in mz_values:
        seen[mz] = seen.get(mz, 0) + 1
    dupes = [mz for mz, n in seen.items() if n > 1]
    if dupes:
        warnings.append(f"duplicate m/z values: {dupes}")

    return warnings


def _compare_pipelines(v_results, r_results):
    """Cross-compare vector and raster results. Returns list of warning strings."""
    warnings = []
    v_count, r_count = len(v_results), len(r_results)

    if r_count == 0:
        warnings.append("cross-validate: raster pipeline found no bars")
        return warnings

    diff_pct = abs(v_count - r_count) / max(v_count, r_count)
    if diff_pct > 0.2:
        warnings.append(
            f"cross-validate: bar count mismatch — vector={v_count}, raster={r_count} ({diff_pct*100:.0f}% diff)"
        )

    v_mz = {r["mz"] for r in v_results}
    r_mz = {r["mz"] for r in r_results}
    # allow ±1 tolerance for raster pixel imprecision
    only_vector = sorted(mz for mz in v_mz if not any(abs(mz - rm) <= 1 for rm in r_mz))
    only_raster = sorted(mz for mz in r_mz if not any(abs(mz - vm) <= 1 for vm in v_mz))

    if len(only_vector) > max(1, len(v_mz) * 0.1):
        warnings.append(f"cross-validate: {len(only_vector)} m/z only in vector: {only_vector[:10]}")
    if len(only_raster) > max(1, len(r_mz) * 0.1):
        warnings.append(f"cross-validate: {len(only_raster)} m/z only in raster: {only_raster[:10]}")

    return warnings


# --- Unified entry point ----------------------------------------------------

def process_pdf(pdf_path, cross_validate=False, write_visual=WRITE_VISUALS, pdf_bytes=None,
                visuals_dir=None, method="auto", calib_graph=False, calib_dir=None):
    """Extract peaks from one PDF. Designed for Pool workers.

    method:
      "auto"   - try vector first, fall back to raster (then text). Default.
      "vector" - vector pipeline only (no fallback).
      "raster" - raster pipeline only (then text fallback if it finds nothing).

    Returns the full result dict — does NOT write JSON.  The main process
    is responsible for serialisation (see _bounded_submit / main).
    """
    pdf_path = Path(pdf_path)
    stem     = pdf_path.stem
    t0       = time.time()
    used     = method if method in ("vector", "raster") else "vector"
    compound_name = None
    mz_warnings   = []
    try:
        compound_name = _extract_compound_name(pdf_path, pdf_bytes=pdf_bytes)
        max_mz = None
        if method == "raster":
            results, mz_warnings = process_pdf_raster(
                pdf_path, write_visual=write_visual, stem=stem, pdf_bytes=pdf_bytes,
                visuals_dir=visuals_dir, calib_graph=calib_graph, calib_dir=calib_dir
            )
        elif method == "vector":
            results, max_mz, mz_warnings = process_pdf_vector(
                pdf_path, write_visual=write_visual, stem=stem, pdf_bytes=pdf_bytes,
                visuals_dir=visuals_dir, calib_graph=calib_graph, calib_dir=calib_dir
            )
        else:  # auto
            try:
                results, max_mz, mz_warnings = process_pdf_vector(
                    pdf_path, write_visual=write_visual, stem=stem, pdf_bytes=pdf_bytes,
                    visuals_dir=visuals_dir, calib_graph=calib_graph, calib_dir=calib_dir
                )
            except Exception:
                used    = "raster"
                results, mz_warnings = process_pdf_raster(
                    pdf_path, write_visual=write_visual, stem=stem, pdf_bytes=pdf_bytes,
                    visuals_dir=visuals_dir, calib_graph=calib_graph, calib_dir=calib_dir
                )

        warnings = _validate_result(results)
        warnings.extend(mz_warnings)

        if cross_validate and used == "vector":
            try:
                raster_results, _ = process_pdf_raster(pdf_path, pdf_bytes=pdf_bytes)
                warnings.extend(_compare_pipelines(results, raster_results))
            except Exception as e:
                warnings.append(f"cross-validate: raster pipeline failed: {e}")

        return {
            "file":      pdf_path.name,
            "name":      compound_name,
            "bar_count": len(results),
            "peaks":     results,
            "warnings":  warnings,
            "method":    used,
            "ok":        True,
            "time":      time.time() - t0,
        }
    except Exception as e:
        return {
            "file":     pdf_path.name,
            "name":     compound_name,
            "peaks":    [],
            "warnings": [str(e)],
            "method":   used,
            "ok":       False,
            "time":     time.time() - t0,
        }


# --- Bounded byte-preloading submission -------------------------------------

def _bounded_submit(ex, pdf_files, cross_validate, write_visual, max_inflight=64, visuals_dir=None,
                    method="auto", calib_graph=False, calib_dir=None):
    """Read PDF bytes sequentially in the main thread, submit to the worker pool
    with a bounded window so we never hold more than max_inflight PDFs in RAM.

    Yields completed futures in completion order so the caller can stream
    results to disk without waiting for the whole batch.
    """
    pending = {}
    for p in pdf_files:
        # drain one completed future before adding a new one when at the limit
        while len(pending) >= max_inflight:
            done = next(as_completed(pending))
            del pending[done]
            yield done
        pdf_bytes = p.read_bytes()
        fut = ex.submit(process_pdf, p, cross_validate, write_visual, pdf_bytes, visuals_dir,
                        method, calib_graph, calib_dir)
        pending[fut] = p
    for fut in as_completed(pending):
        yield fut


# --- Driver -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",          default=str(_ROOT / "data/samples"),
                        help="PDF file or folder of PDFs; nested folders are scanned recursively")
    parser.add_argument("--workers",        type=int, default=0, help="0 = auto")
    parser.add_argument("--pool",           choices=["thread", "process"], default="thread",
                        help="thread (default, good for fast vector PDFs) or process (better for CPU-heavy raster batches)")
    parser.add_argument("--cross-validate", action="store_true", help="run both pipelines and compare results")
    parser.add_argument("--method", choices=["auto", "vector", "raster"], default="auto",
                        help="auto = vector then raster fallback (default); "
                             "vector = vector only; raster = raster only")
    parser.add_argument("--visual",         action="store_true", help="save debug overlay PNG(s)")
    parser.add_argument("--calib-graph",    action="store_true",
                        help="save a per-PDF m/z calibration graph (anchors + Theil-Sen fit + "
                             "residuals) to visuals/<SUBDIR>/calib/<stem>_calib.png")
    parser.add_argument("--per-file-json",  action="store_true",
                        help="also write individual outputs/<stem>.json files (slower; for sanitycheck.py compat)")
    parser.add_argument("--output",         default=str(_ROOT / "data/processed/results.jsonl"),
                        help="path for the JSON Lines output file")
    parser.add_argument("--subdir",         default=None,
                        help="route per-file JSON to outputs/<SUBDIR>/ and visuals to visuals/<SUBDIR>/. "
                             "Pass 'auto' to name the subfolder after the input folder "
                             "(e.g. --input data/samples/IISc_Data --subdir auto -> outputs/IISc_Data/, visuals/IISc_Data/)")
    parser.add_argument("--frange", default=None,
                        help="select F-folders by number, e.g. '21-40' or '21,25,30-35'")
    parser.add_argument("--list-folders", action="store_true",
                        help="list available folders (and counts) in --input, then exit")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists() and not input_path.is_absolute():
        for base in (_ROOT, _ROOT / "data", _ROOT / "data" / "samples"):
            alt_input = base / args.input
            if alt_input.exists():
                input_path = alt_input
                break

    if args.list_folders:
        for d in sorted(input_path.iterdir()):
            if not d.is_dir():
                continue
            pdfs = list(d.glob("**/*.pdf"))
            if not pdfs:
                continue
            n = _folder_fnum(d)
            if n is not None:
                print(f"  F{n:<4} {d.name}  ({len(pdfs)} PDFs)")
            else:
                print(f"       {d.name}  ({len(pdfs)} PDFs)")
        return

    pdf_files = _gather_pdfs(input_path, args.frange)
    if not pdf_files:
        raise FileNotFoundError(f"No PDFs found under {args.input}/")
    write_visual = (len(pdf_files) == 1) or args.visual

    # Optional per-run subfolder for outputs/ and visuals/ (e.g. --subdir IISc_Data)
    subdir = args.subdir
    if subdir == "auto":
        subdir = input_path.name if input_path.is_dir() else input_path.parent.name
    visuals_dir = (VISUALS_FOLDER / subdir) if subdir else VISUALS_FOLDER
    json_dir    = (JSON_FOLDER / subdir) if subdir else JSON_FOLDER
    calib_dir   = visuals_dir / "calib"
    if write_visual:
        visuals_dir.mkdir(parents=True, exist_ok=True)
    if args.calib_graph:
        calib_dir.mkdir(parents=True, exist_ok=True)
    if args.per_file_json:
        json_dir.mkdir(parents=True, exist_ok=True)

    workers = args.workers or min(len(pdf_files), os.cpu_count())
    print(f"Processing {len(pdf_files)} PDF(s) with {workers} workers"
          + (" [visuals on]" if write_visual else ""))

    Executor = ProcessPoolExecutor if args.pool == "process" else ThreadPoolExecutor
    print(f"Pool type: {args.pool}  |  output: {args.output}")

    t0 = time.time()
    n_vector = n_raster = n_warn = n_fail = 0

    with Executor(max_workers=workers) as ex, \
         open(args.output, "w", buffering=1 << 20) as jsonl_out:
        with tqdm(total=len(pdf_files), unit="pdf", dynamic_ncols=True) as bar:
            for fut in _bounded_submit(ex, pdf_files, args.cross_validate, write_visual,
                                       visuals_dir=visuals_dir, method=args.method,
                                       calib_graph=args.calib_graph, calib_dir=calib_dir):
                r = fut.result()

                # write one JSONL line — no per-file open/close
                jsonl_out.write(json.dumps(r) + "\n")

                # optional per-file JSON for sanitycheck.py compatibility
                if args.per_file_json:
                    stem = Path(r["file"]).stem
                    with open(json_dir / f"{stem}.json", "w") as f:
                        json.dump(
                            {"name": r["name"], "bar_count": r.get("bar_count", 0),
                             "peaks": r["peaks"], "warnings": r["warnings"],
                             "method": r["method"]},
                            f, indent=2,
                        )

                if r["ok"]:
                    if r["method"] == "vector": n_vector += 1
                    else:                       n_raster += 1
                    if r.get("warnings") and len(r["warnings"]) > 0:
                        n_warn += 1
                else:
                    n_fail += 1
                    tqdm.write(f"  FAIL {r['file']}: {r['warnings']}")
                bar.update(1)

    elapsed = time.time() - t0

    print(f"\nDone in {elapsed:.2f}s")
    print(f"  vector: {n_vector}    raster: {n_raster}    warned: {n_warn}    failed: {n_fail}")
    print(f"  throughput: {len(pdf_files) / elapsed:.1f} PDFs/s")
    print(f"  results written to: {args.output}")
    if n_warn or n_fail:
        print(f"  run sanitycheck.py (after --per-file-json) to review flagged files")

def _folder_fnum(path):
    """Pull the integer after 'F' from a folder name like 'Spectrum F21 06-10-23'."""
    m = re.search(r"\bF(\d+)\b", path.name)
    return int(m.group(1)) if m else None


def _parse_frange(spec):
    """Turn a spec like '21-40', '21,25,30-35', or '21' into a set of ints."""
    wanted = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            wanted.update(range(int(lo), int(hi) + 1))
        else:
            wanted.add(int(part))
    return wanted


def _gather_pdfs(input_path, frange_spec=None):
    """Resolve the input into a sorted list of PDF paths.

    - a single .pdf            -> [that file]
    - folder of 'F<x>' subdirs -> PDFs from folders whose F-number is in the
                                   range (all of them if frange_spec is None)
    - plain folder of PDFs     -> *.pdf directly inside
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        return [input_path]

    f_folders = sorted(
        ((_folder_fnum(d), d) for d in input_path.iterdir()
         if d.is_dir() and _folder_fnum(d) is not None),
        key=lambda t: t[0],
    )

    if f_folders:
        wanted = _parse_frange(frange_spec) if frange_spec else None
        selected = [(n, d) for n, d in f_folders if wanted is None or n in wanted]
        if not selected:
            available = [n for n, _ in f_folders]
            raise FileNotFoundError(
                f"No folders matched F-range {frange_spec!r}. Available F: {available}"
            )
        pdfs = []
        for n, d in selected:
            found = sorted(d.glob("**/*.pdf"))
            print(f"  F{n}: {d.name}  ({len(found)} PDFs)")
            pdfs.extend(found)
        return pdfs

    return sorted(input_path.rglob("*.pdf"))
if __name__ == "__main__":
    main()