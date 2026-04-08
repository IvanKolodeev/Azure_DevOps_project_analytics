"""
Run the notebook calculation pipeline and save baseline dataframes as parquet files.

Usage (from repo root):
    python tests/freeze_baselines.py

Reads source data from data/, runs calculations, saves results to tests/baselines/.
"""

import sys
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import numpy as np
from natsort import natsort_keygen, ns

# -- Setup paths --
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import ado_reporting as cl
import utilisation as ut

BASELINE_DIR = REPO_ROOT / "tests" / "baselines"
BASELINE_DIR.mkdir(parents=True, exist_ok=True)

SOURCE_DIR = BASELINE_DIR / "source"
SOURCE_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# Config (mirrors notebook cell 1)
# ============================================================
project = "MarketRMS"
start_date = date(2026, 3, 1)
today = date(2026, 4, 1)  # frozen date for reproducibility
DAYS_BACK = (today - start_date).days + 1

BASE_DIR = REPO_ROOT / "data"

leaf_types = ["Task", "Bug"]
removed_state = "Removed"
doneish_statuses = {"Code Review", "Ready for UAT", "Ready for Prod", "In UAT", "Closed"}


def _freeze_source_data():
    """Copy source parquet files to tests/baselines/source/ for reproducibility."""
    import shutil

    hist_src = BASE_DIR / f"{project}_work_item_snapshot.parquet"
    live_dir = BASE_DIR / "live_snapshots"

    if hist_src.exists():
        shutil.copy2(hist_src, SOURCE_DIR / hist_src.name)
        print(f"  Copied {hist_src.name}")

    live_dest = SOURCE_DIR / "live_snapshots"
    live_dest.mkdir(exist_ok=True)
    for f in sorted(live_dir.glob(f"{project}_live_snapshot_*.parquet")):
        shutil.copy2(f, live_dest / f.name)
        print(f"  Copied live_snapshots/{f.name}")


