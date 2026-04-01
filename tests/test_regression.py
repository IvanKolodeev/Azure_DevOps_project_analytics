"""
Regression tests: re-run the pipeline against frozen source data and compare
each output dataframe to the saved baseline.

Usage:
    pytest tests/test_regression.py -v
"""

import sys
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import numpy as np
import pytest
from natsort import natsort_keygen, ns

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import ado_reporting as cl

BASELINE_DIR = REPO_ROOT / "tests" / "baselines"
SOURCE_DIR = BASELINE_DIR / "source"

# ---- Config (must match freeze_baselines.py) ----
project = "MarketRMS"
start_date = date(2026, 3, 1)
today = date(2026, 4, 1)
DAYS_BACK = (today - start_date).days + 1
removed_state = "Removed"
doneish_statuses = {"Code Review", "Ready for UAT", "Ready for Prod", "In UAT", "Closed"}


def _load_baseline(name: str) -> pd.DataFrame:
    path = BASELINE_DIR / f"{name}.parquet"
    if not path.exists():
        pytest.skip(f"Baseline {path.name} not found. Run: python tests/freeze_baselines.py")
    return pd.read_parquet(path)


def _compare_dataframes(actual: pd.DataFrame, expected: pd.DataFrame, name: str):
    """Compare two dataframes, raise AssertionError with details on mismatch."""
    # Shape check
    assert actual.shape == expected.shape, (
        f"{name}: shape mismatch — got {actual.shape}, expected {expected.shape}"
    )

    # Column check
    assert list(actual.columns) == list(expected.columns), (
        f"{name}: column mismatch\n"
        f"  got:      {list(actual.columns)}\n"
        f"  expected: {list(expected.columns)}"
    )

    # Align indexes for comparison
    actual = actual.reset_index(drop=True)
    expected = expected.reset_index(drop=True)

    # Compare column by column for better error messages
    mismatches = []
    for col in actual.columns:
        a = actual[col]
        e = expected[col]

        if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(e):
            # numeric: use close comparison
            close = np.isclose(
                a.fillna(-999999.0).values,
                e.fillna(-999999.0).values,
                rtol=1e-6, atol=1e-9, equal_nan=True
            )
            if not close.all():
                n_diff = (~close).sum()
                idx_first = np.where(~close)[0][0]
                mismatches.append(
                    f"  {col}: {n_diff} value(s) differ "
                    f"(first at row {idx_first}: got {a.iloc[idx_first]}, expected {e.iloc[idx_first]})"
                )
        else:
            # non-numeric: exact string comparison (treat NaN/None as equal)
            both_na = a.isna() & e.isna()
            a_str = a.astype(str).fillna("")
            e_str = e.astype(str).fillna("")
            diff = (a_str != e_str) & ~both_na
            if diff.any():
                n_diff = diff.sum()
                idx_first = diff.idxmax()
                mismatches.append(
                    f"  {col}: {n_diff} value(s) differ "
                    f"(first at row {idx_first}: got {repr(a.iloc[idx_first])}, expected {repr(e.iloc[idx_first])})"
                )

    assert not mismatches, f"{name}: value mismatches:\n" + "\n".join(mismatches)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="module")
def wi_raw_snapshots():
    return cl.get_wi_snapshots(
        project=project,
        days_back=DAYS_BACK,
        hist_snapshot_path=SOURCE_DIR / f"{project}_work_item_snapshot.parquet",
        live_snap_dir=SOURCE_DIR / "live_snapshots",
        removed_state=removed_state,
    )


@pytest.fixture(scope="module")
def sprint_info(wi_raw_snapshots):
    sprints, current_sprint, previous_sprint = cl.get_sprint_info(wi_raw_snapshots, today)
    return sprints, current_sprint, previous_sprint


@pytest.fixture(scope="module")
def df_with_daily_charge(wi_raw_snapshots):
    df = wi_raw_snapshots.copy()
    df = df.sort_values(["WorkItemId", "SnapshotDate", "WorkItemRevisionSK"])
    df = df.drop_duplicates(["WorkItemId", "SnapshotDate"], keep="last")
    df["daily_charge"] = (
        df.groupby("WorkItemId")["CompletedWork"]
          .diff()
          .fillna(df["CompletedWork"])
    )
    return df


