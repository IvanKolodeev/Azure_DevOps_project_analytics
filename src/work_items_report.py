"""Stand-up (Daily meeting) work item report.

Builds the per-leaf work item table consumed by the "Status" sheet of the
Team Analytics Dashboard. The table augments each leaf work item (Task / Bug,
plus any work item that carries hours) with:

  * its parent ``Epic`` and ``User Story`` titles (walked from the snapshot's
    ParentWorkItemId chain), and
  * three "Completed for <date>" columns covering the last three business
    days of activity (with weekend hours folded into the following Monday).

Items that are clearly irrelevant to a stand-up (closed/removed leaves from
past sprints with no recent activity and no remaining work) are flagged via
an ``ignore`` boolean column and then dropped before the table is sorted.

Public API
----------
- compute_daily_meeting(wi_raw_snapshots, daily_charge_by_task, today)
      -> (daily_meeting_filtered, completed_cols)
"""

from datetime import date, timedelta

import pandas as pd
from natsort import natsort_keygen, ns


# ---------------------------------------------------------------------------
# Private helpers - leaf selection & parent hierarchy
# ---------------------------------------------------------------------------

def _latest_snapshot(wi_raw_snapshots: pd.DataFrame) -> pd.DataFrame:
    """Return only the rows from the most recent snapshot date.

    ``SnapshotDate`` may be timestamp- or date-typed; both are normalised to
    plain ``date`` so the comparison is unambiguous.
    """
    df = wi_raw_snapshots.copy()
    if pd.api.types.is_datetime64_any_dtype(df["SnapshotDate"]):
        df["SnapshotDate"] = df["SnapshotDate"].dt.date
    last_snap = df["SnapshotDate"].max()
    return df[df["SnapshotDate"] == last_snap].copy()


def _select_leaves(today_snap: pd.DataFrame) -> pd.DataFrame:
    """Pick the work items that should appear on the stand-up sheet.

    A "leaf" here is either:
      * a Task or Bug (the natural execution units), OR
      * any work item that already carries hours (estimate, completed, or
        remaining) — this preserves Stories/Features that PMs charge to
        directly.
    """
    return today_snap[
        today_snap["WorkItemType"].isin({"Task", "Bug"})
        | (today_snap["CompletedWork"] != 0)
        | (today_snap["OriginalEstimate"] != 0)
        | (today_snap["RemainingWork"] != 0)
    ].copy()


def _build_id_map(today_snap: pd.DataFrame) -> pd.DataFrame:
    """Lookup of ``WorkItemId -> (WorkItemType, Title, ParentWorkItemId)``."""
    return today_snap.set_index("WorkItemId")[
        ["WorkItemType", "Title", "ParentWorkItemId"]
    ]


def _parent_chain(row: pd.Series, id_map: pd.DataFrame) -> pd.Series:
    """Walk the parent chain of one leaf and return its Story/Epic titles.

    Self-handling:
      * If the leaf itself is a User Story, it is its own ``User Story``.
      * If the leaf itself is an Epic, it is its own ``Epic``.

    The walk is bounded (10 hops) and cycle-safe; it always keeps the
    *top-most* Epic encountered along the way.
    """
    wid = row["WorkItemId"]
    wtype = row["WorkItemType"]
    title = row["Title"]

    story_title = title if wtype == "User Story" else None
    epic_title = title if wtype == "Epic" else None

    visited: set[int] = set()
    cur_id = wid

    for _ in range(10):
        try:
            parent_id = id_map.at[cur_id, "ParentWorkItemId"]
        except KeyError:
            break
        if pd.isna(parent_id):
            break
        parent_id = int(parent_id)
        if parent_id in visited:
            break
        visited.add(parent_id)

        try:
            parent_type = id_map.at[parent_id, "WorkItemType"]
            parent_title = id_map.at[parent_id, "Title"]
        except KeyError:
            break

        if parent_type == "User Story" and story_title is None:
            story_title = parent_title
        if parent_type == "Epic":
            epic_title = parent_title  # keep walking to the topmost Epic

        cur_id = parent_id

    return pd.Series({"User Story": story_title, "Epic": epic_title})


