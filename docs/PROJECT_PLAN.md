# GC-MS Mixture Identification & Quantification — Project Plan

**Goal in one line:** Given the GC-MS output of a sample, automatically determine
**which chemical components are present** — including components not present in any reference
library — and **the percentage composition of each.**

---

## 0. Executive Summary

We have already built and validated a pipeline that **digitizes GC-MS spectrum charts
(PDFs/images) into structured peak data**, proven correct against the NIST reference
library (**48,574 / 48,576 spectra at cosine similarity ≥ 0.99**). This document proposes
the next phase: turning those readable spectra into **actionable chemistry** — identifying
the components of a mixture, **including compounds absent from the reference library**, and
their percentage composition.

The machine-learning method for identifying compounds absent from the library already exists
(**DeepEI**, 2020; improved by **EI2FP**, 2024). We **use EI2FP as the engine, not the
contribution.** Our novelty is what no existing method does:

> **Identification — and quantification — of mixture components from spectra that were
> extracted from charts, made robust to extraction artifacts.**

Crucially, this can be **studied immediately** using only NIST + our own extractor (no new
instrument data required), because we can manufacture paired clean/extracted training data
ourselves (Section 8).

---

## 1. Background: What Is Already Built

A deterministic, validated **chart-to-data extraction pipeline**:

- **Input:** GC-MS spectrum charts in PDFs/images.
- **Output:** structured peak lists (m/z + intensity).
- **Validation (qualitative correctness):** every extracted spectrum compared to its NIST
  reference by cosine similarity → **48,574 / 48,576 at ≥ 0.99**. Base-peak m/z agreed for
  the same 48,574. The 2 outliers are pathological under-extraction on very large molecules,
  not calibration errors.

This is the **precursor**. It makes archived chart data machine-readable — and, as Section 8
shows, it is also the tool that generates our ML training data.

---

## 2. Problem Statement

> **From a sample's GC-MS data, identify every component present in the mixture and the
> percentage composition of each — including components not present in any reference library.**

What makes this hard:

- **Many components** — real samples (e.g., petroleum) contain dozens to hundreds.
- **Isomers** — structurally distinct compounds with near-identical mass spectra; MS alone
  cannot separate them.
- **Novel compounds** — components absent from the reference library cannot be found by
  ordinary library search.
- **Real-world / extraction noise** — spectra are imperfect, especially when recovered from
  charts.

---

## 3. Motivation (Why This Matters)

- Vast archives of GC-MS results exist **only as rendered charts** — not machine-readable.
  We have unlocked them.
- The value of *readable* spectra is *actionable chemistry*: knowing **what is in a sample
  and how much**.
- Applications: petroleum characterization, gas analysis, quality control, environmental
  and forensic unknown identification.

---

## 4. Why Library Search Is Not Enough

Ordinary cosine **library search compares a spectrum to known spectra** — so it can only
identify a compound whose spectrum is **already in the library**. A component absent from
NIST cannot be found this way.

Identifying such components requires a fundamentally different mechanism: **predicting the
molecular structure directly from the spectrum** (the fingerprint model, Section 7), then
searching a large *structure* database. This is the core capability the project depends on.

---

## 5. Related Work & Novelty Positioning

A literature scan (2020–2026) shows the core ML is mature, and most obvious improvements are
already published. We position around them honestly.

### Already done (we do NOT claim these)

| Capability | Status | Reference |
|---|---|---|
| Spectrum → fingerprint → identify compounds absent from the library | done (2020) | **DeepEI** |
| Same, but faster/more accurate single-network | done (2024) | **EI2FP** (acc. 0.91 vs 0.89, ~100× faster) |
| De novo structure generation from EI-MS | done (2025) | **MASSISTANT** (SELFIES; ~10–54% exact) |
| Deep-learning deconvolution of co-eluting GC-MS | done | **DeepResolution / DeepResolution2** |
| Deep classification of EI-MS into known classes | done (2021) | deep classification model for EI-MS |
| Noise-robust spectral CNNs via augmentation (general) | known technique | spectral-CNN data augmentation |

**Implication:** do **not** compete on DeepEI's clean-data benchmark (EI2FP already won),
on de novo generation (MASSISTANT), or on DL deconvolution alone (DeepResolution2).

