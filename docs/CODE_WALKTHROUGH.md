# Code Walkthrough — GC-MS Stick-Spectrum Extraction Pipeline

A complete, function-by-function explanation of every script in `src/`, in the
order data flows through the system. For each function you get: **what it does**,
**its inputs and outputs**, **every step in sequence**, and **why each design
decision was made** (thresholds, tie-breaks, fallbacks).

---

## 0. The pipeline at a glance

The system has four logical stages. Data flows top to bottom; each box is one
script.

```
STAGE A — Build the reference library (run once)
  NIST_ext.py     NIST MS Search GUI ──automation──▶ *.msp chunks
  sort_msp.py     *.msp ──sort by NIST#──▶ *_sorted.msp        (optional tidy step)
  msp_to_jsonl.py full_mainlib.msp ──parse──▶ NISTds.jsonl     (the reference peak lists)

STAGE B — Extract spectra from PDFs (the core contribution)
  imgprocess.py   folder of PDFs ──vector/raster extraction──▶ results.jsonl
                                                    └──(optional)──▶ outputs/<stem>.json, visuals/<stem>.png

STAGE C — Validate & score
  sanitycheck.py  outputs/*.json ──7-section QC──▶ reports/validation_report.txt
  compare_peaks.py results.jsonl × NISTds.jsonl ──cosine──▶ reports/compare_report.txt + .csv
  intersect.py    results.jsonl ∩ NISTds.jsonl ──name match──▶ counts
  benchmark.py    PDFs ──timing / vector-vs-raster cosine──▶ explanations/*.txt + graphs/*.png

STAGE D — Store for downstream ML
  build_db.py     results.jsonl ──load──▶ spectra.duckdb (+ peaks_long view)
```

The two **record shapes** that tie everything together:

```jsonc
// One extracted spectrum (imgprocess.py → results.jsonl)
{"file": "1A.pdf", "name": "9-Tricosene, (Z)-", "bar_count": 12,
 "peaks": [{"mz": 41, "intensity": 22.1}, ...],
 "warnings": [], "method": "vector", "ok": true, "time": 0.031}

// One reference spectrum (msp_to_jsonl.py → NISTds.jsonl)
{"name": "9-Tricosene, (Z)-", "bar_count": 40,
 "peaks": [{"mz": 41, "intensity": 220}, ...]}   // RAW 0–999 intensities, no scaling
```

Everything downstream keys on `name` (after canonicalisation) and compares the
`peaks` arrays.

---

# STAGE A — Building the reference library

## 1. `NIST_ext.py` — automated export of the NIST library to MSP

The NIST MS Search 2.3 desktop program has no batch-export API, so this script
**drives its GUI by sending keystrokes** (via `pywinauto`), reproducing the exact
manual click sequence a human would perform, one ID-range chunk at a time. This is
how the ~267,000-compound `mainlib` was dumped to disk.

### Module-level configuration (constants)

| Constant | Value | Why |
|---|---|---|
| `OUTPUT_DIR` | `C:\NIST17\MSSEARCH\MSP_Export` | Where chunks land. |
| `CHUNK_SIZE` | `1000` | NIST can only reliably select/export a bounded number of results at once; 1000 is a safe batch. |
| `TOTAL_SPECTRA` | `267380` | Library size — the default end of the ID range. |
| `LIBRARY_NAME` | `"mainlib"` | Used only to name output files; the actual library is chosen manually in the GUI before running. |
| `WINDOW_TITLE` | `"NIST MS Search"` | Matched as a substring so minor title variations still attach. |
| `STEP_DELAY`, `SEARCH_WAIT`, `EXPORT_WAIT`, `KEY_DELAY` | 0.6 / 4 / 12 / 0.08 s | GUI automation is timing-fragile: each delay waits for a specific UI transition (menu open, search to finish, save-to-disk to complete). They are deliberately generous; the header notes "if any failures, increase delay times." |

### `connect_to_nist()`
**Purpose:** find the already-running NIST window and attach to it.
**Returns:** a `pywinauto` window wrapper.

Steps:
1. `findwindows.find_windows(title_re=".*NIST MS Search.*")` — regex/substring search for the window handle.
2. If no handle, `sys.exit(...)` with a clear message — the program must be opened manually first (the script never launches it, because library selection and login state are done by hand).
3. `Application(backend="win32").connect(handle=...)` attaches; returns `app.window(handle=...)`.

**Decision:** `win32` backend (not `uia`) because NIST MS Search is an old Win32
app; `win32` is faster and more reliable for keystroke injection here.

### `export_one_chunk(win, start_id, end_id, output_file)`
**Purpose:** perform the full nine-step export gesture for a single ID range.
**Returns:** nothing (success is judged afterward by whether the file exists).

