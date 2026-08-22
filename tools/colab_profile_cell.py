# ============================================================================
#  QMIS - profile Raw_Data.xlsb from an iPhone, using Google Colab.
#
#  Nothing is downloaded to your phone and no donor data leaves Google.
#  Colab reads the file straight from your Drive, and writes a small profile
#  back to the same folder for me to pick up.
#
#  HOW TO RUN (all in Safari on your iPhone):
#    1. open  colab.research.google.com   ->  New notebook
#    2. paste this whole cell in
#    3. tap the run button, approve the Drive prompt when it appears
#    4. tell me when it says DONE
# ============================================================================

FOLDER = "TMO_Weekly_Data"     # folder in My Drive holding the file
FILENAME = "Raw_Data.xlsb"     # the file to profile
SAMPLE_ROWS = 2000             # rows in the pseudonymised sample
MAX_ROWS = None                # set to e.g. 300000 if Colab runs out of memory

# ---------------------------------------------------------------------------
import subprocess, sys
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyxlsb"], check=False)

import hashlib, json, math, os, zipfile
from datetime import datetime, date
import pandas as pd
from google.colab import drive

drive.mount("/content/drive", force_remount=True)

BASE = f"/content/drive/MyDrive/{FOLDER}"
path = os.path.join(BASE, FILENAME)
if not os.path.exists(path):
    print(f"NOT FOUND: {path}\nFiles in {BASE}:")
    for f in sorted(os.listdir(BASE)) if os.path.isdir(BASE) else []:
        print("   ", f)
    raise SystemExit("fix FOLDER / FILENAME above and run again")

print(f"reading {FILENAME} ({os.path.getsize(path)/1e6:.1f} MB) - this can take a few minutes...")

# --- identify the real format from the container, not the extension ---------
def detect(p):
    with open(p, "rb") as fh:
        if fh.read(2) != b"PK":
            return "other"
    with zipfile.ZipFile(p) as z:
        names = z.namelist()
    if any(n.endswith("workbook.bin") for n in names): return "xlsb"
    if any(n.endswith("workbook.xml") for n in names): return "xlsx"
    return "zip"

fmt = detect(path)
engine = {"xlsb": "pyxlsb", "xlsx": "openpyxl"}.get(fmt)
print(f"format: {fmt}  engine: {engine}")

book = pd.read_excel(path, sheet_name=None, header=None, nrows=MAX_ROWS, engine=engine)

# --- helpers ---------------------------------------------------------------
PERSONAL = ("donor","name","phone","mobile","email","address","pan","aadhaar",
            "account","ifsc","card","contact","dob","customer")
STRUCTURAL = ("baname","ba name","owner name","ownername","team","eventname","city")
SERIAL_MIN, SERIAL_MAX = 40000, 50000

def personal(c):
    c = str(c).strip().lower()
    if any(t == c or t in c for t in STRUCTURAL): return False
    return any(h in c for h in PERSONAL)

def datey(c):
    return any(t in str(c).strip().lower() for t in ("date","dt","time","day","month","year"))

def pseudo(v, pre="X"):
    if pd.isna(v): return ""
    return f"{pre}-" + hashlib.sha256(str(v).strip().lower().encode()).hexdigest()[:8]

