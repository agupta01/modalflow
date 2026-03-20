# Modalflow

## Project overview

Modalflow is a custom Airflow 3.x executor (`ModalExecutor`) that dispatches tasks to Modal Functions. The key files are:

- `src/modalflow/executor/modal_executor.py` — The executor. Runs in the Airflow scheduler process. Spawns Modal functions via `modal.Function.spawn()` and tracks state via `modal.Dict`.
- `src/modalflow/modal_app.py` — The Modal app definition. Defines the `execute_modal_task` function, the base image, volumes, and state dict. Deployed via `modal deploy`.
- `src/modalflow/cli.py` — The `modalflow deploy` and `modalflow sync` CLI commands.
- `tests/system/test_app.py` — E2E tests that run `airflow standalone` inside a Modal Sandbox.

## Notes

- Use `uv` for all Python package management.
- System tests use `airflow standalone` in a Modal Sandbox (no Docker required).

## Airflow 3.x execution API architecture (CRITICAL)

Understanding this is essential for debugging task execution issues:

1. **Two state reporting channels exist.** The task SDK reports state directly to the execution API (primary, authoritative). The executor reports state via `self.success()`/`self.fail()` event buffer (secondary). In Airflow 3.x, the execution API is the authority — executor events alone may not be sufficient to transition task state.

