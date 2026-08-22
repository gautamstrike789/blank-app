# ============================================================================
#  QMIS - aggregate the full Raw_Data.xlsb into weekly quality facts.
#
#  Runs on Google Colab (works from Safari on a phone). The 45 MB file never
#  moves and no donor-level data leaves Google: Colab reads it in place, folds
#  it to (entity x week x metric), and writes back only the aggregated numbers.
#
#  It installs and runs the SAME aggregation code the system uses, straight from
#  the repository, so what you get here is exactly what the pipeline computes -
#  no second implementation to drift out of step.
#
#  HOW TO RUN (all in Safari):
#    1. colab.research.google.com  ->  New notebook
#    2. paste this whole cell
#    3. run it, approve the Drive prompt
#    4. tell me when it prints DONE
#
#  Expect 5-15 minutes: 308,922 rows x 96 columns through the .xlsb reader is
#  the slow part.
# ============================================================================

BRANCH = "claude/quality-metrics-monitoring-alerts-bn71k5"
FOLDER = "TMO_Weekly_Data"
FILENAME = "Raw_Data.xlsb"
SHEET = "DATA"
MAX_ROWS = None       # set e.g. 150000 if Colab runs out of memory

# ---------------------------------------------------------------------------
import subprocess, sys
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyxlsb"], check=False)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps",
                f"git+https://github.com/gautamstrike789/blank-app@{BRANCH}"], check=False)

import os, time
import pandas as pd
from google.colab import drive
drive.mount("/content/drive", force_remount=True)

from qmis.ingest.aggregation import SourceMap, aggregate_donations, facts_to_wide

BASE = f"/content/drive/MyDrive/{FOLDER}"
path = os.path.join(BASE, FILENAME)
if not os.path.exists(path):
    raise SystemExit(f"not found: {path}")

print(f"reading {FILENAME} ({os.path.getsize(path)/1e6:.1f} MB), sheet {SHEET!r}...")
t0 = time.time()
frame = pd.read_excel(path, sheet_name=SHEET, engine="pyxlsb", nrows=MAX_ROWS)
frame.columns = [str(c).strip() for c in frame.columns]
print(f"  {len(frame):,} rows x {len(frame.columns)} columns in {time.time()-t0:.0f}s")

print("aggregating...")
t0 = time.time()
result = aggregate_donations(frame, SourceMap.load())
print(f"  {len(result.facts):,} facts, {len(result.entities):,} entities, "
      f"{len(result.periods)} weeks ({result.periods[0]} to {result.periods[-1]}) "
      f"in {time.time()-t0:.0f}s")
for w in result.warnings:
    print("  note:", w)

wide = facts_to_wide(result.facts)

# Two files: the whole thing, and an owner-and-above cut that is guaranteed
# small enough to transfer even if the full one is not.
full = os.path.join(BASE, "qmis_facts.csv.gz")
wide.to_csv(full, index=False, compression="gzip")

rollup = wide.loc[wide["level"] != "ba"]
small = os.path.join(BASE, "qmis_facts_rollup.csv.gz")
rollup.to_csv(small, index=False, compression="gzip")

def mb(p): return os.path.getsize(p) / 1e6

print("\nDONE")
print(f"  {full}          {mb(full):5.1f} MB   ({len(wide):,} entity-weeks, all levels)")
print(f"  {small}   {mb(small):5.1f} MB   ({len(rollup):,} entity-weeks, owner and above)")
if mb(full) > 9.5:
    print("\n  The full file is over the 9.5 MB transfer limit - the rollup one is")
    print("  the one to use, and I can ask for BA detail for specific weeks separately.")
print("\nBoth are aggregates only: no donor names, contacts or individual donations.")
