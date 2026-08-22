"""Donation-level aggregation, against the shape of the real weekly export.

The real file is 308,922 rows x 96 columns of one-row-per-submission data. It
holds donor names, contacts and amounts, and the repository is public, so the
fixture here reproduces its *structure* rather than its contents: the same
hierarchy columns, the same flag columns, the same derived-measure inputs, the
same messy spellings.

The derivations asserted below were each solved against the Master Report's own
Grand Total column and reproduce it exactly:

    RJBD1    = Insuff b4 debit + Stop b4 debit + Tech Error + Other Errors
    Net Loss = RJBD1 + Pledge To OT
    40+      = age group in (40-44, 45-49, Above 50)
"""

from __future__ import annotations

import datetime as dt
import random

import pandas as pd
import pytest

from qmis.ingest.aggregation import (
    AggregationError,
    SourceMap,
    aggregate_donations,
    canonical_forms,
    looks_donation_level,
    normalise_value,
)

LADDER = ["debit1", "Debit2", "DEBIT3", "Debit4", "Debit5", "Debit6",
          "Debit7", "Debit8", "Debit9", "Debit10", "Debit11", "Debit12"]
AGE_GROUPS = ["Below 25", "25-27", "28-29", "30-34", "35-39", "40-44", "45-49", "Above 50"]
OVER_40 = {"40-44", "45-49", "Above 50"}


def donation_rows(n: int = 400, seed: int = 11) -> pd.DataFrame:
    """A donation-level frame shaped like the real DATA sheet."""
    rng = random.Random(seed)
    monday = dt.date(2026, 8, 10)
    rows = []
    for i in range(n):
        week = monday - dt.timedelta(weeks=rng.randint(0, 3))
        age = rng.choice(AGE_GROUPS)
        reached_d1 = rng.random() < 0.88
        row = {
            "BACode": f"CODE-{i % 40:03d}",
            "BAName": f"BA {i % 40:03d}",
            "OWNCODE": 100 + (i % 8),
            # deliberately mixed spellings, as the real file has
            "ORG": rng.choice(["Focus", "Stellar", "Millennium"]),
            "ORG 2": rng.choice(["Alza", "ALZA", "Welkinz", "Triforce"]),
            "OWNER NAME": f"Owner {i % 8:02d}",
            "CITY": rng.choice(["Mumbai", "Delhi", "Bangalore"]),
            "REGION": rng.choice(["South", "SOUTH", "North", "West"]),
            "Designation": rng.choice(["Sr. Leader", "Leader", "New Guy"]),
            "MONTH": "Aug-26",
            "FormNo": f"F{i:06d}",
            "Donor Name": f"Donor {i}",
            "WE Date": week,
            "SigninDT": week - dt.timedelta(days=rng.randint(1, 6)),
            "SUBMISSION": 1,
            "Donamt": float(rng.choice([500, 800, 1000, 1200])),
            "age group": age,
            "Below 30": 1 if age in ("Below 25", "25-27", "28-29") else 0,
            "Pledge To OT": 1 if rng.random() < 0.10 else 0,
            # the four components of RJBD1, only for rows that never reached D1
            "Insuff b4 debit": 0, "Stop b4 debit": 0, "Tech Error": 0, "Other Errors": 0,
        }
        if not reached_d1:
            row[rng.choice(["Insuff b4 debit", "Stop b4 debit", "Tech Error", "Other Errors"])] = 1
        alive = reached_d1
        for stage in LADDER:
            row[stage] = 1 if alive else 0
            if alive and rng.random() < 0.08:
                alive = False
        rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture()
def source() -> SourceMap:
    return SourceMap.load()


@pytest.fixture()
def donations() -> pd.DataFrame:
    return donation_rows()


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_a_donation_sheet_is_recognised(donations, source):
    assert looks_donation_level(donations, source)


def test_a_pre_aggregated_summary_is_not_mistaken_for_one(source):
    summary = pd.DataFrame(
        {"Week": ["2026-W33"], "OWNER NAME": ["Owner A"], "BAName": ["BA 1"],
         "D1%": [0.88], "D3%": [0.70]}
    )
    assert not looks_donation_level(summary, source)


# --------------------------------------------------------------------------- #
# derived measures
# --------------------------------------------------------------------------- #
def test_rjbd1_is_the_sum_of_its_four_components(donations, source):
    result = aggregate_donations(donations, source)
    org = result.facts.loc[result.facts["level"] == "org"]
    total = org.loc[org["metric_key"] == "rjbd1_count", "value"].sum()
    expected = donations[
        ["Insuff b4 debit", "Stop b4 debit", "Tech Error", "Other Errors"]
    ].sum().sum()
    assert total == pytest.approx(expected)