def _attach_parent_hierarchy(
    leaves: pd.DataFrame, id_map: pd.DataFrame
) -> pd.DataFrame:
    """Add ``Epic`` and ``User Story`` columns to *leaves*."""
    hier = leaves.apply(lambda r: _parent_chain(r, id_map), axis=1)
    out = pd.concat([leaves, hier], axis=1)

    # If a leaf is itself a User Story but the chain didn't fill it (e.g.
    # because the loop only fills from parents), backfill from its own Title.
    self_story = out["WorkItemType"].isin({"User Story"}) & out["User Story"].isna()
    out.loc[self_story, "User Story"] = out.loc[self_story, "Title"]
    return out


# ---------------------------------------------------------------------------
# Private helpers - last 3 business days of charge
# ---------------------------------------------------------------------------

def _stand_up_window(today: date) -> list[date]:
    """Pick the calendar dates whose hours feed the three "Completed for ..."
    columns.

    Rules:
      * On Mon/Tue/Wed we look back 5 calendar days so that the previous
        Friday + weekend land in the window (otherwise stand-up on Monday
        would only see the same Monday).
      * On Thu/Fri/Sat/Sun we look back 3 calendar days.

    The dates are returned uniqued and unsorted; downstream code maps weekend
    dates onto the following Monday.
    """
    look_back = 5 if today.weekday() < 3 else 3
    return list({today - timedelta(days=i) for i in range(look_back)})


def _melt_task_charge(daily_charge_by_task: pd.DataFrame) -> pd.DataFrame:
    """Reshape the wide per-task charge table into long ``(URL, Date, hours)``.

    Only the three columns we actually need (URL, date, charge) are kept.
    """
    date_cols = [
        c for c in daily_charge_by_task.columns
        if c not in (
            "Assigned To", "IterationName", "WorkItemType",
            "Title", "total_charge", "WorkItemURL",
        )
    ]
    long = daily_charge_by_task.melt(
        id_vars=["WorkItemURL"],
        value_vars=date_cols,
        var_name="Date",
        value_name="daily_charge",
    )
    long["Date"] = pd.to_datetime(long["Date"], errors="coerce")
    return long


def _fold_weekends_to_monday(long_df: pd.DataFrame) -> pd.DataFrame:
    """Add a ``WorkDate`` column where Sat/Sun are bumped onto Monday.

    Sat (weekday 5) -> +2 days, Sun (weekday 6) -> +1 day, others unchanged.
    """
    out = long_df.copy()
    offset_days = out["Date"].dt.weekday.map({5: 2, 6: 1}).fillna(0)
    out["WorkDate"] = out["Date"] + pd.to_timedelta(offset_days, unit="D")
    return out


def _build_completed_columns(
    daily_charge_by_task: pd.DataFrame, today: date
) -> tuple[pd.DataFrame, list[str]]:
    """Build the per-URL wide table of "Completed for <date>" columns.

    Returns the wide frame (one row per WorkItemURL, three date columns named
    ``Completed for YYYY-MM-DD``) plus the ordered list of those column names
    so callers can address them by name.
    """
    long_df = _melt_task_charge(daily_charge_by_task)

    window = _stand_up_window(today)
    long_df = long_df[long_df["Date"].isin(pd.to_datetime(window))]
    long_df = _fold_weekends_to_monday(long_df)

    per_url = (
        long_df.groupby(["WorkItemURL", "WorkDate"], as_index=False)["daily_charge"]
        .sum()
    )
    wide = per_url.pivot(
        index="WorkItemURL", columns="WorkDate", values="daily_charge"
    ).reset_index(drop=False)

    work_dates = sorted(long_df["WorkDate"].unique())
    rename_map = {wd: f"Completed for {pd.Timestamp(wd).date()}" for wd in work_dates}
    wide = wide.rename(columns=rename_map)
    completed_cols = [rename_map[wd] for wd in work_dates]
    return wide, completed_cols


# ---------------------------------------------------------------------------
# Private helpers - assemble, filter, sort
# ---------------------------------------------------------------------------

_FINAL_COLUMNS_PREFIX = (
    "IterationName", "Epic", "User Story", "Title", "Priority",
    "AssignedTo", "State", "OriginalEstimate", "CompletedWork",
)
_FINAL_COLUMNS_SUFFIX = (
    "RemainingWork", "WorkItemURL", "IterationStart", "IterationEnd",
)