### Unaddressed — and uniquely ours

No prior work addresses these, because no one else extracts spectra from charts at scale:

1. **Identification from chart-extracted / digitized spectra.** Every method above assumes
   *clean digital* spectra. Identification in the **chart-extracted regime** — and robustness
   to extraction artifacts — is unaddressed.
2. **Integrated mixture pipeline.** Deconvolution (DeepResolution2) and identification of
   library-absent compounds (EI2FP) exist *separately*; combining them to identify and
   **quantify** the components of a mixture — including the novel ones — is unaddressed.

**Our novelty lives entirely here:** the chart-extracted data regime + the integration +
quantification — not in the fingerprint method itself.

---

## 6. The Pipeline

**Input:** a mass spectrum **at every retention time** (mzML), *or* a digitized
**chromatogram + one spectrum per peak**.
**Core idea:** the GC **separates components in time** → identify each one **per peak**.

```
GC-MS data → find peaks → deconvolve each → identify each → quantify by area → components + %
```

1. **Get the data** — mzML directly, or digitize chromatogram + per-peak spectra with our
   extractor. *(Provides the time dimension that makes separation possible.)*
2. **Peak detection** — find peaks along the chromatogram → retention times of components.
   *(Tells you how many components and where; defines integration windows for quantification.)*
3. **Deconvolution** ★ — at each peak, separate co-eluting compounds → clean per-compound
   spectra (NNLS / MCR-ALS; DeepResolution2-style as a benchmark).
4. **Identify each component:**
   - **4a.** NIST cosine search → candidate (for compounds in the library).
   - **4b.** Retention-index check → resolve isomers MS alone cannot.
   - **4c.** No good match → **fingerprint model (EI2FP)** → ranked candidate structures
     (for compounds absent from the library). ***← the core ML.***
5. **Quantify (%)** ★ — each component's **peak area** × response factor → normalize → %.
6. *(optional)* **Name the mixture-type** from overall composition (e.g., "air", a product
   class). Matched against named profiles; if nothing matches, the component list is the answer.
7. **Report + confidence** — match scores; residual flags components absent from the library.

**Where the ML is:** **fingerprint model (4c)** identifies compounds absent from the library;
deconvolution (3) optionally ML-assisted.
**Requires:** the time dimension (mzML, or a digitized chromatogram + per-peak spectra). A
single mass spectrum is insufficient for a multi-component mixture — it is underdetermined and
isomers are indistinguishable without separation.

### 6.1 How quantification works (Step 5 in detail)

Quantification uses the **chromatographic peak area** — *not* mass-spectrum peak heights. The
amount of a compound is proportional to the area under its elution peak (signal integrated over
retention time as it elutes). Three steps:

1. **Integrate area** — for each component, integrate its signal over the retention-time window
   → area `Aᵢ`. Best done on an *extracted-ion chromatogram* (a characteristic m/z) to avoid
   contamination from neighbors; for co-eluters, integrate the **deconvolved** elution profile
   from Step 3.
2. **Correct by response factor** — areas are not directly comparable across compounds, because
   each responds with a different efficiency `RFᵢ`, so corrected amount ∝ `Aᵢ / RFᵢ`. Response
   factors come from **calibration standards of known concentration** (the known-composition
   validation samples in §10.1 do double duty as calibration).
3. **Normalize** — `%ᵢ = (Aᵢ / RFᵢ) / Σⱼ(Aⱼ / RFⱼ) × 100`. The percentages are the relative
   corrected areas.

**Two levels of accuracy:**

| Level | Formula | Needs | Accuracy |
|---|---|---|---|
| **Area % (semi-quantitative)** | assume `RFᵢ = 1` → `Aᵢ / ΣAⱼ` | nothing extra | quick; wrong when responses differ |
| **Calibrated (quantitative)** | measured `RFᵢ` | calibration standards | true % |

Area % is reported by default; true % follows once response factors are calibrated. From mzML
you can integrate clean per-ion areas (best precision); from a digitized chromatogram you
integrate the plotted total-signal curve (lower precision, and overlaps are harder to resolve).

---

