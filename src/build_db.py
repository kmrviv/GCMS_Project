"""
Build spectra.duckdb from results.jsonl produced by imgprocess.py.
Idempotent — re-running rebuilds from scratch.
"""
import duckdb
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
JSONL = str(_ROOT / "data/processed/results.jsonl")
DB    = str(_ROOT / "data/processed/spectra.duckdb")


def build():
    if not Path(JSONL).exists():
        raise SystemExit(f"{JSONL} not found — run imgprocess.py first")

    con = duckdb.connect(DB)

    # Drop existing table if rebuilding
    con.execute("DROP TABLE IF EXISTS spectra")

    # DuckDB infers the nested schema directly from the JSONL.
    # peaks stays as LIST<STRUCT(mz, intensity)> — keep it nested for storage.
    con.execute(f"""
        CREATE TABLE spectra AS
        SELECT
            row_number() OVER () AS id,
            file,
            name,
            method,
            ok,
            bar_count,
            warnings,
            peaks,
            time AS extract_time_s
        FROM read_json('{JSONL}', format='newline_delimited')
    """)

    # A flat view for ML feature extraction — one row per peak.
    con.execute("""
        CREATE OR REPLACE VIEW peaks_long AS
        SELECT
            s.id              AS spectrum_id,
            s.name            AS compound,
            s.method,
            p.mz              AS mz,
            p.intensity       AS intensity
        FROM spectra s, UNNEST(s.peaks) AS t(p)
    """)

    # Quick index for compound lookups
    con.execute("CREATE INDEX IF NOT EXISTS idx_spectra_name ON spectra(name)")

    # Report
    n_spectra = con.execute("SELECT COUNT(*) FROM spectra").fetchone()[0]
    n_peaks   = con.execute("SELECT COUNT(*) FROM peaks_long").fetchone()[0]
    print(f"Loaded {n_spectra:,} spectra  /  {n_peaks:,} peaks")
    print(f"Database written to {DB}")

    con.close()


if __name__ == "__main__":
    build()
