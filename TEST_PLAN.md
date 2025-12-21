# Test Plan for Serverless Airflow on Modal

This document outlines the testing strategy for the `modalflow` runtime. Our goal is to ensure stability and correctness while leveraging Modal's serverless infrastructure. We will use `pytest` as our primary runner and integrate specific tests from the Apache Airflow core suite to validate our custom components.

## 1. Testing Strategy Overview

We will employ a three-tiered testing approach:
1.  **Unit Tests**: Isolated tests for individual components (CLI, Executor, Scheduler Parsing), utilizing mocks for Modal and Airflow internals.
2.  **Integration Tests**: Tests verifying the interaction between the `ModalExecutor`, `Scheduler`, and Postgres DB (dockerized for local tests).
3.  **Core Compatibility Tests**: Running a subset of Apache Airflow's official test suite against our custom `ModalExecutor`.

## 2. Test Environment Setup

### Dependencies
-   `pytest`
-   `pytest-mock`
-   `pytest-asyncio`
-   `modal` (client)
-   `apache-airflow-core`
-   `testcontainers` (for local Postgres)

### Mocking Modal
To run tests fast and without cloud costs, we will use `unittest.mock` to mock:
-   `modal.Volume`: Mock filesystem operations (read/write/list).
-   `modal.Dict`: Mock dictionary operations with local state.
-   `modal.Function`: Mock `.spawn()` calls.

## 3. Component-Level Test Plan

### 3.1 Executor (`src/airflow_serverless/executor/modal_executor.py`)
The `ModalExecutor` is the most critical component.
-   **Unit Tests**:
    -   `execute_async`: Verify command serialization and `execute_task.spawn` call.
    -   `sync`: Verify status polling and reconciliation logic (DB vs Dict).
    -   **Reconciliation Test**: Simulate mismatch where DB=QUEUED and Dict=Missing -> Ensure task is re-queued.
    -   `terminate`: Verify task cancellation.
-   **Airflow Compliance**:
    -   Run standard Airflow executor tests.
    -   Target: `tests/executors/test_base_executor.py` (from Airflow source).

### 3.2 Scheduler (`src/airflow_serverless/scheduler/scheduler.py`)
-   **Unit Tests**:
    -   **Locking**: Verify `pg_try_advisory_lock` usage.
    -   **Parsing Timeout**: Create a "slow DAG" (sleeps 10s at top level). Verify the scheduler kills the parse process after 5s and records an `ImportError`.
    -   **Run Creation**: Verify `DagRun` entries in Postgres.
-   **Integration Tests**:
    -   Simulate `scheduler_tick()`.
    -   Verify it picks up a DAG from the (mocked) Volume.
    -   Verify it schedules a task to the `ModalExecutor`.
    -   Verify state updates in Postgres.

### 3.3 Database & Storage
-   **Database**:
    -   **Postgres Only**: Use `testcontainers-postgres` for local integration tests.
    -   Schema initialization: Verify tables are created correctly.
-   **Logging**:
    -   Test writing logs to `/vol/logs/dag/task/...`.
    -   Test reading logs via Airflow's `FileTaskHandler`.

### 3.4 CLI & Configuration
-   Test `modalflow create` config parsing.
-   Test `modalflow dags create` validation logic.

## 4. Leveraging Airflow Core Tests

We will run relevant subsets of the Airflow test suite.

| Component | Airflow Test File (Reference) | Goal |
| :--- | :--- | :--- |
| **Executor** | `tests/executors/test_base_executor.py` | Verify `ModalExecutor` implements the interface correctly. |
| **Scheduler** | `tests/jobs/test_scheduler_job.py` | Verify simplified scheduler loop respects task states. |
| **DAG Parsing** | `tests/models/test_dagbag.py` | Verify DAG loading mimics standard behavior. |
| **XCom** | `tests/models/test_xcom.py` | Verify `VolumeXComBackend` handles serialization correctly. |

## 5. End-to-End (E2E) Tests

E2E tests will run against a **dev** environment in Modal.

1.  **Deploy Env**: `modalflow create --env test-e2e` (Requires Postgres URL).
2.  **Upload DAG**: Upload `hello_world.py`.
3.  **Trigger**: Wait for scheduler cron.
4.  **Verify**:
    -   Check `scheduler` logs.
    -   Check `worker` logs in `/vol/logs/`.
    -   Check Postgres state for `success`.
5.  **Teardown**: Remove the test environment.

## 6. Continuous Integration (CI)

1.  **Lint**: `ruff` / `black`.
2.  **Unit Tests**: `pytest tests/unit` (Mocked Modal).
3.  **Integration**: `pytest tests/integration` (Local Postgres via Docker).
4.  **(Optional) Live Tests**: On merge to main.

## 7. Implementation Roadmap (Testing)

-   [ ] **Phase 1**: Set up `pytest` fixture for Mock Modal and Local Postgres.
-   [ ] **Phase 2**: Write `ModalExecutor` tests (including reconciliation logic).
-   [ ] **Phase 3**: Port `test_base_executor.py`.
-   [ ] **Phase 4**: Write "Slow Parsing" tests for Scheduler.
-   [ ] **Phase 5**: Write Logging I/O tests.
