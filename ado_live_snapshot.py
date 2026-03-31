import os
from datetime import datetime

import pandas as pd
import requests
from requests.auth import HTTPBasicAuth


FIELDS_TO_GET = [
    "System.Id",
    "System.AreaPath",
    "System.IterationPath",
    "System.WorkItemType",
    "System.State",
    "System.AssignedTo",
    "System.CreatedDate",
    "System.CreatedBy",
    "System.ChangedDate",
    "System.ChangedBy",
    "System.Title",
    "Microsoft.VSTS.Scheduling.OriginalEstimate",
    "Microsoft.VSTS.Common.StateChangeDate",
    "Microsoft.VSTS.Common.ClosedDate",
    "Microsoft.VSTS.Common.Priority",
    "System.Tags",
    "Microsoft.VSTS.Scheduling.RemainingWork",
    "System.History",
    "Microsoft.VSTS.Scheduling.CompletedWork",
    "System.TeamProject",
    "System.Parent",
]

RENAME_MAP = {
    "fields.System.Id": "WorkItemId",
    "fields.System.AreaPath": "AreaPath",
    "fields.System.IterationPath": "IterationPath",
    "fields.System.WorkItemType": "WorkItemType",
    "fields.System.State": "State",
    "fields.System.TeamProject": "ProjectName",
    "fields.System.Parent": "ParentWorkItemId",
    "fields.System.Title": "Title",
    "fields.System.CreatedDate": "CreatedDate",
    "fields.System.ChangedDate": "ChangedDate",
    "fields.Microsoft.VSTS.Scheduling.OriginalEstimate": "OriginalEstimate",
    "fields.Microsoft.VSTS.Common.StateChangeDate": "StateChangeDate",
    "fields.Microsoft.VSTS.Common.ClosedDate": "ClosedDate",
    "fields.Microsoft.VSTS.Common.Priority": "Priority",
    "fields.System.Tags": "TagNames",
    "fields.Microsoft.VSTS.Scheduling.RemainingWork": "RemainingWork",
    "fields.Microsoft.VSTS.Scheduling.CompletedWork": "CompletedWork",
    "fields.System.History": "Comments",
    "fields.System.AssignedTo.displayName": "AssignedTo",
    "fields.System.AssignedTo.uniqueName": "AssignedToEmail",
    "fields.System.CreatedBy.displayName": "CreatedBy",
    "fields.System.CreatedBy.uniqueName": "CreatedByEmail",
    "fields.System.ChangedBy.displayName": "ChangedBy",
    "fields.System.ChangedBy.uniqueName": "ChangedByEmail",
}

FINAL_COLS = [
    "WorkItemId",
    "AreaPath",
    "IterationPath",
    "WorkItemType",
    "State",
    "ProjectName",
    "ParentWorkItemId",
    "Title",
    "CreatedDate",
    "ChangedDate",
    "OriginalEstimate",
    "StateChangeDate",
    "ClosedDate",
    "Priority",
    "TagNames",
    "RemainingWork",
    "CompletedWork",
    "Comments",
    "AssignedTo",
    "AssignedToEmail",
    "CreatedBy",
    "CreatedByEmail",
    "ChangedBy",
    "ChangedByEmail",
]

DATE_COLS = ["CreatedDate", "ChangedDate", "StateChangeDate", "ClosedDate"]


def _fetch_work_item_ids(org: str, project: str, auth: HTTPBasicAuth) -> list[int]:
    """Run a WIQL query to get all work item IDs for the given project."""
    wiql_url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.0"
    wiql_query = f"SELECT [System.Id] FROM workitems WHERE [System.TeamProject] = '{project}'"
    resp = requests.post(wiql_url, json={"query": wiql_query}, auth=auth)
    resp.raise_for_status()
    ids = [w["id"] for w in resp.json().get("workItems", [])]
    print(f"Retrieved {len(ids)} work item IDs for project '{project}'")
    if not ids:
        raise ValueError(f"No work items found for project '{project}'")
    return ids


