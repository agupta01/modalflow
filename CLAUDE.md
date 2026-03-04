# Modalflow

## Notes

- Use `uv` for all Python package management.
- System tests use `airflow standalone` in a Modal Sandbox (no Docker required).

## System test infrastructure

- Base image: `apache/airflow:3.0.6-python3.10`, upgraded to 3.1.5 via pip. The airflow user (uid 50000) owns the Python environment.
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
