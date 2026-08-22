#!/usr/bin/env python3
"""Profile a large source file WITHOUT shipping the file itself.

Run this on your own machine against the raw export that the Master Report is
pivoted from.  It writes two small artefacts:

  <name>_profile.json    every sheet, every column, dtype, null and distinct
                         counts, ranges, and the full value list for
                         low-cardinality columns.  Usually well under 1 MB.

  <name>_sample.csv      an optional row sample with personal fields
                         pseudonymised (donor names/contacts hashed, BA and
                         Owner names mapped to stable labels).

Between them I can see the true grain, the Owner -> BA hierarchy, every column
name and type, and how the measures are actually derived - which is everything
I need - without a 500 MB upload and without moving donor personal data.

Usage
-----
    python profile_source.py RAWFILE.xlsx
    python profile_source.py RAWFILE.csv --sample-rows 3000
    python profile_source.py RAWFILE.xlsx --no-sample        # profile only
    python profile_source.py RAWFILE.xlsx --keep-names       # do not pseudonymise

Only needs pandas and openpyxl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Columns whose contents are treated as personal data and pseudonymised in the
# sample.  Matched case-insensitively as substrings, so "Donor First Name" and
# "DonorName" both match "donor".
PERSONAL_HINTS = (
    "donor", "name", "phone", "mobile", "email", "address", "pan", "aadhaar",
    "account", "ifsc", "card", "contact", "dob", "customer", "client name",
)
# ...except these, which are the structural columns I actually need to see.
STRUCTURAL = ("baname", "ba name", "owner name", "ownername", "team", "eventname", "city")

# A column with at most this many distinct values gets its full value list in
# the profile - that is how the Owner list, the metric flags and the lookup
# codes become visible.
FULL_VALUE_LIMIT = 60
SAMPLE_VALUES = 5


def is_personal(column: str) -> bool:
    lowered = str(column).strip().lower()
    if any(token == lowered or token in lowered for token in STRUCTURAL):
        return False
    return any(hint in lowered for hint in PERSONAL_HINTS)


def pseudonym(value, prefix: str = "ID") -> str:
    if pd.isna(value):
        return ""
    digest = hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def jsonable(value):
    """Make numpy/pandas scalars and timestamps JSON-serialisable."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def profile_column(series: pd.Series, name: str) -> dict:
    non_null = series.dropna()
    info: dict = {
        "dtype": str(series.dtype),
        "rows": int(len(series)),
        "non_null": int(len(non_null)),
        "null_pct": round((1 - len(non_null) / len(series)) * 100, 2) if len(series) else None,
        "distinct": int(non_null.nunique()) if len(non_null) else 0,
        "looks_personal": is_personal(name),
    }
    if non_null.empty:
        return info

    if pd.api.types.is_numeric_dtype(non_null):
        described = non_null.describe()
        info["stats"] = {
            k: jsonable(described.get(k))
            for k in ("min", "25%", "50%", "75%", "max", "mean", "std")
        }
        info["zeros"] = int((non_null == 0).sum())
        info["negatives"] = int((non_null < 0).sum())
        # A 0/1 column is a flag, and flags are how measures get built.
        uniques = set(non_null.unique().tolist()[:5])
        if uniques <= {0, 1, 0.0, 1.0}:
            info["looks_like_flag"] = True
            info["ones"] = int((non_null == 1).sum())
    elif pd.api.types.is_datetime64_any_dtype(non_null):
        info["stats"] = {"min": str(non_null.min()), "max": str(non_null.max())}
    else:
        lengths = non_null.astype(str).str.len()
        info["stats"] = {"min_len": int(lengths.min()), "max_len": int(lengths.max())}

    if info["looks_personal"]:
        info["sample"] = "<withheld: column looks like personal data>"
    elif info["distinct"] <= FULL_VALUE_LIMIT:
        counts = non_null.value_counts().head(FULL_VALUE_LIMIT)
        info["all_values"] = {str(jsonable(k)): int(v) for k, v in counts.items()}
    else:
        info["sample"] = [jsonable(v) for v in non_null.head(SAMPLE_VALUES).tolist()]
        top = non_null.value_counts().head(10)
        info["most_common"] = {str(jsonable(k)): int(v) for k, v in top.items()}
    return info


def find_header_row(raw: pd.DataFrame, max_scan: int = 25) -> int:
    """The row with the most non-empty, non-numeric, distinct cells wins."""
    best_row, best_score = 0, -1
    for i in range(min(max_scan, len(raw))):
        cells = [c for c in raw.iloc[i].tolist() if pd.notna(c) and str(c).strip()]
        if len(cells) < 2:
            continue
        texty = sum(1 for c in cells if not isinstance(c, (int, float)))
        score = texty + len(set(map(str, cells))) - (len(cells) - len(set(map(str, cells))))
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def relationships(frame: pd.DataFrame) -> dict:
    """Look for the Owner -> BA hierarchy and the reporting grain."""
    lowered = {str(c).strip().lower(): c for c in frame.columns}

    def find(*candidates):
        for candidate in candidates:
            if candidate in lowered:
                return lowered[candidate]
        for key, original in lowered.items():
            if any(c in key for c in candidates):
                return original
        return None

    owner = find("owner name", "ownername", "owner")
    ba = find("baname", "ba name", "ba")
    out: dict = {"owner_column": owner, "ba_column": ba}
    if owner:
        out["distinct_owners"] = int(frame[owner].nunique())
    if ba:
        out["distinct_bas"] = int(frame[ba].nunique())
    if owner and ba:
        pairs = frame[[ba, owner]].dropna().drop_duplicates()
        per_ba = pairs.groupby(ba)[owner].nunique()
        out["bas_under_multiple_owners"] = int((per_ba > 1).sum())
        out["bas_per_owner"] = {
            "min": int(pairs.groupby(owner)[ba].nunique().min()),
            "median": float(pairs.groupby(owner)[ba].nunique().median()),
            "max": int(pairs.groupby(owner)[ba].nunique().max()),
        }
    date_columns = [c for c in frame.columns if pd.api.types.is_datetime64_any_dtype(frame[c])]
    if date_columns:
        out["date_columns"] = {
            str(c): {"min": str(frame[c].min()), "max": str(frame[c].max())} for c in date_columns
        }
    # Is one row one donation, or one row one BA-week?
    out["total_rows"] = int(len(frame))
    if ba and date_columns:
        grain = frame[[ba, date_columns[0]]].dropna()
        out["rows_per_ba_per_date"] = round(len(grain) / max(1, len(grain.drop_duplicates())), 2)
    return out