@pytest.fixture(scope="module")
def daily_charge_by_person(df_with_daily_charge):
    df = df_with_daily_charge
    agg1 = df.groupby(["AssignedTo", "SnapshotDate"], dropna=False)["daily_charge"].sum().reset_index()
    agg1["AssignedTo"] = agg1["AssignedTo"].fillna("Unassigned")
    result = agg1.pivot(index="AssignedTo", columns="SnapshotDate", values="daily_charge").fillna(0)
    result.columns.name = None
    result = result.sort_index(axis=1)
    earliest_col = result.columns.min()
    result = result.drop(columns=[earliest_col])
    result["total_charge"] = result.sum(axis=1)
    result = result[result["total_charge"] != 0]
    result.reset_index(inplace=True)
    result.rename(columns={"AssignedTo": "Assigned To"}, inplace=True)
    date_cols = [c for c in result.columns if c not in ("Assigned To", "total_charge")]
    rename_map = {c: c.strftime("%Y-%m-%d") if hasattr(c, "strftime") else str(c) for c in date_cols}
    result.rename(columns=rename_map, inplace=True)
    return result


@pytest.fixture(scope="module")
def daily_charge_by_task(df_with_daily_charge):
    df = df_with_daily_charge
    agg2 = df.groupby(
        ["AssignedTo", "IterationName", "WorkItemType", "Title", "WorkItemURL", "SnapshotDate"],
        dropna=False
    )["daily_charge"].sum().reset_index()
    agg2["AssignedTo"] = agg2["AssignedTo"].fillna("Unassigned")
    result = agg2.pivot(
        index=["AssignedTo", "IterationName", "WorkItemType", "Title", "WorkItemURL"],
        columns="SnapshotDate", values="daily_charge"
    ).fillna(0)
    result = result.sort_index(axis=1)
    earliest_col = result.columns.min()
    result = result.drop(columns=[earliest_col])
    result["total_charge"] = result.sum(axis=1)
    result = result[result["total_charge"] != 0]
    date_cols_t = [c for c in result.columns if c != "total_charge"]
    rename_dates = {d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for d in date_cols_t}
    result = result.rename(columns=rename_dates)
    result.reset_index(inplace=True)
    result.rename(columns={"AssignedTo": "Assigned To"}, inplace=True)
    base_cols = ["Assigned To", "IterationName", "WorkItemType", "Title"]
    date_cols_str = [rename_dates[d] for d in date_cols_t]
    final_cols = base_cols + date_cols_str + ["total_charge", "WorkItemURL"]
    final_cols = [c for c in final_cols if c in result.columns]
    result = result[final_cols]
    return result