def _fetch_work_items_batch(org: str, ids: list[int], auth: HTTPBasicAuth, chunk_size: int = 200) -> list[dict]:
    """Fetch full work item details in batches using the WorkItemsBatch API."""
    batch_url = f"https://dev.azure.com/{org}/_apis/wit/workitemsbatch?api-version=7.0"
    rows = []
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        print(f"  Fetching items {i + 1}-{i + len(chunk)} of {len(ids)}")
        body = {"ids": chunk, "fields": FIELDS_TO_GET, "errorPolicy": "Omit"}
        r = requests.post(batch_url, json=body, auth=auth)
        r.raise_for_status()
        rows.extend(r.json().get("value", []))
    print(f"Retrieved total {len(rows)} work items")
    return rows


def _flatten_iterations(node: dict, parent_path: str | None = None) -> list[dict]:
    """Recursively flatten the iteration classification tree into a list of dicts with path and dates."""
    name = node["name"]
    path = name if parent_path is None else f"{parent_path}\\{name}"
    attrs = node.get("attributes", {})
    rows = [
        {
            "IterationPath": path,
            "IterationStart": attrs.get("startDate"),
            "IterationEnd": attrs.get("finishDate"),
        }
    ]
    for child in node.get("children", []):
        rows.extend(_flatten_iterations(child, path))
    return rows


def _fetch_iteration_dates(org: str, project: str, auth: HTTPBasicAuth) -> pd.DataFrame:
    """Fetch iteration start/end dates from the classification nodes API and return as a DataFrame."""
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/classificationnodes/iterations?$depth=10&api-version=7.0"
    resp = requests.get(url, auth=auth)
    resp.raise_for_status()
    df = pd.DataFrame(_flatten_iterations(resp.json()))
    df["IterationStart"] = pd.to_datetime(df["IterationStart"], errors="coerce")
    df["IterationEnd"] = pd.to_datetime(df["IterationEnd"], errors="coerce")
    return df


def _last_segment(series: pd.Series) -> pd.Series:
    """Extract the last backslash-separated segment from a path series (e.g. 'Project\\Area' -> 'Area')."""
    return series.fillna("").str.split("\\").str[-1].replace("", pd.NA)


def download_live_snapshot(
    org: str,
    project: str,
    auth: HTTPBasicAuth,
    output_folder: str = "live_snapshots",
) -> pd.DataFrame:
    """Download a live snapshot of all work items from Azure DevOps and save as Parquet.

    Returns the resulting DataFrame.
    """
    base_url = f"https://dev.azure.com/{org}"

    # 1. Get work item IDs via WIQL
    ids = _fetch_work_item_ids(org, project, auth)

    # 2. Fetch full work item data in batches
    rows = _fetch_work_items_batch(org, ids, auth)

    # 3. Flatten and rename
    wi_live = pd.json_normalize(rows, sep=".")
    wi_live = wi_live.rename(columns=RENAME_MAP)
    wi_live = wi_live[FINAL_COLS].copy()

    # 4. Derived columns
    wi_live["AreaName"] = _last_segment(wi_live["AreaPath"])
    wi_live["IterationName"] = _last_segment(wi_live["IterationPath"])
    wi_live["WorkItemURL"] = (
        base_url + "/" + wi_live["ProjectName"].astype(str) + "/_workitems/edit/" + wi_live["WorkItemId"].astype(str)
    )

    # 5. Parse dates
    for col in DATE_COLS:
        wi_live[col] = pd.to_datetime(wi_live[col], utc=True, errors="coerce", format="ISO8601")

    # 6. Merge iteration start/end dates
    iters_df = _fetch_iteration_dates(org, project, auth)
    wi_live = wi_live.merge(iters_df, on="IterationPath", how="left")

    # 7. Save to parquet
    os.makedirs(output_folder, exist_ok=True)
    today_str = datetime.today().strftime("%Y-%m-%d")
    file_path = os.path.join(output_folder, f"{project}_live_snapshot_{today_str}.parquet")
    wi_live.to_parquet(file_path, index=False)
    print(f"Saved {len(wi_live)} rows to {file_path}")

    return wi_live