def jsonable(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    if isinstance(v, (pd.Timestamp, datetime, date)): return str(v)
    if hasattr(v, "item"):
        try: return v.item()
        except Exception: return str(v)
    return v if isinstance(v, (str, int, float, bool)) else str(v)

def header_row(raw):
    best, score_best = 0, -1
    for i in range(min(25, len(raw))):
        cells = [c for c in raw.iloc[i].tolist() if pd.notna(c) and str(c).strip()]
        if len(cells) < 2: continue
        texty = sum(1 for c in cells if not isinstance(c, (int, float)))
        score = texty + len(set(map(str, cells)))
        if score > score_best: best, score_best = i, score
    return best

profile = {"file": FILENAME, "size_mb": round(os.path.getsize(path)/1e6, 2),
           "format": fmt, "generated": datetime.now().isoformat(timespec="seconds"),
           "sheets": {}}
biggest, biggest_n = None, -1
frames = {}   # keep the CLEANED frames; sampling from the raw ones misaligns columns

for name, raw in book.items():
    if raw.empty: continue
    h = header_row(raw)
    df = raw.iloc[h+1:].copy()
    df.columns = [str(c).strip() if pd.notna(c) else f"unnamed_{i}"
                  for i, c in enumerate(raw.iloc[h].tolist())]
    df = df.dropna(how="all").dropna(axis=1, how="all").infer_objects()

    # pyxlsb hands back dates as Excel serial numbers, not datetimes
    for c in df.columns:
        if engine == "pyxlsb" and datey(c):
            num = pd.to_numeric(df[c], errors="coerce")
            if num.notna().any() and num.between(SERIAL_MIN, SERIAL_MAX).mean() > 0.9:
                df[c] = pd.to_datetime(num, unit="D", origin="1899-12-30", errors="coerce")
                continue
        if df[c].dtype == object:
            num = pd.to_numeric(df[c], errors="coerce")
            if num.notna().sum() >= df[c].notna().sum() * 0.9:
                df[c] = num

    fields = {}
    for c in df.columns:
        s, nn = df[c], df[c].dropna()
        info = {"dtype": str(s.dtype), "non_null": int(len(nn)),
                "distinct": int(nn.nunique()) if len(nn) else 0,
                "looks_personal": personal(c)}
        if len(nn):
            if pd.api.types.is_numeric_dtype(nn):
                d = nn.describe()
                info["stats"] = {k: jsonable(d.get(k)) for k in ("min","25%","50%","75%","max","mean")}
                info["zeros"] = int((nn == 0).sum())
                if set(nn.unique().tolist()[:5]) <= {0, 1, 0.0, 1.0}:
                    info["looks_like_flag"] = True
                    info["ones"] = int((nn == 1).sum())
            elif pd.api.types.is_datetime64_any_dtype(nn):
                info["stats"] = {"min": str(nn.min()), "max": str(nn.max())}
            if info["looks_personal"]:
                info["sample"] = "<withheld: personal data>"
            elif info["distinct"] <= 60:
                info["all_values"] = {str(jsonable(k)): int(v)
                                      for k, v in nn.value_counts().head(60).items()}
            else:
                info["sample"] = [jsonable(v) for v in nn.head(5).tolist()]
                info["most_common"] = {str(jsonable(k)): int(v)
                                       for k, v in nn.value_counts().head(10).items()}
        fields[str(c)] = info

    low = {str(c).strip().lower(): c for c in df.columns}
    owner = next((low[k] for k in low if "owner" in k), None)
    ba = next((low[k] for k in low if k in ("baname","ba name","ba") or "baname" in k), None)
    rel = {"owner_column": owner, "ba_column": ba, "total_rows": int(len(df))}
    if owner: rel["distinct_owners"] = int(df[owner].nunique())
    if ba: rel["distinct_bas"] = int(df[ba].nunique())
    if owner and ba:
        pairs = df[[ba, owner]].dropna().drop_duplicates()
        per = pairs.groupby(ba)[owner].nunique()
        rel["bas_under_multiple_owners"] = int((per > 1).sum())
        pb = pairs.groupby(owner)[ba].nunique()
        rel["bas_per_owner"] = {"min": int(pb.min()), "median": float(pb.median()), "max": int(pb.max())}
    dates = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if dates:
        rel["date_columns"] = {str(c): {"min": str(df[c].min()), "max": str(df[c].max())} for c in dates}

    profile["sheets"][str(name)] = {"header_row": h + 1, "rows": int(len(df)),
                                    "columns": int(len(df.columns)),
                                    "column_order": [str(c) for c in df.columns],
                                    "relationships": rel, "fields": fields}
    frames[str(name)] = df
    print(f"  {name!r}: {len(df):,} rows x {len(df.columns)} cols")
    if len(df) > biggest_n: biggest, biggest_n = str(name), len(df)

out_json = os.path.join(BASE, "Raw_Data_profile.json")
with open(out_json, "w") as fh:
    json.dump(profile, fh, indent=2, default=str)

# Pseudonymised sample of the largest sheet, taken from the CLEANED frame.
# Sampling the raw frame and then assigning the cleaned column names fails on
# any export containing an all-empty column, because dropna(axis=1) removed it
# from the names but not from the raw data.
out_csv = None
try:
    if biggest is None:
        raise ValueError("no readable sheet was found")
    sample = frames[biggest].head(SAMPLE_ROWS).copy()
    for c in sample.columns:
        lc = str(c).strip().lower()
        if lc in ("baname", "ba name", "ba"):
            sample[c] = sample[c].map(lambda v: pseudo(v, "BA"))
        elif "owner" in lc:
            sample[c] = sample[c].map(lambda v: pseudo(v, "OWNER"))
        elif personal(c):
            sample[c] = sample[c].map(lambda v: pseudo(v, "X"))
    out_csv = os.path.join(BASE, "Raw_Data_sample.csv")
    sample.to_csv(out_csv, index=False)
except Exception:
    # The profile is the artefact that matters; never lose it to a sample error.
    import traceback
    print("\nsample step failed, but the profile below is still valid:")
    traceback.print_exc()

print("\nDONE")
print(f"  {out_json}  ({os.path.getsize(out_json)/1e3:.0f} KB)")
if out_csv:
    print(f"  {out_csv}   ({os.path.getsize(out_csv)/1e3:.0f} KB)")
print("\nSaved into your TMO_Weekly_Data folder.")
print("Upload them in the chat (they are small), or tell me and I will fetch them.")
