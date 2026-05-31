from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union

import modal
from airflow.configuration import conf
from airflow.executors.base_executor import BaseExecutor
from airflow.executors import workloads as executor_workloads
from airflow.models.taskinstance import TaskInstanceKey

from modalflow import modal_app

if TYPE_CHECKING:
    from airflow.executors.workloads import All as ExecutorWorkload
    from airflow.models.taskinstance import TaskInstance

# Type alias for command - can be a list containing a workload or list of strings
CommandType = Union[List[executor_workloads.ExecuteTask], List[str]]

# Configuration - should match modal_app.py
ENV = os.environ.get("MODALFLOW_ENV", "main")
CONCURRENCY_LIMIT = 100


class ModalExecutor(BaseExecutor):
    """
    An Airflow Executor that runs each task as an independent Modal Sandbox.
    """

    is_local: bool = True

    def __init__(self):
        # Use the same concurrency limit as the Modal function
        super().__init__(parallelism=CONCURRENCY_LIMIT)
        # Maps task_key_str -> handle dict:
        #   {"key": TaskInstanceKey, "sandbox": Sandbox,
        #    "process": ContainerProcess, "log_path": str | None}
        self.active_tasks: Dict[str, Dict[str, Any]] = {}
        # These will be initialized in start()
        self._app = None
        self._image = None
        self._volumes = None
        self._execution_api_url = None

    @property
    def slots_available(self) -> int:
        """
        Return the number of slots available to run tasks.
        This is checked by the scheduler to determine if more tasks can be queued.
        """
        return self.parallelism - len(self.running) - len(self.queued_tasks)

    def start(self):
        """
        Initialize the executor by building the task image, resolving the
        DAG + log volumes, and looking up (or creating) the Modal App that
        owns the per-task sandboxes.

        No deployed function or state dict is required — each task runs as
        an independent Modal Sandbox created on demand by ``execute_async``.
        """
        self.log.info("Starting ModalExecutor")

        # Build the task image and resolve volumes via the builder module.
        try:
            self._app = modal.App.lookup(modal_app.APP_NAME, create_if_missing=True)
            self._image = modal_app.build_task_image()
            self._volumes = modal_app.resolve_volumes()
            self.log.info(
                f"ModalExecutor ready: app={modal_app.APP_NAME}, "
                f"volumes={list(self._volumes.keys())}"
            )
        except Exception as e:
            self.log.error(f"Failed to initialize Modal resources: {e}")
            raise

        # Set up execution API URL.
        # Priority: env var > Airflow config > error
        self._execution_api_url = self._resolve_execution_api_url()
        self.log.info(f"Execution API URL: {self._execution_api_url}")

    def execute_async(
        self,
        key: TaskInstanceKey,
        command: CommandType,
        queue: Optional[str] = None,
        executor_config: Optional[Any] = None,
    ) -> None:
        """
        Trigger a task execution by creating a Modal Sandbox.

        Handles two formats:
        - New-style: command is a list containing an ExecuteTask workload object.
          The sandbox runs ``python -m airflow.sdk.execution_time.execute_workload``.
        - Old-style: command is a list of strings (CLI command), run directly.
        """
        task_key_str = self._get_key_str(key)

        if self._execution_api_url is None:
            raise RuntimeError(
                "Execution API URL not configured. Ensure start() was called successfully."
            )

        log_path = None

        if len(command) == 1 and isinstance(command[0], executor_workloads.ExecuteTask):
            # New-style: serialize the workload to JSON and run via the SDK.
            workload = command[0]
            workload_json = workload.model_dump_json()
            try:
                log_path = json.loads(workload_json).get("log_path")
            except Exception:
                log_path = None
            sandbox_command = [
                "python",
                "-m",
                "airflow.sdk.execution_time.execute_workload",
                "--json-string",
                workload_json,
            ]
        elif all(isinstance(c, str) for c in command):
            # Old-style: run the CLI command directly.
            sandbox_command = list(command)
        else:
            raise RuntimeError(
                f"ModalExecutor doesn't know how to handle command of type: {type(command)}"
            )

        self.log.info(f"Creating Modal sandbox for {task_key_str}")

        task_env = self._get_task_env(key, executor_config)

        try:
            # Create an idle (sleep infinity) sandbox, then run the task as
            # an exec'd process inside it.  This keeps the sandbox alive after
            # the task exits so we can read the structured log file before
            # terminating it.
            sb = modal_app.create_task_sandbox(
                app=self._app,
                image=self._image,
                volumes=self._volumes,
                env=task_env,
            )
            proc = sb.exec(*sandbox_command, env=task_env)
            self.active_tasks[task_key_str] = {
                "key": key,
                "sandbox": sb,
                "process": proc,
                "log_path": log_path,
            }
        except Exception as e:
            self.log.error(f"Failed to create Modal sandbox: {e}")
            self.fail(key)

    def _process_workloads(self, workloads: Sequence) -> None:
        """
        Process workloads from the base executor.

        Handles both new-style ExecuteTask workloads and old-style
        (command, priority, queue, executor_config) tuples queued via
        queue_command.
        """
        for workload in workloads:
            if isinstance(workload, executor_workloads.ExecuteTask):
                ti = workload.ti
                key = TaskInstanceKey(
                    dag_id=ti.dag_id,
                    task_id=ti.task_id,
                    run_id=ti.run_id,
                    try_number=ti.try_number,
                    map_index=ti.map_index,
                )
                queue = ti.queue
                executor_config = ti.executor_config or {}
                command = [workload]
            elif isinstance(workload, tuple):
                # Old-style: (command, priority, queue, executor_config)
                command, _priority, queue, executor_config = workload
                # Parse TaskInstanceKey from the CLI command list
                # Format: ['airflow', 'tasks', 'run', dag_id, task_id, run_id, ...]
                key = TaskInstanceKey(
                    dag_id=command[3],
                    task_id=command[4],
                    run_id=command[5],
                    try_number=1,
                    map_index=-1,
                )
            else:
                self.log.error(
                    f"Skipping unrecognized workload type: {type(workload)}"
                )
                continue

            if key in self.queued_tasks:
                del self.queued_tasks[key]

            self.execute_async(
                key=key,
                command=command,
                queue=queue,
                executor_config=executor_config,
            )
            self.running.add(key)

    def sync(self) -> None:
        """
        Poll each tracked sandbox for completion (non-blocking).

        For each task whose sandbox process has finished, read the
        structured log file out of the sandbox (before terminating it),
        write it to the local log folder, report success/failure based on
        the return code, terminate the sandbox, and stop tracking it.
        """
        if not self.active_tasks:
            return

        completed_keys = []

        for task_key_str, handle in self.active_tasks.items():
            key = handle["key"]
            proc = handle["process"]
            sb = handle["sandbox"]
            log_path = handle.get("log_path")

            # poll() returns None while running, else the exit code.
            try:
                return_code = proc.poll()
            except Exception as e:
                self.log.warning(f"Error polling sandbox for {task_key_str}: {e}")
                continue

            if return_code is None:
                # Still running.
                continue

            # Read the structured log file out of the sandbox BEFORE
            # terminating it, so logs are available locally.
            log_content = self._read_sandbox_log(sb, log_path)
            if log_content:
                self._write_task_log(log_content, key)

            if return_code == 0:
                self.success(key)
                self.log.info(f"Task {task_key_str} succeeded")
            else:
                self.fail(key)
                self.log.error(
                    f"Task {task_key_str} failed (return code {return_code})"
                )

            # Terminate the sandbox to release resources.
            try:
                sb.terminate()
            except Exception as e:
                self.log.warning(f"Failed to terminate sandbox for {task_key_str}: {e}")

            completed_keys.append(task_key_str)

        for k in completed_keys:
            del self.active_tasks[k]

    def _read_sandbox_log(self, sandbox, log_path: Optional[str]) -> str:
        """Read the structured log file from a (still-running) sandbox.

        Returns the file contents, or an empty string if unavailable.
        """
        if not log_path:
            return ""

        log_file = os.path.join("/opt/airflow/logs", log_path)
        try:
            proc = sandbox.exec("cat", log_file)
            content = proc.stdout.read()
            proc.wait()
            if proc.returncode != 0:
                return ""
            return content or ""
        except Exception as e:
            self.log.warning(f"Failed to read log file {log_file} from sandbox: {e}")
            return ""

    def get_task_log(self, ti: TaskInstance, try_number: int) -> tuple[list[str], list[str]]:
        """Return partial logs from the live sandbox for running tasks.

        Called by FileTaskHandler when ti.state == RUNNING.  Reads the
        in-progress structured log file directly from the running sandbox.
        Once the task completes, full logs are read from the local
        filesystem (written by ``_write_task_log`` during ``sync()``).
        """
        task_key_str = self._get_key_str(ti.key)
        handle = self.active_tasks.get(task_key_str)
        if not handle:
            return [], []

        content = self._read_sandbox_log(handle["sandbox"], handle.get("log_path"))
        if content:
            return (
                [f"Modal task output for {task_key_str}"],
                [content],
            )
        return [f"Task {task_key_str} running on Modal"], ["Waiting for output..."]

    def _write_task_log(self, content: str, key: TaskInstanceKey) -> None:
        """Write task log content to the local base_log_folder.

        This makes ``FileTaskHandler._read_from_local()`` work for completed
        tasks even when the Airflow scheduler doesn't share a Modal Volume
        with the task sandboxes (e.g. local / production deployments).
        """
        if not content:
            return

        try:
            base_log_folder = conf.get("logging", "base_log_folder")
        except Exception:
            base_log_folder = "/opt/airflow/logs"

        # Construct the log path to match FileTaskHandler's default template:
        #   dag_id={dag_id}/run_id={run_id}/task_id={task_id}/attempt={try_number}.log
        parts = [
            f"dag_id={key.dag_id}",
            f"run_id={key.run_id}",
            f"task_id={key.task_id}",
        ]
        if key.map_index >= 0:
            parts.append(f"map_index={key.map_index}")
        parts.append(f"attempt={key.try_number}.log")
        log_rel_path = os.path.join(*parts)

        full_path = Path(base_log_folder) / log_rel_path
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            self.log.info(f"Wrote task log ({len(content)} bytes) to {full_path}")
        except Exception as e:
            self.log.warning(f"Failed to write task log to {full_path}: {e}")

    def end(self) -> None:
        """
        Terminate the executor and cleanup resources.

        Terminates any sandboxes that are still tracked (e.g. on shutdown
        while tasks are mid-flight).
        """
        self.log.info("Shutting down ModalExecutor")
        self.heartbeat_interval = 0

        for task_key_str, handle in list(self.active_tasks.items()):
            try:
                handle["sandbox"].terminate()
            except Exception as e:
                self.log.warning(
                    f"Failed to terminate sandbox for {task_key_str}: {e}"
                )
        self.active_tasks.clear()

    def terminate(self) -> None:
        """
        Force terminate.
        """
        self.end()

    def _get_key_str(self, key: TaskInstanceKey) -> str:
        """
        Serialize TaskInstanceKey to a string.
        Format: dag_id:task_id:run_id:try_number[:map_index]

        map_index is included only for mapped tasks (>= 0) to avoid
        collisions in state_dict and active_tasks.
        """
        base = f"{key.dag_id}:{key.task_id}:{key.run_id}:{key.try_number}"
        if key.map_index >= 0:
            return f"{base}:{key.map_index}"
        return base

    def _resolve_execution_api_url(self) -> str:
        """
        Resolve the execution API URL.

        Priority:
        1. Environment variable AIRFLOW__CORE__EXECUTION_API_SERVER_URL
        2. Airflow config: core.execution_api_server_url

        The URL must end with ``/execution/`` because the Airflow task SDK
        uses relative paths (e.g. ``task-instances/{id}/run``).  If the
        configured URL doesn't include this suffix, we append it
        automatically so requests route to the execution API sub-app
        instead of the REST API.

        Returns:
            Execution API URL string

        Raises:
            RuntimeError: If URL cannot be determined
        """
        # 1. Check environment variable (takes precedence)
        env_url = os.environ.get("AIRFLOW__CORE__EXECUTION_API_SERVER_URL")
        if env_url:
            self._validate_api_url(env_url)
            return self._ensure_execution_prefix(env_url)

        # 2. Check Airflow config
        try:
            config_url = conf.get("core", "execution_api_server_url", fallback=None)
            if config_url:
                self._validate_api_url(config_url)
                return self._ensure_execution_prefix(config_url)
        except Exception as e:
            self.log.warning(f"Error reading execution_api_server_url from config: {e}")

        raise RuntimeError(
            "Execution API URL not configured. "
            "Set AIRFLOW__CORE__EXECUTION_API_SERVER_URL or the "
            "core.execution_api_server_url Airflow config."
        )

    def _validate_api_url(self, url: str) -> None:
        """
        Validate that the execution API URL is properly formatted.

        Args:
            url: URL string to validate

        Raises:
            ValueError: If URL is invalid
        """
        if not url:
            raise ValueError("Execution API URL cannot be empty")

        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(
                f"Execution API URL must start with http:// or https://: {url}"
            )

    @staticmethod
    def _ensure_execution_prefix(url: str) -> str:
        """Ensure the URL ends with ``/execution/``.

        The Airflow task SDK uses *relative* paths (e.g.
        ``task-instances/{id}/run``) against the base URL.  If the URL
        doesn't include the ``/execution/`` path, requests hit the REST
        API instead of the execution API, producing 405 errors.
        """
        stripped = url.rstrip("/")
        if not stripped.endswith("/execution"):
            stripped += "/execution"
        return stripped + "/"

    def _get_task_env(self, key: TaskInstanceKey, executor_config: Any) -> Dict[str, str]:
        """
        Gather environment variables to pass to the worker.

        Includes the execution API URL so Modal Functions can phone home.
        """
        if self._execution_api_url is None:
            raise RuntimeError(
                "Execution API URL not set. Ensure start() was called successfully."
            )

        env = {
            "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": self._execution_api_url,
        }
        return env