## 7. The ML Engine: Fingerprint Prediction (EI2FP)

This is the **engine** for identifying compounds absent from the library — an existing method
we *use*, not invent.

**A molecular fingerprint** encodes a molecule's *structure* as a fixed binary vector
(~2048 bits), each bit = "does this molecule contain a particular substructure?" Similar
molecules share bits (**Tanimoto similarity**); fingerprints are computed from a structure
(SMILES) with **RDKit**.

**The model** (DeepEI / EI2FP): a neural network mapping spectrum → predicted fingerprint.

```
mass spectrum → [ neural network ] → predicted fingerprint (bit-probabilities)
```

- **Type:** multi-label classification (each bit independent), trained with binary
  cross-entropy. **EI2FP** uses a single multi-output network (vs DeepEI's ~100 models).
- **Training data:** NIST *(spectrum, structure)* pairs. Structures come from the **InChIKey
  already in our NIST `.msp` files**, resolved to SMILES via PubChem, then to fingerprints
  via RDKit.

**Identification:**

```
unknown spectrum → predicted fingerprint → rank PubChem structures by Tanimoto → top-k
```

The compound only needs to *exist as a structure* in PubChem — not as a spectrum anywhere.
**Output is a ranked shortlist + confidence, not a single certain answer.** Trains entirely
on NIST — no mzML.

---

## 8. The Novel Contribution: Extraction-Noise-Robust, Mixture-Aware Identification

This is **our** research, built on the EI2FP engine.

### 8.1 The claim

> Identification that stays accurate on spectra **recovered from charts** (where vanilla EI2FP
> degrades), extended to **mixtures** with **quantification**.

### 8.2 The key enabler: we manufacture our own paired data

Using our extractor, we generate paired **(clean, extracted)** spectra at scale, with
ground-truth structures — a dataset **no one else can produce**:

```
NIST spectrum (clean, known structure)
      │ render as a chart
      ▼
   chart image
      │ run OUR extractor (the precursor)
      ▼
extracted spectrum (with realistic extraction noise)
```

### 8.3 The experiments

1. **Quantify the problem.** Run EI2FP on *clean* vs *extracted* spectra → measure the
   identification-accuracy drop (top-1/5/10). Shows extraction noise is a real, unaddressed
   failure mode.
2. **Fix it.** Train/fine-tune EI2FP with **extraction-noise augmentation** → recover
   accuracy on extracted spectra. *(The methodological contribution: a fingerprint model
   robust to chart-digitization artifacts.)*
3. **Extend to mixtures.** Couple deconvolution with the robust fingerprint model → identify
   **library-absent mixture components** + quantify them.

Experiments 1–2 need **only NIST + our extractor** — no mzML, no waiting.

### 8.4 Characterizing the extraction noise

To make the model robust to extraction noise, we first **quantify** that noise — and we do
not have to guess it, because the extractor produces it on demand. Comparing each extracted
spectrum to its clean NIST reference yields the noise directly (the same per-peak diff the
validation already computes). It decomposes into four components:

- **Intensity error** — recovered peak height vs. true height, overall and as a function of
  peak size.
- **Peak dropout** — reference peaks the extractor missed, by peak size (expect the smallest
  peaks dropped most).
- **Spurious peaks** — peaks produced that are absent from the reference (expect mostly low
  intensity — gridlines, labels, artifacts).
- **m/z shifts** — peaks landing at ±1 of the true mass (expected rare, given the ≥ 0.99
  validation).

Aggregating these across many compounds gives a **noise profile** (e.g., "drops X% of peaks
below 5% intensity; median Y% intensity error"). That profile **quantifies the problem** for
the paper and **defines the augmentation distribution** for training the robust model — jitter
intensities, drop small peaks per the dropout curve, inject occasional low-intensity spurious
peaks, apply rare ±1 shifts.

Two ways to use it: **train directly on extracted spectra** (noise baked in, no modeling), or
**sample the measured profile to augment clean spectra** cheaply. Recommended: the direct
approach as the main study, with the characterization reported to quantify the problem.

### 8.5 Mixture-aware identification (the harder, more novel half)

Applying identification to a *mixture* is not a drop-in — it is where the genuine integration
novelty lies, because the fingerprint model was trained on clean *single-compound* spectra.
The pipeline's deconvolution (Step 3) returns a per-component spectrum, which goes to the
fingerprint model.

**The core difficulty — error propagation.** A deconvolved spectrum is *itself* noisy:
imperfect separation leaves cross-contamination from co-eluters. That compounds with
extraction noise and degrades the predicted fingerprint → worse candidates. So mixture-aware
identification must be characterized as a **chain**:

```
extraction noise → deconvolution error → fingerprint error → ranking error
```

The contribution is to **measure each link and show the chain still yields useful candidates**
— e.g., quantify how top-k retrieval degrades as the number of co-eluting components and their
overlap increase. No prior work provides this, because none couples identification of
library-absent compounds with deconvolution (DeepEI/EI2FP are single-compound; DeepResolution2
is deconvolution-then-library-search).

**Evaluation with synthetic mixtures (no mzML needed):** build controlled mixtures by summing
known NIST spectra over simulated elution profiles, so the true components are known. Vary
overlap and component count and report retrieval vs. difficulty. The *characterization* can
therefore be done before real data arrives.

### 8.6 Is the extraction noise significant enough to matter? (the key risk)

Our extraction validation is **cosine ≥ 0.99** — near-perfect *in aggregate*. The obvious
challenge a reviewer will raise: *if extraction is 99% faithful, is there room for a
noise-robustness contribution?* This must be answered head-on, not assumed.

- **Why it can still matter:** cosine is dominated by the *large* peaks; fingerprint prediction
  also depends on *small* peaks, which carry substructure information. A spectrum can be
  cosine-0.99 yet have dropped/distorted small peaks that change the predicted fingerprint.
  Section 8.4's by-peak-size analysis is exactly what tests this.
- **The experiment that decides it:** the clean-vs-extracted top-k comparison (8.3, experiment
  1). If extracted-spectrum identification is materially worse than clean-spectrum
  identification, the noise matters and the robustness contribution is real. If not, it is
  negligible for identification.
- **The two-leg strategy (de-risking):** the project does **not** rest on robustness alone.
  - *Leg 1 — robustness:* meaningful **iff** experiment 1 shows a clean-vs-extracted gap.
  - *Leg 2 — mixture integration + quantification (8.5, Section 11):* novel **regardless** of
    the noise result, because no prior work couples identification of library-absent compounds
    with deconvolution and quantification.

  If Leg 1 turns out weak, Leg 2 carries the contribution — and "extraction is faithful enough
  that identification is unaffected" is itself a **publishable, useful finding** about
  chart-digitized data.

---

## 9. Validation Strategy

- **Extraction (qualitative):** cosine vs NIST — *already done* (≥ 0.99 on 48k+ spectra).
- **Robustness (Section 8):** clean vs extracted top-k retrieval, before/after augmentation.
- **Identification model:** **scaffold split** (test compounds structurally unlike training)
  → top-1/5/10 — the honest test of identifying compounds the model has not seen.
- **Quantification:** on **known-composition** mixtures, predicted % vs true % → RMSEP, R²,
  bias, LOD/LOQ.
- **End-to-end:** real samples of **known composition** before trusting unknowns.

### 9.1 Success Criteria & Baselines

Each experiment has a defined baseline and a number that constitutes success — not open-ended
"see if it works." (Targets are starting points, to be calibrated once Phase 1 produces the
first clean-data results.)

| Experiment | Baseline to beat | Success criterion (illustrative) |
|---|---|---|
| Reproduce identification on clean NIST | cosine library search | match published EI2FP top-k (sanity check) |
| **Extraction robustness** | vanilla EI2FP on extracted spectra | augmented model **recovers ≥ ~90% of the clean-data top-5** on extracted spectra |
| **Quantification** (known mixtures) | — | **RMSEP ≤ ~2–3 percentage points**, R² ≥ 0.98 on held-out compositions |
| **Mixture-aware ID** (synthetic) | single-compound EI2FP | true component **in top-10** for ≤ 3–4 co-eluting components |

---

## 10. Data Requirements

| Asset | What it is | Role | Status |
|---|---|---|---|
| **NIST library** | single-compound reference spectra (+ InChIKey) | trains the fingerprint model; reference library; **source of paired data (Section 8)** | ✅ have it (`.msp`) |
| **Our extractor** | chart → spectrum | generates extracted (noisy) spectra for training | ✅ built |
| **mzML / CDF** | raw GC-MS runs of *real samples* (time × m/z) | deconvolution + real-mixture analysis (the pipeline) | ⏳ to obtain |

Notes:
- **NIST is never needed as mzML** — it is the dictionary, not a sample.
- The mzML must be of **our own samples**: some of **known composition** (to validate %) and
  the **real unknowns** (to apply the method).
- **The fingerprint model and the robustness study need no mzML** — NIST + extractor suffice.

### 10.1 Real-World Data Budget

**Headline: tens of samples, not hundreds — because real data is for *validation and
demonstration*, not training.** The model trains on NIST; real runs only test and apply it.

| Purpose | Real samples | Why |
|---|---|---|
| Train fingerprint model + robustness study (Phase 1) | **0** | trains on NIST + paired data from the extractor |
| Validate deconvolution + quantification (Phase 2) | **~20–50** | known-composition mixtures across a composition range |
| Apply to real unknowns (Phase 3, the demo) | **~5–20** | well-characterized real samples for the application section |
| *(optional)* domain adaptation if an instrument gap appears | tens–low hundreds | fine-tune NIST model to the instrument |

**Total for a complete, publishable study: ~30–70 real runs.** The core ML needs **none**.

**What drives the Phase-2 number** (a calibration design): ~8 composition levels × 2–3
replicates per mixture *type* (≈16–24 runs); more components, a wider range, or more mixture
types → more runs.

**Quality > quantity.** Validation samples must have **known composition** (ground truth) —
you cannot check predicted % vs true % otherwise. 20 known-composition mixtures are worth
more than 200 mystery samples. For unknowns, a few **confirmed by reference standards** let
you verify answers rather than only report candidates.

**Domain-gap caveat.** A NIST-trained model may not transfer perfectly to a specific
instrument, but EI-MS is relatively standardized (this is why NIST library search works
across instruments), so the gap is small; if it appears, a small fine-tuning set (tens–low
hundreds) closes it. Likely optional.

> **Bottom line:** the project is **not bottlenecked on large real datasets** — only on a few
> dozen *well-characterized* samples. Prioritize **known composition** over count.

---

## 11. Phased Plan & Deliverables

| Phase | Work | Data needed | Status |
|---|---|---|---|
| **0** | Chart extraction + NIST validation (precursor) | charts + NIST | ✅ done |
| **1** | EI2FP engine on NIST + **extraction-noise robustness study** (Section 8) | **NIST + extractor only** | **start now — unblocked** |
| **2** | Peak detection + deconvolution + quantification, validated on **known** mixtures | **mzML (known)** | needs raw data |
| **3** | Full pipeline on real unknowns | **mzML (real)** | endpoint |

**Recommended first action:** Phase 1 — reproduce EI2FP on NIST, then run the
clean-vs-extracted robustness study and augmentation fix. This is the novel, unblocked core.

### 11.1 Rough Effort Estimate

Part-time, assuming the ML foundations (deep learning + RDKit) are learned in parallel:

- **Phase 1 — ~2–3 months (unblocked, start now).** Data prep MSP → InChIKey → SMILES →
  fingerprints (~2–3 wks, mostly PubChem-resolution wait) · reproduce EI2FP on NIST (~3–4 wks)
  · robustness study: render→extract pairs + clean-vs-extracted + augmentation (~3–4 wks).
- **Phase 2 — ~2–3 months** after mzML arrives. Peak detection + deconvolution + quantification
  + validation on known mixtures.
- **Phase 3 — ~1–2 months.** Apply to real unknowns; write up.

**Total ≈ 6–8 months part-time**, gated by (a) ML skill ramp-up and (b) mzML availability for
Phases 2–3. Phase 1 needs neither and can begin immediately.

---

## 12. Honest Scope & Limitations (state these up front)

- **The pipeline needs the time dimension.** A single mass spectrum is insufficient for a
  multi-component mixture (underdetermined; isomers indistinguishable). The pipeline requires
  raw GC-MS data (mzML) or a digitized chromatogram + per-peak spectra.
- **Identification yields a ranked shortlist + confidence, not a certain name.** Even SOTA
  reaches ~30–70% top-k on truly novel compounds; de novo (MASSISTANT) ~10–54% exact. No
  method does better without a reference standard.
- **Quantification is semi-quantitative (area %)** without per-compound calibration standards.
- **The fingerprint method is not ours** — EI2FP/DeepEI own it. Our novelty is the regime +
  integration + robustness, not the model.

### 12.1 Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Extraction noise is negligible** → robustness contribution weak | medium | Two-leg strategy (8.6): mixture integration + quantification is novel regardless; the negative result is itself publishable |
| **InChIKeys don't all resolve to SMILES** (PubChem gaps, odd NIST names) | medium | Resolve the bulk, report coverage; fall back to formula/CAS; train on the resolvable subset (still hundreds of thousands) |
| **Instrument domain gap** — NIST-trained model transfers poorly to real spectra | low–medium | EI-MS is standardized (library search transfers); small fine-tuning set closes it if needed (10.1) |
| **Deconvolution error swamps identification** on real co-elution | medium | Characterize the error chain on synthetic mixtures first (8.5); scope to well-separated peaks; report difficulty vs. overlap |
| **No mzML / no known-composition samples** | medium | Phase 1 needs none; secure data early (14, decision 1) |

---

## 13. Expected Contributions

1. **First demonstration of identification in the chart-extracted regime**, with a method made
   **robust to extraction artifacts** — uniquely enabled by our extractor.
2. **An integrated mixture pipeline** that deconvolves, identifies (including components absent
   from the library), and **quantifies** — combining capabilities that exist only separately
   today.
3. **A doubly-validated, end-to-end open-source system** from chart/raw data to components +
   percentages.

*Honest framing:* the fingerprint method (EI2FP) and the classical steps (deconvolution,
library search, area quantification) are existing tools we **assemble and adapt**. The
novelty is the chart-extracted regime, the integration, the quantification, and the
extraction-noise robustness.

---

## 14. Decisions to Settle With Advisor

1. **Data access:** Can we obtain **mzML of known-composition samples** (to validate) and
   **real unknowns** (to apply)? Gates Phases 2–3.
2. **Contribution level:** confirm the goal is the **methodological contribution**
   (extraction-noise robustness + mixture integration on top of EI2FP), not merely applying
   EI2FP — and that this scope is acceptable.

---

## Appendix A: Glossary

- **EI mass spectrum** — m/z vs intensity for one snapshot; what NIST stores per compound.
- **Chromatogram (TIC)** — total signal vs retention time; one peak per component.
- **mzML / CDF** — raw GC-MS file: a mass spectrum at every retention time (time × m/z matrix).
- **Cosine similarity** — spectrum-to-spectrum match score (library search metric).
- **NNLS** — non-negative least squares; solves spectral unmixing.
- **Deconvolution** — separating co-eluting compounds into pure spectra (MCR-ALS, AMDIS).
- **Retention index (RI)** — retention-time-based identifier; disambiguates isomers.
- **Molecular fingerprint** — binary vector encoding a molecule's substructures (Morgan/ECFP).
- **Tanimoto similarity** — structure-to-structure match score (shared fingerprint bits).
- **Scaffold split** — train/test split by structural scaffold; honest test of generalization.

## Appendix B: Key References

- **DeepEI** — Ji et al., *Anal. Chem.* 2020 — spectrum → fingerprint → identify library-absent
  compounds. https://github.com/hcji/DeepEI
- **EI2FP** — 2024 — single multi-output network; our engine/baseline.
- **MASSISTANT** — 2025 — de novo structure generation from EI-MS (SELFIES).
- **DeepResolution / DeepResolution2** — DL-assisted deconvolution of co-eluting GC-MS.
- **NEIMS** — Wei et al., *ACS Cent. Sci.* 2019 — structure → spectrum.
- **CSI:FingerID / SIRIUS** — fingerprint prediction for LC-MS/MS (the strong adjacent line).
