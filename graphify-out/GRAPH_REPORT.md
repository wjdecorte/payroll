# Graph Report - payroll  (2026-09-17)

## Corpus Check
- 48 files · ~15,095 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 8 file(s) not represented in the graph (top: (none) 5, .example 1, .ini 1)

## Summary
- 417 nodes · 728 edges · 29 communities (12 shown, 17 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 62 edges (avg confidence: 0.95)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `92c99339`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- PayrollRunService
- PayrollAPIClient
- conftest.py
- AppBaseError
- payroll_run/routers.py
- TestCalcPayroll
- TestCalculateEndpoint
- alembic_runner.py
- TestUsePreviousYtdFica
- frontend/__init__.py
- TestCalculate
- Payroll
- TestInfo
- AGENTS.md
- TestListRuns
- TestDeleteRun
- TestGetRun
- gunicorn_config.py
- payroll

## God Nodes (most connected - your core abstractions)
1. `PayrollRunService` - 36 edges
2. `PayrollAPIClient` - 20 edges
3. `PayrollConfigService` - 20 edges
4. `APIError` - 19 edges
5. `TestCalcPayroll` - 19 edges
6. `AppBaseSchema` - 15 edges
7. `AppBaseError` - 15 edges
8. `TestCalculate` - 14 edges
9. `PayrollInput` - 13 edges
10. `calc_federal_withholding()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `TestCalculate` --uses--> `PayrollConfigUpdate`  [INFERRED]
  tests/payroll_run/test_service.py → payroll/payroll_run/config_schemas.py
- `TestDeleteRun` --uses--> `PayrollRunNotFoundError`  [INFERRED]
  tests/payroll_run/test_service.py → payroll/payroll_run/exceptions.py
- `TestGetRun` --uses--> `PayrollRunNotFoundError`  [INFERRED]
  tests/payroll_run/test_service.py → payroll/payroll_run/exceptions.py
- `TestCalculate` --uses--> `PayrollRunAlreadyExistsError`  [INFERRED]
  tests/payroll_run/test_service.py → payroll/payroll_run/exceptions.py
- `TestCalculate` --uses--> `InvalidPayPeriodError`  [INFERRED]
  tests/payroll_run/test_service.py → payroll/payroll_run/exceptions.py

## Import Cycles
- None detected.

## Communities (29 total, 17 thin omitted)

### Community 0 - "PayrollRunService"
Cohesion: 0.07
Nodes (44): BaseModel, computed_field, math, AppBaseSchema, Base Pydantic schema for all API input/output models. - Uses camelCase aliases…, Convert snake_case to camelCase for JSON serialization., to_camel(), InvalidPayPeriodError (+36 more)

### Community 1 - "PayrollAPIClient"
Cohesion: 0.08
Nodes (39): asyncio, fastapi_templating, HTMLResponse, httpx, payroll_frontend, create_app(), FastAPI, APIError (+31 more)

### Community 2 - "conftest.py"
Cohesion: 0.07
Nodes (30): alembic, collections_abc, datetime, fastapi_testclient, get_session(), Session, FastAPI dependency that provides a SQLModel database session., AppBaseModel (+22 more)

### Community 3 - "AppBaseError"
Cohesion: 0.07
Nodes (30): APIRoute, contextlib, dataclasses, fastapi, fastapi_responses, fastapi_routing, json, logging (+22 more)

### Community 4 - "payroll_run/routers.py"
Cohesion: 0.10
Nodes (26): PayrollConfigBase, PayrollConfigRead, PayrollConfigUpdate, Full replacement update for payroll configuration., Editable payroll defaults and QuickBooks journal account names., PayrollConfigService, Session, Read and update the single runtime payroll configuration row. (+18 more)

### Community 5 - "TestCalcPayroll"
Cohesion: 0.07
Nodes (12): calc_federal_withholding(), calc_payroll(), Compute the annual federal income tax using IRS Pub 15-T Percentage Method for…, Calculate all payroll withholdings for an S-Corp owner-employee. S-Corp tax…, pytest, fixture, Unit tests for the pure tax calculation engine. These tests have no database or…, Test the federal income tax percentage method calculation. (+4 more)

### Community 6 - "TestCalculateEndpoint"
Cohesion: 0.06
Nodes (7): Integration tests for the payroll_run HTTP endpoints. Uses TestClient with the…, TestCalculateEndpoint, TestConfigEndpoint, TestDeleteRunEndpoint, TestGetRunEndpoint, TestListRunsEndpoint, TestTaxConstantsEndpoint

### Community 7 - "alembic_runner.py"
Cohesion: 0.17
Nodes (14): alembic_config, Config, pathlib, downgrade(), generate_revision(), _get_alembic_config(), _get_legacy_stamp_revision(), Alembic runner helpers. Called by app.py on startup to ensure the database… (+6 more)

### Community 8 - "TestUsePreviousYtdFica"
Cohesion: 0.17
Nodes (7): Tests for auto-pulling YTD FICA wages from the previous saved run., Second run should pick up ytd_fica_after from the first., If no saved run exists, ytd_fica_wages should default to 0., Three consecutive runs should chain YTD correctly., A run in 2027-01 should NOT pull YTD from a 2026 run., When use_previous_ytd_fica=True, the manual ytd_fica_wages is ignored., TestUsePreviousYtdFica

### Community 9 - "frontend/__init__.py"
Cohesion: 0.24
Nodes (8): functools, FrontendSettings, get_frontend_settings(), BaseSettings, get_settings(), BaseSettings, Settings, pydantic_settings

### Community 11 - "Payroll"
Cohesion: 0.20
Nodes (9): API endpoints, Configuration, Development, Docker, Payroll, Project layout, Quick start, Schema migrations (Alembic) (+1 more)

### Community 13 - "AGENTS.md"
Cohesion: 0.40
Nodes (4): Conventions, Global Agent Conventions, payroll, Python Conventions

## Knowledge Gaps
- **12 isolated node(s):** `payroll`, `Conventions`, `Global Agent Conventions`, `Python Conventions`, `Quick start` (+7 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 205 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **17 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `PayrollRunService` connect `PayrollRunService` to `payroll_run/routers.py`?**
  _High betweenness centrality (0.071) - this node is a cross-community bridge._
- **Why does `calc_payroll()` connect `TestCalcPayroll` to `PayrollRunService`?**
  _High betweenness centrality (0.069) - this node is a cross-community bridge._
- **Are the 19 inferred relationships involving `PayrollRunService` (e.g. with `calculate_payroll()` and `delete_payroll_run()`) actually correct?**
  _`PayrollRunService` has 19 INFERRED edges - model-reasoned connections that need verification._
- **Are the 9 inferred relationships involving `PayrollAPIClient` (e.g. with `admin()` and `admin_save()`) actually correct?**
  _`PayrollAPIClient` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `PayrollConfigService` (e.g. with `PayrollConfig` and `PayrollConfigRead`) actually correct?**
  _`PayrollConfigService` has 7 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `APIError` (e.g. with `admin()` and `admin_save()`) actually correct?**
  _`APIError` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `payroll`, `Conventions`, `Global Agent Conventions` to the rest of the system?**
  _12 weakly-connected nodes found - possible documentation gaps or missing edges._
