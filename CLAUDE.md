# Modalflow

## Notes

- Use `uv` for all Python package management.
- System tests use `airflow standalone` in a Modal Sandbox (no Docker required).

## System test infrastructure

- Base image: `apache/airflow:3.0.6-python3.10`. The airflow user (uid 50000) owns the Python environment.
- **pip must run as the `airflow` user**, not root. Use `su -s /bin/bash airflow -c "pip install ..."` in `run_commands`.
- `uv build` creates wheels with 600 permissions; `make system.setup` runs `chmod 644` to fix this.
- `airflow standalone` checks `executor_class.is_local` and forces LocalExecutor if False. ModalExecutor sets `is_local = True` to bypass this.
- Output from `airflow standalone` requires `PYTHONUNBUFFERED=1` to stream in real time (no TTY in sandbox).
- Use `runuser -u airflow` (not `su`) for exec'd commands — `su` buffers stdout.

## Modal patterns

- `modal.Mount` is **deprecated**. Use `Image.add_local_file()` / `Image.add_local_dir()` instead.
- `add_local_file(..., copy=True)` bakes files into the image layer (needed before `run_commands`).
- `add_local_dir(...)` without `copy=True` mounts files at container startup (good for frequently changing files like test code).
- Docs: https://modal.com/docs/guide/sandbox-files#efficient-file-syncing
