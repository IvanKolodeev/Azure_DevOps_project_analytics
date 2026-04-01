import re
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import pandas as pd


class SprintInfo(NamedTuple):
    sprints: pd.DataFrame
    current_sprint: str | None
    previous_sprint: str | None


# ---------------------------------------------------------------------------
# Private helpers for prepare_dataset
# ---------------------------------------------------------------------------

def _load_historical_snapshots(
    hist_snapshot_path: Path, days_back: int
) -> pd.DataFrame:
    """Load historical parquet and filter to the last `days_back` days."""
    hist = pd.read_parquet(hist_snapshot_path)
    hist["SnapshotDate"] = pd.to_datetime(
        hist["SnapshotDate"], utc=True, errors="coerce"
    ).dt.date

    days_from_hist = max(days_back, 0)
    if days_from_hist > 0:
        last_hist_date = hist["SnapshotDate"].max()
        hist_cutoff = last_hist_date - timedelta(days=days_from_hist - 1)
        hist_recent = hist[hist["SnapshotDate"] >= hist_cutoff].copy()
    else:
        hist_recent = hist.iloc[0:0].copy()

    print(
        f"Historical data selected: "
        f"{hist_recent.SnapshotDate.min()} - {hist_recent.SnapshotDate.max()}"
    )
    return hist_recent


def _load_live_snapshot(live_snap_dir: Path, project: str) -> pd.DataFrame:
    """Find and load the latest live snapshot parquet file."""
    live_snap_dir.mkdir(exist_ok=True)
    live_files = sorted(live_snap_dir.glob(f"{project}_live_snapshot_*.parquet"))

    if not live_files:
        print("No live snapshot files found in", live_snap_dir)
        return pd.DataFrame()

    def _extract_date(p: Path) -> date:
        m = re.search(r"_live_snapshot_(\d{4}-\d{2}-\d{2})\.parquet", p.name)
        return pd.to_datetime(m.group(1)).date() if m else date.min

    latest_file = max(live_files, key=_extract_date)
    live_date = _extract_date(latest_file)
    print(f"Latest live snapshot file: {latest_file.name} (date={live_date})")

    live_df = pd.read_parquet(latest_file)
    live_df["SnapshotDate"] = live_date
    print("Live rows loaded:", len(live_df))
    return live_df


def _combine_frames(
    hist_recent: pd.DataFrame, live_df: pd.DataFrame
) -> pd.DataFrame:
    """Concatenate historical and live frames, dropping all-NaN columns."""
    frames = []
    if not hist_recent.empty:
        frames.append(hist_recent)
    if not live_df.empty:
        if not hist_recent.empty:
            live_df = live_df.reindex(columns=hist_recent.columns, fill_value=pd.NA)
        frames.append(live_df)

    cleaned_frames = []
    for f in frames:
        if not f.empty:
            f = f.loc[:, f.notna().any(axis=0)]
            if not f.empty:
                cleaned_frames.append(f)

    if cleaned_frames:
        dataset = pd.concat(cleaned_frames, ignore_index=True)
    else:
        dataset = pd.DataFrame()

    print(
        "Final dataset date range:",
        dataset["SnapshotDate"].min() if not dataset.empty else None,
        "→",
        dataset["SnapshotDate"].max() if not dataset.empty else None,
    )
    return dataset


def _coerce_dtypes(dataset: pd.DataFrame) -> pd.DataFrame:
    """Normalize column data types in-place and return the dataframe."""
    date_cols = [
        "SnapshotDate", "ChangedDate", "CreatedDate", "ClosedDate",
        "InProgressDate", "CompletedDate", "StateChangeDate",
        "IterationStart", "IterationEnd",
    ]
    for c in date_cols:
        if c in dataset.columns:
            dataset[c] = (
                pd.to_datetime(dataset[c], utc=True, errors="coerce")
                .dt.tz_localize(None)
            )

    # IterationStart/End are date-level; strip time to avoid spurious duplicates
    for c in ["IterationStart", "IterationEnd"]:
        if c in dataset.columns:
            dataset[c] = dataset[c].dt.normalize()

    for c in ["OriginalEstimate", "CompletedWork", "RemainingWork"]:
        dataset[c] = pd.to_numeric(dataset[c], errors="coerce").fillna(0.0)

    for c in ["ParentWorkItemId", "Priority"]:
        dataset[c] = pd.to_numeric(dataset[c], errors="coerce").astype("Int64")

    return dataset


def _remove_trash_tasks(
    dataset: pd.DataFrame, removed_state: str
) -> pd.DataFrame:
    """Drop removed leaf tasks with zero estimate and zero completed work."""
    trash = (
        dataset["WorkItemType"].isin(["Task", "Bug"])
        & (dataset["State"] == removed_state)
        & (dataset["OriginalEstimate"] == 0)
        & (dataset["CompletedWork"] == 0)
    )
    return dataset[~trash]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent.parent / "data"


def get_wi_snapshots(
    project: str,
    days_back: int,
    hist_snapshot_path: Path | None = None,
    live_snap_dir: Path | None = None,
    removed_state: str = "Removed",
) -> pd.DataFrame:
    """Load historical + live snapshots, clean and combine into analysis dataset.

    When *hist_snapshot_path* or *live_snap_dir* are ``None`` they default to
    ``data/{project}_work_item_snapshot.parquet`` and ``data/live_snapshots/``
    relative to the repository root.
    """
    if hist_snapshot_path is None:
        hist_snapshot_path = _BASE_DIR / f"{project}_work_item_snapshot.parquet"
    if live_snap_dir is None:
        live_snap_dir = _BASE_DIR / "live_snapshots"

    hist_recent = _load_historical_snapshots(hist_snapshot_path, days_back)
    live_df = _load_live_snapshot(live_snap_dir, project)
    wi_raw_snapshots = _combine_frames(hist_recent, live_df)
    wi_raw_snapshots = _coerce_dtypes(wi_raw_snapshots)
    wi_raw_snapshots = _remove_trash_tasks(wi_raw_snapshots, removed_state)
    return wi_raw_snapshots


def get_sprint_info(dataset: pd.DataFrame, today: date) -> SprintInfo:
    """Derive sprints table, current sprint and previous sprint from dataset."""
    sprints = (
        dataset[["IterationName", "IterationStart", "IterationEnd"]]
        .drop_duplicates()
        .sort_values("IterationStart")
    )

    today_ts = pd.Timestamp(today)
    mask_current = (sprints["IterationStart"] <= today_ts) & (
        sprints["IterationEnd"] >= today_ts
    )
    current_row = sprints.loc[mask_current].sort_values("IterationStart").tail(1)

    if not current_row.empty:
        current_sprint = current_row.iloc[0]["IterationName"]
        current_start = current_row.iloc[0]["IterationStart"]
    else:
        current_sprint = None
        current_start = None

    if current_start is not None:
        prev_mask = sprints["IterationEnd"] < current_start
    else:
        prev_mask = sprints["IterationEnd"] < today_ts

    prev_row = sprints.loc[prev_mask].sort_values("IterationEnd").tail(1)
    previous_sprint = prev_row.iloc[0]["IterationName"] if not prev_row.empty else None

    return SprintInfo(sprints, current_sprint, previous_sprint)