def _run_pipeline():
    """Execute the notebook logic and return all key dataframes."""
    results = {}

    # -- prepare dataset --
    wi_raw_snapshots = cl.get_wi_snapshots(
        project=project,
        days_back=DAYS_BACK,
        removed_state=removed_state,
    )
    results["wi_raw_snapshots"] = wi_raw_snapshots

    # -- sprint info --
    sprints, current_sprint, previous_sprint = cl.get_sprint_info(wi_raw_snapshots, today)
    results["sprints"] = sprints

    # -- daily charge calc (cell 7) --
    df = wi_raw_snapshots.copy()
    df = df.sort_values(["WorkItemId", "SnapshotDate", "WorkItemRevisionSK"])
    df = df.drop_duplicates(["WorkItemId", "SnapshotDate"], keep="last")
    df["daily_charge"] = (
        df.groupby("WorkItemId")["CompletedWork"]
          .diff()
          .fillna(df["CompletedWork"])
    )

    # -- daily charge by person (cell 8) --
    agg1 = (
        df.groupby(["AssignedTo", "SnapshotDate"], dropna=False)["daily_charge"]
          .sum()
          .reset_index()
    )
    agg1["AssignedTo"] = agg1["AssignedTo"].fillna("Unassigned")
    daily_charge_by_person = (
        agg1.pivot(index="AssignedTo", columns="SnapshotDate", values="daily_charge")
            .fillna(0)
    )
    daily_charge_by_person.columns.name = None
    daily_charge_by_person = daily_charge_by_person.sort_index(axis=1)
    earliest_col = daily_charge_by_person.columns.min()
    daily_charge_by_person = daily_charge_by_person.drop(columns=[earliest_col])
    daily_charge_by_person["total_charge"] = daily_charge_by_person.sum(axis=1)
    daily_charge_by_person = daily_charge_by_person[daily_charge_by_person["total_charge"] != 0]
    daily_charge_by_person.reset_index(inplace=True)
    daily_charge_by_person.rename(columns={"AssignedTo": "Assigned To"}, inplace=True)
    date_cols = [c for c in daily_charge_by_person.columns if c not in ("Assigned To", "total_charge")]
    rename_date_cols = {c: c.strftime("%Y-%m-%d") if hasattr(c, "strftime") else str(c) for c in date_cols}
    daily_charge_by_person.rename(columns=rename_date_cols, inplace=True)
    results["daily_charge_by_person"] = daily_charge_by_person

    # -- daily charge by task (cell 9) --
    agg2 = (
        df.groupby(
            ["AssignedTo", "IterationName", "WorkItemType", "Title", "WorkItemURL", "SnapshotDate"],
            dropna=False
        )["daily_charge"]
        .sum()
        .reset_index()
    )
    agg2["AssignedTo"] = agg2["AssignedTo"].fillna("Unassigned")
    daily_charge_by_task = (
        agg2.pivot(
            index=["AssignedTo", "IterationName", "WorkItemType", "Title", "WorkItemURL"],
            columns="SnapshotDate",
            values="daily_charge"
        )
        .fillna(0)
    )
    daily_charge_by_task = daily_charge_by_task.sort_index(axis=1)
    earliest_col = daily_charge_by_task.columns.min()
    daily_charge_by_task = daily_charge_by_task.drop(columns=[earliest_col])
    daily_charge_by_task["total_charge"] = daily_charge_by_task.sum(axis=1)
    daily_charge_by_task = daily_charge_by_task[daily_charge_by_task["total_charge"] != 0]
    date_cols_t = [c for c in daily_charge_by_task.columns if c != "total_charge"]
    rename_dates = {d: d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d) for d in date_cols_t}
    daily_charge_by_task = daily_charge_by_task.rename(columns=rename_dates)
    daily_charge_by_task.reset_index(inplace=True)
    daily_charge_by_task.rename(columns={"AssignedTo": "Assigned To"}, inplace=True)
    base_cols = ["Assigned To", "IterationName", "WorkItemType", "Title"]
    date_cols_str = [rename_dates[d] for d in date_cols_t]
    final_cols = base_cols + date_cols_str + ["total_charge", "WorkItemURL"]
    final_cols = [c for c in final_cols if c in daily_charge_by_task.columns]
    daily_charge_by_task = daily_charge_by_task[final_cols]
    results["daily_charge_by_task"] = daily_charge_by_task

    # -- daily meeting / stand-up prep (cells 12-17) --
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
    d_earliest, d_mid, d_latest = workdays[0], workdays[1], workdays[2]
    col_completed_earliest = f"Completed for {d_earliest}"
    col_completed_mid = f"Completed for {d_mid}"
    col_completed_latest = f"Completed for {d_latest}"

    today_snap = dataset_copy[dataset_copy["SnapshotDate"] == last_snap].copy()
    id_map = today_snap.set_index("WorkItemId")[["WorkItemType", "Title", "ParentWorkItemId"]]
    leaves = today_snap[
        (today_snap["WorkItemType"].isin({"Task", "Bug"})) |
        (
            (today_snap["CompletedWork"] != 0) |
            (today_snap["OriginalEstimate"] != 0) |
            (today_snap["RemainingWork"] != 0)
        )
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

    # completed time for last 3 business days
    date_cols_charge = [c for c in daily_charge_by_task.columns
                        if c not in ("Assigned To", "IterationName", "WorkItemType", "Title", "total_charge", "WorkItemURL")]
    long_charge = daily_charge_by_task.melt(
        id_vars=["WorkItemURL"],
        value_vars=date_cols_charge,
        var_name="charge_date",
        value_name="charge_hours"
    )
    long_charge["charge_date"] = pd.to_datetime(long_charge["charge_date"], errors="coerce").dt.date

    for d, col_name in [(d_earliest, col_completed_earliest),
                         (d_mid, col_completed_mid),
                         (d_latest, col_completed_latest)]:
        day_slice = long_charge[long_charge["charge_date"] == d][["WorkItemURL", "charge_hours"]]
        day_slice = day_slice.rename(columns={"charge_hours": col_name})
        daily_meeting = daily_meeting.merge(day_slice, on="WorkItemURL", how="left")
        daily_meeting[col_name] = daily_meeting[col_name].fillna(0)

    # final filtering
    today_ts = pd.Timestamp(today)
    daily_meeting = daily_meeting[[
        "IterationName", "Epic", "User Story", "Title", "Priority",
        "AssignedTo", "State", "OriginalEstimate", "CompletedWork",
        col_completed_earliest, col_completed_mid, col_completed_latest,
        "RemainingWork", "WorkItemURL", "IterationStart", "IterationEnd"
    ]].copy()
    state_closed = daily_meeting["State"].isin(["Closed", "Removed"])
    in_current_sprint = (
        (daily_meeting["IterationStart"] <= today_ts) &
        (daily_meeting["IterationEnd"] >= today_ts)
    )
    not_current_sprint = ~in_current_sprint
    num_cols = [col_completed_earliest, col_completed_mid, col_completed_latest, "RemainingWork"]
    daily_meeting[num_cols] = daily_meeting[num_cols].fillna(0)
    all_zero_activity = (
        (daily_meeting[col_completed_earliest] == 0) &
        (daily_meeting[col_completed_mid] == 0) &
        (daily_meeting[col_completed_latest] == 0) &
        (daily_meeting["RemainingWork"] == 0)
    )
    to_drop = state_closed & not_current_sprint & all_zero_activity
    daily_meeting_filtered = daily_meeting[~to_drop].copy()
    natkey = natsort_keygen(alg=ns.IGNORECASE)
    daily_meeting_filtered = (
        daily_meeting_filtered
        .sort_values(
            by=["IterationStart", "IterationName", "Epic", "User Story", "Priority"],
            ascending=[True, True, True, True, True],
            key=lambda col: col.astype(str).map(natkey)
        )
        .reset_index(drop=True)
    ).drop(columns=["IterationStart", "IterationEnd"])
    results["daily_meeting_filtered"] = daily_meeting_filtered

    # -- burndowns (cells 19-21) --
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
        sub = sub.loc[
            (sub["SnapshotDate"] >= sprint_start) &
            (sub["SnapshotDate"] <= sprint_last_inclusive)
        ].copy()
        if sub.empty:
            return pd.DataFrame(columns=["Date", "RemainingWork"])
        sub = sub[sub["State"] != removed_state]
        sub["RemainingBD"] = sub["RemainingWork"]
        sub.loc[sub["State"].isin(doneish_statuses), "RemainingBD"] = 0.0
        bd = (
            sub.groupby(sub["SnapshotDate"].dt.normalize())["RemainingBD"]
               .sum()
               .sort_index()
               .reset_index(name="Date")
               .rename(columns={"RemainingBD": "RemainingWork"})
        )
        return bd

    bd_current = sprint_burndown(wi_raw_snapshots, current_sprint)
    bd_prev = sprint_burndown(wi_raw_snapshots, previous_sprint)
    results["bd_current"] = bd_current
    results["bd_prev"] = bd_prev

    # -- BD by assignee --
    wi_snap = wi_raw_snapshots.copy()
    wi_snap["SnapshotDate"] = pd.to_datetime(wi_snap["SnapshotDate"], utc=True).dt.tz_localize(None)
    leaf_curr = wi_snap[
        (wi_snap["IterationName"] == current_sprint)
        & (wi_snap["SnapshotDate"].isin(bd_current.SnapshotDate.unique()))
    ].copy()
    leaf_curr = leaf_curr[leaf_curr["State"] != removed_state]
    leaf_curr["RemainingBD"] = leaf_curr["RemainingWork"]
    leaf_curr.loc[leaf_curr["State"].isin(doneish_statuses), "RemainingBD"] = 0.0
    bd_assignee = (
        leaf_curr.groupby(["SnapshotDate", "AssignedTo"])["RemainingBD"]
                 .sum()
                 .reset_index()
    )
    bd_assignee_pivot = (
        bd_assignee
        .pivot(index="SnapshotDate", columns="AssignedTo", values="RemainingBD")
        .fillna(0.0)
        .sort_index()
    )
    bd_assignee_pivot = bd_assignee_pivot.reset_index().rename(columns={"SnapshotDate": "Date"})
    results["bd_assignee_pivot"] = bd_assignee_pivot

    # -- utilisation (delegated to src/utilisation.py) --
    # The local daily_charge_by_task here uses "Assigned To"; the utilisation
    # module expects "AssignedTo" to match wi_raw_snapshots.
    dct = daily_charge_by_task.rename(columns={"Assigned To": "AssignedTo"})
    charge_by_sprint = ut.compute_charge_by_sprint(dct, sprints)
    results["charge_by_sprint"] = charge_by_sprint

    utilisation = ut.compute_utilisation_detail(
        wi_raw_snapshots=wi_raw_snapshots,
        sprints=sprints,
        charge_by_sprint=charge_by_sprint,
        start_date=start_date,
    )
    results["utilisation"] = utilisation

    utilisation_summary = ut.compute_utilisation_summary(utilisation)
    results["utilisation_summary"] = utilisation_summary

    # save scalar values
    scalars = pd.DataFrame([{
        "current_sprint": current_sprint or "",
        "previous_sprint": previous_sprint or "",
    }])
    results["scalars"] = scalars

    return results


def main():
    print("Freezing source data...")
    _freeze_source_data()

    print("\nRunning pipeline...")
    results = _run_pipeline()

    print(f"\nSaving {len(results)} baseline dataframes to {BASELINE_DIR}/")
    for name, df in results.items():
        path = BASELINE_DIR / f"{name}.parquet"
        df.to_parquet(path, index=False)
        print(f"  {name}: {df.shape} -> {path.name}")

    print("\nDone! Baselines frozen.")


if __name__ == "__main__":
    main()
