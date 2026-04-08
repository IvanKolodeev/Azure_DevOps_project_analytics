"""Excel export for the Team Analytics Dashboard.

This module owns everything related to writing the main reporting workbook
(``{project}_ProjectStatus_{date}_ADO_API.xlsx``) from the in-memory
DataFrames produced by the reporting notebook. The previous implementation
lived inline inside ``notebooks/1-ADO_Reporting.ipynb``; here it is split
into one function per sheet, plus a single orchestrator
``save_team_analytics_report``.

Design
------
* A single :class:`FormatBook` lazily builds xlsxwriter formats that are
  reused across sheets (so e.g. the same ``number_fmt`` is never recreated).
* :data:`COLUMN_SPECS` is the cross-sheet registry: if a DataFrame column has
  a name listed here, its width and value/header formatting are applied
  automatically on every worksheet via :func:`_apply_column_specs`. This
  replaces the per-sheet if/elif ladders that used to live in the notebook.
* Date columns (``YYYY-MM-DD``) are detected by column name and rendered
  with vertical headers; weekend dates get a pink background.
* The ``completed_cols`` passed into :func:`write_status_sheet` are treated
  as a per-call spec extension so they can be highlighted in blue without
  hard-coding their dynamic names in the registry.

Public API
----------
- :func:`save_team_analytics_report` - write the full workbook.
- :func:`write_status_sheet` - stand-up / status sheet.
- :func:`write_charge_person_sheet` - per-assignee daily charge.
- :func:`write_charge_task_sheet` - per-(assignee, task) daily charge.
- :func:`write_sprint_burndown_sheet` - single-series burndown + line chart.
- :func:`write_burndown_by_assignee_sheet` - multi-series burndown,
  completion histogram.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd
from xlsxwriter.utility import xl_col_to_name


# ---------------------------------------------------------------------------
# Format book - lazy builders for the xlsxwriter formats we reuse everywhere.
# ---------------------------------------------------------------------------

_NUMBER_FMT = '#,##0.0;-#,##0.0;"-"'


class FormatBook:
    """Lazy cache of xlsxwriter formats keyed by short string names.

    One instance is created per workbook. Callers ask for a format by key
    (``"number"``, ``"header"``, ...) and the book builds it on first use and
    returns the cached object on subsequent calls.
    """

    def __init__(self, workbook):
        self._wb = workbook
        self._cache: dict[str, object] = {}

    def get(self, key: str):
        if key not in self._cache:
            self._cache[key] = self._build(key)
        return self._cache[key]

    def _build(self, key: str):
        wb = self._wb
        if key == "number":
            return wb.add_format({"num_format": _NUMBER_FMT})
        if key == "blue_number":
            return wb.add_format(
                {"num_format": _NUMBER_FMT, "font_color": "#0070C0"}
            )
        if key == "weekend_number":
            return wb.add_format(
                {
                    "num_format": _NUMBER_FMT,
                    "bg_color": "#EAB8D8",
                    "align": "center",
                    "valign": "vcenter",
                }
            )
        if key == "percent":
            return wb.add_format({"num_format": "0.0%"})
        if key == "header":
            return wb.add_format(
                {
                    "bold": True,
                    "text_wrap": True,
                    "valign": "top",
                    "align": "center",
                    "border": 1,
                }
            )
        if key == "header_blue":
            return wb.add_format(
                {
                    "bold": True,
                    "text_wrap": True,
                    "valign": "top",
                    "align": "center",
                    "border": 1,
                    "font_color": "#0070C0",
                }
            )
        if key == "header_horizontal":
            return wb.add_format(
                {
                    "bold": True,
                    "align": "center",
                    "valign": "top",
                    "text_wrap": False,
                    "border": 1,
                }
            )
        if key == "header_vertical":
            return wb.add_format(
                {
                    "bold": True,
                    "align": "center",
                    "valign": "bottom",
                    "rotation": 90,
                    "text_wrap": False,
                    "border": 1,
                }
            )
        if key == "header_weekend":
            return wb.add_format(
                {
                    "bg_color": "#EAB8D8",
                    "align": "center",
                    "valign": "bottom",
                    "rotation": 90,
                    "text_wrap": False,
                    "border": 1,
                }
            )
        if key == "blocked_row":
            return wb.add_format({"bg_color": "#FFCDCE"})
        raise KeyError(f"Unknown format key: {key!r}")


# ---------------------------------------------------------------------------
# Cross-sheet column specs.
# ---------------------------------------------------------------------------
#
# Any column whose name matches a key here has its width and (optionally)
# its value/header format key auto-applied on every worksheet by
# :func:`_apply_column_specs`.
#
# Keys:
#   width        - column width in Excel units.
#   value_fmt    - FormatBook key for cell values (None = default).
#   header_fmt   - FormatBook key for the header row (None = use sheet
#                  default, usually "header" or "header_horizontal").
#   header_text  - optional override for the header label (e.g. insert
#                  line breaks). When None the raw column name is written.

COLUMN_SPECS: dict[str, dict] = {
    # Status-sheet-shaped columns.
    "IterationName": {"width": 22},
    "Epic": {"width": 22},
    "User Story": {"width": 40},
    "Title": {"width": 60},
    "Priority": {"width": 7},
    "AssignedTo": {"width": 18},
    "State": {"width": 18},
    "OriginalEstimate": {"width": 15, "value_fmt": "number"},
    "CompletedWork": {"width": 15, "value_fmt": "number"},
    "RemainingWork": {"width": 15, "value_fmt": "number"},
    "WorkItemURL": {"width": 60},
    "IterationStart": {"width": 18},
    "IterationEnd": {"width": 18},
    # Charge-sheet tail column (shared across by_person / by_task).
    "total_charge": {"width": 12, "value_fmt": "number"},
}


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _is_date_column(col_name) -> pd.Timestamp | None:
    """Return the parsed date if ``col_name`` looks like ``YYYY-MM-DD``.

    Returns ``None`` when the column is not a date string.
    """
    if not isinstance(col_name, str) or len(col_name) < 10 or not col_name[:4].isdigit():
        return None
    try:
        return pd.to_datetime(col_name)
    except (ValueError, TypeError):
        return None


def _date_column_formats(col_name: str, fmts: FormatBook) -> tuple[object, object]:
    """Return ``(header_fmt, value_fmt)`` for a date-named column.

    Weekend dates get the pink ``header_weekend`` / ``weekend_number`` pair;
    weekdays get the vertical header + plain number format.
    """
    dt = _is_date_column(col_name)
    if dt is not None and dt.weekday() >= 5:
        return fmts.get("header_weekend"), fmts.get("weekend_number")
    return fmts.get("header_vertical"), fmts.get("number")


def _apply_column_specs(
    ws,
    df: pd.DataFrame,
    fmts: FormatBook,
    *,
    default_width: int = 15,
    extra_specs: dict[str, dict] | None = None,
) -> None:
    """Apply widths + value formats to every column of ``df`` on ``ws``.

    Columns known in :data:`COLUMN_SPECS` (or in the per-call ``extra_specs``)
    get their registered width and number format. Date-named columns
    (``YYYY-MM-DD``) get the number format too, with weekend highlighting
    inherited from :func:`_date_column_formats`. Unknown columns fall back
    to ``default_width`` and no value format.

    This replaces the per-sheet if/elif ladders that used to live in the
    notebook, so any new DataFrame that happens to contain e.g. a
    ``RemainingWork`` column automatically gets the right width and
    number format.
    """
    merged = {**COLUMN_SPECS, **(extra_specs or {})}
    for col_idx, col_name in enumerate(df.columns):
        spec = merged.get(col_name, {})

        if spec:
            width = spec.get("width", default_width)
            value_fmt_key = spec.get("value_fmt")
            value_fmt = fmts.get(value_fmt_key) if value_fmt_key else None
        elif _is_date_column(col_name) is not None:
            width = 4
            _, value_fmt = _date_column_formats(col_name, fmts)
        else:
            width = default_width
            value_fmt = None

        ws.set_column(col_idx, col_idx, width, value_fmt)


def _write_headers(
    ws,
    df: pd.DataFrame,
    fmts: FormatBook,
    *,
    default_key: str = "header",
    extra_specs: dict[str, dict] | None = None,
) -> None:
    """Write the header row using a matching header format per column.

    Resolution order for each column:
      1. Explicit ``header_fmt`` in ``extra_specs`` / :data:`COLUMN_SPECS`.
      2. Date-named columns -> vertical / weekend header.
      3. ``default_key`` from :class:`FormatBook`.
    """
    merged = {**COLUMN_SPECS, **(extra_specs or {})}
    for col_idx, col_name in enumerate(df.columns):
        spec = merged.get(col_name, {})
        header_text = spec.get("header_text", col_name)

        header_key = spec.get("header_fmt")
        if header_key:
            header_fmt = fmts.get(header_key)
        elif _is_date_column(col_name) is not None:
            header_fmt, _ = _date_column_formats(col_name, fmts)
        else:
            header_fmt = fmts.get(default_key)

        ws.write(0, col_idx, header_text, header_fmt)


def _write_date_values(
    ws, df: pd.DataFrame, fmts: FormatBook
) -> None:
    """Write per-cell values for date columns with weekend highlighting.

    Unlike ``set_column``, which only applies a format when the cell is
    otherwise untouched, writing each cell explicitly lets weekend columns
    keep their pink background even after ``to_excel`` has already populated
    them.
    """
    for col_idx, col_name in enumerate(df.columns):
        if _is_date_column(col_name) is None:
            continue
        _, value_fmt = _date_column_formats(col_name, fmts)
        for row_idx, cell_value in enumerate(df[col_name], start=1):
            ws.write(row_idx, col_idx, cell_value, value_fmt)


# ---------------------------------------------------------------------------
# Per-sheet writers.
# ---------------------------------------------------------------------------


def write_status_sheet(
    writer,
    summary: pd.DataFrame,
    completed_cols: Iterable[str],
    fmts: FormatBook,
    sheet_name: str = "Status",
) -> None:
    """Write the stand-up ``Status`` sheet.

    Parameters
    ----------
    writer:
        Open ``pd.ExcelWriter`` with the xlsxwriter engine.
    summary:
        Row-per-leaf stand-up DataFrame from ``work_items_report``.
    completed_cols:
        The three dynamic "Completed for <date>" column names; they are
        rendered with the blue header/value formats.
    fmts:
        Shared :class:`FormatBook`.
    sheet_name:
        Worksheet tab name (default ``"Status"``).

    Side effects: writes headers with inserted newlines for wrapping, freezes
    the header row, and adds a conditional-format rule that highlights rows
    whose ``State`` equals ``"Blocked"``.
    """
    # Strip tz from datetime columns so xlsxwriter can serialize them.
    summary = summary.copy()
    for col in ("IterationStart", "IterationEnd"):
        if col in summary.columns and pd.api.types.is_datetime64_any_dtype(summary[col]):
            try:
                summary[col] = summary[col].dt.tz_convert(None)
            except AttributeError:
                pass

    summary.to_excel(writer, index=False, sheet_name=sheet_name)
    ws = writer.sheets[sheet_name]

    completed_cols = list(completed_cols)
    extra = {
        c: {"width": 15, "value_fmt": "blue_number", "header_fmt": "header_blue"}
        for c in completed_cols
    }

    _apply_column_specs(ws, summary, fmts, extra_specs=extra)

    # Headers: insert newlines in place of spaces so long labels wrap nicely.
    for col_idx, col_name in enumerate(summary.columns):
        spec = {**COLUMN_SPECS, **extra}.get(col_name, {})
        header_key = spec.get("header_fmt", "header")
        header_fmt = fmts.get(header_key)
        label = col_name.replace(" ", "\n") if isinstance(col_name, str) else col_name
        ws.write(0, col_idx, label, header_fmt)

    ws.set_row(0, 47)
    ws.freeze_panes(1, 0)

    if "State" in summary.columns and len(summary) > 0:
        col_state = summary.columns.get_loc("State")
        col_letter = xl_col_to_name(col_state)
        ws.conditional_format(
            1,
            0,
            len(summary),
            len(summary.columns) - 1,
            {
                "type": "formula",
                "criteria": f'=${col_letter}2="Blocked"',
                "format": fmts.get("blocked_row"),
            },
        )


def _write_daily_charge_sheet(
    writer,
    df: pd.DataFrame,
    fmts: FormatBook,
    sheet_name: str,
    freeze_col: int,
) -> None:
    """Shared body for the two "wide date columns" charge sheets.

    ``freeze_col`` is the number of leading identifier columns to freeze
    (1 for by-person, 4 for by-task).
    """
    df.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]

    _apply_column_specs(ws, df, fmts, default_width=15)
    _write_headers(ws, df, fmts, default_key="header_horizontal")
    _write_date_values(ws, df, fmts)

    ws.set_row(0, 61)
    ws.freeze_panes(1, freeze_col)


def write_charge_person_sheet(
    writer,
    daily_charge_by_person: pd.DataFrame,
    fmts: FormatBook,
    sheet_name: str = "Charge_Person",
) -> None:
    """Write the per-assignee daily charge sheet.

    Identifier column (``AssignedTo``) is frozen on the left; date columns
    use vertical headers with weekend highlighting.
    """
    _write_daily_charge_sheet(writer, daily_charge_by_person, fmts, sheet_name, freeze_col=1)


def write_charge_task_sheet(
    writer,
    daily_charge_by_task: pd.DataFrame,
    fmts: FormatBook,
    sheet_name: str = "Charge_Task",
) -> None:
    """Write the per-(assignee, task) daily charge sheet.

    First four identifier columns are frozen; date columns use vertical
    headers with weekend highlighting.
    """
    _write_daily_charge_sheet(writer, daily_charge_by_task, fmts, sheet_name, freeze_col=4)


def write_sprint_burndown_sheet(
    writer,
    bd: pd.DataFrame,
    sprint_name: str,
    fmts: FormatBook,
    sheet_name: str,
) -> None:
    """Write a single-series burndown sheet and insert a line chart.

    The sheet is assumed to already exist (created by ``to_excel`` in the
    orchestrator); this function formats the header row and inserts the
    ``Burndown - <sprint>`` line chart at ``D2``.
    """
    ws = writer.sheets[sheet_name]
    wb = writer.book

    _write_headers(ws, bd, fmts, default_key="header")
    ws.freeze_panes(1, 0)

    max_row = len(bd)
    chart = wb.add_chart({"type": "line"})
    chart.add_series(
        {
            "name": f"Burndown - {sprint_name}",
            "categories": [sheet_name, 1, 0, max_row, 0],
            "values": [sheet_name, 1, 1, max_row, 1],
        }
    )
    chart.set_title({"name": f"Burndown - {sprint_name}"})
    chart.set_x_axis({"name": "Date"})
    chart.set_y_axis({"name": "Remaining Work (hours)"})
    ws.insert_chart("D2", chart)


def write_burndown_by_assignee_sheet(
    writer,
    bd_assignee_pivot: pd.DataFrame,
    current_sprint: str,
    fmts: FormatBook,
    sheet_name: str = "CurrentByAssignee",
) -> None:
    """Write the per-assignee burndown sheet with two charts.

    Produces:
      * a line chart with one series per assignee (inserted at ``H2``);
      * a ``Completion %`` summary row below the pivot and a matching
        colored column chart (inserted at ``H20``).
    """
    ws = writer.sheets[sheet_name]
    wb = writer.book

    _write_headers(ws, bd_assignee_pivot, fmts, default_key="header")
    ws.freeze_panes(1, 1)

    max_row = len(bd_assignee_pivot)
    num_assignees = len(bd_assignee_pivot.columns) - 1

    # Line chart per assignee.
    line_chart = wb.add_chart({"type": "line"})
    for col_idx in range(1, len(bd_assignee_pivot.columns)):
        line_chart.add_series(
            {
                "name": [sheet_name, 0, col_idx],
                "categories": [sheet_name, 1, 0, max_row, 0],
                "values": [sheet_name, 1, col_idx, max_row, col_idx],
            }
        )
    line_chart.set_title({"name": f"Burndown by Assignee - {current_sprint}"})
    line_chart.set_x_axis({"name": "Date"})
    line_chart.set_y_axis({"name": "Remaining Work (hours)"})
    ws.insert_chart("H2", line_chart)

    # Completion % row.
    progress_row = max_row + 2
    ws.write(progress_row, 0, "Completion %", fmts.get("header"))
    percent_fmt = fmts.get("percent")

    for col_idx in range(1, len(bd_assignee_pivot.columns)):
        start_val = bd_assignee_pivot.iloc[0, col_idx]
        current_val = bd_assignee_pivot.iloc[-1, col_idx]
        if pd.notna(start_val) and start_val != 0:
            pct = max(0.0, min(1.0, (start_val - current_val) / start_val))
        else:
            pct = 0.0
        ws.write_number(progress_row, col_idx, pct, percent_fmt)

    # Colored histogram.
    colors = [
        "#4472C4",  # blue
        "#ED7D31",  # orange
        "#A5A5A5",  # gray
        "#FFC000",  # yellow
        "#5B9BD5",  # light blue
        "#70AD47",  # green
    ]
    points = [{"fill": {"color": colors[i % len(colors)]}} for i in range(num_assignees)]

    hist = wb.add_chart({"type": "column"})
    hist.add_series(
        {
            "name": "Completion %",
            "categories": [sheet_name, 0, 1, 0, num_assignees],
            "values": [sheet_name, progress_row, 1, progress_row, num_assignees],
            "points": points,
        }
    )
    hist.set_title({"name": f"Assignee Completion - {current_sprint}"})
    hist.set_y_axis(
        {
            "name": "Completion",
            "min": 0,
            "max": 1,
            "major_unit": 0.25,
            "num_format": "0%",
        }
    )
    ws.insert_chart("H20", hist)


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------


def save_team_analytics_report(
    output_file: Path,
    *,
    summary: pd.DataFrame,
    completed_cols: Iterable[str],
    daily_charge_by_person: pd.DataFrame,
    daily_charge_by_task: pd.DataFrame,
    bd_current: pd.DataFrame,
    bd_prev: pd.DataFrame,
    bd_assignee_pivot: pd.DataFrame,
    current_sprint: str,
    previous_sprint: str,
) -> Path:
    """Write the full Team Analytics Dashboard workbook.

    All DataFrames are already-computed outputs from the reporting notebook.
    Empty DataFrames are skipped (so e.g. a sprint with no burndown data
    does not produce an empty sheet).

    Returns the ``output_file`` path for convenience / chaining.
    """
    with pd.ExcelWriter(output_file, engine="xlsxwriter") as writer:
        # 1) Status sheet - written first so the tab order matches what PMs expect.
        fmts = FormatBook(writer.book)
        write_status_sheet(writer, summary, completed_cols, fmts)

        # 2) CurrentByAssignee (needs to_excel before formatting + charting).
        if not bd_assignee_pivot.empty:
            bd_assignee_pivot.to_excel(writer, index=False, sheet_name="CurrentByAssignee")

        # 3) Charge sheets.
        write_charge_person_sheet(writer, daily_charge_by_person, fmts)
        write_charge_task_sheet(writer, daily_charge_by_task, fmts)

        # 4) Burndowns.
        if not bd_current.empty:
            bd_current.to_excel(writer, index=False, sheet_name="CurrentSprint")
            write_sprint_burndown_sheet(
                writer, bd_current, current_sprint, fmts, "CurrentSprint"
            )
        if not bd_prev.empty:
            bd_prev.to_excel(writer, index=False, sheet_name="PreviousSprint")
            write_sprint_burndown_sheet(
                writer, bd_prev, previous_sprint, fmts, "PreviousSprint"
            )

        # 5) By-assignee charts (sheet already written above).
        if not bd_assignee_pivot.empty:
            write_burndown_by_assignee_sheet(
                writer, bd_assignee_pivot, current_sprint, fmts
            )

    return output_file