@pytest.fixture(scope="module")
def daily_meeting_filtered(wi_raw_snapshots, daily_charge_by_task):
    dataset_copy = wi_raw_snapshots.copy()
    dataset_copy["SnapshotDate"] = dataset_copy["SnapshotDate"].dt.date
    last_snap = dataset_copy["SnapshotDate"].max()
    workdays = []
    cur = last_snap
    while len(workdays) < 3:
        if cur.weekday() < 5:
            workdays.append(cur)
        cur -= timedelta(days=1)
    workdays = sorted(workdays)
    d_earliest, d_mid, d_latest = workdays
    col_completed_earliest = f"Completed for {d_earliest}"
    col_completed_mid = f"Completed for {d_mid}"
    col_completed_latest = f"Completed for {d_latest}"

    today_snap = dataset_copy[dataset_copy["SnapshotDate"] == last_snap].copy()
    id_map = today_snap.set_index("WorkItemId")[["WorkItemType", "Title", "ParentWorkItemId"]]
    leaves = today_snap[
        (today_snap["WorkItemType"].isin({"Task", "Bug"})) |
        ((today_snap["CompletedWork"] != 0) | (today_snap["OriginalEstimate"] != 0) | (today_snap["RemainingWork"] != 0))
    ].copy()

    def parent_chain(row):
        epic = feature = user_story = None
        pid = row.get("ParentWorkItemId")
        for _ in range(10):
            if pd.isna(pid) or int(pid) not in id_map.index:
                break
            parent = id_map.loc[int(pid)]
            ptype = parent["WorkItemType"]
            ptitle = parent["Title"]
            if ptype == "Epic":
                epic = ptitle
            elif ptype == "Feature":
                feature = ptitle
            elif ptype == "User Story":
                user_story = ptitle
            pid = parent["ParentWorkItemId"]
        return pd.Series({"Epic": epic, "Feature": feature, "User Story": user_story})

    hier = leaves.apply(parent_chain, axis=1)
    daily_meeting = pd.concat([leaves.reset_index(drop=True), hier.reset_index(drop=True)], axis=1)

    dct = daily_charge_by_task
    date_cols_charge = [c for c in dct.columns if c not in ("Assigned To", "IterationName", "WorkItemType", "Title", "total_charge", "WorkItemURL")]
    long_charge = dct.melt(id_vars=["WorkItemURL"], value_vars=date_cols_charge, var_name="charge_date", value_name="charge_hours")
    long_charge["charge_date"] = pd.to_datetime(long_charge["charge_date"], errors="coerce").dt.date

    for d, col_name in [(d_earliest, col_completed_earliest), (d_mid, col_completed_mid), (d_latest, col_completed_latest)]:
        day_slice = long_charge[long_charge["charge_date"] == d][["WorkItemURL", "charge_hours"]].rename(columns={"charge_hours": col_name})
        daily_meeting = daily_meeting.merge(day_slice, on="WorkItemURL", how="left")
        daily_meeting[col_name] = daily_meeting[col_name].fillna(0)

    today_ts = pd.Timestamp(today)
    daily_meeting = daily_meeting[[
        "IterationName", "Epic", "User Story", "Title", "Priority",
        "AssignedTo", "State", "OriginalEstimate", "CompletedWork",
        col_completed_earliest, col_completed_mid, col_completed_latest,
        "RemainingWork", "WorkItemURL", "IterationStart", "IterationEnd"
    ]].copy()
    state_closed = daily_meeting["State"].isin(["Closed", "Removed"])
    in_current_sprint = (daily_meeting["IterationStart"] <= today_ts) & (daily_meeting["IterationEnd"] >= today_ts)
    num_cols = [col_completed_earliest, col_completed_mid, col_completed_latest, "RemainingWork"]
    daily_meeting[num_cols] = daily_meeting[num_cols].fillna(0)
    all_zero_activity = (
        (daily_meeting[col_completed_earliest] == 0) & (daily_meeting[col_completed_mid] == 0) &
        (daily_meeting[col_completed_latest] == 0) & (daily_meeting["RemainingWork"] == 0)
    )
    to_drop = state_closed & ~in_current_sprint & all_zero_activity
    result = daily_meeting[~to_drop].copy()
    natkey = natsort_keygen(alg=ns.IGNORECASE)
    result = (
        result.sort_values(
            by=["IterationStart", "IterationName", "Epic", "User Story", "Priority"],
            ascending=True, key=lambda col: col.astype(str).map(natkey)
        ).reset_index(drop=True)
    ).drop(columns=["IterationStart", "IterationEnd"])
    return result


