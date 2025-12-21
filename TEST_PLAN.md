# Test Plan for Serverless Airflow on Modal

This document outlines the testing strategy for the `airflow-serverless` runtime. Our goal is to ensure stability and correctness while leveraging Modal's serverless infrastructure. We will use `pytest` as our primary runner and integrate specific tests from the Apache Airflow core suite to validate our custom components.

## 1. Testing Strategy Overview

We will employ a three-tiered testing approach:
1.  **Unit Tests**: Isolated tests for individual components (CLI, Config, DB backends), utilizing mocks for Modal and Airflow internals.
2.  **Integration Tests**: Tests verifying the interaction between the `ModalExecutor`, `Scheduler`, and Modal infrastructure (mocked or local).
3.  **Core Compatibility Tests**: Running a subset of Apache Airflow's official test suite against our custom `ModalExecutor` to ensure strict adherence to the Executor interface.

## 2. Test Environment Setup

### Dependencies
-   `pytest`
-   `pytest-mock`
-   `pytest-asyncio`
-   `modal` (client)
-   `apache-airflow-core` (and its test requirements)

### Mocking Modal
To run tests fast and without cloud costs, we will use `modal.App.local_entrypoint` patterns and `unittest.mock` to mock:
-   `modal.Volume`: Mock filesystem operations (read/write/list).
-   `modal.Dict`: Mock dictionary operations with local state.
-   `modal.Function`: Mock `.spawn()` calls to verify payload serialization.

## 3. Component-Level Test Plan

### 3.1 Executor (`src/airflow_serverless/executor/modal_executor.py`)
The `ModalExecutor` is the most critical component.
-   **Unit Tests**:
    -   `execute_async`: Verify command serialization and `execute_task.spawn` call.
    -   `sync`: Verify status polling from the (mocked) Modal Dict and state updates to `self.event_buffer`.
    -   `terminate`: Verify task cancellation logic.
-   **Airflow Compliance**:
    -   Run standard Airflow executor tests.
    -   Target: `tests/executors/test_base_executor.py` (from Airflow source).

### 3.2 Scheduler (`src/airflow_serverless/scheduler/scheduler.py`)
-   **Unit Tests**:
    -   Lock acquisition/release (using `sqlite` and `postgres` backends).
    -   DAG parsing logic (verify `DagBag` integration).
    -   Run creation: Verify `DagRun` and `TaskInstance` creation in the DB.
-   **Integration Tests**:
    -   Simulate a `scheduler_tick()` call.
    -   Verify it picks up a DAG from the (mocked) Volume.
    -   Verify it schedules a task to the `ModalExecutor`.
    -   Verify it updates the DB state.

### 3.3 Database & Storage
-   **Database**:
    -   Test `SQLiteBackend` concurrency (locking mechanism).
    -   Test `PostgresBackend` connection handling.
    -   Schema initialization: Verify tables are created correctly.
-   **DAG Storage**:
    -   Test `upload`, `list`, and `delete` against a local directory mimicking a Volume.
-   **XCom**:
    -   Test `VolumeXComBackend` pushing and pulling data to/from files.

### 3.4 CLI & Configuration
-   Test `airflow-serverless create` config parsing.
-   Test `airflow-serverless dags create` validation logic (ensuring bad DAGs are rejected locally).

## 4. Leveraging Airflow Core Tests

We will not reinvent tests for core Airflow logic. Instead, we will run relevant subsets of the Airflow test suite.

### 4.1 How to Run
We can install the airflow test suite or vendor specific files into our `tests/vendor` directory.

### 4.2 Target Tests
| Component | Airflow Test File (Reference) | Goal |
| :--- | :--- | :--- |
| **Executor** | `tests/executors/test_base_executor.py` | Verify `ModalExecutor` implements the interface correctly. |
| **Scheduler** | `tests/jobs/test_scheduler_job.py` | Verify our simplified scheduler loop respects task states (adapt as needed since we don't run the full Job). |
| **DAG Parsing** | `tests/models/test_dagbag.py` | Verify our DAG loading mimics standard behavior. |
| **XCom** | `tests/models/test_xcom.py` | Verify `VolumeXComBackend` handles serialization/deserialization correctly. |

## 5. End-to-End (E2E) Tests

E2E tests will run against a **dev** environment in Modal (or a high-fidelity local simulation).

1.  **Deploy Env**: `airflow-serverless create --env test-e2e`
2.  **Upload DAG**: Upload a `hello_world.py` DAG.
3.  **Trigger**: Wait for the scheduler cron (or manually invoke `scheduler_tick` for speed).
4.  **Verify**:
    -   Check `scheduler` logs for successful scheduling.
    -   Check `worker` logs for "Hello World" output.
    -   Check DB state for `success`.
5.  **Teardown**: Remove the test environment.

## 6. Continuous Integration (CI)

Our CI pipeline (GitHub Actions) will:
1.  **Lint**: `ruff` / `black`.
2.  **Unit Tests**: Run `pytest tests/unit` (mocked Modal).
3.  **Integration**: Run `pytest tests/integration` (mocked Modal/DB).
4.  *(Optional)* **Live Tests**: On merge to main, run E2E tests against a real Modal environment (requires `MODAL_TOKEN`).

## 7. Implementation Roadmap (Testing)

-   [ ] **Phase 1**: Set up `pytest` fixture for `modal.Volume` and `modal.Dict`.
-   [ ] **Phase 2**: Write unit tests for `ModalExecutor`.
-   [ ] **Phase 3**: Port/Adapt `test_base_executor.py` from Airflow.
-   [ ] **Phase 4**: Write integration tests for `scheduler_tick`.
-   [ ] **Phase 5**: Create the E2E test script using `modal` client.