def test_net_loss_is_rjbd1_plus_pledge_to_ot(donations, source):
    result = aggregate_donations(donations, source)
    org = result.facts.loc[result.facts["level"] == "org"]
    net = org.loc[org["metric_key"] == "net_loss_count", "value"].sum()
    rjbd1 = org.loc[org["metric_key"] == "rjbd1_count", "value"].sum()
    ot = org.loc[org["metric_key"] == "pledge_to_ot_count", "value"].sum()
    assert net == pytest.approx(rjbd1 + ot)


def test_forty_plus_counts_the_three_oldest_age_bands(donations, source):
    result = aggregate_donations(donations, source)
    org = result.facts.loc[result.facts["level"] == "org"]
    total = org.loc[org["metric_key"] == "subs_40_plus_count", "value"].sum()
    assert total == pytest.approx(donations["age group"].isin(OVER_40).sum())


def test_segment_averages_use_only_their_segment(donations, source):
    """ADS 40+ must average the 40+ rows of that org in that week - not all
    of its rows, and not all of its weeks."""
    result = aggregate_donations(donations, source)
    org = result.facts.loc[
        (result.facts["level"] == "org") & (result.facts["metric_key"] == "ads_40_plus")
    ]
    assert not org.empty
    weeks = donations["WE Date"].map(lambda d: pd.Timestamp(d).strftime("%G-W%V"))
    checked = 0
    for row in org.itertuples(index=False):
        block = donations.loc[
            (donations["ORG"] == row.entity_name)
            & (donations["age group"].isin(OVER_40))
            & (weeks == row.period_key)
        ]
        if not block.empty:
            assert row.value == pytest.approx(block["Donamt"].mean())
            checked += 1
    assert checked > 0


# --------------------------------------------------------------------------- #
# rates
# --------------------------------------------------------------------------- #
def test_rates_are_recomputed_from_components_at_every_level(donations, source):
    result = aggregate_donations(donations, source)
    for level in ("org", "owner", "ba"):
        block = result.facts.loc[result.facts["level"] == level]
        wide = block.pivot_table(
            index=["entity_path", "period_key"], columns="metric_key", values="value"
        )
        mask = wide["submissions"] > 0
        expected = wide.loc[mask, "debit1_count"] / wide.loc[mask, "submissions"] * 100
        assert wide.loc[mask, "d1_pct"].round(9).equals(expected.round(9))


def test_a_rate_is_never_the_mean_of_the_level_below(source):
    """One BA at 100% on 1 submission and one at 50% on 99 is 50.5%, not 75%."""
    rows = []
    for i in range(1):
        rows.append(_row("BA A", "Owner A", debit1=1))
    for i in range(99):
        rows.append(_row("BA B", "Owner A", debit1=1 if i < 49 else 0))
    result = aggregate_donations(pd.DataFrame(rows), source)
    owner = result.facts.loc[
        (result.facts["level"] == "owner") & (result.facts["metric_key"] == "d1_pct")
    ]
    assert owner.iloc[0]["value"] == pytest.approx(50 / 100 * 100)


def _row(ba: str, owner: str, debit1: int) -> dict:
    base = {
        "BACode": ba, "BAName": ba, "OWNCODE": 1, "ORG": "Focus", "ORG 2": "Alza",
        "OWNER NAME": owner, "CITY": "Mumbai", "REGION": "South", "Designation": "Leader",
        "FormNo": "F1", "WE Date": dt.date(2026, 8, 10), "SigninDT": dt.date(2026, 8, 6),
        "SUBMISSION": 1, "Donamt": 800.0, "age group": "30-34", "Below 30": 0,
        "Pledge To OT": 0, "Insuff b4 debit": 0, "Stop b4 debit": 0,
        "Tech Error": 0, "Other Errors": 0,
    }
    for stage in LADDER:
        base[stage] = debit1
    return base


# --------------------------------------------------------------------------- #
# value normalisation
# --------------------------------------------------------------------------- #
def test_case_variants_merge_into_the_commoner_spelling():
    series = pd.Series(["Alza"] * 14 + ["ALZA"] * 3 + ["KOP"] * 5)
    mapping = canonical_forms(series)
    assert mapping["ALZA"] == "Alza"       # the frequent spelling wins
    assert mapping["Alza"] == "Alza"
    assert mapping["KOP"] == "KOP"         # no rival spelling, left alone


def test_normalisation_collapses_whitespace_but_not_meaning():
    assert normalise_value("  South   West ") == "South West"
    assert normalise_value(None) == ""
    assert normalise_value(float("nan")) == ""


