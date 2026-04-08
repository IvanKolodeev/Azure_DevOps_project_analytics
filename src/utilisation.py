"""Utilisation calculations.

This module derives sprint utilisation metrics for the Team Analytics
pipeline. The "utilisation" view answers, for each (assignee, sprint)
combination, three intertwined questions:

* How much work was *planned* into the sprint?
* How much of that planned work actually got *charged* (and how much of
  the charge was overrun against the original estimate)?
* How much *unplanned* work landed in the sprint anyway?

The calculation is split into three pure functions so they can be tested
and re-used independently:

1. :func:`compute_charge_by_sprint` re-aggregates the per-task daily
   charge table to the sprint each charge day actually belongs to.
2. :func:`compute_utilisation_detail` joins planned-at-sprint-start hours
   with planned and unplanned charges, producing one row per task.
3. :func:`compute_utilisation_summary` rolls the detail up to one row per
   ``(AssignedTo, IterationName)`` and adds the team-capacity columns.

The Excel writer for the resulting workbook lives in
``excel_import.save_utilisation_report``.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping

import numpy as np
import pandas as pd


# Default per-sprint capacity (hours) per assignee. Override via the
# ``utilisation_time_map`` parameter when team composition changes.
DEFAULT_UTILISATION_TIME_MAP: dict[str, int] = {
    "Vasilov Denys": 60,        # 80 hours * 70%
    "Vasilov Denys S.": 60,     # 80 hours * 70%
    "Sukonnik Illia V.": 50,    # 80 hours * 60%
    "Kolodieiev Ivan": 50,      # 80 hours * 60%
    "Andrianov Vladyslav": 40,  # 80 hours * 50%
}

# Assignees excluded from utilisation reporting (e.g. QA who tracks hours
# elsewhere). Override per call if needed.
DEFAULT_EXCLUDED_ASSIGNEES: tuple[str, ...] = ("Horin Serhii",)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalise_sprints(sprints: pd.DataFrame) -> pd.DataFrame:
    """Drop sprints with missing window dates and coerce to ``datetime64``."""
    sub = sprints[
        (~sprints["IterationStart"].isna()) & (~sprints["IterationEnd"].isna())
    ].copy()
    sub["IterationStart"] = pd.to_datetime(sub["IterationStart"])
    sub["IterationEnd"] = pd.to_datetime(sub["IterationEnd"])
    return sub


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_charge_by_sprint(
    daily_charge_by_task: pd.DataFrame,
    sprints: pd.DataFrame,
    *,
    assignee_col: str = "AssignedTo",
) -> pd.DataFrame:
    """Re-aggregate per-task daily charges to the sprint each day belongs to.

    The wide ``daily_charge_by_task`` table carries one column per snapshot
    date. We melt it back to long form, look up the iteration whose window
    contains the snapshot date, and group by ``(WorkingIteration, assignee,
    work item)``. Charges that fall outside any sprint window are dropped.

    The *working* iteration may differ from the work item's planned
    ``IterationName``: someone continuing a task into a later sprint will
    show up in the later sprint here.

    Parameters
    ----------
    daily_charge_by_task : pd.DataFrame
        Output of ``daily_charge.compute_daily_charge_by_task``: identifier
        columns plus one ``YYYY-MM-DD`` date column per day plus
        ``total_charge``.
    sprints : pd.DataFrame
        Sprint catalogue with at least ``IterationName``, ``IterationStart``,
        ``IterationEnd``. Sprints with missing dates are skipped.
    assignee_col : str
        Name of the assignee column in ``daily_charge_by_task``. The
        notebook pipeline uses ``"AssignedTo"``.

    Returns
    -------
    pd.DataFrame
        Columns: ``WorkingIteration``, ``<assignee_col>``, ``WorkItemType``,
        ``Title``, ``WorkItemURL``, ``charge``.

    Example
    -------
    >>> cbs = compute_charge_by_sprint(daily_charge_by_task, sprints)
    >>> cbs.head(2)
      WorkingIteration  AssignedTo WorkItemType        Title          WorkItemURL  charge
    0  Sprint 35        Alice      Task          Refactor cache  https://.../123    4.0
    1  Sprint 36        Alice      Bug           Login redirect  https://.../456    1.5
    """
    sprints_n = _normalise_sprints(sprints)

    id_cols = [assignee_col, "IterationName", "WorkItemType", "Title", "WorkItemURL"]
    date_cols = [
        c for c in daily_charge_by_task.columns
        if c not in id_cols + ["total_charge"]
    ]

    melted = daily_charge_by_task.melt(
        id_vars=id_cols,
        value_vars=date_cols,
        var_name="SnapshotDate",
        value_name="charge",
    )
    melted["SnapshotDate"] = pd.to_datetime(melted["SnapshotDate"], errors="coerce")
    melted = melted[melted["charge"].fillna(0) != 0]

    # Add the sprint window columns to make the side-by-side reasoning easy
    # to debug; they are dropped after the interval lookup.
    melted = melted.merge(
        sprints_n[["IterationName", "IterationStart", "IterationEnd"]],
        how="left",
        suffixes=("", "_s"),
    )

    intervals = pd.IntervalIndex.from_arrays(
        sprints_n["IterationStart"], sprints_n["IterationEnd"], closed="both"
    )
    idx = intervals.get_indexer(melted["SnapshotDate"])
    melted["WorkingIteration"] = np.where(
        idx >= 0, sprints_n["IterationName"].to_numpy()[idx], pd.NA
    )

    grouped = (
        melted.groupby(
            ["WorkingIteration", assignee_col, "WorkItemType", "Title", "WorkItemURL"],
            as_index=False,
        )["charge"].sum()
    )
    return grouped.dropna(subset=["WorkingIteration"])


def compute_utilisation_detail(
    wi_raw_snapshots: pd.DataFrame,
    sprints: pd.DataFrame,
    charge_by_sprint: pd.DataFrame,
    start_date: date,
    *,
    excluded_assignees: Iterable[str] = DEFAULT_EXCLUDED_ASSIGNEES,
    assignee_col: str = "AssignedTo",
) -> pd.DataFrame:
    """One row per task: planned vs. unplanned, charged vs. debt.

    Steps
    -----
    1. Take the snapshot of every work item *as of its sprint's first day*
       (post-planning) and compute ``Planned_for_sprint`` =
       ``OriginalEstimate - CompletedWork @ day before IterationStart``,
       clipped to ``>= 0``.
    2. Left-join the actual sprint charge from ``charge_by_sprint`` on
       ``(WorkItemURL, IterationName == WorkingIteration)``. Rows that
       match are flagged ``Planned == "yes"``.
    3. Charges in ``charge_by_sprint`` that did *not* match any planned
       row are appended with ``Planned == "no"`` and
       ``Planned_for_sprint = 0``.
    4. Drop ``excluded_assignees`` (e.g. QA who tracks hours elsewhere).
    5. Add the four derived metric columns:

       * ``Completed for planned tasks``  -
         ``min(charge, plan)`` for planned rows, ``0`` otherwise.
       * ``Overcharged_planned_tasks``    -
         ``max(0, charge - plan)`` for planned rows.
       * ``NOT DONE but planed``          -
         ``max(0, plan - charge)`` for every row.
       * ``Completed non-sprint tasks``   -
         ``charge`` for unplanned rows, ``0`` otherwise.

    Parameters
    ----------
    wi_raw_snapshots : pd.DataFrame
        Combined historical + live snapshot table. Must contain the
        columns hard-coded in ``cols`` below.
    sprints : pd.DataFrame
        Sprint catalogue (passed for parity with the rest of the API,
        currently only used for normalisation).
    charge_by_sprint : pd.DataFrame
        Output of :func:`compute_charge_by_sprint`.
    start_date : datetime.date
        Earliest sprint start to include. Older sprints are excluded so
        the report only covers the configured reporting window.
    excluded_assignees : Iterable[str]
        Assignees to remove from the result.
    assignee_col : str
        Name of the assignee column in both inputs.

    Returns
    -------
    pd.DataFrame
        See "Returns" section of :func:`compute_utilisation_summary` for
        the rolled-up version. The detail table has columns:
        ``IterationName``, ``<assignee_col>``, ``WorkItemType``, ``Title``,
        ``Planned``, ``Planned_for_sprint``, ``charge``, ``WorkItemURL``,
        ``Completed time``, ``Completed for planned tasks``,
        ``Overcharged_planned_tasks``, ``NOT DONE but planed``,
        ``Completed non-sprint tasks``.

    Example
    -------
    >>> detail = compute_utilisation_detail(
    ...     wi_raw_snapshots, sprints, charge_by_sprint, date(2026, 3, 1)
    ... )
    >>> detail[['IterationName', 'AssignedTo', 'Planned', 'Planned_for_sprint',
    ...         'Completed time']].head(2)
      IterationName AssignedTo Planned  Planned_for_sprint  Completed time
    0  Sprint 35    Alice      yes                    8.0             6.0
    1  Sprint 35    Alice      yes                    4.0             5.5
    """
    _normalise_sprints(sprints)  # validate, even though we don't use the result here

    cols = [
        "SnapshotDate", "IterationName", "IterationStart", "IterationEnd",
        "WorkItemType", "Title", "State", assignee_col,
        "CompletedWork", "OriginalEstimate", "RemainingWork", "WorkItemURL",
    ]
    util = wi_raw_snapshots[cols].copy()
    util["IterationStart"] = pd.to_datetime(util["IterationStart"])
    util["IterationEnd"] = pd.to_datetime(util["IterationEnd"])
    util = util[util["IterationStart"] >= pd.to_datetime(start_date)]

    # Snapshot at the first day of each sprint (taken at end-of-day, so
    # after planning).
    util = util[util["SnapshotDate"] == util["IterationStart"]]

    # Look up CompletedWork on the day before the sprint started so we can
    # back out hours that bled in from prior sprints.
    util["PrevDate"] = util["SnapshotDate"] - pd.Timedelta(days=1)
    util = util.merge(
        wi_raw_snapshots[["SnapshotDate", "WorkItemURL", "CompletedWork"]],
        left_on=["WorkItemURL", "PrevDate"],
        right_on=["WorkItemURL", "SnapshotDate"],
        how="left",
        suffixes=("", "_start"),
    ).drop(columns=["SnapshotDate_start", "PrevDate"])
    util["Completed_at_sprint_start"] = util["CompletedWork_start"].fillna(0)
    util = util.drop(columns=["CompletedWork", "CompletedWork_start"])
    util["Planned_for_sprint"] = (
        util["OriginalEstimate"] - util["Completed_at_sprint_start"]
    ).clip(lower=0)

    # Join planned charges.
    util = util.merge(
        charge_by_sprint[["WorkItemURL", "WorkingIteration", "charge"]],
        left_on=["WorkItemURL", "IterationName"],
        right_on=["WorkItemURL", "WorkingIteration"],
        how="left",
    )
    util["charge"] = util["charge"].fillna(0)
    util["Planned"] = "yes"
    util = util[[
        "IterationName", assignee_col, "WorkItemType", "Title",
        "Planned", "Planned_for_sprint", "charge", "WorkItemURL",
    ]]

    # Append unplanned charges (rows in charge_by_sprint that did not match
    # any planned task in their working iteration).
    not_planned = charge_by_sprint.merge(
        util[["WorkItemURL", "IterationName"]],
        left_on=["WorkItemURL", "WorkingIteration"],
        right_on=["WorkItemURL", "IterationName"],
        how="left",
    )
    not_planned = not_planned[not_planned["IterationName"].isna()]
    not_planned["Planned"] = "no"
    not_planned["Planned_for_sprint"] = 0
    not_planned = not_planned[[
        "WorkingIteration", assignee_col, "WorkItemType", "Title",
        "Planned_for_sprint", "charge", "WorkItemURL", "Planned",
    ]].rename(columns={"WorkingIteration": "IterationName"})

    util = pd.concat([util, not_planned])
    util["Completed time"] = util["charge"]

    excluded = list(excluded_assignees)
    if excluded:
        util = util[~util[assignee_col].isin(excluded)]

    util["Completed for planned tasks"] = np.where(
        util["Planned"] == "yes",
        util[["Completed time", "Planned_for_sprint"]].min(axis=1),
        0,
    )
    util["Overcharged_planned_tasks"] = np.where(
        util["Planned"] == "yes",
        (util["Completed time"] - util["Planned_for_sprint"]).clip(lower=0),
        0,
    )
    util["NOT DONE but planed"] = (
        util["Planned_for_sprint"] - util["Completed time"]
    ).clip(lower=0)
    util["Completed non-sprint tasks"] = np.where(
        util["Planned"] == "no", util["Completed time"], 0
    )
    return util


def compute_utilisation_summary(
    utilisation: pd.DataFrame,
    *,
    utilisation_time_map: Mapping[str, int] = DEFAULT_UTILISATION_TIME_MAP,
    assignee_col: str = "AssignedTo",
) -> pd.DataFrame:
    """Roll the per-task utilisation detail to one row per (assignee, sprint).

    Adds team-capacity columns derived from ``utilisation_time_map`` and a
    few derived comparisons against the planned/completed totals:

    * ``Utilisation time``           - capacity hours per sprint.
    * ``Overplanned / Underplanned`` - ``planned - capacity``.
    * ``Work hours debt``            - capacity minus completed (clipped
      so that overcommitting against capacity does not create artificial
      negative debt).

    Parameters
    ----------
    utilisation : pd.DataFrame
        Output of :func:`compute_utilisation_detail`.
    utilisation_time_map : Mapping[str, int]
        Capacity (hours / sprint) per assignee. Defaults to
        :data:`DEFAULT_UTILISATION_TIME_MAP`. Unknown assignees get ``0``.
    assignee_col : str
        Name of the assignee column in ``utilisation``.

    Returns
    -------
    pd.DataFrame
        Columns in order:
        ``<assignee_col>``, ``IterationName``, ``Utilisation time``,
        ``Planned_for_sprint``, ``Overplanned / Underplanned``,
        ``Completed for planned tasks``, ``Overcharged_planned_tasks``,
        ``Completed non-sprint tasks``, ``Work hours debt``,
        ``Comment on Plan Focus``.

    Example
    -------
    >>> summary = compute_utilisation_summary(detail)
    >>> summary.head(1).T
                                       0
    AssignedTo                       Vasilov Denys
    IterationName                    Sprint 36
    Utilisation time                 60
    Planned_for_sprint               58.0
    Overplanned / Underplanned       -2.0
    Completed for planned tasks      52.0
    Overcharged_planned_tasks         3.0
    Completed non-sprint tasks        4.0
    Work hours debt                   8.0
    Comment on Plan Focus
    """
    metrics = [
        "Planned_for_sprint", "Completed for planned tasks", "NOT DONE but planed",
        "Completed time", "Completed non-sprint tasks", "Overcharged_planned_tasks",
    ]
    summary = (
        utilisation.groupby([assignee_col, "IterationName"], as_index=False)[metrics].sum()
    )

    summary["Utilisation time"] = (
        summary[assignee_col].map(utilisation_time_map).fillna(0).astype(int)
    )
    summary = summary.sort_values([assignee_col, "IterationName"], ascending=False)

    summary["Underutilised"] = summary["Utilisation time"] - summary["Completed time"]
    summary["Overplanned / Underplanned"] = (
        summary["Planned_for_sprint"] - summary["Utilisation time"]
    )
    summary["Work hours debt"] = (
        np.maximum(
            np.minimum(summary["Planned_for_sprint"], summary["Utilisation time"]),
            summary["Utilisation time"],
        )
        - summary["Completed time"]
    )
    summary["Comment on Plan Focus"] = ""

    return summary[[
        assignee_col, "IterationName", "Utilisation time", "Planned_for_sprint",
        "Overplanned / Underplanned", "Completed for planned tasks",
        "Overcharged_planned_tasks", "Completed non-sprint tasks",
        "Work hours debt", "Comment on Plan Focus",
    ]]
