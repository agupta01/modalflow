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

## Task logging architecture

Understanding how task logs flow is essential. There are two deployment modes with different log paths:

1. **Sandbox mode** (E2E tests): The Modal Functions write structured logs to `/opt/airflow/logs/{log_path}` on the `airflow-logs-{ENV}` volume. The Sandbox mounts the **same** volume, so `FileTaskHandler._read_from_local()` finds the files directly.

2. **Local/production mode** (user runs `airflow standalone` or a real Airflow deployment): No shared volume. The executor's `_write_task_log()` writes log content from `state_dict` to the local `base_log_folder` when tasks complete in `sync()`. This is the **only** way logs reach the local filesystem.

**How `FileTaskHandler._read()` works** (in `airflow/utils/log/file_task_handler.py`):
1. Try `_read_remote_logs()` — only if remote logging is configured (S3, GCS, etc.)
2. If task is RUNNING → call `executor.get_task_log(ti, try_number)` — our executor returns partial logs from `state_dict`
3. Try `_read_from_local(Path(self.local_base, rendered_path))` — globs for `{filename}*` in the parent directory
4. If no local or remote logs found AND task is finished → `_read_from_logs_server()` (HTTP to `ti.hostname:8793`) — this is what produces the "Could not read served logs" error since the Modal container is gone

**The log file path** must match `FileTaskHandler._render_filename()` exactly. The default template is:
```
dag_id={{ ti.dag_id }}/run_id={{ ti.run_id }}/task_id={{ ti.task_id }}/{% if ti.map_index >= 0 %}map_index={{ ti.map_index }}/{% endif %}attempt={{ try_number|default(ti.try_number) }}.log
```
The executor constructs this from `TaskInstanceKey` fields. The `base_log_folder` comes from `conf.get("logging", "base_log_folder")` — on macOS this is typically `~/airflow/logs`, in Docker it's `/opt/airflow/logs`.

**The `state_dict` carries log data** from Modal to the executor:
- `log_content`: Structured log file read from the volume (preferred, properly formatted)
- `stdout`: Full subprocess stdout (fallback, contains supervisor + task output)
- `stderr`: Last 2KB of stderr (for scheduler debug logging)

**The `ExecuteTask` workload model** (`airflow.executors.workloads.ExecuteTask`) has these fields: `token`, `ti`, `dag_rel_path`, `bundle_info`, `log_path`, `type`. The `log_path` field contains the relative log path (e.g., `dag_id=X/run_id=Y/task_id=Z/attempt=1.log`). The Modal function uses this to read the structured log file after execution.

**The Airflow SDK supervisor writes structured logs** to `{base_log_folder}/{workload.log_path}` inside the Modal function. These are the properly formatted logs with timestamps. The file exists on the Modal Volume at `/opt/airflow/logs/{log_path}`. The Modal function reads this file after the subprocess completes and stores it in `state_dict["log_content"]`.

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
- The E2E test Sandbox mounts the `airflow-logs-main` volume at `/opt/airflow/logs` for shared log access. This requires `chmod 777 /opt/airflow/logs` before starting Airflow (see Modal gotchas below).
- Airflow 3.1.5 does NOT write `standalone_admin_password.txt` to disk. The password is only printed to stdout (`Simple auth manager | Password for user 'admin': <pw>`). The `start_airflow_and_wait_ready()` helper captures it from stdout and writes it to `/tmp/admin_password.txt` for the `get_task_logs()` helper to use.
- E2E log verification uses the Airflow REST API: `GET /api/v2/dags/{dag_id}/dagRuns/{run_id}/taskInstances/{task_id}/logs/{try_number}` with basic auth (`admin:<password>`).

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
- **Modal FUSE volumes don't support `chown`.** The command returns success but has no effect. The volume root is owned by root. Use `chmod 777 /mount/path` to make it world-writable so the airflow user (uid 50000) can create subdirectories. This must be done before starting Airflow, otherwise the dag-processor crashes with `PermissionError: [Errno 13] Permission denied: '/opt/airflow/logs/dag_processor'`.

## Debugging tips

