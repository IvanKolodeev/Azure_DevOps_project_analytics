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

Notebooks are run sequentially in Jupyter:
1. `0-Load_Azure_Snapshots.ipynb` - fetches data from ADO APIs, saves snapshots
2. `1-ADO_Reporting.ipynb` - reads snapshots, generates Excel reports

## Architecture

**Two-notebook pipeline:**

- **Notebook 0 (Data Ingestion):** Connects to Azure DevOps REST API (`dev.azure.com`) and OData Analytics endpoint (`analytics.dev.azure.com`). Downloads live work item snapshot via WIQL + batch API, saves to `live_snapshots/live_snapshot_YYYY-MM-DD.parquet`. Then loads historical snapshots from OData `WorkItemSnapshot` endpoint, resolves surrogate keys (UserSK, AreaSK, IterationSK) by joining dimension tables (Users, Areas, Iterations, Projects), and appends to `work_item_snapshot.parquet`.

- **Notebook 1 (Reporting):** Loads historical + latest live snapshot into a combined dataset. Computes daily charge deltas, parent work item hierarchy (Epic/Feature/User Story chains), sprint burndowns, and utilisation metrics. Outputs two Excel files: the main Team Analytics Dashboard and a utilisation report.

**Key data flow:** ADO REST API -> live Parquet snapshots -> historical Parquet (`work_item_snapshot.parquet`) -> pandas processing -> Excel (.xlsx) reports

**Configuration** is inline at the top of each notebook (org, project, paths, date ranges). ADO org is `Dtek-SP`. Project is set up by user.

## Data Files (gitignored)

- `work_item_snapshot.parquet` - cumulative historical snapshots, keyed on (WorkItemId, SnapshotDate)
- `live_snapshots/*.parquet` - daily live snapshots from REST API
- `*.xlsx` - generated Excel reports

## Key Dependencies

- `requests` + `HTTPBasicAuth` for ADO API calls (PAT-based auth)
- `pandas` + `pyarrow` for Parquet I/O and data manipulation
- `xlsxwriter` for Excel report generation with charts
- `openpyxl` for Excel reading
- `natsort` for natural sorting of sprint names
- `scipy` used for statistical calculations

## Known Issues

Tracked in `Functional_Requirements_Team_Analytics.md` under "Code fail cases and todos":
- Expired PAT gives unclear errors
- Empty current sprint causes calculation failures in burndown
