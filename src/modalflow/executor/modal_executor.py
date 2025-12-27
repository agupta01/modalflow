from typing import Any, Dict, List, Optional

from airflow.executors.base_executor import BaseExecutor
from airflow.models.taskinstance import TaskInstanceKey

from modalflow.modal_app import execute_modal_task, state_dict

class ModalExecutor(BaseExecutor):
    """
    An Airflow Executor that runs tasks as Modal Functions.
    """
    
    def __init__(self, parallelism: int = 10):
        super().__init__(parallelism=parallelism)
        self.active_tasks: Dict[str, TaskInstanceKey] = {}

    def start(self):
        """
        Initialize the executor.
        """
        self.log.info("Starting ModalExecutor")
        # Verify connection to the state dictionary
        try:
            # Perform a lightweight operation to ensure connectivity/existence
            # len() on a Modal Dict triggers a remote call
            _ = len(state_dict)
            self.log.info(f"Connected to Modal Dict: {state_dict.name}")
        except Exception as e:
            self.log.error(f"Failed to connect to Modal Dict: {e}")
            raise

    def execute_async(
        self,
        key: TaskInstanceKey,
        command: List[str],
        queue: Optional[str] = None,
        executor_config: Optional[Any] = None,
    ) -> None:
        """
        Trigger a task execution on Modal.
        """
        self.validate_command(command)
        
        # Serialize the key to use as a unique ID
        task_key_str = self._get_key_str(key)
        
        self.log.info(f"Spawning Modal task for {task_key_str}")
        
        # Prepare payload
        payload = {
            "command": command,
            "task_key": task_key_str,
            "env": self._get_task_env(key, executor_config)
        }
        
        # Spawn the function asynchronously
        # .spawn() returns a FunctionCall object, but we rely on the Dict for status
        try:
            execute_modal_task.spawn(payload)
            self.active_tasks[task_key_str] = key
        except Exception as e:
            self.log.error(f"Failed to spawn Modal task: {e}")
            self.fail(key)

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
                task_state = state_dict.get(task_key_str)
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
                state_dict.pop(k)
            except Exception as e:
                self.log.warning(f"Failed to cleanup remote state for {k}: {e}") 

    def end(self) -> None:
        """
        Terminate the executor.
        """
        self.log.info("Shutting down ModalExecutor")
        self.heartbeat_interval = 0

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

    def _get_task_env(self, key: TaskInstanceKey, executor_config: Any) -> Dict[str, str]:
        """
        Gather environment variables to pass to the worker.
        """
        # BaseExecutor doesn't inherently give us the full task env.
        # But we can pass specific vars if needed.
        # For now, we return a basic set.
        return {
            "AIRFLOW__CORE__EXECUTOR": "modalflow.executor.modal_executor.ModalExecutor",
        }