@pytest.fixture(scope="module")
def burndowns(wi_raw_snapshots, sprint_info):
    _, current_sprint, previous_sprint = sprint_info

    def sprint_burndown(df_leaf, sprint_name):
        if not sprint_name:
            return pd.DataFrame(columns=["Date", "RemainingWork"])
        sub = df_leaf.loc[df_leaf["IterationName"] == sprint_name].copy()
        if sub.empty:
            return pd.DataFrame(columns=["Date", "RemainingWork"])
        sprint_start = sub["IterationStart"]
        sprint_end = sub["IterationEnd"]
        if sprint_start.isna().all() or sprint_end.isna().all():
            return pd.DataFrame(columns=["Date", "RemainingWork"])
        sprint_last_inclusive = sprint_end - pd.Timedelta(days=1)
        sub = sub.loc[(sub["SnapshotDate"] >= sprint_start) & (sub["SnapshotDate"] <= sprint_last_inclusive)].copy()
        if sub.empty:
            return pd.DataFrame(columns=["Date", "RemainingWork"])
        sub = sub[sub["State"] != removed_state]
        sub["RemainingBD"] = sub["RemainingWork"]
        sub.loc[sub["State"].isin(doneish_statuses), "RemainingBD"] = 0.0
        bd = (
            sub.groupby(sub["SnapshotDate"].dt.normalize())["RemainingBD"]
               .sum().sort_index().reset_index(name="Date")
               .rename(columns={"RemainingBD": "RemainingWork"})
        )
        return bd

    return sprint_burndown(wi_raw_snapshots, current_sprint), sprint_burndown(wi_raw_snapshots, previous_sprint)


@pytest.fixture(scope="module")
def bd_assignee_pivot(wi_raw_snapshots, burndowns, sprint_info):
    bd_current, _ = burndowns
    _, current_sprint, _ = sprint_info
    wi_snap = wi_raw_snapshots.copy()
    wi_snap["SnapshotDate"] = pd.to_datetime(wi_snap["SnapshotDate"], utc=True).dt.tz_localize(None)
    leaf_curr = wi_snap[
        (wi_snap["IterationName"] == current_sprint) &
        (wi_snap["SnapshotDate"].isin(bd_current.SnapshotDate.unique()))
    ].copy()
    leaf_curr = leaf_curr[leaf_curr["State"] != removed_state]
    leaf_curr["RemainingBD"] = leaf_curr["RemainingWork"]
    leaf_curr.loc[leaf_curr["State"].isin(doneish_statuses), "RemainingBD"] = 0.0
    bd_assignee = leaf_curr.groupby(["SnapshotDate", "AssignedTo"])["RemainingBD"].sum().reset_index()
    result = bd_assignee.pivot(index="SnapshotDate", columns="AssignedTo", values="RemainingBD").fillna(0.0).sort_index()
    result = result.reset_index().rename(columns={"SnapshotDate": "Date"})
    return result


