"""Daily charge calculations.

This module derives the per-day "charge" (logged hours) from a snapshot
dataset and reshapes it into the wide tables consumed by the reporting
notebook and the Power BI export.

Public API
----------
- compute_daily_charge_by_person(wi_raw_snapshots) -> DataFrame
- compute_daily_charge_by_task(wi_raw_snapshots) -> DataFrame
- export_daily_charge_for_powerbi(daily_charge_by_person, output_path) -> DataFrame
"""

from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _add_daily_charge(wi_raw_snapshots: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *wi_raw_snapshots* with a ``daily_charge`` column.

    ``daily_charge`` is the day-over-day diff of ``CompletedWork`` per
    ``WorkItemId``. The first day for each work item falls back to that day's
    ``CompletedWork`` (so historical incoming hours are preserved on the
    earliest snapshot row).

    Example
    -------
    For a single work item::

        SnapshotDate  CompletedWork   daily_charge
        2026-03-01    2.0             2.0   <- first day, falls back to value
        2026-03-02    5.0             3.0
        2026-03-03    5.0             0.0
    """
    df = wi_raw_snapshots.copy()
    df = df.sort_values(["WorkItemId", "SnapshotDate", "WorkItemRevisionSK"])
    df = df.drop_duplicates(["WorkItemId", "SnapshotDate"], keep="last")
    df["daily_charge"] = (
        df.groupby("WorkItemId")["CompletedWork"]
          .diff()
          .fillna(df["CompletedWork"])
    )
    return df


def _drop_earliest_date_column(wide: pd.DataFrame) -> pd.DataFrame:
    """Drop the earliest column from a wide table whose columns are dates.

    The earliest snapshot column represents incoming/historical hours rather
    than activity within the reporting window, so the report excludes it.
    Assumes ``wide`` contains only date columns at this stage.
    """
    if wide.shape[1] == 0:
        return wide
    earliest = wide.columns.min()
    return wide.drop(columns=[earliest])


def _stringify_date_columns(
    df: pd.DataFrame, exclude: tuple[str, ...] = ()
) -> pd.DataFrame:
    """Rename date-typed columns to ``YYYY-MM-DD`` strings (for Excel export)."""
    rename_map = {
        c: c.strftime("%Y-%m-%d")
        for c in df.columns
        if c not in exclude and hasattr(c, "strftime")
    }
    return df.rename(columns=rename_map)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_daily_charge_by_person(
    wi_raw_snapshots: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate daily charge per assignee into a wide table.

    Result columns: ``AssignedTo`` + one column per snapshot date
    (as ``YYYY-MM-DD`` strings) + ``total_charge``. The earliest date is
    dropped (incoming hours), and rows that sum to zero are removed.

    Example output::

        AssignedTo   2026-03-02  2026-03-03  ...  total_charge
        Alice        2.0         3.0         ...  12.5
        Bob          0.0         4.0         ...   8.0
    """
    df = _add_daily_charge(wi_raw_snapshots)

    agg = (
        df.groupby(["AssignedTo", "SnapshotDate"], dropna=False)["daily_charge"]
          .sum()
          .reset_index()
    )
    agg["AssignedTo"] = agg["AssignedTo"].fillna("Unassigned")

    wide = (
        agg.pivot(index="AssignedTo", columns="SnapshotDate", values="daily_charge")
           .fillna(0)
    )
    wide.columns.name = None
    wide = wide.sort_index(axis=1)

    wide = _drop_earliest_date_column(wide)
    wide["total_charge"] = wide.sum(axis=1)
    wide = wide[wide["total_charge"] != 0]

    wide = wide.reset_index()
    wide = _stringify_date_columns(wide, exclude=("AssignedTo", "total_charge"))
    return wide


def compute_daily_charge_by_task(
    wi_raw_snapshots: pd.DataFrame,
) -> pd.DataFrame:
    """Aggregate daily charge per (assignee, work item) into a wide table.

    Each row uniquely identifies one work item charged by one assignee.
    Date columns are ``YYYY-MM-DD`` strings; the earliest date is dropped
    (incoming hours), and zero-total rows are removed.

    Example output (some columns elided)::

        AssignedTo  IterationName  WorkItemType  Title       2026-03-02  ...  total_charge  WorkItemURL
        Alice       Sprint 12      Task          Refactor X  2.0         ...  5.0           https://...
    """
    df = _add_daily_charge(wi_raw_snapshots)

    id_cols = ["AssignedTo", "IterationName", "WorkItemType", "Title", "WorkItemURL"]
    agg = (
        df.groupby(id_cols + ["SnapshotDate"], dropna=False)["daily_charge"]
          .sum()
          .reset_index()
    )
    agg["AssignedTo"] = agg["AssignedTo"].fillna("Unassigned")

    wide = (
        agg.pivot(index=id_cols, columns="SnapshotDate", values="daily_charge")
           .fillna(0)
    )
    wide = wide.sort_index(axis=1)

    wide = _drop_earliest_date_column(wide)
    wide["total_charge"] = wide.sum(axis=1)
    wide = wide[wide["total_charge"] != 0]

    wide = _stringify_date_columns(wide, exclude=("total_charge",))
    wide = wide.reset_index()

    base_cols = ["AssignedTo", "IterationName", "WorkItemType", "Title"]
    tail_cols = ["total_charge", "WorkItemURL"]
    date_cols_str = [c for c in wide.columns if c not in base_cols + tail_cols]
    final_cols = [c for c in base_cols + date_cols_str + tail_cols if c in wide.columns]
    return wide[final_cols]


def export_daily_charge_for_powerbi(
    daily_charge_by_person: pd.DataFrame, output_path: Path
) -> pd.DataFrame:
    """Reshape per-assignee charge to long format and write a Power BI CSV.

    Output columns: ``AssignedTo``, ``Date`` (``YYYY-MM-DD``), ``Hours``,
    ``MonthYear`` (e.g. ``Mar-26``), ``DayNum``.

    Example output::

        AssignedTo   Date         Hours   MonthYear  DayNum
        Alice        2026-03-02   2.0     Mar-26     2
        Alice        2026-03-03   3.0     Mar-26     3
    """
    date_cols = [
        c for c in daily_charge_by_person.columns
        if c not in ("AssignedTo", "total_charge")
    ]

    long_df = daily_charge_by_person.melt(
        id_vars=["AssignedTo"],
        value_vars=date_cols,
        var_name="Date",
        value_name="Hours",
    )

    long_df["Date"] = pd.to_datetime(long_df["Date"], errors="coerce")
    long_df = long_df.dropna(subset=["Date"])

    long_df["MonthYear"] = long_df["Date"].dt.strftime("%b-%y")
    long_df["DayNum"] = long_df["Date"].dt.day
    long_df["Date"] = long_df["Date"].dt.strftime("%Y-%m-%d")

    long_df.to_csv(output_path, index=False)
    return long_df