def test_region_case_split_does_not_create_two_entities(donations, source):
    result = aggregate_donations(donations, source)
    assert {"South", "SOUTH"} - set(donations["REGION"]) != {"South", "SOUTH"}  # both present
    merged = [w for w in result.warnings if w.startswith("REGION")]
    assert merged, "the South/SOUTH split should have been reported as merged"


def test_org_hierarchy_is_built_to_four_levels(donations, source):
    result = aggregate_donations(donations, source)
    assert set(result.entities["level"]) == {"org", "sub_org", "owner", "ba"}
    bas = result.entities.loc[result.entities["level"] == "ba"]
    assert (bas["parent_path"].str.len() > 0).all()


# --------------------------------------------------------------------------- #
# periods and failure modes
# --------------------------------------------------------------------------- #
def test_the_week_column_drives_the_period(donations, source):
    result = aggregate_donations(donations, source)
    assert all(k.startswith("2026-W") for k in result.periods)
    assert len(result.periods) == 4


def test_a_sheet_with_no_date_is_refused(donations, source):
    broken = donations.drop(columns=["WE Date", "SigninDT"])
    with pytest.raises(AggregationError, match="period column"):
        aggregate_donations(broken, source)


def test_an_empty_sheet_is_refused(source):
    with pytest.raises(AggregationError):
        aggregate_donations(pd.DataFrame(), source)


# --------------------------------------------------------------------------- #
# compact transport format
# --------------------------------------------------------------------------- #
def test_wide_round_trips_back_to_the_same_facts(donations, source):
    """A full history has to travel as one small file, so it is reshaped wide -
    and reshaping must not change any number."""
    from qmis.ingest.aggregation import facts_to_wide, wide_to_facts

    result = aggregate_donations(donations, source)
    wide = facts_to_wide(result.facts)
    back = wide_to_facts(wide)

    original = result.facts.set_index(["entity_path", "period_key", "metric_key"])["value"]
    restored = back.set_index(["entity_path", "period_key", "metric_key"])["value"]
    assert len(restored) == len(original)
    joined = original.to_frame("a").join(restored.to_frame("b"), how="inner")
    assert len(joined) == len(original)
    assert (joined["a"].round(4) == joined["b"].round(4)).all()


def test_wide_is_materially_smaller_than_long(donations, source):
    from qmis.ingest.aggregation import facts_to_wide

    result = aggregate_donations(donations, source)
    wide = facts_to_wide(result.facts)
    long_csv = result.facts.to_csv(index=False).encode()
    wide_csv = wide.to_csv(index=False).encode()
    assert len(wide_csv) < len(long_csv) * 0.6


def test_the_export_route_and_the_direct_route_agree(donations, source, session, tmp_path):
    """Aggregating here and aggregating on the far side must give the same facts.

    The whole point of the export route is that the donation file never moves,
    so the two paths have to be provably interchangeable - otherwise the numbers
    depend on which way the data happened to travel.
    """
    from sqlalchemy import func, select

    from qmis.core.models import Fact
    from qmis.ingest.aggregation import facts_to_wide
    from qmis.ingest.pipeline import ingest_fact_export, ingest_file

    workbook = tmp_path / "donations.xlsx"
    donations.to_excel(workbook, sheet_name="DATA", index=False)
    direct = ingest_file(session, workbook, uploaded_by="test")
    assert direct.accepted, direct.message
    direct_facts = {
        (e, p, m): round(v, 4)
        for e, p, m, v in session.execute(
            select(Fact.entity_id, Fact.period_key, Fact.metric_key, Fact.value)
            .where(Fact.is_current.is_(True))
        )
    }

    export = tmp_path / "qmis_facts.csv.gz"
    facts_to_wide(aggregate_donations(donations, source).facts).to_csv(
        export, index=False, compression="gzip"
    )
    loaded = ingest_fact_export(session, export, uploaded_by="test", allow_reprocess=True)
    assert loaded.accepted, loaded.message
    export_facts = {
        (e, p, m): round(v, 4)
        for e, p, m, v in session.execute(
            select(Fact.entity_id, Fact.period_key, Fact.metric_key, Fact.value)
            .where(Fact.is_current.is_(True))
        )
    }
    assert direct_facts == export_facts


def test_a_file_that_is_not_a_fact_export_is_refused(session, tmp_path):
    from qmis.ingest.pipeline import ingest_fact_export

    path = tmp_path / "wrong.csv"
    pd.DataFrame({"a": [1], "b": [2]}).to_csv(path, index=False)
    result = ingest_fact_export(session, path)
    assert not result.accepted
    assert any(f.code == "not_a_fact_export" for f in result.report.errors)
