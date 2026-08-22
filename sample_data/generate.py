"""Generate a realistic weekly Owner/BA workbook for demo and testing.

The uploaded Master Report is an OLAP pivot whose Owner and BA dimensions are
all filtered to "All", so it contains organisation-level monthly totals only.
This generator produces the shape the system is designed around -- one row per
BA per week -- calibrated to the distributions actually observed in that file:

    Submissions/BA/week   ~ 3.6     D1%   ~ 88.3   (org level)
    RJBD1%                ~ 11.0    D3%   ~ 70.6
    Net loss%             ~ 20.8    Avg donation ~ 765

It also plants the specific situations the alert engine must catch, so the test
suite can assert on them by name rather than hoping the random draw cooperates.
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from qmis.core.periods import Period

OWNER_NAMES = [
    "Aarav Mehta", "Bhavna Rao", "Chetan Iyer", "Deepa Nair", "Farhan Qureshi",
    "Gauri Deshpande", "Harish Menon", "Ishita Bose", "Jatin Kapoor", "Kavya Reddy",
    "Lalit Sharma", "Meera Pillai", "Nikhil Joshi", "Osman Sheikh", "Pooja Malhotra",
    "Rahul Verma", "Sneha Ghosh", "Tarun Bhatia", "Uma Krishnan", "Vikram Singh",
    "Wasim Ansari", "Yamini Patel", "Zoya Khan", "Anand Kulkarni", "Bina Shetty",
    "Chirag Doshi", "Divya Menon", "Eshan Roy", "Fatima Sayed", "Girish Naik",
    "Hema Varma", "Imran Dalal", "Jaya Prakash", "Karan Grover", "Latika Sinha",
    "Manoj Pandey", "Neha Chawla", "Omkar Salvi", "Priya Ramesh", "Qamar Hussain",
    "Ritu Agarwal", "Sanjay Dutta", "Tanvi Shah", "Uday Bhat",
]

FIRST = [
    "Rahul", "Priya", "Amit", "Sneha", "Vikas", "Anjali", "Rohit", "Kavita", "Suresh",
    "Neha", "Arjun", "Pooja", "Manish", "Ritika", "Sandeep", "Divya", "Nitin", "Swati",
    "Ajay", "Megha", "Rajesh", "Shweta", "Deepak", "Anita", "Vishal", "Preeti", "Gaurav",
    "Sunita", "Ankit", "Rekha", "Sachin", "Nisha", "Alok", "Payal", "Varun", "Jyoti",
]
LAST = [
    "Sharma", "Verma", "Patel", "Reddy", "Nair", "Iyer", "Bose", "Khan", "Singh", "Gupta",
    "Joshi", "Rao", "Menon", "Das", "Kulkarni", "Shah", "Pillai", "Ghosh", "Malhotra",
    "Chauhan", "Bhatt", "Kapoor", "Mishra", "Sinha", "Naik", "Dubey", "Salvi", "Roy",
]

# Organisation-level anchors read off Master_Report__140826.xlsx.
BASE = {
    "d1_pct": 88.3,
    "d3_pct": 70.6,
    "rjbd1_pct": 11.0,
    "net_loss_pct": 20.8,
    "ot_pct": 9.8,
    "subs_below_30_pct": 27.5,
    "subs_40_plus_pct": 22.0,
    "avg_donation_amount": 765.0,
}
LADDER_BASE = {2: 75.9, 4: 67.6, 5: 65.0, 6: 62.8, 7: 60.7, 8: 59.0,
               9: 57.3, 10: 55.9, 11: 54.2, 12: 52.8}

COLUMNS = [
    "Week", "OWNER NAME", "BAName", "CITY",
    "Sum of SUBMISSION", "Rejects Before Debit 1", "Rejects Before Debit 1%",
    "Sum of debit1", "D1%", "Sum of DEBIT3", "D3%",
    "Sum of Pledge To OT", "OT%", "Net Loss", "Net Loss%",
    "Subs Below 30", "Subs Below 30%", "40+", "40+ %",
    "Average of Donamt",
] + [f"D{n}%" for n in sorted(LADDER_BASE)]


@dataclass
class Scenario:
    """A planted situation the alert engine is expected to find."""

    ba: str
    kind: str
    metric: str
    note: str


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build(
    weeks: int = 12,
    owners: int = 44,
    bas: int = 650,
    end_period: str = "2026-W33",
    seed: int = 20260814,
) -> tuple[pd.DataFrame, list[Scenario]]:
    rng = random.Random(seed)
    end = Period.from_key(end_period)
    periods = [end.shift(-(weeks - 1 - i)) for i in range(weeks)]

    owner_names = OWNER_NAMES[:owners]
    # Owners carry a persistent quality offset, so Owner-level ranking is
    # driven by something real rather than noise.
    owner_bias = {name: rng.gauss(0, 1.6) for name in owner_names}

    roster: list[tuple[str, str, str]] = []
    used: set[str] = set()
    for i in range(bas):
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name not in used:
                used.add(name)
                break
        owner = owner_names[i % owners]
        roster.append((name, owner, rng.choice(
            ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Pune", "Hyderabad", "Kolkata", "Jaipur"]
        )))

    scenarios: list[Scenario] = []
    crash_ba = roster[3][0]        # Debit 1 collapse through the critical line
    spike_ba = roster[11][0]       # RJBD1 anomaly with no threshold breach
    slide_ba = roster[19][0]       # four straight weeks of Debit 1 decline
    rally_ba = roster[27][0]       # sustained improvement
    tiny_ba = roster[35][0]        # real decline on a sample too small to act on
    new_ba = roster[41][0]         # only appears in the final two weeks
    gone_ba = roster[47][0]        # stops reporting before the final week
    scenarios += [
        Scenario(crash_ba, "critical_decline", "d1_pct",
                 "Debit 1 falls from ~85% to ~76%, below the 80% critical threshold"),
        Scenario(spike_ba, "anomaly", "rjbd1_pct",
                 "RJBD1 holds ~2.2% then jumps to ~4.0% - unusual, but never past 14%"),
        Scenario(slide_ba, "sustained_decline", "d1_pct",
                 "Debit 1 declines every week for the last four weeks"),
        Scenario(rally_ba, "improvement", "d3_pct",
                 "Debit 3 climbs steadily from ~62% to ~84%"),
        Scenario(tiny_ba, "small_sample", "d1_pct",
                 "Debit 1 halves, but on 4 submissions - must be suppressed"),
        Scenario(new_ba, "new_ba", "-", "Appears for the first time in the last two weeks"),
        Scenario(gone_ba, "removed_ba", "-", "Stops reporting before the final week"),
    ]

    rows: list[dict] = []
    for week_index, period in enumerate(periods):
        last_week = week_index == weeks - 1
        for ba_name, owner, city in roster:
            if ba_name == new_ba and week_index < weeks - 2:
                continue
            if ba_name == gone_ba and week_index >= weeks - 1:
                continue

            # ~2,300 submissions a week across 650 BAs is what the source file
            # implies (about 10,000 a month). Generating a comfortable 14 a week
            # would have hidden the very small-sample problem the system has to
            # cope with.
            submissions = max(0, int(rng.gauss(3.6, 2.2)))
            bias = owner_bias[owner] + rng.gauss(0, 2.5)

            d1 = _clamp(BASE["d1_pct"] + bias + rng.gauss(0, 2.0), 40, 100)
            rjbd1 = _clamp(BASE["rjbd1_pct"] - bias * 0.4 + rng.gauss(0, 1.6), 0, 45)
            d3 = _clamp(BASE["d3_pct"] + bias * 0.9 + rng.gauss(0, 2.6), 25, 95)
            net_loss = _clamp(BASE["net_loss_pct"] - bias * 0.5 + rng.gauss(0, 1.8), 5, 55)

            # -- planted scenarios ------------------------------------------
            if ba_name == crash_ba:
                submissions = rng.randint(38, 46)
                d1 = 85.2 + rng.gauss(0, 0.5) if not last_week else 76.1
            elif ba_name == spike_ba:
                submissions = rng.randint(34, 42)
                rjbd1 = 2.2 + rng.gauss(0, 0.18) if not last_week else 4.0
            elif ba_name == slide_ba:
                submissions = rng.randint(30, 36)
                steps_from_end = weeks - 1 - week_index
                d1 = 90.0 - max(0, 4 - steps_from_end) * 1.4 + rng.gauss(0, 0.2)
            elif ba_name == rally_ba:
                submissions = rng.randint(30, 38)
                d3 = 62.0 + (week_index / max(1, weeks - 1)) * 22.0 + rng.gauss(0, 0.4)
            elif ba_name == tiny_ba:
                submissions = 4
                d1 = 100.0 if not last_week else 50.0

            if submissions == 0:
                continue  # a BA who signed nobody this week has nothing to report

            debit1 = int(round(submissions * d1 / 100))
            rejects = int(round(submissions * rjbd1 / 100))
            debit3 = int(round(submissions * d3 / 100))
            loss = int(round(submissions * net_loss / 100))
            ot = int(round(submissions * _clamp(BASE["ot_pct"] + rng.gauss(0, 1.4), 0, 40) / 100))
            below30 = int(round(submissions * _clamp(BASE["subs_below_30_pct"] + rng.gauss(0, 4), 0, 90) / 100))
            over40 = int(round(submissions * _clamp(BASE["subs_40_plus_pct"] + rng.gauss(0, 4), 0, 90) / 100))

            def pct(numerator: int) -> float:
                return round(numerator / submissions, 6) if submissions else 0.0

            row = {
                "Week": period.key,
                "OWNER NAME": owner,
                "BAName": ba_name,
                "CITY": city,
                "Sum of SUBMISSION": submissions,
                "Rejects Before Debit 1": rejects,
                "Rejects Before Debit 1%": pct(rejects),
                "Sum of debit1": debit1,
                "D1%": pct(debit1),
                "Sum of DEBIT3": debit3,
                "D3%": pct(debit3),
                "Sum of Pledge To OT": ot,
                "OT%": pct(ot),
                "Net Loss": loss,
                "Net Loss%": pct(loss),
                "Subs Below 30": below30,
                "Subs Below 30%": pct(below30),
                "40+": over40,
                "40+ %": pct(over40),
                "Average of Donamt": round(
                    _clamp(BASE["avg_donation_amount"] + bias * 6 + rng.gauss(0, 35), 300, 1600), 2
                ),
            }
            # The debit ladder must stay monotonic: D1 >= D2 >= D3 >= ... >= D12.
            # D1 and D3 are already fixed above, so D2 is placed between them
            # and D4 onwards descend from D3.  Generating each stage
            # independently produced ladders where D4 beat D3, which the
            # validator (correctly) rejected.
            d1_frac, d3_frac = pct(debit1), pct(debit3)
            d2_frac = _clamp(
                d3_frac + (d1_frac - d3_frac) * rng.uniform(0.35, 0.75), d3_frac, d1_frac
            )
            row["D2%"] = round(d2_frac, 6)
            previous = d3_frac
            for n in sorted(LADDER_BASE):
                if n in (2, 3):
                    continue
                decay = (LADDER_BASE[n] / LADDER_BASE.get(n - 1, LADDER_BASE[n])) if n > 4 else 0.958
                value = previous * _clamp(decay + rng.gauss(0, 0.008), 0.90, 0.999)
                value = _clamp(value, 0.02, previous)
                row[f"D{n}%"] = round(value, 6)
                previous = value
            rows.append(row)

    return pd.DataFrame(rows, columns=COLUMNS), scenarios


def write_weekly_files(
    outdir: Path, weeks: int = 12, one_file_per_week: bool = True, **kwargs
) -> list[Path]:
    """Write the dataset out the way the organisation actually receives it."""
    frame, scenarios = build(weeks=weeks, **kwargs)
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if one_file_per_week:
        for period_key, block in frame.groupby("Week"):
            period = Period.from_key(str(period_key))
            start, _ = period.date_range()
            path = outdir / f"Quality_Report_{start:%d%m%y}.xlsx"
            block.drop(columns=["Week"]).assign(**{"Week": period_key}).to_excel(
                path, sheet_name="BA Quality", index=False
            )
            written.append(path)
    else:
        path = outdir / "Quality_Report_All_Weeks.xlsx"
        frame.to_excel(path, sheet_name="BA Quality", index=False)
        written.append(path)

    manifest = outdir / "scenarios.md"
    lines = ["# Planted scenarios\n", "| BA | Kind | Metric | Expected behaviour |", "|---|---|---|---|"]
    lines += [f"| {s.ba} | {s.kind} | {s.metric} | {s.note} |" for s in scenarios]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="data/inbox", type=Path)
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--owners", type=int, default=44)
    parser.add_argument("--bas", type=int, default=650)
    parser.add_argument("--end-period", default="2026-W33")
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--single-file", action="store_true")
    args = parser.parse_args()
    written = write_weekly_files(
        args.outdir,
        weeks=args.weeks,
        one_file_per_week=not args.single_file,
        owners=args.owners,
        bas=args.bas,
        end_period=args.end_period,
        seed=args.seed,
    )
    print(f"wrote {len(written)} file(s) to {args.outdir}")
    for path in written:
        print("  ", path.name)


if __name__ == "__main__":
    main()
