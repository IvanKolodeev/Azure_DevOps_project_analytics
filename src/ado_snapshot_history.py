import os
from datetime import date, timedelta

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth


def _fetch_earliest_snapshot_date(base: str, auth: HTTPBasicAuth) -> date:
    """Query OData WorkItems for the earliest CreatedDate in the project.

    Uses WorkItems (not WorkItemSnapshot) because snapshot entity sets
    don't support direct broad reads with $orderby/$top.
    """
    url = f"{base}/WorkItems"
    params = {
        "$select": "CreatedDate",
        "$orderby": "CreatedDate asc",
        "$top": "1",
    }
    resp = requests.get(url, auth=auth, params=params)
    resp.raise_for_status()
    rows = resp.json().get("value", [])
    if not rows:
        raise ValueError("No work items found in OData for this project.")
    return pd.to_datetime(rows[0]["CreatedDate"]).date()


def _load_existing_snapshots(snapshot_path: str) -> pd.DataFrame | None:
    """Load existing parquet file if it exists, otherwise return None."""
    if not os.path.exists(snapshot_path):
        return None
    df = pd.read_parquet(snapshot_path)
    df["SnapshotDate"] = pd.to_datetime(df["SnapshotDate"], utc=True, errors="coerce")
    return df


def _fetch_snapshot_days(
    base: str, auth: HTTPBasicAuth, start: date, end: date
) -> pd.DataFrame:
    """Fetch WorkItemSnapshot rows from OData, one day at a time, from start to end inclusive."""
    wi_url = f"{base}/WorkItemSnapshot"
    select_fields = (
        "DateValue,ProjectSK,WorkItemId,WorkItemType,Title,State,StateCategory,"
        "StateChangeDate,Priority,TagNames,ParentWorkItemId,RevisedDate,"
        "AssignedToUserSK,ChangedByUserSK,CreatedByUserSK,"
        "OriginalEstimate,CompletedWork,RemainingWork,StoryPoints,"
        "WorkItemRevisionSK,AreaSK,IterationSK,"
        "ChangedDate,CreatedDate,ClosedDate,InProgressDate,CompletedDate"
    )

    all_rows: list[dict] = []
    cur = start
    while cur <= end:
        next_day = cur + timedelta(days=1)
        params: dict | None = {
            "$select": select_fields,
            "$filter": f"DateValue ge {cur.isoformat()} and DateValue lt {next_day.isoformat()}",
            "$count": "true",
        }
        url: str | None = wi_url
        while url:
            resp = requests.get(url, auth=auth, params=params)
            resp.raise_for_status()
            data = resp.json()
            all_rows.extend(data["value"])
            url = data.get("@odata.nextLink")
            params = None  # nextLink already contains query params
        print(f"Loaded snapshot for {cur} (rows so far: {len(all_rows)})")
        cur = next_day

    return pd.DataFrame(all_rows)


def _parse_snapshot_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Parse date columns and rename DateValue -> SnapshotDate.

    ``DateValue`` from OData represents a calendar date, but ADO encodes it as
    midnight in the organisation's local timezone (e.g. 21:00 UTC for UTC+3).
    Rounding to the nearest day recovers the intended calendar date regardless
    of the server's UTC offset (handles offsets up to ±12 h).
    """
    date_cols = [
        "DateValue", "ChangedDate", "CreatedDate",
        "ClosedDate", "InProgressDate", "CompletedDate", "StateChangeDate",
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="ISO8601")
    # Normalise SnapshotDate to midnight UTC so that e.g. 2026-07-08 21:00 UTC
    # (= midnight UTC+3) rounds to 2026-07-09 00:00 UTC, matching the intended date.
    if "DateValue" in df.columns:
        df["DateValue"] = df["DateValue"].dt.round("D")
    df = df.rename(columns={"DateValue": "SnapshotDate"})
    return df


def _fetch_dimension_tables(base: str, auth: HTTPBasicAuth) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch Users, Areas, Iterations, and Projects dimension tables from OData."""
    # Users
    resp = requests.get(f"{base}/Users", auth=auth, params={"$select": "UserSK,UserName,UserEmail"})
    resp.raise_for_status()
    users_df = pd.DataFrame(resp.json()["value"])

    # Areas
    resp = requests.get(f"{base}/Areas", auth=auth, params={"$select": "AreaSK,AreaName,AreaPath"})
    resp.raise_for_status()
    areas_df = pd.DataFrame(resp.json()["value"])

    # Iterations
    resp = requests.get(f"{base}/Iterations", auth=auth, params={"$select": "IterationSK,IterationName,IterationPath,StartDate,EndDate"})
    resp.raise_for_status()
    iters_df = pd.DataFrame(resp.json()["value"])
    for col in ["StartDate", "EndDate"]:
        iters_df[col] = pd.to_datetime(iters_df[col], utc=True)

    # Projects
    resp = requests.get(f"{base}/Projects", auth=auth, params={"$select": "ProjectSK,ProjectName"})
    resp.raise_for_status()
    projects_df = pd.DataFrame(resp.json()["value"])

    return users_df, areas_df, iters_df, projects_df