2. **The `/execution/` URL suffix is mandatory.** The Airflow task SDK uses *relative* paths (e.g. `task-instances/{id}/run`) with httpx. The base URL must end with `/execution/` so httpx resolves these relative paths to the execution API sub-app, not the REST API. Without it, requests hit wrong endpoints → 405 "Method Not Allowed". See [apache/airflow#51235](https://github.com/apache/airflow/issues/51235). The executor auto-appends `/execution/` if missing.

3. **Task execution lifecycle on Modal:**
   - Executor spawns Modal function with `workload_json` and env vars (including `AIRFLOW__CORE__EXECUTION_API_SERVER_URL`)
   - Modal function runs `python -m airflow.sdk.execution_time.execute_workload --json-string <json>`
   - The `execute_workload` process creates a supervisor, which `os.fork()`s a task runner child
   - The task runner loads the DAG via the bundle system, executes the operator, and sends a `SucceedTask`/`TaskState` message back to the supervisor via socketpair
   - The supervisor calls `client.task_instances.succeed()` → `PATCH /execution/task-instances/{id}/state`
   - The supervisor exits; Modal function reads return code and updates `state_dict`

4. **DAG bundles matter.** Airflow 3.x loads DAGs through named bundles. User DAGs use the `dags-folder` bundle (maps to `/opt/airflow/dags/`). Airflow's built-in example DAGs use the `example_dags` bundle, which is NOT available on Modal — only DAGs deployed via `--dags-path` are present. Tasks referencing unavailable bundles will fail silently (return code 0 from supervisor, but no terminal state reported to execution API).

5. **Version matching matters.** The execution API uses cadwyn-based versioning. The task SDK sends version headers. If the Modal function runs a different Airflow version than the server, requests may be rejected. Use `--airflow-version` flag to match.

## System test infrastructure

- Base image: `apache/airflow:3.0.6-python3.10`, upgraded via pip. The Airflow version is configurable via `MODALFLOW_AIRFLOW_VERSION` env var (default `3.1.5`). The airflow user (uid 50000) owns the Python environment.
- **pip must run as the `airflow` user**, not root. Use `su -s /bin/bash airflow -c "pip install ..."` in `run_commands`.
- `uv build` creates wheels with 600 permissions; `make system.setup` runs `chmod 644` to fix this.
- `airflow standalone` checks `executor_class.is_local` and forces LocalExecutor if False. ModalExecutor sets `is_local = True` to bypass this.
- Output from `airflow standalone` requires `PYTHONUNBUFFERED=1` to stream in real time (no TTY in sandbox).
- Use `runuser -u airflow` (not `su`) for exec'd commands — `su` buffers stdout.
- Airflow 3.0.6 uses old-style `queue_command` (tuples); 3.1.x uses `queue_workload` (ExecuteTask objects). Must use 3.1.x.
- `modal.forward()` only works inside Modal Functions, not Sandboxes. Use `encrypted_ports=[8080]` + `sandbox.tunnels()` for Sandbox networking.
- The Modal function needs DAG files — via image (`MODALFLOW_DAGS_DIR`), Volume (`MODALFLOW_DAGS_VOLUME`), or cloud bucket (`MODALFLOW_DAGS_BUCKET`).
- E2E tests: `make system.setup` (build + deploy with DAGs) then `make system.test.e2e` (pytest).
- Volume E2E tests: `make system.setup.volume` then `make system.test.e2e.volume`.
- Dev dependencies (pytest-timeout, etc.) require `uv sync --extra dev`.

## Modal patterns

- `modal.Mount` is **deprecated**. Use `Image.add_local_file()` / `Image.add_local_dir()` instead.
- `add_local_file(..., copy=True)` bakes files into the image layer (needed before `run_commands`).
- `add_local_dir(...)` without `copy=True` mounts files at container startup (good for frequently changing files like test code).
- Docs: https://modal.com/docs/guide/sandbox-files#efficient-file-syncing

## Modal gotchas

- **Conditional object definitions break containers.** Modal re-evaluates `modal_app.py` on the container side. If you define a Volume/Mount inside an `if ENV_VAR:` block, the env var won't be set remotely, so the container sees fewer deps than the deployed function expects → `ExecutionError: Function has N dependencies but container got M object ids`. Fix: pass the same env vars to the function via `@app.function(env={...})`.
- **`app.function()` has no `cloud_bucket_mounts` kwarg.** Both `modal.Volume` and `modal.CloudBucketMount` go in the `volumes={}` dict.
- **`modal volume put <dir> /` nests the directory.** Uploading `./dags` to `/` creates `/dags/`, not files at `/`. Use the Python SDK (`vol.batch_upload()` + `batch.put_file()`) to place files at exact paths.
- **`modal volume rm -r /` fails** ("Cannot remove the root directory"). Iterate `vol.listdir("/")` and `vol.remove_file(path, recursive=True)` each entry instead.

## Debugging tips

- **405 "Method Not Allowed" from api-server**: The execution API URL is missing the `/execution/` suffix. The executor should auto-append it, but check the `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` value being passed to the Modal function.
- **Task succeeds on Modal but shows `state=failed` in Airflow**: Check the full Modal function stdout (not truncated) for errors. Most likely the task SDK failed to send `PATCH /execution/task-instances/{id}/state`. Common causes: DAG bundle not available on Modal, ngrok tunnel dropped, JWT token expired.
- **`executor_state=success` but `state=failed`**: The ModalExecutor reported success (via state_dict), but the execution API never received the terminal state. The execution API is authoritative in Airflow 3.x.
- **Unit tests fail to collect with `ModuleNotFoundError: No module named 'airflow'`**: The test file at `tests/unit/test_modal_executor.py` mocks airflow modules before importing. If a new airflow submodule is imported by the executor, it must be added to the mock list (e.g., `sys.modules["airflow.configuration"] = mock_airflow.configuration`).
- **`test_execute_async` fails with `AttributeError: ... does not have the attribute 'execute_modal_task'`**: Pre-existing issue. The test patches `modalflow.executor.modal_executor.execute_modal_task` but the executor uses `modal.Function.from_name()` at runtime — there's no module-level attribute to patch.

## Code conventions

- The executor module re-exports `ModalExecutor` via lazy `__getattr__` in `__init__.py` so both `modalflow.executor.ModalExecutor` and `modalflow.executor.modal_executor.ModalExecutor` work. The lazy import avoids pulling in airflow dependencies at module load time.
- Environment variables prefixed with `MODALFLOW_` are used for deploy-time configuration (passed from CLI to `modal deploy`). Airflow-standard `AIRFLOW__` env vars are passed to the task subprocess at runtime.