def build_sample(frame: pd.DataFrame, rows: int, keep_names: bool) -> pd.DataFrame:
    sample = frame.head(rows).copy()
    if keep_names:
        return sample
    for column in sample.columns:
        label = str(column).strip().lower()
        if label in ("baname", "ba name", "ba"):
            sample[column] = sample[column].map(lambda v: pseudonym(v, "BA"))
        elif label in ("owner name", "ownername", "owner"):
            sample[column] = sample[column].map(lambda v: pseudonym(v, "OWNER"))
        elif is_personal(column):
            sample[column] = sample[column].map(lambda v: pseudonym(v, "X"))
    return sample


def load_sheets(path: Path, max_rows: int | None) -> dict[str, pd.DataFrame]:
    if path.suffix.lower() in (".csv", ".tsv", ".txt"):
        separator = "\t" if path.suffix.lower() == ".tsv" else ","
        frame = pd.read_csv(path, sep=separator, nrows=max_rows, low_memory=False)
        return {"(csv)": frame}

    book = pd.read_excel(path, sheet_name=None, header=None, nrows=max_rows, engine="openpyxl")
    out: dict[str, pd.DataFrame] = {}
    for name, raw in book.items():
        if raw.empty:
            continue
        header_row = find_header_row(raw)
        frame = raw.iloc[header_row + 1 :].copy()
        frame.columns = [
            str(c).strip() if pd.notna(c) else f"unnamed_{i}"
            for i, c in enumerate(raw.iloc[header_row].tolist())
        ]
        frame = frame.dropna(how="all").dropna(axis=1, how="all")
        frame = frame.infer_objects()
        for column in frame.columns:
            if frame[column].dtype == object:
                converted = pd.to_numeric(frame[column], errors="coerce")
                if converted.notna().sum() >= frame[column].notna().sum() * 0.9:
                    frame[column] = converted
                    continue
                as_date = pd.to_datetime(frame[column], errors="coerce", format="mixed")
                if as_date.notna().sum() >= frame[column].notna().sum() * 0.9:
                    frame[column] = as_date
        out[name] = frame
        out[f"__header_row__{name}"] = header_row  # type: ignore[assignment]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--sample-rows", type=int, default=2000)
    parser.add_argument("--no-sample", action="store_true")
    parser.add_argument("--keep-names", action="store_true",
                        help="do not pseudonymise (only if the file holds no personal data)")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="only read the first N rows (for a very large file)")
    parser.add_argument("--outdir", type=Path, default=Path("."))
    args = parser.parse_args()

    if not args.path.exists():
        print(f"not found: {args.path}", file=sys.stderr)
        return 1

    print(f"reading {args.path.name} ({args.path.stat().st_size / 1e6:.1f} MB)...")
    loaded = load_sheets(args.path, args.max_rows)
    header_rows = {k.replace("__header_row__", ""): v
                   for k, v in loaded.items() if str(k).startswith("__header_row__")}
    sheets = {k: v for k, v in loaded.items() if not str(k).startswith("__header_row__")}

    profile: dict = {
        "file": args.path.name,
        "size_mb": round(args.path.stat().st_size / 1e6, 2),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "pseudonymised": not args.keep_names,
        "sheets": {},
    }

    args.outdir.mkdir(parents=True, exist_ok=True)
    stem = args.path.stem
    biggest, biggest_rows = None, -1

    for name, frame in sheets.items():
        print(f"  profiling {name!r}: {len(frame):,} rows x {len(frame.columns)} columns")
        profile["sheets"][name] = {
            "header_row": header_rows.get(name, 0) + 1,
            "rows": int(len(frame)),
            "columns": int(len(frame.columns)),
            "column_order": [str(c) for c in frame.columns],
            "relationships": relationships(frame),
            "fields": {str(c): profile_column(frame[c], str(c)) for c in frame.columns},
        }
        if len(frame) > biggest_rows:
            biggest, biggest_rows = name, len(frame)

    profile_path = args.outdir / f"{stem}_profile.json"
    profile_path.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {profile_path} ({profile_path.stat().st_size / 1e3:.0f} KB)")

    if not args.no_sample and biggest is not None:
        sample = build_sample(sheets[biggest], args.sample_rows, args.keep_names)
        sample_path = args.outdir / f"{stem}_sample.csv"
        sample.to_csv(sample_path, index=False)
        print(f"wrote {sample_path} ({sample_path.stat().st_size / 1e3:.0f} KB, "
              f"{len(sample):,} rows from sheet {biggest!r})")
        if not args.keep_names:
            print("      names pseudonymised; the same person maps to the same label everywhere")

    print("\nSend me those two files. Open the JSON first if you want to check "
          "nothing sensitive is in it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