def _resolve_surrogate_keys(
    df: pd.DataFrame,
    users_df: pd.DataFrame,
    areas_df: pd.DataFrame,
    iters_df: pd.DataFrame,
    projects_df: pd.DataFrame,
    base_url: str,
) -> pd.DataFrame:
    """Replace surrogate-key columns with human-readable names and add WorkItemURL."""
    df = df.copy()

    user_sk_map = {
        "AssignedToUserSK": "AssignedTo",
        "ChangedByUserSK": "ChangedBy",
        "CreatedByUserSK": "CreatedBy",
    }
    for sk_col, name_col in user_sk_map.items():
        df = (
            df.merge(users_df[["UserSK", "UserName"]], left_on=sk_col, right_on="UserSK", how="left")
            .rename(columns={"UserName": name_col})
            .drop(columns=[sk_col, "UserSK"])
        )

    df = (
        df.merge(areas_df, on="AreaSK", how="left")
        .drop(columns=["AreaSK"])
        .merge(iters_df, on="IterationSK", how="left")
        .rename(columns={"StartDate": "IterationStart", "EndDate": "IterationEnd"})
        .drop(columns=["IterationSK"])
        .merge(projects_df, on="ProjectSK", how="left")
        .drop(columns=["ProjectSK"])
    )

    df["WorkItemURL"] = (
        base_url + "/" + df["ProjectName"].astype(str) + "/_workitems/edit/" + df["WorkItemId"].astype(str)
    )

    return df


def download_snapshot_history(
    org: str,
    project: str,
    auth: HTTPBasicAuth,
    snapshot_path: str,
    today: date | None = None,
) -> pd.DataFrame:
    """Download historical snapshots from OData and append to the parquet file.

    On first run (no existing parquet), starts from the earliest date available
    in the project. Returns the combined DataFrame.
    """
    if today is None:
        today = date.today()

    base = f"https://analytics.dev.azure.com/{org}/{project}/_odata/v3.0-preview"
    base_url = f"https://dev.azure.com/{org}"

    # Determine start date
    existing = _load_existing_snapshots(snapshot_path)
    if existing is not None:
        last_snapshot_date = existing["SnapshotDate"].max().date()
        start = last_snapshot_date + timedelta(days=1)
    else:
        start = _fetch_earliest_snapshot_date(base, auth)
        print(f"First run: starting from earliest available date {start}")

    # ADO Analytics may contain incomplete data for today, so stop at yesterday
    end = today - timedelta(days=1)

    if start > end:
        print("No new days to load - data is up to date.")
        if existing is not None:
            return existing
        return pd.DataFrame()

    print(f"Loading snapshots from {start} to {end}")

    # Fetch new snapshot rows
    new_snapshots = _fetch_snapshot_days(base, auth, start, end)
    print(f"New rows loaded: {len(new_snapshots)}")

    # Parse dates
    new_snapshots = _parse_snapshot_dates(new_snapshots)

    # Resolve surrogate keys
    users_df, areas_df, iters_df, projects_df = _fetch_dimension_tables(base, auth)
    new_snapshots = _resolve_surrogate_keys(new_snapshots, users_df, areas_df, iters_df, projects_df, base_url)

    # Combine with existing data
    if existing is not None:
        work_items = pd.concat([existing, new_snapshots], ignore_index=True)
    else:
        work_items = new_snapshots

    work_items = work_items.drop_duplicates(subset=["WorkItemId", "SnapshotDate"], keep="last")

    # Save
    work_items.to_parquet(snapshot_path, index=False)
    print(f"{snapshot_path} updated. Total rows now: {len(work_items)}")

    return work_items
