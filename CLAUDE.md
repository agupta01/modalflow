# Modalflow

## Project overview

Modalflow is a custom Airflow 3.x executor (`ModalExecutor`) that dispatches each Airflow task to its own **Modal Sandbox** created on demand. There is no deployed long-lived Modal Function and no `modal.Dict` state channel — the executor holds a handle to each task's Sandbox and reads results directly from it. The key files are:

- `src/modalflow/executor/modal_executor.py` — The executor. Runs in the Airflow scheduler process. Creates one Modal **Sandbox** per task via `modal.Sandbox.create()`, tracks each sandbox handle in `active_tasks`, and polls the sandbox process for completion (exit code). On completion it reads the structured task log file out of the sandbox via `sandbox.exec("cat", ...)` before terminating it, then writes the log locally via `_write_task_log()`. No `modal.Dict` is involved.
- `src/modalflow/modal_app.py` — A **builder module** (not a deployed app). Provides the task image, resolves the DAG Modal Volume, and exposes a `create_task_sandbox()` helper the executor uses to spin up per-task sandboxes. There is no `execute_modal_task` function and nothing to `modal deploy`.
- `src/modalflow/cli.py` — The `modalflow sync` CLI command (push DAGs to a Modal Volume). There is no `deploy` command — sandboxes are created on demand by the executor at task dispatch time.
- `tests/system/test_app.py` — E2E tests that run `airflow standalone` inside a Modal Sandbox.

## Notes

- Use `uv` for all Python package management.
- System tests use `airflow standalone` in a Modal Sandbox (no Docker required).

## Airflow 3.x execution API architecture (CRITICAL)

Understanding this is essential for debugging task execution issues:

1. **Two state reporting channels exist.** The task SDK reports state directly to the execution API (primary, authoritative). The executor reports state via `self.success()`/`self.fail()` event buffer (secondary). In Airflow 3.x, the execution API is the authority — executor events alone may not be sufficient to transition task state.

