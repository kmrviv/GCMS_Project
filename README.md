# GCMS Spectrum Extractor

A deterministic pipeline for extracting and validating structured peak data
(m/z + intensity) from gas chromatography–mass spectrometry (GC-MS) spectrum
PDFs at scale.

## Abstract

Large archives of GC-MS results exist only as rendered PDF charts, which are
not machine-readable. We present a deterministic, training-free pipeline that
digitizes these charts into structured, validated peak lists. A two-tier
extractor reads the PDF vector layer directly when available (~6 ms/file) and
falls back to image-based raster analysis otherwise. Every output passes a
seven-layer physics-and-consistency validator, and the method as a whole is
validated against the NIST reference library: across 48,576 extracted spectra,
**48,574 (100.0%) reached cosine similarity ≥ 0.99** with their NIST reference
spectra, providing large-scale empirical evidence that the extraction is
correct. The result is a clean, labeled spectral dataset. This work is a
precursor to machine-learning models trained on the resulting dataset, which
are the subject of future work.

## Future Work

- Train ML models on the extracted dataset for compound identification and
  spectral prediction.


## Validation

Correctness was established empirically rather than by spot-checking. The two
extraction pipelines are validated **independently**, since they reach 100% of
files for different reasons.

### Prong 1 — Vector pipeline vs NIST (large-scale)

Each spectrum extracted by the vector pipeline was compared against its NIST
reference spectrum using cosine similarity (`compare_peaks.py`):

| Cosine similarity to NIST | Compounds | Share |
|---------------------------|-----------|-------|
| ≥ 0.99                    | 48,574    | 100.0% |
| 0.90 – 0.99               | 0         | 0.0%   |
| < 0.90                    | 2         | 0.0%   |
| **Total compared**        | **48,576**| —      |

Base-peak m/z agreed for 48,574 / 48,576 (100.0%) of compounds. The 2 outliers
are pathological under-extraction cases on large/complex molecules, not
calibration errors: *2-Acetamido-5-p-tosylamido-p-benzoquinone* (cosine 0.065,
2 of 93 reference peaks recovered) and *Benzo[g]pteridine, 2,4-diamino-6,7,8,9-
tetrahydro-7-methyl-* (cosine 0.278, 72 of 125). Separately, `sanitycheck.py`
ran per-file physics/consistency checks on 49,999 outputs: 49,946 passed clean,
0 failed (33 warnings, 20 informational alerts), with 99.9% handled by the fast
vector pipeline.

> Scope note: the NIST set used here is the same library the sample PDFs were
> rendered from, so this validates *faithful recovery of the data in each PDF*,
> not generalization to arbitrary unseen instruments — the appropriate claim for
> this precursor stage.

### Prong 2 — Raster pipeline on real instrument data (hand-validated)

The NIST comparison above only exercises the vector path. To validate the
**raster** pipeline, it is run on 71 real GC-MS spectra from IISc
(`data/samples/IISc_Data/`, ~24 gas samples: air, Ar, CH₄, CO₂, CO, He, N₂, O₂,
…). All 71 fall through to the raster path, isolating it cleanly. These have no
NIST counterpart, so each spectrum is cross-validated by running **both**
pipelines and scoring their agreement with the same cosine metric.

On the 71 IISc spectra the raster output agrees with the vector output at
**mean cosine 0.9814** (min 0.7051), with base-peak m/z agreement on 66 / 71
(93.0%). As a broader internal-consistency check, the two pipelines were also
compared across the full 50,078-PDF corpus: **50,045 / 50,046** scored files
reached cosine ≥ 0.99 (mean 1.0000), with base-peak agreement on 50,019 / 50,078
(99.9%). See `reports/compare_vr_iisc.txt` and `reports/final_compare_summary.txt`.

## How it works

A **two-tier pipeline**:

1. **Vector extraction** (fast, ~6 ms/file) — reads the PDF's vector drawing layer
   directly with PyMuPDF. Used for clean digital PDFs.
2. **Raster fallback** (~200–500 ms/file) — rasterizes the page and uses OpenCV
   Otsu thresholding to find bars. Used when the vector layer is missing.
3. **Text fallback** — parses NIST "10 largest peaks:" text if both above fail.

Output is streamed to a single JSONL file (one spectrum per line).

## Setup

```bash
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt
```

## Quick start (runs on the included sample data)

The repo ships the 71 real IISc GC-MS spectra in `data/samples/IISc_Data/`
(the large NIST-rendered sample set is not included — see Notes).

```bash
# 1. Extract peaks from the included PDFs
python src\imgprocess.py --input data\samples\IISc_Data --per-file-json --visual

# 2. Validate the extraction
python src\sanitycheck.py

# 3. (optional) Build a queryable DuckDB database
python src\build_db.py
```

Outputs land in `data/processed/results.jsonl`, per-file JSON in `outputs/`,
debug overlays in `visuals/`, and the validation report in `reports/`.

## Full pipeline (with NIST comparison)

The NIST library is a **licensed commercial product and is not included in this
repo.** To run the comparison steps, supply your own `NISTds.msp`:

```bash
# Convert NIST .msp library to JSONL (one-time)
python src\msp_to_jsonl.py data\raw\NISTds.msp -o data\processed\NISTds.jsonl

# Compare extracted spectra against NIST (cosine similarity)
python src\compare_peaks.py

# Name-overlap check
python src\intersect.py
```

## Scripts

| Script | Purpose |
|--------|---------|
| `src/imgprocess.py` | Main extractor (vector → raster → text pipeline) |
| `src/sanitycheck.py` | 7-layer automated validation of outputs |
| `src/compare_peaks.py` | Cosine-similarity comparison vs NIST reference |
| `src/msp_to_jsonl.py` | Convert NIST .msp → JSONL |
| `src/build_db.py` | Build DuckDB database from results |
| `src/intersect.py` | Name-overlap between results and NIST |
| `src/benchmark.py` | Parallel-worker performance benchmark |

## Notes

- The large NIST-rendered sample PDFs, `outputs/`, `visuals/`, `final/`, `venv/`,
  and the NIST library itself are git-ignored (large / licensed / regenerable).
  Only source, docs, the paper, and the small IISc real-instrument sample set are
  tracked.
- See `explanations/` for detailed code walkthroughs and performance analysis.
- A runnable walkthrough is in [`demo.ipynb`](demo.ipynb).

## Citation

A precursor paper describing this work is in preparation. Citation details will
be added here once available. For now, please cite this repository:

```bibtex
@misc{kumar_gcms_extractor,
  author       = {Kumar, Vivek},
  title        = {GCMS Spectrum Extractor},
  year         = {2026},
  howpublished = {\url{https://github.com/kmrviv/GCMS_Project}},
  note         = {Precursor work; ML models forthcoming}
}
```

## License

Released under the [MIT License](LICENSE).
