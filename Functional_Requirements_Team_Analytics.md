# Team Management Analytics Dashboard (Azure DevOps)
**Functional Requirements Document (FRD)**  
**Version:** v0.2  
**Date:** 2025-11-03

---

## Code fail cases and todos as of 2026-03-30:
#### 0-Load_Azure_Snapshots.ipynb
- if it is first run for the project - sync of historical data will not work
- if PAT has expired, error raised warning is not clear. Possibly add step which checks PAT and returns clear error if any.
- if "last_snapshot_date" is up-to-date, we do not need to load new info. As at now solved via SystemExit. Need something more proper
#### 1-ADO_Reporting.ipynb
- it is 1st day of sprint and no work item is assigned to it (or sprint not created). So, dataframe bd_current is empty and calculation fails. Need separate checks for both cases, returt WARNING and continue calculation without uploading burndowns


## 1. Purpose and Scope

The purpose of this dashboard is to provide Project Managers (PMs) and team members with a unified view of delivery progress, capacity usage, estimation accuracy, and daily work trends based on Azure DevOps (ADO) exports.

The system will operate on manually exported CSV/Excel files from ADO, process and enrich the data using Python (Pandas), and generate a consolidated analytical Excel report for PM and team usage.

---

## 2. Users and Use Cases

| User Type | Primary Needs |
|------------|----------------|
| **Project Manager (PM)** | Visibility into project and sprint progress, workload balance, and estimation reliability. |
| **Team Member (Developer / QA)** | Awareness of workload, daily progress, and self-management of time and estimation accuracy. |

---

## 3. Data Inputs

### 3.1 Required Fields (from Azure DevOps export)

| Field | Description |
|--------|-------------|
| `EPIC`, `FEATURE`, `USER STORY`, `TASK` | Hierarchical work structure (`FEATURE` and `USER STORY` are optional). |
| `Assigned To` | Team member name (email removed during processing). |
| `State` | Current workflow status. |
| `Iteration Path` | Sprint path (e.g. `ProjectX\Iteration MVP\Sprint 25`). |
| `Work Item Type` | e.g. Task, Bug, Tech Debt. |
| `Original Estimate` | Planned effort in hours (1 story point = 1 hour in current convention). |
| `Completed Work` | Logged completed effort. |
| `Remaining Work` | Remaining effort in hours. |
| `Changed Date` | Date of last update. |
| `Created Date` | Work item creation date. |

### 3.2 Historical Data Retention

- All data extracted from Azure DevOps shall be **stored historically**.  
- Each exported dataset (CSV) will be appended to a **master data table** (initially Excel; later optionally SQLite).  
- Each record will include a `Snapshot Date` — the date when the export was taken.  
- This allows reconstruction of project state for any past date and trend-based reporting.

---

## 4. Functional Requirements

### 4.1 Data Processing

