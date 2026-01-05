from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union
from urllib.parse import urljoin

import modal
import requests
from airflow.configuration import conf
from airflow.executors.base_executor import BaseExecutor
from airflow.executors import workloads as executor_workloads
from airflow.models.taskinstance import TaskInstanceKey

if TYPE_CHECKING:
    from airflow.executors.workloads import All as ExecutorWorkload

# Type alias for command - can be a list containing a workload or list of strings
CommandType = Union[List[executor_workloads.ExecuteTask], List[str]]

# Configuration - should match modal_app.py
ENV = os.environ.get("MODALFLOW_ENV", "main")
CONCURRENCY_LIMIT = 100


class ModalExecutor(BaseExecutor):
    """
    An Airflow Executor that runs tasks as Modal Functions.
    """

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

        # Set up networking for execution API
        try:
            if self._is_local_environment():
                # Create tunnel to localhost:8080
                # modal.forward() returns a context manager - we need to enter it and keep it alive
                try:
                    self._tunnel_context = modal.forward(8080)
                    self._tunnel = self._tunnel_context.__enter__()
                    tunnel_url = self._tunnel.url
                    self._execution_api_url = urljoin(tunnel_url, "/execution/")
                    self.log.info(
                        f"Created tunnel for local Airflow API: {self._execution_api_url}"
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to create tunnel to localhost:8080. "
                        f"Ensure Airflow is running on localhost:8080. Error: {e}"
                    ) from e
            else:
                # Production: read from Airflow config
                try:
                    self._execution_api_url = self._get_production_api_url()
                    self.log.info(
                        f"Using production API URL: {self._execution_api_url}"
                    )
                except ValueError as e:
                    # Re-raise ValueError with clearer context
                    raise ValueError(
                        f"Production execution API URL not configured. {str(e)}"
                    ) from e
        except (RuntimeError, ValueError) as e:
            # These are expected errors - re-raise with clear messages
            raise
        except Exception as e:
            # Unexpected errors
            self.log.error(f"Unexpected error setting up execution API URL: {e}")
            raise RuntimeError(f"Failed to set up execution API URL: {e}") from e

    def execute_async(
        self,
        key: TaskInstanceKey,
        command: CommandType,
        queue: Optional[str] = None,
        executor_config: Optional[Any] = None,
    ) -> None:
        """
        Trigger a task execution on Modal.

        Following the Airflow 3.x pattern (like AWS Lambda Executor), the command
        parameter is a list containing an ExecuteTask workload object. We serialize
        it to JSON and pass it to the Modal function, which executes it using the
        Airflow SDK's execute_workload module.
        """
        # Serialize the key to use as a unique ID
        task_key_str = self._get_key_str(key)

        # Handle Airflow 3.x workload pattern (command contains ExecuteTask object)
        if len(command) == 1 and isinstance(command[0], executor_workloads.ExecuteTask):
            workload = command[0]
            # Serialize the workload to JSON using pydantic's model_dump_json
            serialized_workload = workload.model_dump_json()
        else:
            raise RuntimeError(
                f"ModalExecutor doesn't know how to handle command of type: {type(command)}"
            )

        self.log.info(f"Spawning Modal task for {task_key_str}")

        # Prepare payload with serialized workload
        # The Modal function will use airflow.sdk.execution_time.execute_workload
        payload = {
            "task_key": task_key_str,
            "workload_json": serialized_workload,
            "env": self._get_task_env(key, executor_config),
        }

        # Ensure execution API URL is set before spawning
        if self._execution_api_url is None:
            raise RuntimeError(
                "Execution API URL not configured. Ensure start() was called successfully."
            )

        # Spawn the function asynchronously
        # .spawn() returns a FunctionCall object, but we rely on the Dict for status
        try:
            self._modal_function.spawn(payload)
            self.active_tasks[task_key_str] = key
        except Exception as e:
            self.log.error(f"Failed to spawn Modal task: {e}")
            self.fail(key)

    def _process_workloads(self, workloads: Sequence[ExecutorWorkload]) -> None:
        """
        Process workloads by delegating to execute_async.
        This is the Airflow 3.x API for task execution.

        Following the same pattern as the AWS Lambda Executor, we pass the workload
        object wrapped in a list as the 'command' parameter.
        """
        for workload in workloads:
            if not isinstance(workload, executor_workloads.ExecuteTask):
                raise RuntimeError(
                    f"{type(self).__name__} cannot handle workloads of type {type(workload)}"
                )

            ti = workload.ti

            # Build TaskInstanceKey from the TaskInstance fields
            key = TaskInstanceKey(
                dag_id=ti.dag_id,
                task_id=ti.task_id,
                run_id=ti.run_id,
                try_number=ti.try_number,
                map_index=ti.map_index,
            )

            queue = ti.queue
            executor_config = ti.executor_config or {}

            # Remove from queued tasks if tracked by base class
            if key in self.queued_tasks:
                del self.queued_tasks[key]

            # Pass the workload wrapped in a list (following Lambda executor pattern)
            command = [workload]
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
                self.success(key)
                completed_keys.append(task_key_str)
                self.log.info(f"Task {task_key_str} succeeded")

            elif status == "FAILED":
                self.fail(key)
                completed_keys.append(task_key_str)
                error_msg = task_state.get("error", "Unknown error")
                self.log.error(f"Task {task_key_str} failed: {error_msg}")

        # Cleanup local state
        for k in completed_keys:
            del self.active_tasks[k]
            # Cleanup remote state
            try:
                self._state_dict.pop(k)
            except Exception as e:
                self.log.warning(f"Failed to cleanup remote state for {k}: {e}") 

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
        Format: dag_id:task_id:run_id:try_number
        """
        # Note: TaskInstanceKey is a named tuple, but the fields vary slightly by Airflow version
        # We construct a stable string key
        return f"{key.dag_id}:{key.task_id}:{key.run_id}:{key.try_number}"

    def _is_local_environment(self) -> bool:
        """
        Check if we're running in a local environment by testing if localhost:8080 is accessible.

        Returns:
            True if localhost:8080 is accessible, False otherwise
        """
        try:
            # Try to connect to localhost:8080
            response = requests.get("http://localhost:8080/health", timeout=2)
            # If we get any response (even 404), localhost is accessible
            return True
        except (
            requests.exceptions.RequestException,
            socket.timeout,
            ConnectionRefusedError,
        ):
            # Connection failed - not a local environment
            return False

    def _get_production_api_url(self) -> str:
        """
        Get the execution API URL from Airflow configuration.

        Reads from:
        1. Environment variable AIRFLOW__CORE__EXECUTION_API_SERVER_URL (takes precedence)
        2. Airflow config: core.execution_api_server_url
        3. Constructs default from api.base_url if available

        Returns:
            Execution API URL string

        Raises:
            ValueError: If URL cannot be determined
        """
        # Check environment variable first (takes precedence)
        env_url = os.environ.get("AIRFLOW__CORE__EXECUTION_API_SERVER_URL")
        if env_url:
            self._validate_api_url(env_url)
            return env_url

        # Check Airflow config
        try:
            config_url = conf.get("core", "execution_api_server_url", fallback=None)
            if config_url:
                self._validate_api_url(config_url)
                return config_url
        except Exception as e:
            self.log.warning(f"Error reading execution_api_server_url from config: {e}")

        # Try to construct from base_url
        try:
            base_url = conf.get("api", "base_url", fallback="/")
            if base_url.startswith("/"):
                # Relative URL - construct default
                default_url = f"http://localhost:8080{base_url.rstrip('/')}/execution/"
                self.log.warning(
                    f"execution_api_server_url not configured. Using default: {default_url}. "
                    "This may not work in production. Set AIRFLOW__CORE__EXECUTION_API_SERVER_URL "
                    "or configure core.execution_api_server_url in airflow.cfg"
                )
                return default_url
            else:
                # Absolute URL - append /execution/
                execution_url = urljoin(base_url.rstrip("/"), "/execution/")
                self._validate_api_url(execution_url)
                return execution_url
        except Exception as e:
            self.log.error(f"Error constructing execution API URL from base_url: {e}")

        # If we get here, we couldn't determine the URL
        raise ValueError(
            "execution_api_server_url not configured. Set AIRFLOW__CORE__EXECUTION_API_SERVER_URL "
            "or ensure Airflow config has core.execution_api_server_url"
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
            "AIRFLOW__CORE__EXECUTOR": "modalflow.executor.modal_executor.ModalExecutor",
            "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": self._execution_api_url,
        }
        return env