@pytest.fixture(scope="module")
def utilisation_results(wi_raw_snapshots, sprint_info, daily_charge_by_task):
    sprints, _, _ = sprint_info
    sprints_u = sprints[(~sprints['IterationStart'].isna()) & (~sprints['IterationEnd'].isna())].copy()
    sprints_u['IterationStart'] = pd.to_datetime(sprints_u["IterationStart"])
    sprints_u['IterationEnd'] = pd.to_datetime(sprints_u["IterationEnd"])

    utilisation = wi_raw_snapshots[["SnapshotDate", "IterationName", "IterationStart", "IterationEnd", "WorkItemType", "Title", "State", "AssignedTo", "CompletedWork", "OriginalEstimate", "RemainingWork", "WorkItemURL"]].copy()
    utilisation['IterationStart'] = pd.to_datetime(utilisation["IterationStart"])
    utilisation['IterationEnd'] = pd.to_datetime(utilisation["IterationEnd"])
    utilisation = utilisation[utilisation["IterationStart"] >= pd.to_datetime(start_date)]
    utilisation = utilisation[utilisation["SnapshotDate"] == utilisation["IterationStart"]]
    utilisation["PrevDate"] = utilisation["SnapshotDate"] - pd.Timedelta(days=1)
    utilisation = utilisation.merge(
        wi_raw_snapshots[["SnapshotDate", "WorkItemURL", "CompletedWork"]],
        left_on=["WorkItemURL", "PrevDate"], right_on=["WorkItemURL", "SnapshotDate"],
        how="left", suffixes=('', '_start')
    ).drop(columns=["SnapshotDate_start", "PrevDate"])
    utilisation["Completed_at_sprint_start"] = utilisation["CompletedWork_start"].fillna(0)
    utilisation = utilisation.drop(columns=["CompletedWork", "CompletedWork_start"])
    utilisation["Planned_for_sprint"] = (utilisation["OriginalEstimate"] - utilisation["Completed_at_sprint_start"]).clip(lower=0)

    id_cols = ["Assigned To", "IterationName", "WorkItemType", "Title", "WorkItemURL"]
    date_cols_cbs = [c for c in daily_charge_by_task.columns if c not in id_cols + ["total_charge"]]
    charge_by_sprint = daily_charge_by_task.melt(id_vars=id_cols, value_vars=date_cols_cbs, var_name="SnapshotDate", value_name="charge")
    charge_by_sprint["SnapshotDate"] = pd.to_datetime(charge_by_sprint["SnapshotDate"], errors="coerce")
    charge_by_sprint = charge_by_sprint[charge_by_sprint["charge"].fillna(0) != 0]
    charge_by_sprint = charge_by_sprint.merge(sprints_u[['IterationName', 'IterationStart', 'IterationEnd']], how='left', suffixes=('', '_s'))
    intervals = pd.IntervalIndex.from_arrays(sprints_u['IterationStart'], sprints_u['IterationEnd'], closed='both')
    idx = intervals.get_indexer(charge_by_sprint['SnapshotDate'])
    charge_by_sprint['WorkingIteration'] = np.where(idx >= 0, sprints_u['IterationName'].to_numpy()[idx], pd.NA)
    charge_by_sprint = charge_by_sprint.groupby(['WorkingIteration', 'Assigned To', 'WorkItemType', 'Title', 'WorkItemURL'], as_index=False)['charge'].sum()
    charge_by_sprint = charge_by_sprint.dropna(subset=['WorkingIteration'])

    utilisation = utilisation.merge(charge_by_sprint[["WorkItemURL", "WorkingIteration", "charge"]], left_on=["WorkItemURL", "IterationName"], right_on=["WorkItemURL", "WorkingIteration"], how="left")
    utilisation["charge"] = utilisation["charge"].fillna(0)
    utilisation["Planned"] = "yes"
    utilisation = utilisation[["IterationName", "AssignedTo", "WorkItemType", "Title", "Planned", "Planned_for_sprint", "charge", "WorkItemURL"]]

    not_planned = charge_by_sprint.merge(utilisation[["WorkItemURL", "IterationName"]], left_on=["WorkItemURL", "WorkingIteration"], right_on=["WorkItemURL", "IterationName"], how="left")
    not_planned = not_planned[not_planned["IterationName"].isna()]
    not_planned["Planned"] = "no"
    not_planned["Planned_for_sprint"] = 0
    not_planned = not_planned[["WorkingIteration", "Assigned To", "WorkItemType", "Title", "Planned_for_sprint", "charge", "WorkItemURL", "Planned"]].rename(columns={"Assigned To": "AssignedTo", "WorkingIteration": "IterationName"})
    utilisation = pd.concat([utilisation, not_planned])
    utilisation["Completed time"] = utilisation["charge"]

    utilisation = utilisation[utilisation["AssignedTo"] != "Horin Serhii"]
    utilisation["Completed for planned tasks"] = np.where(utilisation["Planned"] == "yes", utilisation[["Completed time", "Planned_for_sprint"]].min(axis=1), 0)
    utilisation["Overcharged_planned_tasks"] = np.where(utilisation["Planned"] == "yes", (utilisation["Completed time"] - utilisation["Planned_for_sprint"]).clip(lower=0), 0)
    utilisation["NOT DONE but planed"] = (utilisation["Planned_for_sprint"] - utilisation["Completed time"]).clip(lower=0)
    utilisation["Completed non-sprint tasks"] = np.where(utilisation["Planned"] == "no", utilisation["Completed time"], 0)

    utilisation_summary = utilisation.groupby(['AssignedTo', 'IterationName'], as_index=False)[['Planned_for_sprint', 'Completed for planned tasks', 'NOT DONE but planed', 'Completed time', 'Completed non-sprint tasks', 'Overcharged_planned_tasks']].sum()
    utilisation_time_map = {'Vasilov Denys': 60, 'Vasilov Denys S.': 60, 'Sukonnik Illia V.': 50, 'Kolodieiev Ivan': 50, 'Andrianov Vladyslav': 40}
    utilisation_summary['Utilisation time'] = utilisation_summary['AssignedTo'].map(utilisation_time_map).fillna(0).astype(int)
    utilisation_summary = utilisation_summary.sort_values(["AssignedTo", "IterationName"], ascending=False)
    utilisation_summary["Underutilised"] = utilisation_summary['Utilisation time'] - utilisation_summary["Completed time"]
    utilisation_summary["Overplanned / Underplanned"] = utilisation_summary['Planned_for_sprint'] - utilisation_summary["Utilisation time"]
    utilisation_summary["Work hours debt"] = np.maximum(np.minimum(utilisation_summary["Planned_for_sprint"], utilisation_summary["Utilisation time"]), utilisation_summary["Utilisation time"]) - utilisation_summary["Completed time"]
    utilisation_summary = utilisation_summary[["AssignedTo", "IterationName", "Utilisation time", "Planned_for_sprint", "Overplanned / Underplanned", "Completed for planned tasks", 'Overcharged_planned_tasks', "Completed non-sprint tasks", "Work hours debt"]]

    return charge_by_sprint, utilisation, utilisation_summary