Steps (each mirrors one manual action, with a delay after it):
1. `win.set_focus()` — bring the window forward so keystrokes land on it.
2. `menu_select("Search->ID_Number")` — open the ID-number search dialog via the menu (more robust than coordinate clicks).
3. Clear the field (`Ctrl+A`, `Delete`) then type `start_id`, `-`, `end_id`. Clearing first guards against a value lingering from the previous chunk.
4. `Enter` → runs the search; wait `SEARCH_WAIT` (4 s) for results to populate.
5. `Ctrl+A` → select all results in the list.
6. `Shift+F10` → keyboard equivalent of right-click (opens the context menu without needing mouse coordinates).
7. `Down`×6 → navigate to the "Export selected" item (its fixed menu position).
8. `Enter` → opens the Save dialog.
9. Clear the filename field, type the **full path** (so it lands in `OUTPUT_DIR` regardless of the dialog's remembered folder), `Enter`, wait `EXPORT_WAIT` (12 s) for the write.
10. Send `y` — answers a possible "overwrite?" prompt; harmless if no prompt appears (the keypress just goes to the main window).

**Decisions:**
- **Keyboard-only navigation** (menus, `Shift+F10`, arrow keys) instead of pixel clicks — survives window moves and DPI scaling.
- **Full path in the save box** — eliminates dependence on the dialog's current directory.
- **Fixed `Down`×6** — brittle but matches this exact NIST version's context menu; documented in the header.

### `main()`
**Purpose:** plan the chunk list, then export each chunk, **resumably**.

Steps:
1. Parse `--start-id`, `--end-id`, `--chunk`, `--test`, `--dry-run`.
2. `mkdir(OUTPUT_DIR)`.
3. Build `chunks` = list of `(start, end)` covering the range in `chunk`-sized steps; `--test` keeps only the first chunk (verify the gesture once before a multi-hour run).
4. Print the plan. `--dry-run` lists each chunk, marking ones already on disk as `skip` and the rest `would export`, then returns without touching the GUI.
5. `connect_to_nist()`, then a 5-second countdown so the user can foreground the NIST window and take hands off the keyboard.
6. For each chunk:
   - Compute `output_file = mainlib_<start>_<end>.msp`.
   - **Resume logic:** if it already exists and is non-empty, print `skip (exists)` and continue. This makes the whole run idempotent — re-running after a crash picks up where it stopped.
   - Otherwise call `export_one_chunk(...)`. Afterward, confirm the file exists and is non-empty; if not, print `FAILED` and offer interactive `[r]etry / [s]kip / [q]uit`.
   - `KeyboardInterrupt` → clean message that re-running resumes; other exceptions → same retry/skip/quit prompt.
7. At the end, print the PowerShell one-liner to concatenate the chunks into `full_mainlib.msp`.

**Decision — resumability by file presence:** rather than a checkpoint file, the
*existence of a non-empty output* is the checkpoint. Simple and crash-proof.

---

## 2. `sort_msp.py` — order MSP records by an ID field

An optional tidiness step: re-orders the compound blocks inside one or more `.msp`
files by `NIST#` (or `DB#`), preserving every record byte-for-byte. Useful so the
reference file is in a predictable order.

### `_split_records(text)`  *(generator)*
**Purpose:** break a multi-record `.msp` into individual record strings.
**Yields:** each block, stripped, that starts with `Name:`.

- `re.split(r"(?m)^(?=Name:)", text)` splits at every line beginning with `Name:`
  using a **zero-width lookahead** `(?=...)`, so the delimiter (`Name:`) is *kept*
  at the start of each piece rather than consumed.
- `(?m)` makes `^` match at every line start, not just the file start.
- Each piece is `.strip()`-ed and only yielded if it actually begins with `Name:`
  (drops any leading preamble before the first record).

### `_record_id(block, field)`
**Purpose:** pull the integer value of a named field (e.g. `NIST#`) from a block.
**Returns:** the int, or `None` if absent.

- `re.search(rf"(?m)^{re.escape(field)}:\s*(\d+)", block)` finds `NIST#: 1234` at a
  line start. `re.escape(field)` is used because field names like `NIST#` contain
  the regex-special `#`-free but `.`-safe characters — escaping is defensive.
- Returns `int(group(1))` or `None`.

### `sort_msp_text(text, field)`
**Purpose:** return the records re-sorted by the field.
**Returns:** `(sorted_text, n_records, n_missing)`.

Steps:
1. `records = list(_split_records(text))`.
2. `n_missing` = how many lack the key field.
3. **Stable sort** with key `(id is None, id or 0)`:
   - First component `id is None` is `False`(0) for records that *have* an id, `True`(1) for those that don't → records with ids sort **before** those without.
   - Second component is the numeric id (ascending); `or 0` supplies a placeholder for the `None` group (whose relative order is then preserved by sort stability).
4. Re-join with blank lines between records and a trailing newline.

**Decision — missing ids go last, original order preserved:** a Python sort is
stable, so the `(True, 0)` group keeps its input order; nothing is lost or
reshuffled arbitrarily.

### `main()`
1. Map `--key` (`nist`/`db`) to the actual field name via `KEY_FIELDS`.
2. Resolve `input` to a single file or every `*.msp` in a folder.
3. For each file: read (UTF-8, `errors="replace"` so a stray bad byte never aborts the run), sort, and write to `<stem>_sorted.msp` — or overwrite in place with `--inplace`.
4. Print a per-file summary, noting any records placed last for missing the key.

**Decision — non-destructive default:** writes `*_sorted.msp` unless `--inplace`,
so the original is never silently mutated.

---

## 3. `msp_to_jsonl.py` — convert the MSP library to the reference JSONL

Turns the (huge) concatenated `.msp` into `NISTds.jsonl`, one compound per line, in
**the same record shape `imgprocess.py` emits**, so the two can be compared
directly. Intensities are kept **raw** (0–999 NIST scale) — scaling happens later
in `compare_peaks.py`.

### `iter_blocks(path)`  *(generator)*
**Purpose:** stream record blocks without loading the whole file.
**Yields:** each record as a string.

- Reads line by line. A new record begins when a line starts with `Name:` **and**
  a block is already accumulating → yield the accumulated block, start a new one.
- Otherwise append the line. Flush the final block at EOF.

**Decision — streaming, not `read()`:** a 267k-record library is hundreds of MB;
line-streaming keeps memory flat regardless of library size.

### `block_to_record(block)`
**Purpose:** parse one block into `{name, bar_count, peaks}` or `None`.

Steps:
1. Regex out `Name:` and `Num Peaks:`. If either is missing, return `None` (the
   block isn't a real compound — e.g. a header).
2. `declared = int(Num Peaks)`.
3. `_PAIR = re.compile(r"(\d+)\s+(\d+)")` finds every `mz intensity` pair in the
   text **after** the `Num Peaks:` line (`block[npk.end():]`).
4. **Guard:** if more pairs were found than `declared`, truncate to `declared` —
   protects against stray trailing numbers (e.g. a synonyms section that contains
   digits) being mistaken for peaks.
5. Build `peaks = [{"mz": int, "intensity": int}, ...]`, return the record.

**Decisions:**
- **Parse pairs only after `Num Peaks:`** — the peak table always follows that line; this avoids matching digits in the name or metadata.
- **`declared`-based truncation** — trusts the file's own peak count as the authority on where the table ends.

### `main()`
1. Resolve input/output (`<input>.jsonl` by default).
2. Stream blocks → records; skip `None` blocks (counted as `skipped`).
3. Write each as one JSON line with `ensure_ascii=False` (preserves Greek letters
   and accented names as UTF-8 rather than `\uXXXX`).
4. Progress print every 25,000 records; final summary with skip count.

---

# STAGE B — The core extractor: `imgprocess.py`

This is the heart of the system. It has **two extraction tiers**:

- **Tier 2 (vector)** — reads the PDF's drawing operators directly (exact geometry, no pixels). Default fast path.
- **Tier 1 (raster)** — renders the page to an image and detects bars by pixel projection. Fallback when no vector layer exists.

Plus a **label-anchored m/z correction** (Theil–Sen), a **validation layer**, and a
**parallel batch driver**.

### Module configuration

| Constant | Value | Why |
|---|---|---|
| `_ROOT` | repo root | All paths derived from the file location, so the script runs from anywhere. |
| `JSON_FOLDER` | `outputs/` | Per-file JSON (compat mode only). |
| `VISUALS_FOLDER` | `visuals/` | Debug overlay PNGs. |
| `RASTER_DPI` | `600` | Double the typical 300 DPI print resolution, so adjacent bars stay separable and no peak is lost to sub-pixel mixing. |
| `WRITE_VISUALS` | `True` | Default flag passed through; the driver only actually writes visuals for single files or `--visual`. |

---

## 3.1 Label-anchored m/z correction (Theil–Sen)

**The problem it solves.** The x-axis tick calibration (Eq. 1 in the paper) is
coarse: ticks are sparse and each carries sub-pixel position error, so a small
slope/intercept drift accumulates across the axis. On some real-instrument exports
this is a near-uniform ≈+0.45-unit rightward drift, enough to push bars over the
0.5 rounding boundary and assign the **wrong integer mass** (m/z 4→5, 17→18, 29→30).
But every real peak has its integer m/z **printed as a label above its bar** — the
instrument's own ground truth. These functions harvest those labels and refit the
calibration robustly.

### `_theil_sen(x, y)`
**Purpose:** dependency-free Theil–Sen robust line fit `y = a·x + b`.
**Returns:** `(a, b)` or `(None, None)` if no slope can be formed.

Steps:
1. Coerce to float arrays; if fewer than 2 points, return `(None, None)`.
2. For each `i`, compute slopes to all later points `j>i`: `(y[j]-y[i])/(x[j]-x[i])`,
   keeping only pairs where `dx != 0` (avoids division by zero / vertical pairs).
3. `a = median(all pairwise slopes)`.
4. `b = median(y - a·x)`.

**Decisions:**
- **Theil–Sen over least squares** — a single mislabelled anchor (an outlier) cannot
  drag the median the way it drags a mean. Theil–Sen tolerates up to ~29% arbitrary
  outliers, which is far more than the anchor filter ever lets through.
- **Median-of-pairwise-slopes** is the textbook estimator; the intercept as
  `median(y − a·x)` is the matching robust intercept.
- **No SciPy** — implemented in NumPy so the worker has no heavy dependency and the
  estimate is deterministic and threshold-free. Cost is O(n²) in the number of
  anchors, but anchors number only ~10–25 per spectrum, so this is negligible.

### `_collect_mz_label_anchors(page, bar_xs, bar_tops_y, xaxis_y, ppu, scale_x, scale_y)`
**Purpose:** match each printed numeric peak-label to its bar.
**Returns:** an `N×2` array of `(bar_x, mz)` anchor pairs (in the caller's coordinate system).

For every word from `page.get_text("words")`:
1. Keep only strictly-integer words (`re.fullmatch(r"\d+")`) whose value is in
   `[1, 2000]`. *(Note: the docstring says "(1, 2000]"; the code accepts 1 as well —
   a harmless one-off, since m/z 1 essentially never labels a bar.)*
2. Convert the word's centre from PDF units to the caller's coordinate system via
   `scale_x/scale_y` (needed because the raster tier works in pixels but text words
   always come from `get_text()` in PDF units).
3. **Reject tick labels:** if the label sits on/below the x-axis (`ly ≥ xaxis_y − 3·scale_y`), it's an axis tick, not a peak label → skip.
4. **Reject far-away annotations:** find the nearest bar by x; if the label is more
   than `0.4·ppu` (40% of one m/z pitch) away horizontally, it isn't sitting over a
   bar (this rejects the y-axis "100" and scan annotations like "2.09e9") → skip.
5. **Reject labels too high above the tip:** if the label is more than `4·ppu` above
   the bar's tip, it's not this bar's label → skip.
6. Survivors become `(bar_x, value)` anchors.

**Decisions — the three geometric filters** each remove a specific false positive:
on/below axis = tick label; far in x = unrelated annotation/axis text; far in y =
a label belonging to nothing nearby. Together they leave only genuine peak labels.

### `_correct_mz_with_labels(page, bars, ppu, x_intercept, xaxis_y, scale_x, scale_y, min_anchors=3)`
**Purpose:** re-assign every bar's integer m/z using the label anchors.
**Returns:** `(mz_map, info)` where `mz_map = {bar_x: corrected_int_mz}` and `info`
records whether the correction anchored, the slope/intercept, residuals, and any
warning.

Steps:
1. Build `bar_xs`, `bar_tops` arrays. Define a local `_tick_calibrated()` fallback
   that maps each bar to `round((bar_x − x_intercept)/ppu)` — i.e. the *original*
   coarse calibration.
2. If there are no bars, return the tick-calibrated map.
3. Collect anchors. If fewer than `min_anchors=3`, set a warning and return the
   tick-calibrated map unchanged.
4. Fit `bar_x = a·(m/z) + b` with `_theil_sen` (note: fit x **as a function of m/z**,
   the natural direction since m/z is the clean integer axis). If degenerate
   (`a is None or a == 0`), warn and fall back.
5. Otherwise build `mz_map = {bar_x: round((bar_x − b)/a)}` for **every** bar
   (labelled or not — unlabelled bars inherit the corrected line).
6. Record the max rounding residual (how close any anchor came to the ±0.5 wrong-integer boundary) for diagnostics.

**Decisions:**
- **`min_anchors = 3`** — two points define a line exactly and can't reveal an
  outlier; three is the minimum where the median is meaningful. Below this, trusting
  the labels is riskier than keeping the tick calibration.
- **Never degrade a good page:** if anchoring can't be done safely, the function
  returns the original calibration plus a warning — the correction is strictly
  opt-in per page. (On the clean NIST corpus, anchors and tick calibration already
  coincide, so this step is a no-op there; it matters only for the drifted
  in-house exports.)

### `_save_calibration_graph(path, info, ppu, x_intercept, stem, calib_dir)`
**Purpose:** (debug only, `--calib-graph`) write a 2-panel PNG visualising the fit.

- Top panel: the anchors as points, the Theil–Sen line, and the old tick line, so
  you can *see* the drift.
- Bottom panel: per-peak rounding residual for old vs new, with the ±0.5 "wrong
  integer" boundaries drawn — anything crossing ±0.5 under the old line but not the
  new is a mass the correction rescued.
- `matplotlib` is imported **lazily inside the function** so the heavy import cost is
  paid only when this debug flag is set, never in the hot extraction path.
- Returns immediately if the page didn't anchor (nothing to plot).

---

## 3.2 Tier 2 — vector extraction

### `_collect_line_segments(drawings)`
**Purpose:** flatten PDF drawing operators into plain line segments.
**Returns:** list of `(x0, y0, x1, y1)` in PDF coordinates (y increases downward).

- For each drawing's items: a line item `"l"` contributes its segment directly;
  a rectangle `"re"` contributes its **left and right vertical edges** as segments.

**Decision — include rectangle edges:** some PDFs draw bars as filled rectangles
rather than strokes; capturing the vertical edges means those bars are still found.

### `_find_baseline_and_yaxis(segs, page_w, page_h)`
**Purpose:** locate the x-axis (baseline) and y-axis from the segment soup.
**Returns:** `(xaxis_y, yaxis_x, xaxis_x_right)` or `(None, None, None)`.

Steps:
1. `edge_margin = 0.02·page_w` — a guard band at each page edge.
2. **Horizontal candidates** `h_segs`: near-horizontal (`|y1−y0| < 0.5` PDF units)
   and long (`|x1−x0| > 0.3·W`).
3. **Vertical candidates** `v_segs`: near-vertical (`|x1−x0| < 0.5`), tall
   (`|y1−y0| > 0.15·H`), and whose x-midpoint is **not** within `edge_margin` of
   either page edge.
4. If either list is empty → axes not found, return `None`s (caller will fall back
   to raster).
5. **x-axis** = the longest horizontal in the lower part of the page
   (`mid_y > 0.3·H`); record its y and its right end `xaxis_x_right`.
6. **y-axis** = the **leftmost** tall vertical in the left 40% of the page; record
   its x.

**Decisions (all empirically tuned layout constraints):**
- **`0.5`-unit straightness tolerance** — axis strokes are drawn dead straight, but
  floating-point/rounding can wobble endpoints by a fraction of a unit.
- **Length thresholds (`0.3W`, `0.15H`)** — separate real axes from short tick marks
  and gridlines.
- **Exclude page-edge verticals (`edge_margin`)** — some PDFs (notably the in-house
  set) draw a **full-page border rectangle** whose left edge runs the entire page
  height; without this guard it would be mistaken for the y-axis.
- **y-axis = leftmost, not longest** — a base peak at 100% can be drawn *exactly as
  tall* as the y-axis, so "longest" is ambiguous; but the axis is always to the left
  of every bar, so "leftmost tall vertical" is unambiguous.
- **x-axis = longest in lower half** — the baseline is the longest horizontal and
  always sits low on the page; the `mid_y > 0.3·H` filter avoids a title underline
  or top border.

### `_calibrate(page, xaxis_y, yaxis_x)`
**Purpose:** convert pixel/PDF positions to m/z (x) and intensity-% (y).
**Returns:** `(pixels_per_unit, x_intercept, pixels_per_pct)` — any may be `None`.

x-axis calibration:
1. Gather numeric tick words sitting **just below** the x-axis
   (`xaxis_y < mid_y < xaxis_y+40`, right of the y-axis), rejecting wide text boxes
   (`width > 0.08·page_width` → a compound name or annotation, not a tick) and
   non-numeric strings.
2. A second tight vertical gate (`xaxis_y ≤ mid_y ≤ xaxis_y+12`) keeps only labels
   hugging the axis.
3. If ≥2 ticks, `np.polyfit(values, x_positions, 1)` → slope `pixels_per_unit` and
   `x_intercept` (the x where m/z = 0).

y-axis calibration:
4. Find the `"100"` label just left of the y-axis (`x1 < yaxis_x+5`); the vertical
   distance from it to the baseline divided by 100 gives `pixels_per_pct`.

**Decisions:**
- **Least-squares over *all* ticks** — averages out each tick's sub-pixel error
  rather than trusting any single pair.
- **Width filter `0.08`** — ticks are 1–3 digit labels; anything wider is text.
- **Two vertical gates** — the loose one (`+40`) catches candidates; the tight one
  (`+12`) rejects a second row of labels or axis-title text further down.
- **Intensity from the "100" label** — the chart's own declared 100% line is the
  most reliable y reference; pairing it with the 0% baseline fixes the scale with
  two exact points.

### `_extract_bars(segs, xaxis_y, yaxis_x, xaxis_x_right)`
**Purpose:** select the vertical segments that are actually spectrum bars.
**Returns:** list of `(x_pdf, height_pdf)`.

Vectorised mask over all segments (NumPy), keeping a segment iff:
- it is vertical (`|x1−x0| ≤ 0.5`),
- it is right of the y-axis (`x_mid > yaxis_x + 0.5`),
- its **bottom sits on the baseline** (`xaxis_y − 1 ≤ y_hi ≤ xaxis_y + 3`),
- its **top rises above the baseline** (`y_lo < xaxis_y − 0.1`),
- it is left of the axis's right end (`x_mid < xaxis_x_right − 1`).

Then:
1. Sort by x.
2. **Dedup near-duplicate x positions:** group bars whose x differs by `< 0.01`
   PDF units; within a group take the **minimum** height.

**Decisions:**
- **"Bottom on baseline, top above it"** is the geometric definition of a bar; the
  small tolerances (`+3`, `−1`, `−0.1`) absorb rounding and let a bar that overshoots
  the baseline slightly still qualify.
- **Right-end clip (`xaxis_x_right`)** — discards anything past the plotted axis
  (legend strokes, frame edges).
- **Min height on duplicate x (the key tie-break):** when several strokes share an x
  (e.g. a bar drawn twice, or a gridline coincident with a bar), the **shortest** is
  taken so that a spurious over-tall stroke can't inflate the reported intensity.
  *(This is the behaviour the paper's §3.1 now documents as "minimum height".)*

### `process_pdf_vector(pdf_path, write_visual, stem, pdf_bytes, visuals_dir, calib_graph, calib_dir)`
**Purpose:** the full Tier-2 path for one PDF.
**Returns:** `(results, max_mz, mz_warnings)`. **Raises** on any extraction failure
(so the caller can fall back to raster).

Steps:
1. Open the doc (from in-memory `pdf_bytes` if provided — workers never touch disk),
   take page 0, `get_drawings()`. Raise if there are no drawings.
2. `_collect_line_segments`; raise if fewer than 5 segments (not a real chart).
3. `_find_baseline_and_yaxis`; raise if axes not found.
4. `_calibrate`; raise if either calibration is missing (need ≥2 x-ticks and a "100"
   label).
5. `_extract_bars` → `raw_bars`.
6. Convert bar tops to `(x, tip_y)` and run `_correct_mz_with_labels` to get the
   label-anchored `mz_map` (+ any warning). Optionally save the calibration graph.
7. Build `seen_mz`: for each bar, `mz = mz_map[x]`, `intensity = min(h/pixels_per_pct, 100)`,
   and on m/z collision keep the **minimum** intensity (consistent with the
   min-height tie-break).
8. **Base-peak renormalisation:** if the max intensity is below 100, add the constant
   `δ = 100 − max` to every peak (clamped at 100). This corrects the small fixed
   under-read caused by the "100" label's text-box centre sitting just above the true
   100% gridline, restoring the base peak to exactly 100% as a spectrometer reports.
9. Compute `max_mz` from the axis right end (the highest plotted mass).
10. If `write_visual`, render the page at 600 DPI and overlay the detected axes
    (red x, blue y), origin (green dot), right edge (orange), tick boxes, the 100%
    marker, and every bar-top (red dots) — the debug image used in spot-checks.
11. Close the doc, return `(results, max_mz, mz_warnings)`.

**Decisions:**
- **Raise-to-fall-back** — each precondition that can't be met throws, and
  `process_pdf` catches it to try the raster tier. Failure is explicit, never silent.
- **`pdf_bytes` path** — enables the driver's "read once in main thread, compute in
  workers" design (no per-worker disk I/O).
- **Additive renorm** — chosen over multiplicative because the error is a *fixed
  offset* from the label's centre, not a proportional scale error; it is applied
  uniformly to all bars and skipped when the max already meets 100%.

---

## 3.3 Tier 1 — raster fallback

### `process_pdf_raster(pdf_path, write_visual, stem, pdf_bytes, visuals_dir, calib_graph, calib_dir)`
**Purpose:** extract bars from a *rendered image* when no vector layer exists.
**Returns:** `(results, mz_warnings)`.

Steps:
1. Render page 0 to a pixmap at `RASTER_DPI=600`; reshape to a NumPy image; convert
   straight to greyscale (skipping a BGR detour for speed).
2. **Binarise with Otsu** (`THRESH_OTSU`) — picks an optimal black/white threshold
   per document, robust across differently-rendered PDFs. `inv` is the ink mask.
3. **x-axis row:** sum ink along rows; within the central band `[h/50, h−h/50]`
   (excludes top/bottom borders), take the **lowest** row whose ink ≥ 70% of the
   max-row ink. (Lowest, because the baseline is the bottom-most heavy horizontal.)
4. **y-axis column:** sum ink along columns; in the **left half** `[w/50, w/2]`, take
   the **leftmost** column whose ink ≥ 30% of the max — the y-axis.
5. Compute `scale_x/scale_y` (image px per PDF unit) for projecting text words into
   pixels.
6. **Tick labels:** same idea as the vector calibrator — numeric words just below the
   axis, narrow boxes only, sorted by x, then kept to the top row of labels
   (`c[1] ≤ min_y + 30`). `np.polyfit` → `pixels_per_unit`, `scale_intercept`.
7. **"100" label** left of the y-axis → `pixels_per_pct`.
8. **Find the axis's true right end:** walk the baseline row rightward from the
   y-axis, tolerating small anti-aliasing gaps (`gap_tol = max(3, w/500)`), stopping
   where the black run ends. *Why:* the highest-mass bars (which can include the base
   peak) often sit past the last *labelled* tick, so scanning only to the last tick
   would silently drop them.
9. **Pick a scan row just above the baseline:** at 600 DPI the baseline is several
   pixels thick; if you scan a row still inside it, the whole baseline reads as one
   giant bar. Walk upward past any near-solid row (`> 0.5·w` black) to sample where
   only real bars cross.
10. **Grid-aware bar detection (the key raster idea):** instead of taking one peak
    per contiguous black run, sample the bar height at **each integer-m/z column**
    `c = intercept + mz·ppu`, searching a small window `±half` (just under half the
    m/z pitch) for the tallest bar and measuring its height with `_bar_height` (walk
    up from the scan row while pixels are black). *Why:* when pixels-per-m/z is small
    (wide-range/high-mass spectra) adjacent bars touch; a contiguous-run method would
    merge them, dropping peaks and mis-centring others. Sampling on the calibrated
    grid gives every integer mass its own reading; the sub-half-pitch window catches a
    slightly off-centre bar without bleeding into its neighbour.
11. **Label-anchored m/z correction:** same `_correct_mz_with_labels` as the vector
    tier, but in **image-pixel coordinates** (passing `scale_x/scale_y` so PDF-unit
    text words are projected into pixels). If it anchors, remap intensities onto the
    corrected masses (min on collision).
12. **Base-peak renormalisation:** identical additive `δ` correction as the vector
    tier.
13. Optionally write the overlay PNG; close; return.

**Decisions:**
- **600 DPI** — high enough that dense bars don't merge during rasterisation.
- **Otsu** — parameter-free, document-adaptive thresholding.
- **Projection profiles for axes** — summing ink along rows/columns is a classic,
  robust way to find the dominant horizontal/vertical structures.
- **Scan-to-axis-end + scan-row-above-baseline + grid sampling** — three fixes that
  together stop the raster tier from (a) dropping high-mass bars, (b) reading the
  thick baseline as a bar, and (c) merging touching bars.
- **Same calibration/renorm code as vector** — the two tiers deliberately share the
  m/z-correction and normalisation logic so their outputs are directly comparable.

---

## 3.4 Compound name & validation helpers

### `_extract_compound_name(pdf_path, pdf_bytes)`
**Purpose:** read the compound name from the PDF's first text line.
**Returns:** the cleaned name string.

- Take the first non-empty text line of page 0.
- Strip a library prefix `"(mainlib) Name"` → `Name`, or a `"Name: ..."` NIST-text
  prefix → the name. Otherwise return the line verbatim.

**Decision — two prefix formats** cover both the rendered-PDF style (`(mainlib) …`)
and the text-export style (`Name: …`).

### `_validate_result(results)`
**Purpose:** per-spectrum physics/sanity checks.
**Returns:** a list of human-readable warning strings (empty = clean).

Checks: empty result; base peak `< 99.0%` (y-calibration drift); any m/z `< 2` or
`> 2000` (x-calibration misread); intensity outside `(0, 100.1]`; fewer than 3 peaks
(scanner missed the chart); duplicate m/z (calibration off by < 0.5 mass unit).

**Decision — each check maps to a specific, diagnosable failure mode**, so a warning
tells you *what* went wrong, not just *that* something did. *(Note: the base-peak
threshold here is 99.0; the paper's prose rounds this to "below 99.5%" — a minor
wording gap worth aligning.)*

### `_compare_pipelines(v_results, r_results)`
**Purpose:** cross-check the vector vs raster outputs for one file (used by
`--cross-validate`).
**Returns:** warning strings.

- Warn if the raster pipeline found nothing.
- Warn if peak counts differ by `> 20%` of the larger count.
- Compare m/z sets with a **±1 tolerance** (raster pixel imprecision); warn if more
  than `max(1, 10%)` of either side's masses are unique to that side.

**Decision — ±1 tolerance and 10%/20% bands** — small disagreements are expected
(rasterisation loses a low peak here and there); only *systematic* divergence is
flagged.

---

## 3.5 Unified entry point & parallel driver

### `process_pdf(pdf_path, cross_validate, write_visual, pdf_bytes, visuals_dir, method, calib_graph, calib_dir)`
**Purpose:** the single per-file entry the worker pool calls. Dispatches tiers,
validates, optionally cross-validates, and returns a result dict. **Never writes
JSON itself** — the main process owns serialisation.
**Returns:** a result dict (`ok: True` with peaks, or `ok: False` with the error).

Steps:
1. Record `t0`; extract the compound name.
2. Dispatch by `method`:
   - `"raster"` → raster only.
   - `"vector"` → vector only.
   - `"auto"` (default) → **try vector; on any exception, fall back to raster** and
     set `method = "raster"`.
3. `_validate_result(results)` + append any m/z-correction warnings.
4. If `cross_validate` and the vector path was used, run raster too and append
   `_compare_pipelines` warnings (best-effort; a raster failure here is itself a
   warning).
5. Return `{file, name, bar_count, peaks, warnings, method, ok: True, time}`.
6. Any unhandled exception → return an `ok: False` record carrying the error string,
   so one bad file never crashes the batch.

**Decisions:**
- **try-vector-then-raster** realises the two-tier design: the fast exact path is
  default, the robust path is automatic backup.
- **Workers return dicts, never write** — all I/O is centralised in the driver,
  which is what makes the parallel speedup possible (see below).
- **Total per-file isolation** (one `try/except`) — the batch is fault-tolerant.

### `_bounded_submit(ex, pdf_files, cross_validate, write_visual, max_inflight=64, ...)`  *(generator)*
**Purpose:** feed the worker pool while bounding memory, and stream completed
futures back as soon as they finish.
**Yields:** completed `Future` objects in completion order.

Steps:
1. Maintain a `pending` dict of in-flight futures.
2. Before submitting a new file, if `len(pending) ≥ max_inflight`, **block on one
   completion** (`next(as_completed)`), yield it, and drop it — capping the window.
3. Read the next PDF's bytes **sequentially in the main thread**, submit
   `process_pdf` with those bytes.
4. After the loop, drain the remaining futures.

**Decisions:**
- **Sequential byte preloading in the main thread** — the disk does **one sequential
  stream** instead of 22 workers issuing random concurrent reads; the OS readahead
  prefetches contiguous blocks.
- **Bounded window (`max_inflight=64`)** — caps peak RAM at ≈ `64 × mean_pdf_size`
  while keeping the pool saturated. Unbounded submission would read the whole corpus
  into memory.
- **Yield in completion order** — lets the driver stream results to disk before the
  batch finishes (output is readable live).

### `main()`
**Purpose:** the CLI driver — parse args, gather PDFs, run the pool, stream JSONL.

Steps:
1. Parse args: `--input`, `--workers`, `--pool` (thread/process), `--cross-validate`,
   `--method`, `--visual`, `--calib-graph`, `--per-file-json`, `--output`,
   `--subdir`, `--frange`, `--list-folders`.
2. Resolve `--input` even if relative (try repo root, `data/`, `data/samples/`).
3. `--list-folders` → print each subfolder, its F-number, and PDF count, then exit.
4. `_gather_pdfs(...)` → the work list (see below). Error if empty.
5. `write_visual` is on for a single file or when `--visual` is passed (you don't
   want 50k overlay PNGs by accident).
6. Optional `--subdir` routing for outputs/visuals (e.g. `--subdir auto` names the
   subfolder after the input folder).
7. `workers = --workers or min(n_files, cpu_count)`.
8. Choose `ProcessPoolExecutor` or `ThreadPoolExecutor` from `--pool`.
9. Open **one** JSONL output with a 1 MB buffer; wrap the work in a `tqdm` bar.
10. For each completed future from `_bounded_submit`: write its dict as one JSONL
    line; optionally also write a per-file JSON (compat with `sanitycheck.py`);
    tally vector/raster/warn/fail; print a line for hard failures.
11. Print the final summary (counts, throughput PDFs/s, output path) and a hint to
    run `sanitycheck.py` if anything was flagged.

**Decisions:**
- **One open JSONL, buffered, written by the main thread** — eliminates the
  per-file open/allocate/close NTFS churn (and Windows Defender per-file scans) that
  dominates wall time at corpus scale.
- **`--pool` default is `thread`** in the code today; the paper recommends `process`
  for real speedup (threads are GIL-bound on the Python result-collection/JSONL
  phase). *(Worth aligning the default to `process`.)*
- **`--subdir`/`--frange`/`--list-folders`** make it practical to process selected
  F-folders of the large corpus without moving files around.

### `_folder_fnum(path)`, `_parse_frange(spec)`, `_gather_pdfs(input_path, frange_spec)`
Support the corpus's `F<n>`-numbered folder layout.

- **`_folder_fnum`** — pulls the integer from a folder name like `Spectrum F21 06-10-23` via `\bF(\d+)\b`. Returns `None` if there's no F-number.
- **`_parse_frange`** — turns `"21-40"`, `"21,25,30-35"`, or `"21"` into a set of ints (ranges expanded). Lets you benchmark/extract a slice of the corpus.
- **`_gather_pdfs`** — resolves the input into a sorted PDF list:
  1. A single `.pdf` → just that file.
  2. A folder containing `F<n>` subfolders → the PDFs from folders whose F-number is in `frange` (all of them if no range). **This is why `--input data/samples` processes the NIST F-folders but excludes `IISc_Data`** (which has no F-number).
  3. A plain folder → every `*.pdf` recursively.

**Decision — F-number-aware gathering** matches how the rendered NIST corpus is
physically organised (F1…F50), so range selection is a first-class feature.

---

# STAGE C — Validation & scoring

## 4. `sanitycheck.py` — automated QC report on per-file JSON

Reads every `outputs/*.json` and runs a battery of checks, then writes a 7-section
human report. This is the **qualitative** counterpart to the quantitative cosine
comparison.

### Module constants
`SPOT_N=15` (files to flag for manual review), `BASE_PEAK_TOL=0.5` (base peak must
be ≥ 99.5%), `MZ_MIN=1`, `MZ_MAX=2000`, `MIN_PEAKS=3`, `OUTLIER_SIGMA=6.0`.

### `_Tee`
A tiny class that writes every line to **both** the console (ASCII-sanitised with
`encode("ascii","replace")` so a legacy Windows console never crashes on a special
glyph) **and** a UTF-8 report file. `__enter__/__exit__` make it usable as a context
manager.

### `_mean(values)` / `_std(values)`
Plain sample mean and **sample** standard deviation (divides by `n−1`; returns 0 for
fewer than 2 values). Used for the batch outlier z-scores.

### `_load_json(path)`
Loads one JSON file; returns `(data, None)` on success or `(None, error_string)` on
failure — so a corrupted file becomes a reported `LOAD` issue rather than a crash.

### `_check_file(name, data)`
**Purpose:** run all per-file checks; **returns** a list of issue strings (empty =
clean). The order is deliberate — cheap structural checks first, then physics:

1. **Schema** — required keys (`peaks`, `warnings`, `method`); `peaks` must be a list
   (bail out early if not — nothing else is meaningful).
2. **Failed extraction** — if `ok is False`, emit `FAILED` with the recorded error and stop.
3. **Peak entry types** — each peak is a dict with `mz` and `intensity`.
4. **Completeness** — empty → `EMPTY` (stop); fewer than `MIN_PEAKS` → `SPARSE`.
5. **Physics: base peak** — `< 99.5%` → calibration drift; `> 100.1%` → clamp failed.
6. **Physics: intensity range** — any value `≤ 0` or `> 100.1` flagged.
7. **Physics: m/z range** — any `< MZ_MIN` or `> MZ_MAX`, and any negative m/z.
8. **Consistency: duplicate m/z** — a unit-resolution instrument has one bar per mass.
9. **Consistency: m/z order** — must be ascending (else an ordering bug).
10. **Extractor warnings** — surface any `warnings` the extractor itself recorded,
    **except** it suppresses "out-of-range m/z" warnings whose listed values now
    actually fall inside `[MZ_MIN, MZ_MAX]` (the authoritative physics check above
    already covers the range; this avoids double-reporting stale warnings).

**Decision — tags as a vocabulary** (`SCHEMA`, `FAILED`, `EMPTY`, `SPARSE`,
`PHYSICS`, `DUPE`, `ORDER`, `WARN`, `OUTLIER`): the first word of each issue is a
machine-groupable tag, which Section 3 and the alert/warning split rely on.

### `_find_outliers(stats_by_file)`
**Purpose:** flag files whose **peak count** is more than `OUTLIER_SIGMA=6` standard
deviations from the batch mean.
**Returns:** `{filename: [descriptions]}`.

- Needs ≥3 files and non-zero variance to act.
- Computes a z-score per file; `z > 6` → an `OUTLIER` description noting high/low.
- **Max m/z is deliberately not flagged** here — the physics range `[2,2000]` already
  bounds valid masses, so a high max-m/z is legitimate, not an outlier.

**Decision — 6σ** is intentionally conservative: it catches gross detection failures
(e.g. 5 peaks when the mean is 45) without nagging about normal variation.

### `validate(outputs_dir, report_path, spot_n)`
**Purpose:** orchestrate the whole report.

Steps:
1. Glob `outputs/*.json` (exit if none).
2. **Pass 1** — per file: load, `_check_file`, and record stats (`n_peaks`, `max_mz`,
   `base_peak`, `method`); tally method usage.
3. **Pass 2** — `_find_outliers` over the collected stats; merge into each file's issues.
4. **Categorise** every file into: `clean`, `failed` (has `FAILED`/`LOAD`), `warned`
   (has at least one non-alert issue), or `alert`-only (issues are all `OUTLIER`/`SPARSE`).
   `ALERT_TAGS = {OUTLIER, SPARSE}` defines what counts as merely informational.
5. Emit seven sections:
   - **1 Summary** — counts, per-method usage, batch peak-count and max-m/z stats.
   - **2 Files with issues** — failed first, then warnings, then alerts, each with its stats and issue list.
   - **3 Issue-type breakdown** — counts per tag, split into "action required" vs "informational".
   - **4 Spot-check sample** — a reproducible (`random.seed(42)`) sample: prioritise
     failed/warned/alert files, then fill with random clean files up to `spot_n`,
     listing each alongside its JSON and visual PNG path.
   - **5 What to check** — a manual checklist for reading the overlay PNG and JSON.
   - **6 Why each check catches a real error** — the physical/chemical reasoning behind every rule.
   - **7 Next steps** — concrete commands to investigate failures/warnings and regenerate visuals.
6. Final pass/fail line with both the strict clean rate and an "effective" rate that
   counts alert-only files as passing; a one-line verdict by threshold (≥90% good,
   ≥70% moderate, else poor).

**Decisions:**
- **Alert vs warning split** — distinguishes "informational" (sparse/outlier, often
  legitimately dense or simple spectra) from "you should act on this," so the
  headline pass rate isn't dragged down by benign flags.
- **Reproducible spot-check (`seed(42)`)** — the same sample every run, so manual
  review is repeatable.
- **Two-pass design** — outlier detection needs the whole batch's statistics, which
  only exist after pass 1.

---

## 5. `compare_peaks.py` — cosine fidelity vs the NIST reference

Scores each extracted spectrum against its NIST library entry by cosine similarity —
the headline correctness metric. (This is the script that produces the 99.99% number.)

### Name canonicalisation: `canon(name)`
**Purpose:** normalise compound names so the extractor's name and the NIST name match
despite formatting differences.
**Returns:** a canonical string (or `None`).

Steps: lowercase → replace NIST's dotted Greek notation `.alpha.` → `alpha`
(`_NIST_RE`) → replace literal Greek glyphs `α` → `alpha` (`_GLYPH`/`_GLYPH_RE`) →
collapse runs of whitespace → strip trailing spaces/dashes/dots.

**Decision — Greek handling both ways:** the same letter appears as `.alpha.` in NIST
text exports and as `α` in rendered PDFs; normalising both to `alpha` makes the names
join. Trailing-punctuation stripping handles `Name -` vs `Name`.

### `results_peaks(peaks)`
**Purpose:** turn the extractor's peak list into a `{mz: intensity}` dict.
- Rounds m/z to int; on collision keeps the **maximum** intensity.

*(Note: the extractor already emits one peak per integer m/z, so this dedup is
effectively a safety net. It keeps max here — the comparison side — independent of
the extractor's internal min-on-collision tie-break.)*

### `reference_peaks(peaks)`
**Purpose:** turn a NIST reference list into a comparable `{mz: intensity}` dict on a
0–100 scale.
Steps:
1. `div = 10 if all m/z are multiples of 10 else 1`, then `mz // div`. This undoes a
   known NIST quirk where some exported masses are ×10.
2. On collision keep the max.
3. Normalise so the largest intensity becomes 100 (NIST raw intensities are 0–999).

**Decision — the `div`-by-10 heuristic** is a targeted fix for a specific export
scaling artefact; it only triggers when *every* mass is a multiple of 10 (otherwise
it's a no-op), which is the signature of the quirk.

### `cosine(a, b)`
**Purpose:** cosine similarity between two `{mz: intensity}` spectra.
**Returns:** a float in `[0, 1]`.

- `dot = Σ a[m]·b[m]` over **shared** masses only.
- Norms `‖a‖, ‖b‖` over **all** masses of each spectrum.
- `0` if there's no overlap or either norm is 0.

**Decision — dot over the intersection, norms over the full vectors:** this is the
standard mass-spectral library-search cosine. Masses present in only one spectrum
contribute nothing to the dot product but **do** enlarge that spectrum's norm, so
missing/extra peaks correctly *lower* the score.

### `spectrum_differences(extracted, reference)`
**Purpose:** itemise *why* two spectra differ.
**Returns:** `(only_extracted, only_reference, intensity_diff)` — masses unique to
each side, and shared masses whose intensities differ (with the signed delta). Used
to populate the detailed-differences section of the report.

### `main()`
1. Open the report file and **redirect `stdout` into it** so all the `print`s below
   are captured (restored at the end). Paths are configurable via `--results`,
   `--reference`, `--out`, `--report`.
2. **Load extracted** spectra into `{canon(name): (original_name, results_peaks)}`,
   skipping `ok: False` records and counting duplicate canonical names (kept: first
   wins).
3. **Stream the NIST reference**, and for every reference whose canonical name is in
   the extracted set, compute the cosine and difference details, **keeping the best
   (highest) score per compound** (a name can appear multiple times in NIST; we credit
   the best-matching reference entry).
4. Write a CSV sorted by ascending cosine (worst first).
5. Print the summary: bucket counts (`≥0.99`, `[0.95,0.99)`, `[0.90,0.95)`, `<0.90`),
   base-peak agreement rate, the compounds whose peak counts differ, the worst 15
   matches, and a detailed per-compound difference dump for every imperfect match.

**Decisions:**
- **Best-score-per-name** — NIST has duplicate names (isomers, replicate spectra);
  scoring against the best avoids penalising the extractor for a reference ambiguity
  it can't resolve.
- **Stream the reference, hash the extracted** — the extracted set (~50k) fits in
  memory keyed by name; the reference (~267k) is streamed, so peak arrays for
  non-matching compounds are never held.
- **stdout-redirect pattern** — lets the same `print` statements serve console and
  file without threading a writer through every helper.

---

## 6. `intersect.py` — name overlap between extracted and reference

A small diagnostic (runs at import time, no `main()`): how many extracted compounds
have a NIST reference, and how many don't.

Steps:
1. Re-defines the **same `canon()`** as `compare_peaks.py` (kept in sync deliberately
   so the matching is identical).
2. Build `nist_keys` — a set of canonical names from `NISTds.jsonl`, **names only**,
   loading no peak arrays (tiny memory).
3. Walk `results.jsonl` (skipping `ok: False`); each canonical name either lands in
   `common` (a set, so duplicates collapse) or `unmatched` (a list, to preserve every
   instance).
4. Print the two counts.

**Decision — names-only reference set** is the whole point: it answers the coverage
question (how much of the extracted corpus can even be scored) at negligible memory
cost. The 48,576 "matched" figure in the paper comes from this kind of intersection.

---

## 7. `benchmark.py` — performance and vector-vs-raster comparison

Two distinct jobs in one script: (1) measure parallel speedup across worker counts
and pool types, and (2) score vector-vs-raster agreement per file (the in-house
cross-pipeline check). Plotting helpers are skipped gracefully if matplotlib is
absent (`_HAS_PLOT`).

### Console/file output: `_safe_console_print(line)` and `_Tee`
- **`_safe_console_print`** — `print()` that never crashes on a non-ASCII glyph under
  a legacy (cp1252) Windows console: on `UnicodeEncodeError` it re-encodes with
  `replace` so `×`, em-dashes, and arrows degrade to `?` instead of aborting a long
  benchmark. The UTF-8 report file keeps the originals.
- **`_Tee`** — writes each line to console (via the safe printer) and to a UTF-8 file.

### Math helpers
- **`_amdahl(f_par, p)`** — Amdahl's Law speedup `1 / ((1−f) + f/p)`.
- **`_estimate_f_par(T1, Tp, P)`** — invert Amdahl at one measured point to estimate
  the parallel fraction `f`, clamped to `[0,1]`. **This is a single-point (endpoint)
  estimate, not a least-squares fit** — the paper's text and Fig. 4 caption were
  updated to say exactly this.
- **`_ascii_bar(value, ceiling, width)`** — a text bar (`█`/`░`) for the console report.

### Workers (top-level so `ProcessPoolExecutor` can pickle them)
- **`_worker_new(args)`** — times `process_pdf` from `imgprocess.py` on one
  `(path, pdf_bytes)`; no disk writes, pure CPU.
- **`_worker_old(args)`** — same for the legacy `final.py` (lazy-imported), which
  *does* write a per-file JSON — the baseline the `--compare` head-to-head measures.

### `_run_trial(pdf_files, n_workers, pdf_bytes_cache, pool_cls, worker_fn)`
Runs one timed trial: builds `(path, bytes)` args from the cache, opens the pool, maps
the worker over all files, returns `(wall_clock_seconds, [per_file_seconds])`. Bytes
come from a pre-warmed cache so disk I/O is excluded from the timing.

### Report writers
`_write_report_header`, `_write_metrics_table` (S=T1/Tp, E=S/P, Amdahl S_th per worker
count, plus the key summary with the estimated f and ceiling), `_write_throughput`
(PDFs/s with ASCII bars), `_write_comparison` (imgprocess vs final.py head-to-head and
the I/O explanation), and `_write_analysis` (three findings: sub-linear speedup via
Amdahl, efficiency decay with causes ranked, and the marginal-return "sweet spot").
These only format already-measured numbers.

### Plot helpers
`_plot_amdahl`, `_plot_efficiency`, `_plot_file_times`, `_plot_comparison`, and the
four `_plot_pools_*` overlays (speedup, efficiency, throughput, per-file-time box
plots for thread vs process). All write PNGs into `graphs/`. `pools_speedup.png` is
Fig. 4 in the paper.

### `benchmark(pdf_folder, worker_counts, runs, pool_type, compare)`
**Purpose:** the main timing routine for one pool type.
Steps:
1. Gather PDFs; pick a default worker ladder if none given (1, 2, 4, … up to
   cpu//2 and cpu_count); clamp counts to ≤ n_files.
2. **Pre-load all PDF bytes once** into a cache (parallel reads) so every trial times
   pure compute.
3. For each worker count, run `runs` trials, record the average wall time and the
   per-file times.
4. Optionally run the `final.py` baseline trials for `--compare`.
5. Estimate `f_par` from the endpoint, write the metrics/throughput/analysis
   sections, and render the plots.
6. Return `(timings, all_file_times, f_par, worker_counts, n_files)` so the
   `--compare-pools` caller can build overlays.

**Decisions:**
- **Bytes pre-cached** — isolates *parallel scaling* from disk speed; the benchmark
  measures the pool, not the SSD.
- **3 trials averaged** — smooths run-to-run jitter.
- **Endpoint f estimate** — simple and transparent; the curve is an Amdahl
  *prediction* anchored at the largest worker count, which the paper now states.

### Input gathering: `_resolve_input`, `_gather_input_pdfs`, `_display_base`
- **`_resolve_input(item)`** — make a path work regardless of CWD by trying it as
  given, then under repo root, `data/`, and `data/samples/` (mirrors `imgprocess.py`).
- **`_gather_input_pdfs(inputs)`** — accept one path or a **list**; recurse folders;
  dedup by resolved path so the same file passed via two overlapping folders is
  processed once. (This is what let us pass the 23 unique gas folders, excluding the
  duplicate `Mathane_UHP`, to re-score on 68.)
- **`_display_base(inputs)`** — common parent directory of the inputs, for readable
  relative paths in the report (handles different drives gracefully).

### Vector-vs-raster comparison
- **`_extract_both(pdf_path, pdf_bytes)`** — run **both** pipelines directly (not the
  auto-fallback `process_pdf`), capturing each tier's peaks, time, and any error
  independently. *Why direct calls:* the point is to see what *each* method produces,
  not which one the dispatcher would have chosen.
- **`_compare_one(pdf_path, base)`** — read the bytes once, run both tiers, convert to
  `{mz:intensity}` via the **shared** `results_peaks`, score with the **shared**
  `cosine` (so vector-vs-raster is scored exactly like extractor-vs-NIST), and report
  base-peak agreement. Returns a row dict.
- **`compare_methods(inputs, csv_path, workers)`** — run `_compare_one` over all PDFs
  in a **thread pool** (fitz/cv2/numpy release the GIL during their C work, so threads
  overlap), preserving input order via `ex.map`. Writes a per-file table + summary
  (bucket counts, mean/min cosine, base-peak rate), separating files where one or both
  tiers were empty (excluded from the cosine, since cosine needs both sides). Optional
  CSV. **This is the function that generated the 68-file `compare_vr_iisc.txt`.**

**Decisions:**
- **Reuse `cosine`/`results_peaks` from `compare_peaks.py`** — the vector-vs-raster
  agreement is measured with the identical metric used against NIST, so the two
  numbers are comparable.
- **Exclude both-empty / one-empty files from the cosine** — cosine is undefined/zero
  without two non-empty spectra; reporting them separately keeps the mean honest.

### Entry point (`__main__`)
Dispatches on flags: `--compare` → `compare_methods`; `--compare-pools` → run
`benchmark` for **both** thread and process pools then draw the overlay graphs;
otherwise a single `benchmark` run for the chosen `--pool`.

---

# STAGE D — Storage

## 8. `build_db.py` — load results into DuckDB

### `build()`
**Purpose:** create `spectra.duckdb` from `results.jsonl` for SQL/ML access.
Steps:
1. Error out if `results.jsonl` is missing (run the extractor first).
2. Connect (creates the DB file).
3. `DROP TABLE IF EXISTS spectra` — makes the build **idempotent** (re-running
   rebuilds from scratch).
4. `CREATE TABLE spectra AS SELECT … FROM read_json(..., format='newline_delimited')`
   — DuckDB infers the nested schema directly from the JSONL; `peaks` is stored as a
   nested `LIST<STRUCT(mz, intensity)>`, and a `row_number()` provides a surrogate
   `id`.
5. `CREATE VIEW peaks_long` — a **flat, one-row-per-peak** view via `UNNEST(peaks)`,
   convenient for ML feature extraction (`spectrum_id, compound, method, mz, intensity`).
6. `CREATE INDEX idx_spectra_name` for fast compound lookups.
7. Print spectra/peak counts and the DB path.

**Decisions:**
- **Keep `peaks` nested in storage, expose a flat view** — nested storage is compact
  and lossless; the `peaks_long` view gives a tidy/long table for analysis without
  duplicating data.
- **Idempotent rebuild** — the DB is a derived artefact, always reproducible from the
  JSONL.

---

## Appendix — cross-cutting design principles

These recur across the whole codebase and explain many local choices:

1. **Vector before raster.** Exact geometry beats re-detected pixels; raster is only
   the safety net. Every vector precondition that fails *raises*, and the dispatcher
   falls back — failure is explicit, never silent.
2. **Workers compute, the main thread does I/O.** PDFs are read once, sequentially,
   in the main thread and passed as bytes; results come back as dicts and are written
   as one streamed JSONL. This is what makes the process-pool speedup real and keeps
   memory bounded.
3. **Robust over clever.** Theil–Sen (not least squares) for the label fit; Otsu (not
   a fixed threshold) for binarisation; medians and tolerances throughout — so one bad
   stroke, tick, or pixel can't corrupt a result.
4. **Every check maps to a diagnosable cause.** The validation warnings and the
   sanity-check report don't just say "bad"; each rule corresponds to a specific
   physical/geometric failure (y-calibration drift, x-tick misread, merged bars, …).
5. **Shared metric, comparable numbers.** The same `cosine`/`results_peaks` score the
   extractor against NIST *and* vector against raster, so the 0.99-class numbers mean
   the same thing in both experiments.
6. **Reproducibility & idempotence.** Fixed random seeds, rebuild-from-scratch DB,
   resume-by-file-existence export, non-destructive defaults — re-running any stage is
   safe and deterministic.
