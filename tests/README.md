# Regression Tests

Regression tests freeze the current pipeline output as baselines, then verify that code changes produce identical results.

## Directory structure

```
tests/
  README.md                 <- this file
  freeze_baselines.py       <- script to capture current results as baselines
  test_regression.py        <- pytest tests that compare new results to baselines
  baselines/                <- frozen data (gitignored)
    source/                 <- copy of input parquet files used for the test run
      MarketRMS_work_item_snapshot.parquet
      live_snapshots/
        MarketRMS_live_snapshot_*.parquet
    wi_raw_snapshots.parquet      <- combined & cleaned dataset
    sprints.parquet               <- unique sprints table
    scalars.parquet               <- current_sprint, previous_sprint
    daily_charge_by_person.parquet
    daily_charge_by_task.parquet
    daily_meeting_filtered.parquet
    bd_current.parquet            <- current sprint burndown
    bd_prev.parquet               <- previous sprint burndown
    bd_assignee_pivot.parquet     <- burndown by assignee
    charge_by_sprint.parquet      <- charged time mapped to sprints
    utilisation.parquet           <- utilisation detail
    utilisation_summary.parquet   <- utilisation summary per assignee/sprint
```

## How it works

1. **`freeze_baselines.py`** copies source data into `baselines/source/`, runs the full notebook calculation pipeline with a fixed date (`2026-04-01`), and saves each output dataframe as a parquet file in `baselines/`.

2. **`test_regression.py`** reads the frozen source data, re-runs the same pipeline, and compares every output dataframe to its saved baseline. Each test reports pass/fail per dataframe, with column-level detail on mismatches (row index, actual vs expected value).

## Usage

### Run tests after code changes

```bash
source .venv/bin/activate
pytest tests/test_regression.py -v
```

### Freeze new baselines

When you have **verified** that the current output is correct (e.g. after fixing a bug or intentionally changing logic), replace the baselines:

```bash
source .venv/bin/activate
python tests/freeze_baselines.py
```

Then re-run tests to confirm they pass:

```bash
pytest tests/test_regression.py -v
```

### Update baselines after source data changes

If the underlying ADO data has changed (new snapshots, different project), update the frozen date in both files to match, then re-freeze:

1. Edit the `today = date(...)` line in both `freeze_baselines.py` and `test_regression.py`
2. Run `python tests/freeze_baselines.py`
3. Run `pytest tests/test_regression.py -v` to confirm

## What each test covers

| Test | Dataframe | Pipeline stage |
|------|-----------|----------------|
| `test_wi_raw_snapshots` | `wi_raw_snapshots` | Data loading, type coercion, trash removal |
| `test_sprints` | `sprints` | Sprint extraction from dataset |
| `test_scalars` | `scalars` | Current/previous sprint detection |
| `test_daily_charge_by_person` | `daily_charge_by_person` | Daily charge aggregated by assignee |
| `test_daily_charge_by_task` | `daily_charge_by_task` | Daily charge aggregated by task |
| `test_daily_meeting_filtered` | `daily_meeting_filtered` | Stand-up report with parent chain and filtering |
| `test_bd_current` | `bd_current` | Current sprint burndown |
| `test_bd_prev` | `bd_prev` | Previous sprint burndown |
| `test_bd_assignee_pivot` | `bd_assignee_pivot` | Burndown by assignee for current sprint |
| `test_charge_by_sprint` | `charge_by_sprint` | Charged time mapped to working iteration |
| `test_utilisation` | `utilisation` | Utilisation detail (planned vs unplanned) |
| `test_utilisation_summary` | `utilisation_summary` | Utilisation summary per assignee/sprint |
