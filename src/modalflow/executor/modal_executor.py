from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union
from urllib.parse import urljoin

import modal
from airflow.configuration import conf
from airflow.executors.base_executor import BaseExecutor
from airflow.executors import workloads as executor_workloads
from airflow.models.taskinstance import TaskInstanceKey

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
    An Airflow Executor that runs tasks as Modal Functions.
    """

    is_local: bool = True

    def __init__(self):
        # Use the same concurrency limit as the Modal function
        super().__init__(parallelism=CONCURRENCY_LIMIT)
        self.active_tasks: Dict[str, TaskInstanceKey] = {}
        # These will be initialized in start()
        self._modal_function = None
        self._state_dict = None
        self._tunnel_context = None  # Store the context manager
        self._tunnel = None  # Store the tunnel object from __enter__()
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
        Initialize the executor by looking up the deployed Modal function and state dict.
        Also sets up networking (tunnel for local or production URL).
        """
        self.log.info("Starting ModalExecutor")

        app_name = f"modalflow-{ENV}"
        dict_name = f"airflow-state-{ENV}"

        # Look up the deployed Modal function
        try:
            self._modal_function = modal.Function.from_name(
                app_name, "execute_modal_task"
            )
            self.log.info(f"Connected to Modal function: {app_name}/execute_modal_task")
        except Exception as e:
            self.log.error(
                f"Failed to look up Modal function {app_name}/execute_modal_task: {e}"
            )
            raise

        # Look up the state dictionary
        try:
            self._state_dict = modal.Dict.from_name(dict_name)
            self.log.info(f"Connected to Modal Dict: {dict_name}")
        except Exception as e:
            self.log.error(f"Failed to connect to Modal Dict {dict_name}: {e}")
            raise

        # Set up execution API URL.
        # Priority: env var > Airflow config > modal.forward() tunnel > error
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
        Trigger a task execution on Modal.

        Handles two formats:
        - New-style: command is a list containing an ExecuteTask workload object.
        - Old-style: command is a list of strings (CLI command).
        """
        task_key_str = self._get_key_str(key)

        if len(command) == 1 and isinstance(command[0], executor_workloads.ExecuteTask):
            # New-style: serialize the workload to JSON
            workload = command[0]
            serialized_workload = workload.model_dump_json()
            payload = {
                "task_key": task_key_str,
                "workload_json": serialized_workload,
                "env": self._get_task_env(key, executor_config),
            }
        elif all(isinstance(c, str) for c in command):
            # Old-style: pass the CLI command for direct execution
            payload = {
                "task_key": task_key_str,
                "command": command,
                "env": self._get_task_env(key, executor_config),
            }
        else:
            raise RuntimeError(
                f"ModalExecutor doesn't know how to handle command of type: {type(command)}"
            )

        self.log.info(f"Spawning Modal task for {task_key_str}")

        if self._execution_api_url is None:
            raise RuntimeError(
                "Execution API URL not configured. Ensure start() was called successfully."
            )

        try:
            self._modal_function.spawn(payload)
            self.active_tasks[task_key_str] = key
        except Exception as e:
            self.log.error(f"Failed to spawn Modal task: {e}")
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
        Check the status of running tasks.
        """
        if not self.active_tasks:
            return

        # Poll the state dictionary
        # TODO: batch this or use a more efficient lookup

        completed_keys = []

        for task_key_str, key in self.active_tasks.items():
            # Check if this key exists in the remote dict
            # We use .get() to avoid errors if key is missing
            try:
                task_state = self._state_dict.get(task_key_str)
            except Exception as e:
                self.log.warning(f"Error reading state for {task_key_str}: {e}")
                continue

            if not task_state:
                # Task not yet registered by worker, or lost
                # TODO: implement a timeout logic here
                continue

            status = task_state.get("status")

            if status == "SUCCESS":
                self._write_task_log(task_state, key)
                self.success(key)
                completed_keys.append(task_key_str)
                self.log.info(f"Task {task_key_str} succeeded")

            elif status == "FAILED":
                self._write_task_log(task_state, key)
                self.fail(key)
                completed_keys.append(task_key_str)
                error_msg = task_state.get("error", "Unknown error")
                self.log.error(f"Task {task_key_str} failed: {error_msg}")
                if task_state.get("stderr"):
                    self.log.error(f"Task {task_key_str} stderr: {task_state['stderr']}")
                if task_state.get("stdout"):
                    self.log.info(f"Task {task_key_str} stdout: {task_state['stdout']}")

        # Cleanup local state
        for k in completed_keys:
            del self.active_tasks[k]
            # Cleanup remote state
            try:
                self._state_dict.pop(k)
            except Exception as e:
                self.log.warning(f"Failed to cleanup remote state for {k}: {e}") 

    def get_task_log(self, ti: TaskInstance, try_number: int) -> tuple[list[str], list[str]]:
        """Return partial logs from state_dict for running tasks.

        Called by FileTaskHandler when ti.state == RUNNING.  Once the task
        completes, full logs are read from the local filesystem (written by
        ``_write_task_log`` during ``sync()``).
        """
        task_key_str = self._get_key_str(ti.key)
        try:
            task_state = self._state_dict.get(task_key_str)
        except Exception:
            return [], []
        if task_state and task_state.get("stdout"):
            return (
                [f"Modal task output for {task_key_str}"],
                [task_state["stdout"]],
            )
        return [f"Task {task_key_str} running on Modal"], ["Waiting for output..."]

    def _write_task_log(self, task_state: dict, key: TaskInstanceKey) -> None:
        """Write task log content from state_dict to the local base_log_folder.

        This makes ``FileTaskHandler._read_from_local()`` work for completed
        tasks even when the Airflow scheduler doesn't share a Modal Volume
        with the Modal Functions (e.g. local / production deployments).
        """
        # Prefer structured log content from the Airflow SDK supervisor,
        # fall back to raw stdout captured from the subprocess.
        content = task_state.get("log_content") or task_state.get("stdout") or ""
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
        """
        self.log.info("Shutting down ModalExecutor")
        self.heartbeat_interval = 0

        # Cleanup tunnel if it exists
        if self._tunnel_context is not None:
            try:
                # Exit the context manager
                self._tunnel_context.__exit__(None, None, None)
                self.log.info("Closed tunnel")
            except Exception as e:
                self.log.warning(f"Error closing tunnel: {e}")
            finally:
                self._tunnel_context = None
                self._tunnel = None

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
        3. modal.forward() tunnel (only works inside Modal Functions)

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

        # 3. Try modal.forward() tunnel (works inside Modal Functions only)
        self.log.info(
            "No execution API URL configured, attempting modal.forward() tunnel"
        )
        try:
            self._tunnel_context = modal.forward(8080)
            self._tunnel = self._tunnel_context.__enter__()
            tunnel_url = self._tunnel.url
            return self._ensure_execution_prefix(tunnel_url)
        except Exception as e:
            raise RuntimeError(
                f"Execution API URL not configured and tunnel creation failed: {e}. "
                "Set AIRFLOW__CORE__EXECUTION_API_SERVER_URL or run inside a Modal container."
            ) from e

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
