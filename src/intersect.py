"""
Find the name-intersection of NISTds.jsonl (reference) and results.jsonl
(imgprocess output).

Memory-safe: the reference file is loaded names-only (no peak arrays).
Uses the same canon() normalization as compare_peaks.py so names match
across Greek-glyph and whitespace differences.
"""
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_GLYPH = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu",
    "ξ": "xi", "π": "pi", "ρ": "rho", "σ": "sigma",
    "ς": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "χ": "chi", "ψ": "psi", "ω": "omega",
}
_GLYPH_RE = re.compile("|".join(map(re.escape, _GLYPH)))
_NIST_RE  = re.compile(
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


# Build the reference key set (names only — tiny memory footprint)
nist_keys = set()
with open(_ROOT / "data/processed/NISTds.jsonl", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            nist_keys.add(canon(json.loads(line)["name"]))

# Walk results, split matched vs unmatched
common, unmatched = set(), []
with open(_ROOT / "data/processed/results.jsonl", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            r = json.loads(line)
            if not r.get("ok", True):
                continue
            k = canon(r["name"])
            if k in nist_keys:
                common.add(k)
            else:
                unmatched.append(r["name"])

print(f"common:    {len(common)}")
print(f"unmatched: {len(unmatched)}")
