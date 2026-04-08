"""Sprint burndown calculations.

This module derives burndown series for the Team Analytics Dashboard
from the raw work-item snapshot table. A burndown is the per-day trace
of remaining work hours across a sprint window:

* the window is ``[IterationStart, IterationEnd - 1 day]`` (the last
  day is excluded because sprints close on their end date);
* rows in the ``Removed`` state are dropped from the totals;
* rows in any "done-ish" state (see :data:`DONEISH_STATUSES`) are
  counted as zero remaining, regardless of their stored
  ``RemainingWork`` value.

Public API
----------
- :func:`compute_sprint_burndown` - single-series burndown for a sprint.
- :func:`compute_burndown_by_assignee` - per-assignee burndown pivot
  for a sprint.

Both functions return DataFrames whose first column is ``Date`` so
they can be handed straight to ``excel_import.save_team_analytics_report``.
"""

from __future__ import annotations

import pandas as pd


DONEISH_STATUSES: frozenset[str] = frozenset(
    {"Code Review", "Ready for UAT", "Ready for Prod", "In UAT", "Closed"}
)
REMOVED_STATE: str = "Removed"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _prepare_sprint_frame(
    wi_snapshots: pd.DataFrame,
    sprint_name: str,
) -> pd.DataFrame:
    """Return the rows of ``wi_snapshots`` that belong to ``sprint_name``.

    Applies the sprint window (``IterationStart`` .. ``IterationEnd - 1 day``),
    drops ``Removed`` rows, and adds a ``RemainingBD`` column that equals
    ``RemainingWork`` everywhere except for done-ish states where it is
    forced to ``0.0``.

    An empty DataFrame (with no guaranteed columns) is returned whenever
    the sprint has no usable rows; callers must handle that explicitly.
    """
    if not sprint_name:
        return wi_snapshots.iloc[0:0]

    sub = wi_snapshots.loc[wi_snapshots["IterationName"] == sprint_name].copy()
    if sub.empty:
        return sub

    if sub["IterationStart"].isna().all() or sub["IterationEnd"].isna().all():
        return sub.iloc[0:0]

    sprint_last_inclusive = sub["IterationEnd"] - pd.Timedelta(days=1)
    sub = sub.loc[
        (sub["SnapshotDate"] >= sub["IterationStart"])
        & (sub["SnapshotDate"] <= sprint_last_inclusive)
    ]
    if sub.empty:
        return sub

    sub = sub[sub["State"] != REMOVED_STATE].copy()
    sub["RemainingBD"] = sub["RemainingWork"].where(
        ~sub["State"].isin(DONEISH_STATUSES), 0.0
    )
    return sub


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_sprint_burndown(
    wi_snapshots: pd.DataFrame,
    sprint_name: str,
) -> pd.DataFrame:
    """Return a single-series burndown for ``sprint_name``.

    Parameters
    ----------
    wi_snapshots : pd.DataFrame
        Raw work-item snapshots. Must contain at least
        ``IterationName``, ``SnapshotDate``, ``State``, ``RemainingWork``,
        ``IterationStart``, ``IterationEnd``.
    sprint_name : str
        Iteration name to compute the burndown for. An empty or unknown
        sprint returns an empty DataFrame with the expected columns, so
        callers can skip downstream sheets without special-casing.

    Returns
    -------
    pd.DataFrame
        Two columns:

        * ``Date`` - one row per snapshot day in the sprint window.
        * ``RemainingWork`` - total remaining hours across all leaf
          items that day (done-ish items count as 0, ``Removed``
          items are excluded).

    Example
    -------
    >>> bd = compute_sprint_burndown(wi_raw_snapshots, "Sprint 42")
    >>> bd.head()
            Date  RemainingWork
    0 2026-03-23          120.5
    1 2026-03-24          110.0
    2 2026-03-25           96.0
    """
    sub = _prepare_sprint_frame(wi_snapshots, sprint_name)
    if sub.empty:
        return pd.DataFrame(columns=["Date", "RemainingWork"])

    return (
        sub.groupby(sub["SnapshotDate"].dt.normalize())["RemainingBD"]
        .sum()
        .sort_index()
        .rename("RemainingWork")
        .rename_axis("Date")
        .reset_index()
    )


def compute_burndown_by_assignee(
    wi_snapshots: pd.DataFrame,
    sprint_name: str,
) -> pd.DataFrame:
    """Return a per-assignee burndown pivot for ``sprint_name``.

    Parameters
    ----------
    wi_snapshots : pd.DataFrame
        Raw work-item snapshots (see :func:`compute_sprint_burndown`
        for the required columns, plus ``AssignedTo``).
    sprint_name : str
        Iteration name. Empty / unknown returns an empty DataFrame with
        a single ``Date`` column.

    Returns
    -------
    pd.DataFrame
        Wide table with one ``Date`` column followed by one column per
        assignee that worked on the sprint. Each cell holds the
        assignee's remaining hours on that day; missing combinations
        are filled with ``0.0``.

    Example
    -------
    >>> piv = compute_burndown_by_assignee(wi_raw_snapshots, "Sprint 42")
    >>> piv.head()
            Date  Andrianov Vladyslav  Kolodieiev Ivan  Vasilov Denys
    0 2026-03-23                 24.0             40.0           56.5
    1 2026-03-24                 24.0             32.0           54.0
    """
    sub = _prepare_sprint_frame(wi_snapshots, sprint_name)
    if sub.empty:
        return pd.DataFrame(columns=["Date"])

    pivot = (
        sub.assign(Date=sub["SnapshotDate"].dt.normalize())
        .groupby(["Date", "AssignedTo"])["RemainingBD"]
        .sum()
        .unstack("AssignedTo")
        .fillna(0.0)
        .sort_index()
        .reset_index()
    )
    pivot.columns.name = None
    return pivot