2. **The `/execution/` URL suffix is mandatory.** The Airflow task SDK uses *relative* paths (e.g. `task-instances/{id}/run`) with httpx. The base URL must end with `/execution/` so httpx resolves these relative paths to the execution API sub-app, not the REST API. Without it, requests hit wrong endpoints → 405 "Method Not Allowed". See [apache/airflow#51235](https://github.com/apache/airflow/issues/51235). The executor auto-appends `/execution/` if missing.

3. **Task execution lifecycle on Modal (Sandbox-per-task):**
   - Executor creates a Modal **Sandbox** (via `create_task_sandbox()` → `modal.Sandbox.create()`) for the task. The sandbox runs `python -m airflow.sdk.execution_time.execute_workload --json-string <json>` with the workload JSON and env vars (including `AIRFLOW__CORE__EXECUTION_API_SERVER_URL`). The DAG Volume is mounted at `/opt/airflow/dags`.
   - The executor stores the sandbox handle in `active_tasks` keyed by `_get_key_str(key)`. It does **not** fire-and-forget — it keeps the handle so it can poll the process and read logs later.
   - Inside the sandbox, the `execute_workload` process creates a supervisor, which `os.fork()`s a task runner child.
   - The task runner loads the DAG via the bundle system, executes the operator, and sends a `SucceedTask`/`TaskState` message back to the supervisor via socketpair.
   - The supervisor calls `client.task_instances.succeed()` → `PATCH /execution/task-instances/{id}/state`. This is unchanged and remains the authoritative state channel.
   - In `sync()`, the executor polls the sandbox process for completion (its return code). On completion it reads the structured log file from the sandbox via `sandbox.exec("cat", ...)`, calls `self.success()`/`self.fail()` based on the return code, writes the log locally via `_write_task_log()`, then terminates the sandbox.

4. **DAG bundles matter.** Airflow 3.x loads DAGs through named bundles. User DAGs use the `dags-folder` bundle (maps to `/opt/airflow/dags/`, sourced from the Modal Volume mounted into each sandbox). Airflow's built-in example DAGs use the `example_dags` bundle, which is NOT available on Modal — only DAGs pushed to the Volume via `modalflow sync` are present. Tasks referencing unavailable bundles will fail silently (return code 0 from supervisor, but no terminal state reported to execution API).

5. **Version matching matters.** The execution API uses cadwyn-based versioning. The task SDK sends version headers. If the sandbox runs a different Airflow version than the server, requests may be rejected. Use `--airflow-version` flag to match.

## Task logging architecture

Understanding how task logs flow is essential. There is a **single** log flow now — there is no longer a shared-volume vs no-shared-volume distinction driven by a deployed function. Each task runs in its own Sandbox and the executor pulls the log out of that sandbox before terminating it:

1. The Airflow SDK supervisor inside the task sandbox writes the structured log to `/opt/airflow/logs/{log_path}` (the sandbox's local filesystem).
2. When the task completes, the executor reads that file out of the sandbox via `sandbox.exec("cat", "/opt/airflow/logs/{log_path}")` in `sync()`, **before** terminating the sandbox.
3. The executor passes that content to `_write_task_log()`, which writes it to the local `base_log_folder` so `FileTaskHandler._read_from_local()` can find it.

This works identically whether Airflow runs locally (`airflow standalone`) or in production — there is no Modal Volume shared with the scheduler for logs, and no `state_dict`. If the sandbox is terminated before the log is read, the log is lost (the container is gone).

**How `FileTaskHandler._read()` works** (in `airflow/utils/log/file_task_handler.py`):
1. Try `_read_remote_logs()` — only if remote logging is configured (S3, GCS, etc.)
2. If task is RUNNING → call `executor.get_task_log(ti, try_number)` — our executor returns partial logs read live from the running sandbox
3. Try `_read_from_local(Path(self.local_base, rendered_path))` — globs for `{filename}*` in the parent directory
4. If no local or remote logs found AND task is finished → `_read_from_logs_server()` (HTTP to `ti.hostname:8793`) — this is what produces the "Could not read served logs" error since the Modal sandbox is gone

**The log file path** must match `FileTaskHandler._render_filename()` exactly. The default template is:
```
dag_id={{ ti.dag_id }}/run_id={{ ti.run_id }}/task_id={{ ti.task_id }}/{% if ti.map_index >= 0 %}map_index={{ ti.map_index }}/{% endif %}attempt={{ try_number|default(ti.try_number) }}.log
```
The executor constructs this from `TaskInstanceKey` fields. The `base_log_folder` comes from `conf.get("logging", "base_log_folder")` — on macOS this is typically `~/airflow/logs`, in Docker it's `/opt/airflow/logs`.

**The `ExecuteTask` workload model** (`airflow.executors.workloads.ExecuteTask`) has these fields: `token`, `ti`, `dag_rel_path`, `bundle_info`, `log_path`, `type`. The `log_path` field contains the relative log path (e.g., `dag_id=X/run_id=Y/task_id=Z/attempt=1.log`). The executor uses this path to `cat` the structured log file out of the sandbox after execution completes.

**The Airflow SDK supervisor writes structured logs** to `{base_log_folder}/{workload.log_path}` inside the task sandbox. These are the properly formatted logs with timestamps, living at `/opt/airflow/logs/{log_path}` on the sandbox's filesystem. The executor reads this file via `sandbox.exec("cat", ...)` once the sandbox process exits, then hands the content to `_write_task_log()`.

## System test infrastructure

- Base image: `apache/airflow:3.0.6-python3.10`, upgraded via pip. The Airflow version is configurable via `MODALFLOW_AIRFLOW_VERSION` env var (default `3.1.5`). The airflow user (uid 50000) owns the Python environment.
- **pip must run as the `airflow` user**, not root. Use `su -s /bin/bash airflow -c "pip install ..."` in `run_commands`.
- `uv build` creates wheels with 600 permissions; `make system.setup` runs `chmod 644` to fix this.
- `airflow standalone` checks `executor_class.is_local` and forces LocalExecutor if False. ModalExecutor sets `is_local = True` to bypass this.
- Output from `airflow standalone` requires `PYTHONUNBUFFERED=1` to stream in real time (no TTY in sandbox).
- Use `runuser -u airflow` (not `su`) for exec'd commands — `su` buffers stdout.
- Airflow 3.0.6 uses old-style `queue_command` (tuples); 3.1.x uses `queue_workload` (ExecuteTask objects). Must use 3.1.x.
- `modal.forward()` only works inside Modal Functions, not Sandboxes. Use `encrypted_ports=[8080]` + `sandbox.tunnels()` for Sandbox networking.
- DAG source is **volume-only**. DAGs live on a Modal Volume (pushed via `modalflow sync`) and are mounted into each task sandbox at `/opt/airflow/dags`. The old local-bake (`MODALFLOW_DAGS_DIR` / `--dags-source local`) and cloud-bucket (`MODALFLOW_DAGS_BUCKET`) modes have been **removed**.
- E2E tests: `make system.setup` (build wheel + `modalflow sync` DAGs to the Volume) then `make system.test.e2e` (pytest). There is no `modal deploy` step.
- Dev dependencies (pytest-timeout, etc.) require `uv sync --extra dev`.
- The E2E test mounts a logs Volume at `/opt/airflow/logs` inside the test's Airflow Sandbox. This requires `chmod 777 /opt/airflow/logs` before starting Airflow (see Modal gotchas below). Note: per-task sandboxes do NOT share this volume — the executor reads each task's log out of its own sandbox via `sandbox.exec("cat", ...)` and `_write_task_log()` writes it to this folder.
- Airflow 3.1.5 does NOT write `standalone_admin_password.txt` to disk. The password is only printed to stdout (`Simple auth manager | Password for user 'admin': <pw>`). The `start_airflow_and_wait_ready()` helper captures it from stdout and writes it to `/tmp/admin_password.txt` for the `get_task_logs()` helper to use.
- E2E log verification uses the Airflow REST API: `GET /api/v2/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}/logs/{try_number}` with basic auth (`admin:<password>`).

## Modal patterns

- `modal.Mount` is **deprecated**. Use `Image.add_local_file()` / `Image.add_local_dir()` instead.
- `add_local_file(..., copy=True)` bakes files into the image layer (needed before `run_commands`).
- `add_local_dir(...)` without `copy=True` mounts files at container startup (good for frequently changing files like test code).
- Docs: https://modal.com/docs/guide/sandbox-files#efficient-file-syncing

## Modal gotchas

- **`modal volume put <dir> /` nests the directory.** Uploading `./dags` to `/` creates `/dags/`, not files at `/`. Use the Python SDK (`vol.batch_upload()` + `batch.put_file()`) to place files at exact paths. This is what `modalflow sync` does.
- **`modal volume rm -r /` fails** ("Cannot remove the root directory"). Iterate `vol.listdir("/")` and `vol.remove_file(path, recursive=True)` each entry instead.
- **Modal FUSE volumes don't support `chown`.** The command returns success but has no effect. The volume root is owned by root. Use `chmod 777 /mount/path` to make it world-writable so the airflow user (uid 50000) can create subdirectories. This must be done before starting Airflow, otherwise the dag-processor crashes with `PermissionError: [Errno 13] Permission denied: '/opt/airflow/logs/dag_processor'`.

## Debugging tips

- **405 "Method Not Allowed" from api-server**: The execution API URL is missing the `/execution/` suffix. The executor should auto-append it, but check the `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` value being passed to the sandbox.
- **Task succeeds in the sandbox but shows `state=failed` in Airflow**: Read the sandbox's full stdout/stderr (e.g. `sandbox.stdout.read()`) for errors. Most likely the task SDK failed to send `PATCH /execution/task-instances/{id}/state`. Common causes: DAG bundle not available in the sandbox (DAGs not synced to the Volume), ngrok tunnel dropped, JWT token expired.
- **`executor_state=success` but `state=failed`**: The ModalExecutor reported success (the sandbox process exited 0), but the execution API never received the terminal state. The execution API is authoritative in Airflow 3.x.
- **"Could not read served logs" in Airflow UI**: `FileTaskHandler` tried to fetch logs from the sandbox's hostname on port 8793, but the sandbox is gone. This means `_read_from_local()` didn't find the log file. Check: (1) Is the executor package up to date? The `_write_task_log` method must exist. (2) Did `sync()` run and read the log out of the sandbox before terminating it? Check scheduler logs for "Wrote task log" messages. (3) Is `base_log_folder` consistent between the api-server and scheduler processes? Both use `conf.get("logging", "base_log_folder")`.
- **Logs work for static tasks but not mapped tasks**: Check that `_get_key_str` includes `map_index` for mapped tasks. Without it, all mapped instances of the same task collide on the same `active_tasks` key — only the last sandbox handle is tracked, so the others' logs/state are lost. The key format must be `dag_id:task_id:run_id:try_number:map_index` for mapped tasks (map_index >= 0).
- **Mapped tasks fail with `ServerResponseError` on `task-instances/{id}/run`**: The supervisor tries to start the task but the execution API rejects it. This can be transient (race with scheduler creating mapped TIs) or caused by JWT token expiry if the sandbox takes too long to cold-start. Usually self-resolves on retry.
- **dag-processor crashes with `PermissionError` on `/opt/airflow/logs/dag_processor`**: The logs directory is a Modal Volume mount owned by root. Run `chmod 777 /opt/airflow/logs` before starting Airflow. See Modal gotchas section.
- **Unit tests fail to collect with `ModuleNotFoundError: No module named 'airflow'`**: The test file at `tests/unit/test_modal_executor.py` mocks airflow modules before importing. If a new airflow submodule is imported by the executor, it must be added to the mock list (e.g., `sys.modules["airflow.configuration"] = mock_airflow.configuration`).

## Code conventions

- The executor module re-exports `ModalExecutor` via lazy `__getattr__` in `__init__.py` so both `modalflow.executor.ModalExecutor` and `modalflow.executor.modal_executor.ModalExecutor` work. The lazy import avoids pulling in airflow dependencies at module load time.
- Environment variables prefixed with `MODALFLOW_` are used for configuration (e.g. `MODALFLOW_ENV`, `MODALFLOW_AIRFLOW_VERSION`). Airflow-standard `AIRFLOW__` env vars are passed into each task sandbox at runtime.
- `_get_key_str(key)` serializes `TaskInstanceKey` to a string for use as `active_tasks` keys (each entry maps a key to its Modal Sandbox handle). Format: `dag_id:task_id:run_id:try_number` for non-mapped tasks, `dag_id:task_id:run_id:try_number:map_index` for mapped tasks (map_index >= 0). **Any new code using task keys must use this method** to avoid key collisions — without `map_index`, mapped task instances overwrite each other's sandbox handles.

## Testing locally vs in system tests

There are two test environments. Both exercise the **same** log path now: the executor reads each task's log out of its sandbox and `_write_task_log()` writes it to the local `base_log_folder`. There is no shared-volume shortcut that masks `_write_task_log` bugs.

1. **System tests** (`make system.test.e2e`): The test's Airflow runs inside a Modal Sandbox. Per-task sandboxes are created on demand by the executor; the executor `cat`s each task's log out of its sandbox before terminating it, and `_write_task_log()` writes it to `/opt/airflow/logs`. Logs are then read via `FileTaskHandler._read_from_local()`.

2. **Local testing** (user runs `airflow standalone` + ngrok): Airflow runs on the user's machine. Per-task sandboxes run on Modal. The executor's read-from-sandbox + `_write_task_log()` is the mechanism that makes logs available locally. If this code path is broken, the Airflow UI falls back to `_read_from_logs_server()` which tries `http://{ti.hostname}:8793/...` and fails.

The user tests locally by:
1. Running `airflow standalone` in `~/Documents/airflow-test/`
2. Exposing port 8080 via ngrok
3. Setting `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` to the ngrok URL
4. Installing modalflow via `uv pip install -e ~/Documents/modalflow` (or the worktree path)
5. Running `modalflow sync` to push the test DAGs to the Modal Volume
6. Triggering DAGs and checking the Airflow UI logs tab

**Important**: There is no `modal deploy` step anymore — sandboxes are created on demand, so changes to `modal_app.py` and `modal_executor.py` both take effect after reinstalling the package **and** restarting `airflow standalone`. The user may forget to reinstall — if logs aren't working locally, check `python -c "import inspect; import modalflow.executor.modal_executor as m; print(inspect.getsource(m.ModalExecutor._write_task_log))"` to verify the installed code matches. DAG changes take effect after re-running `modalflow sync`.