# ============================================================
# Tests
# ============================================================

def test_wi_raw_snapshots(wi_raw_snapshots):
    _compare_dataframes(wi_raw_snapshots, _load_baseline("wi_raw_snapshots"), "wi_raw_snapshots")


def test_sprints(sprint_info):
    sprints, _, _ = sprint_info
    _compare_dataframes(sprints, _load_baseline("sprints"), "sprints")


def test_scalars(sprint_info):
    _, current_sprint, previous_sprint = sprint_info
    expected = _load_baseline("scalars")
    assert (current_sprint or "") == expected.iloc[0]["current_sprint"], f"current_sprint mismatch: {current_sprint}"
    assert (previous_sprint or "") == expected.iloc[0]["previous_sprint"], f"previous_sprint mismatch: {previous_sprint}"


def test_daily_charge_by_person(daily_charge_by_person):
    _compare_dataframes(daily_charge_by_person, _load_baseline("daily_charge_by_person"), "daily_charge_by_person")


def test_daily_charge_by_task(daily_charge_by_task):
    _compare_dataframes(daily_charge_by_task, _load_baseline("daily_charge_by_task"), "daily_charge_by_task")


def test_daily_meeting_filtered(daily_meeting_filtered):
    _compare_dataframes(daily_meeting_filtered, _load_baseline("daily_meeting_filtered"), "daily_meeting_filtered")


def test_bd_current(burndowns):
    bd_current, _ = burndowns
    _compare_dataframes(bd_current, _load_baseline("bd_current"), "bd_current")


def test_bd_prev(burndowns):
    _, bd_prev = burndowns
    _compare_dataframes(bd_prev, _load_baseline("bd_prev"), "bd_prev")


def test_bd_assignee_pivot(bd_assignee_pivot):
    _compare_dataframes(bd_assignee_pivot, _load_baseline("bd_assignee_pivot"), "bd_assignee_pivot")


def test_charge_by_sprint(utilisation_results):
    charge_by_sprint, _, _ = utilisation_results
    _compare_dataframes(charge_by_sprint, _load_baseline("charge_by_sprint"), "charge_by_sprint")


def test_utilisation(utilisation_results):
    _, utilisation, _ = utilisation_results
    _compare_dataframes(utilisation, _load_baseline("utilisation"), "utilisation")


def test_utilisation_summary(utilisation_results):
    _, _, utilisation_summary = utilisation_results
    _compare_dataframes(utilisation_summary, _load_baseline("utilisation_summary"), "utilisation_summary")
