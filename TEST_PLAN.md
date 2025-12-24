# Test Plan - ModalExecutor

This plan focuses on verifying the `ModalExecutor` (Phase 1) ensures reliable task execution on Modal's infrastructure.

## 1. Unit Tests (Local)
Run via `pytest`. These tests verify the logic of the executor without actually calling Modal.

### mocks
-   **`modal.Function`**: Mock `.spawn()` to verify it receives the correct command payload.
-   **`modal.Dict`**: Use a local python `dict` to simulate remote state updates.
-   **`modal.Volume`**: Mock file operations.

### Test Cases (`tests/unit/test_modal_executor.py`)
1.  **`execute_async`**:
    -   Verify command is correctly serialized.
    -   Verify `spawn()` is called exactly once.
    -   Verify task is added to `pending_tasks` or `running` set.
2.  **`sync` (Happy Path)**:
    -   Simulate a "SUCCESS" entry in the mocked `modal.Dict`.
    -   Verify `executor.success(key)` is called.
    -   Verify the entry is removed from the Dict (cleanup).
3.  **`sync` (Failure Path)**:
    -   Simulate a "FAILED" entry (non-zero exit code).
    -   Verify `executor.fail(key)` is called.
4.  **`sync` (Zombie/Timeout)**:
    -   Simulate a task staying in `RUNNING` state for > timeout.
    -   Verify executor marks it as failed or retries (depending on policy).

## 2. Integration Tests (Live Modal)
Run via `pytest --integration`. These tests deploy a real ephemeral app to Modal.

### Setup
-   Requires `MODAL_TOKEN` in environment.
-   Creates a temporary `modal.Dict` and `modal.Volume` with a unique suffix.

### Test Cases (`tests/integration/test_live_execution.py`)
1.  **Hello World**:
    -   Manually invoke `executor.execute_async()` with a simple command: `echo "hello"`.
    -   Wait for `sync()` loop to detect success.
    -   Verify "hello" appears in the logs on the Volume.
2.  **Environment Variables**:
    -   Task: `echo $MY_ENV_VAR`.
    -   Verify the worker correctly inherited or received the env var.
3.  **Failure Propagation**:
    -   Task: `exit 1`.
    -   Verify `executor` detects the failure and alerts Airflow.

## 3. Airflow Compatibility Tests
(Advanced) Mount the `ModalExecutor` into a standalone Airflow process.

1.  **Setup**: Install `modalflow` in a local Airflow venv.
2.  **Config**: Set `AIRFLOW__CORE__EXECUTOR=modalflow.executor.modal_executor.ModalExecutor`.
3.  **Run**: Trigger a simple DAG.
4.  **Verify**: Check that the task runs on Modal (via Modal dashboard) and succeeds in Airflow UI.