- **405 "Method Not Allowed" from api-server**: The execution API URL is missing the `/execution/` suffix. The executor should auto-append it, but check the `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` value being passed to the Modal function.
- **Task succeeds on Modal but shows `state=failed` in Airflow**: Check the full Modal function stdout (not truncated) for errors. Most likely the task SDK failed to send `PATCH /execution/task-instances/{id}/state`. Common causes: DAG bundle not available on Modal, ngrok tunnel dropped, JWT token expired.
- **`executor_state=success` but `state=failed`**: The ModalExecutor reported success (via state_dict), but the execution API never received the terminal state. The execution API is authoritative in Airflow 3.x.
- **"Could not read served logs" in Airflow UI**: `FileTaskHandler` tried to fetch logs from the Modal container's hostname on port 8793, but the container is gone. This means `_read_from_local()` didn't find the log file. Check: (1) Is the executor package up to date? The `_write_task_log` method must exist. (2) Did `sync()` run? Check scheduler logs for "Wrote task log" messages. (3) Is `base_log_folder` consistent between the api-server and scheduler processes? Both use `conf.get("logging", "base_log_folder")`.
- **Logs work for static tasks but not mapped tasks**: Check that `_get_key_str` includes `map_index` for mapped tasks. Without it, all mapped instances of the same task collide on the same `state_dict` key — only the last one's state/logs are preserved. The key format must be `dag_id:task_id:run_id:try_number:map_index` for mapped tasks (map_index >= 0).
- **Mapped tasks fail with `ServerResponseError` on `task-instances/{id}/run`**: The supervisor tries to start the task but the execution API rejects it. This can be transient (race with scheduler creating mapped TIs) or caused by JWT token expiry if the Modal function takes too long to cold-start. Usually self-resolves on retry.
- **dag-processor crashes with `PermissionError` on `/opt/airflow/logs/dag_processor`**: The logs directory is a Modal Volume mount owned by root. Run `chmod 777 /opt/airflow/logs` before starting Airflow. See Modal gotchas section.
- **Unit tests fail to collect with `ModuleNotFoundError: No module named 'airflow'`**: The test file at `tests/unit/test_modal_executor.py` mocks airflow modules before importing. If a new airflow submodule is imported by the executor, it must be added to the mock list (e.g., `sys.modules["airflow.configuration"] = mock_airflow.configuration`).
- **`test_execute_async` fails with `AttributeError: ... does not have the attribute 'execute_modal_task'`**: Pre-existing issue. The test patches `modalflow.executor.modal_executor.execute_modal_task` but the executor uses `modal.Function.from_name()` at runtime — there's no module-level attribute to patch.

## Code conventions

- The executor module re-exports `ModalExecutor` via lazy `__getattr__` in `__init__.py` so both `modalflow.executor.ModalExecutor` and `modalflow.executor.modal_executor.ModalExecutor` work. The lazy import avoids pulling in airflow dependencies at module load time.
- Environment variables prefixed with `MODALFLOW_` are used for deploy-time configuration (passed from CLI to `modal deploy`). Airflow-standard `AIRFLOW__` env vars are passed to the task subprocess at runtime.
- `_get_key_str(key)` serializes `TaskInstanceKey` to a string for use as `state_dict` keys and `active_tasks` keys. Format: `dag_id:task_id:run_id:try_number` for non-mapped tasks, `dag_id:task_id:run_id:try_number:map_index` for mapped tasks (map_index >= 0). **Any new code using task keys must use this method** to avoid key collisions.
- The Modal function and executor must agree on the key format — the executor sets `payload["task_key"]` using `_get_key_str`, and the Modal function uses it as the `state_dict` key. If you change the key format, both sides update automatically since the key flows through the payload.

## Testing locally vs in system tests

There are two distinct test modes that exercise different code paths:

1. **System tests** (`make system.test.e2e`): Airflow runs inside a Modal Sandbox. The Sandbox and Modal Functions share the `airflow-logs-{ENV}` volume at `/opt/airflow/logs`. Logs are read via `FileTaskHandler._read_from_local()` directly from the shared filesystem. The executor's `_write_task_log()` also runs but is redundant (the file already exists on the volume).

2. **Local testing** (user runs `airflow standalone` + ngrok): Airflow runs on the user's machine. No shared volume. The executor's `_write_task_log()` is the **only** mechanism that makes logs available locally. If this code path is broken, the Airflow UI falls back to `_read_from_logs_server()` which tries `http://{ti.hostname}:8793/...` and fails.

**When making changes to logging, always test BOTH modes.** System tests passing does not guarantee local mode works — shared volume access masks `_write_task_log` bugs. The user tests locally by:
1. Running `airflow standalone` in `~/Documents/airflow-test/`
2. Exposing port 8080 via ngrok
3. Setting `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` to the ngrok URL
4. Installing modalflow via `uv pip install -e ~/Documents/modalflow` (or the worktree path)
5. Triggering DAGs and checking the Airflow UI logs tab

**Important**: Changes to `modal_app.py` take effect after `modal deploy`. Changes to `modal_executor.py` take effect after reinstalling the package **and** restarting `airflow standalone`. The user may forget to reinstall — if logs aren't working locally, check `python -c "import inspect; import modalflow.executor.modal_executor as m; print(inspect.getsource(m.ModalExecutor._write_task_log))"` to verify the installed code matches.