def _assemble_daily_meeting(
    leaves: pd.DataFrame,
    completed_wide: pd.DataFrame,
    completed_cols: list[str],
) -> pd.DataFrame:
    """Merge per-leaf rows with their three "Completed for ..." columns."""
    merged = leaves.merge(completed_wide, on="WorkItemURL", how="left")
    for c in completed_cols:
        if c in merged.columns:
            merged[c] = merged[c].fillna(0.0)

    keep = list(_FINAL_COLUMNS_PREFIX) + list(completed_cols) + list(_FINAL_COLUMNS_SUFFIX)
    return merged[keep].copy()


def _mark_ignore(
    daily_meeting: pd.DataFrame,
    completed_cols: list[str],
    today_ts: pd.Timestamp,
) -> pd.DataFrame:
    """Add a boolean ``ignore`` column flagging rows uninteresting for stand-up.

    A row is ignored when ALL of the following hold:
      * its State is Closed or Removed,
      * the leaf does not belong to the *current* sprint, and
      * it has zero hours in all three completed-day columns AND zero
        remaining work.
    """
    out = daily_meeting.copy()

    num_cols = list(completed_cols) + ["RemainingWork"]
    out[num_cols] = out[num_cols].fillna(0)

    state_closed = out["State"].isin(["Closed", "Removed"])
    in_current_sprint = (
        (out["IterationStart"] <= today_ts) & (out["IterationEnd"] >= today_ts)
    )
    all_zero_activity = (out[num_cols] == 0).all(axis=1)

    out["ignore"] = state_closed & ~in_current_sprint & all_zero_activity
    return out


def _sort_for_standup(df: pd.DataFrame) -> pd.DataFrame:
    """Natural-sort by sprint start, then sprint, Epic, Story, priority.

    Drops the iteration date columns from the final output (they were only
    needed for the in_current_sprint check) and resets the index.
    """
    natkey = natsort_keygen(alg=ns.IGNORECASE)
    return (
        df.sort_values(
            by=["IterationStart", "IterationName", "Epic", "User Story", "Priority"],
            ascending=[True, True, True, True, True],
            key=lambda col: col.astype(str).map(natkey),
        )
        .reset_index(drop=True)
        .drop(columns=["IterationStart", "IterationEnd"])
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_daily_meeting(
    wi_raw_snapshots: pd.DataFrame,
    daily_charge_by_task: pd.DataFrame,
    today: date,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the stand-up "daily meeting" report.

    Parameters
    ----------
    wi_raw_snapshots:
        Cleaned snapshot dataset (output of ``ado_reporting.get_wi_snapshots``).
    daily_charge_by_task:
        Wide per-(assignee, task) charge table from
        ``daily_charge.compute_daily_charge_by_task``.
    today:
        Reference date for the stand-up. Determines the 3-day reporting window
        and the "current sprint" cutoff.

    Returns
    -------
    daily_meeting_filtered:
        One row per relevant leaf work item, sorted for stand-up. Columns are::

            IterationName, Epic, User Story, Title, Priority, AssignedTo,
            State, OriginalEstimate, CompletedWork,
            <Completed for YYYY-MM-DD> x 3,
            RemainingWork, WorkItemURL

    completed_cols:
        The three "Completed for ..." column names, in chronological order.

    Example output (some columns elided)::

        IterationName    Epic            User Story    Title          ...  Completed for 2026-03-30  ...  RemainingWork
        Sprint 35        API rework      Login form    Add CSRF       ...  1.5                       ...  4.0
        Sprint 35        API rework      Login form    Hook into UI   ...  0.0                       ...  2.0
    """
    today_snap = _latest_snapshot(wi_raw_snapshots)
    leaves = _select_leaves(today_snap)
    id_map = _build_id_map(today_snap)
    leaves = _attach_parent_hierarchy(leaves, id_map)

    completed_wide, completed_cols = _build_completed_columns(
        daily_charge_by_task, today
    )

    daily_meeting = _assemble_daily_meeting(leaves, completed_wide, completed_cols)
    daily_meeting = _mark_ignore(daily_meeting, completed_cols, pd.Timestamp(today))

    kept = daily_meeting[~daily_meeting["ignore"]].drop(columns=["ignore"])
    daily_meeting_filtered = _sort_for_standup(kept)
    return daily_meeting_filtered, completed_cols