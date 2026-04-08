# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Azure DevOps (ADO) team analytics dashboard. Fetches work item data from ADO REST APIs, stores daily snapshots as Parquet files, and generates Excel reports with charts for project managers and team members.

## Setup and Running

```bash
# Create venv and install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configure ADO access
cp .env.example .env
# Edit .env with your PERSONAL_ACCESS_TOKEN
```

Notebooks are run sequentially in Jupyter from the `notebooks/` directory:
1. `notebooks/0-Load_Azure_Snapshots.ipynb` - fetches data from ADO APIs, saves snapshots
2. `notebooks/1-ADO_Reporting.ipynb` - reads snapshots, generates the Team Analytics Dashboard workbook (status, daily charge, burndowns)
3. `notebooks/2-Utilisation_Reporting.ipynb` - reads snapshots, generates the per-(assignee, sprint) utilisation workbook

## Architecture

**Three-notebook pipeline:**

- **Notebook 0 (Data Ingestion):** Connects to Azure DevOps REST API (`dev.azure.com`) and OData Analytics endpoint (`analytics.dev.azure.com`). Downloads live work item snapshot via WIQL + batch API, saves to `data/live_snapshots/{project}_live_snapshot_YYYY-MM-DD.parquet`. Then loads historical snapshots from OData `WorkItemSnapshot` endpoint, resolves surrogate keys (UserSK, AreaSK, IterationSK) by joining dimension tables (Users, Areas, Iterations, Projects), and appends to `data/{project}_work_item_snapshot.parquet`.

- **Notebook 1 (Team Analytics Reporting):** Loads historical + latest live snapshot into a combined dataset (`ado_reporting.get_wi_snapshots`). Computes daily charge deltas (`daily_charge`), parent work item hierarchy / stand-up table (`work_items_report`), and sprint burndowns overall + per-assignee (`burndown`). Writes the Team Analytics Dashboard workbook via `excel_import.save_team_analytics_report`.

- **Notebook 2 (Utilisation Reporting):** Loads the same combined snapshot dataset and the per-task daily charge table. Computes charges mapped to working iteration, per-task planned-vs-unplanned utilisation detail, and the per-(assignee, sprint) summary (`utilisation`). Writes the utilisation workbook via `excel_import.save_utilisation_report`.

**Key data flow:** ADO REST API -> live Parquet snapshots -> historical Parquet (`data/{project}_work_item_snapshot.parquet`) -> pandas processing in `src/` modules -> Excel (.xlsx) reports

**Project structure:**
- `notebooks/` - Jupyter notebooks (pipeline entry points)
- `src/` - Python modules:
  - `ado_auth.py`, `ado_live_snapshot.py`, `ado_snapshot_history.py` - ADO API + OData ingestion
  - `ado_reporting.py` - combined-snapshot loader + sprint detection
  - `data_validation.py` - `today` validation helpers
  - `daily_charge.py` - per-person and per-task daily charge tables (+ Power BI export)
  - `work_items_report.py` - stand-up / daily meeting report builder
  - `burndown.py` - sprint burndown (overall and per-assignee)
  - `utilisation.py` - charge-by-sprint, utilisation detail, and summary
  - `excel_import.py` - workbook writers (`save_team_analytics_report`, `save_utilisation_report`)
- `tests/` - regression tests against frozen baselines (see `tests/README.md`)
- `data/` - all input/output data files (gitignored)

**Configuration** is inline at the top of each notebook (org, project, paths, date ranges). ADO org is `Dtek-SP`. Project is set up by user.

## Data Files (gitignored, under `data/`)

- `data/{project}_work_item_snapshot.parquet` - cumulative historical snapshots, keyed on (WorkItemId, SnapshotDate)
- `data/live_snapshots/{project}_live_snapshot_*.parquet` - daily live snapshots from REST API
- `data/{project}_ProjectStatus_{date}_ADO_API.xlsx` - Team Analytics Dashboard workbook (notebook 1)
- `data/{project}_Utilisation_{date}.xlsx` - utilisation workbook (notebook 2)
- `data/{project}_PowerBI_daily_charge.csv` - long-format daily charge for Power BI

## Tests

Regression tests live under `tests/` and verify that pipeline outputs match frozen baselines.

```bash
source .venv/bin/activate
pytest tests/test_regression.py -v        # run regression tests
python tests/freeze_baselines.py          # refreeze baselines after intentional changes
```

**Test layout:**
- `tests/README.md` - usage and the per-test coverage table
- `tests/freeze_baselines.py` - copies the current `data/` source parquets into `tests/baselines/source/`, runs the full pipeline at a fixed `today = 2026-04-01`, saves each output dataframe as a parquet baseline
- `tests/test_regression.py` - reloads source parquets from `tests/baselines/source/`, re-runs the pipeline, and compares each output dataframe against its baseline column-by-column (numeric `np.isclose`, otherwise exact)
- `tests/baselines/` (gitignored) - the frozen source data + per-stage parquet baselines

Both `freeze_baselines.py` and `test_regression.py` import the production modules in `src/` (`daily_charge`, `burndown`, `utilisation`, ...) so the tests exercise the same code the notebooks run.

Frozen baseline dataframes (one per pipeline stage):
`wi_raw_snapshots`, `sprints`, `scalars` (current/previous sprint),
`daily_charge_by_person`, `daily_charge_by_task`, `daily_meeting_filtered`,
`bd_current`, `bd_prev`, `bd_assignee_pivot`,
`charge_by_sprint`, `utilisation`, `utilisation_summary`.

## Key Dependencies

- `requests` + `HTTPBasicAuth` for ADO API calls (PAT-based auth)
- `pandas` + `pyarrow` for Parquet I/O and data manipulation
- `xlsxwriter` for Excel report generation with charts
- `openpyxl` for Excel reading
- `natsort` for natural sorting of sprint names
- `scipy` used for statistical calculations

## Known Issues

Tracked in `Functional_Requirements_Team_Analytics.md` under "Code fail cases and todos":
- Empty current sprint causes calculation failures in burndown