1. Clean and transform ADO exports:  
   - Remove email from `Assigned To` (`Name <mail>` → `Name`).  
   - Extract `Sprint` as substring after the last `\` in `Iteration Path`.  
   - Convert numeric fields and percentages to proper numeric types.  
2. Aggregate task-level data into higher hierarchy levels (`USER STORY`, `FEATURE`, `EPIC`).  
3. Compute derived metrics:  
   - `TOTAL ESTIMATED`, `TOTAL COMPLETED`, `TOTAL REMAINING`.  
   - `% OF COMPLETION` (defined below).  
4. Retain historical snapshots for cumulative calculations (Burn-down, trends).  
5. Exclude tasks in state `"Removed"` from all progress and percentage calculations.

---

### 4.2 Report Outputs

The output is a single Excel workbook **Team Analytics Dashboard.xlsx** containing analytical worksheets, each with both a **data table** and **chart visualization**.

---

#### 0. w/s with all workitems and its hierarcy

**Usage:**
1. Used to navigate between workitems within project. 
2. Perform search of work item, which are not longer visible in ADO board (eg - long-ago closed tasks).

**Report structure**:
`IterationName` - name of Iteration (sprint), currently assigned to work item
`Epic` - parent EPIC of the work item
`User Story` - parent User Story of the work item
`Title` - title of the work item
`WorkItemType` - type of the work item
`Priority` - priority number currently assigned to the work item
`AssignedTo` - account name, to which work item is assigned
`State` - current status of the work item
`WorkItemURL` - URL to this item on ADO desk

#### 1. Report for Stand-Up - <font color = "green">**Done**</font>

**Definition:**
Report shows:
- tasks we should concentrate in current sprint (debt from last sprints + all for current sprint)
- tasks hierarcy - US and Epic for each chargable work item
- completed hours for the last 3 days

**Visualization:**
- Excel table with Iteration, Chargable Work Item (+hierarcy), Original Estimate (current), Completed Time (current), Remaining Time (current), Time completed for past 3 days - daily (time completed for Monday = Monday + Sunday + Saturday)

---

#### 2. Daily Completed Work Trend - <font color = "green">**Done**</font>

**Definition:**
Daily charge of “Completed Work” aggregated per:
- ADO Snapshot Date + Assignee
- ADO Snapshot Date + Assignee + Task

**Visualization:**
- Table: Daily completed hours per assignee and total.  

---

#### 3. Burn-down chart - <font color = "green">**Done**</font>

**Remaining Work Calculation:**
```
Remaining Work (for date D) = Σ(Remaining Work of all non-Removed tasks as of D)
```

**Visualization:**
- Table: Daily remaining and completed work by Epic and Sprint.  
- Chart: Line chart showing remaining work over sprint days (ideal vs. actual).

---

#### 4. Sprint target progress by assignee - <font color = "green">**Done**</font>

**Remaining Work Calculation:**
```
Remaining Work (for date D) = Σ(Remaining Work of all non-Removed tasks as of D)
```

**Visualization:**
- Chart: Histogramm showing how close each Assignee to 0 Remaining time.

---

#### 5. Estimated vs. Completed Work (Commitment reliability)

**(1) Current Sprint**
- Absolute comparison for all tasks in current sprint.  
- Chart: Dual bar (blue = estimated, green = completed).  

**(2) Trend Over Time**
- Compare total estimated vs. total completed work per sprint.  
- Chart: Line or grouped bar chart showing trend across sprints.

---

#### 6. Throughput

**Definition:**
Total count or effort (Original Estimate) of tasks moved to completed statuses per sprint, grouped by `Work Item Type` (Task, Bug, Tech Debt):
- by sprint
- on project level
- on assignee level (throughput trends)

**Visualization:**
- Table: Sprint × Work Item Type with totals.  
- Chart: Stacked bar chart (color-coded by type).

---

#### 7. Cycle Time

**Definition:**
Show how long it takes to complete a work item once work starts: team efficiency indicator.
Cumulative sum of estimated effort (Original Estimate) for tasks moved to completed statuses each day within sprint, aggregated per assignee.
- project level
- assignee level
- per each sprint's work items

**Visualization:**
- Table: Date × Assignee × Cumulative Completed Effort.  
- Chart: Cumulative line chart per assignee (progress growing daily).

---

#### 8. Work Item lead time - weekly / by iteration

**Definition and Visualization:**
Table showing how long it takes from request to delivery (like we did on PPA task)
| Work Item name | Task Stated | Task Started | Completed | Deployed |
| --- | --- | --- | --- | --- |

---

#### 9. Work items tracking issues

**Definition:**
Incorrect task patterns - needed to be clean in order to have proper dashboard

**Rules:**
Desk clearance:
- closed / removed tasks with remaining time
- tasks in state 'New' with any completed time charged.
- tasks in Completed stages, but having Remaining time
- all final childs Closed or removed, but one or more of parents are not completed. Need to complete or create new tasks
- unassigned backlog items
- stale backlog items

#### 10. Project and Team management attention points

**Definition:**
Patterns indicating some actions or ceremonies are needed

**Rules:**
- total estimate for assignee for sprint: 61-80 hours - yellow,  more than 80 hours - red
- unadequate remaining time for assignee in current sprint (Available Capacity = (Working days in sprint × 6 hours per day per team member), Committed Work = Remaining time)
- not Closed/Removed tasks for previous sprints
- estimated tasks below 60 (?) hours per person - please create new tasks and make grooming
- blocked time per item - for statuses with business owners
- WIP in hours by assignee: High WIP = delays + context switching
- Iteration dates do not intersect
- assignee % of completion is calculated based on Remaining Time at 1st date of Sprint. What to do if team was not on time when estimationg time? Can take Estimated time as base od valuate progress based on Remaining time?

#### 10. Management level reports

- Throughput (completed story points / work items) per week/sprint/month - by department and by projects: Shows productivity and trend
- Commitment reliability (planned vs delivered per sprint) - by department and by projects: show if teams / department consistently meets commitments
- Utilisation by employee: instead of 9am time tracking
- Department level utilisation (workload distribution)
- Cycle Time of work item on department / project level: team efficiency indicator
- WIP in hours on project / department level: High WIP = delays + context switching


---

### 4.3 Visual and Formatting Rules

| Element | Rule |
|----------|------|
| **Numeric display** | 0 shown as “–”; numeric columns formatted as `#,##0.0;-#,##0.0;"-"`; percentages as `0.0%`. |
| **Coloring** | Header “COMPLETED FOR YESTERDAY” = blue (#0070C0); rows where `State == "Blocked"` = light red (#FFCDCE). |
| **Headers** | Bold, centered, wrapped; row height = 30 px. |
| **Column widths** | Text columns 22–60 px; numeric columns 15 px. |
| **Sorting** | Natural (“human”) sort by `Sprint`, `EPIC`, `FEATURE`, `USER STORY`; rows starting “Sprint” first. |
| **Freeze Panes** | Header row fixed for all sheets. |
| **Charts** | Embedded Excel charts matching each table. |

---

## 5. Non-Functional Requirements

| Category | Requirement |
|-----------|-------------|
| **Performance** | Report generation time ≤ 2 minutes for ≤ 5,000 tasks. |
| **Maintainability** | All configurable constants stored in a single parameters block. |
| **Portability** | Runs on Python 3.10+ with Pandas + XlsxWriter; outputs Excel (.xlsx). |
| **Data Integrity** | Each generated dataset appended to historical log with timestamp; no overwrites. |
| **Reliability** | Missing or malformed data handled gracefully (NaN → 0 / None). |
| **Usability** | Clear tab names, consistent color coding, embedded charts. |

---

## 6. Security and Data Handling

- All data processed locally; no external API access in Phase 1

---

## 7. Future Enhancements (Phase 2)

- Direct Azure DevOps REST API integration.  
- Automated sprint-length detection and normalization.  
- Predictive velocity and completion forecasting.  
- Power BI or web dashboard integration.  
- SQLite-based historical storage with query interface.  
- Automated daily job execution (scheduler).

---

