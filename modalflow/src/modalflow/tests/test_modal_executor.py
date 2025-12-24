import sys
from unittest import mock
import unittest

# --- Mock Airflow Dependencies ---
# This allows running tests without installing heavy airflow dependencies
mock_airflow = mock.MagicMock()
sys.modules["airflow"] = mock_airflow
sys.modules["airflow.executors"] = mock_airflow.executors
sys.modules["airflow.executors.base_executor"] = mock_airflow.executors.base_executor
sys.modules["airflow.models"] = mock_airflow.models
sys.modules["airflow.models.taskinstance"] = mock_airflow.models.taskinstance
sys.modules["airflow.utils"] = mock_airflow.utils
sys.modules["airflow.utils.state"] = mock_airflow.utils.state

# Mock BaseExecutor class
class MockBaseExecutor:
    def __init__(self, parallelism=16):
        self.parallelism = parallelism
    def fail(self, key): pass
    def success(self, key): pass
    def validate_command(self, cmd): pass

mock_airflow.executors.base_executor.BaseExecutor = MockBaseExecutor

# Mock TaskInstanceKey class
class MockTaskInstanceKey:
    def __init__(self, dag_id, task_id, run_id, try_number):
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id = run_id
        self.try_number = try_number

mock_airflow.models.taskinstance.TaskInstanceKey = MockTaskInstanceKey

# --- Import Code Under Test ---
# We must do this AFTER mocking
from modalflow.executor.modal_executor import ModalExecutor

class TestModalExecutor(unittest.TestCase):
    def setUp(self):
        self.executor = ModalExecutor()
        self.executor.active_tasks = {}

    @mock.patch("modalflow.executor.modal_executor.execute_modal_task")
    def test_execute_async(self, mock_task):
        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        command = ["airflow", "tasks", "run", "dag", "task", "run_id", "--local"]
        
        self.executor.execute_async(key, command)
        
        # Verify spawn called
        mock_task.spawn.assert_called_once()
        call_args = mock_task.spawn.call_args[0][0]
        self.assertEqual(call_args["task_key"], "dag:task:run_id:1")
        self.assertEqual(call_args["command"], command)
        
        # Verify added to active tasks
        self.assertIn("dag:task:run_id:1", self.executor.active_tasks)

    @mock.patch("modalflow.executor.modal_executor.state_dict")
    def test_sync_success(self, mock_state_dict):
        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        task_key_str = "dag:task:run_id:1"
        self.executor.active_tasks[task_key_str] = key
        
        # Mock successful state
        mock_state_dict.get.return_value = {"status": "SUCCESS"}
        
        # Mock airflow methods
        self.executor.success = mock.Mock()
        self.executor.fail = mock.Mock()
        
        self.executor.sync()
        
        self.executor.success.assert_called_once_with(key)
        self.executor.fail.assert_not_called()
        self.assertNotIn(task_key_str, self.executor.active_tasks)

    @mock.patch("modalflow.executor.modal_executor.state_dict")
    def test_sync_failed(self, mock_state_dict):
        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        task_key_str = "dag:task:run_id:1"
        self.executor.active_tasks[task_key_str] = key
        
        # Mock failed state
        mock_state_dict.get.return_value = {"status": "FAILED", "error": "Something exploded"}
        
        # Mock airflow methods
        self.executor.success = mock.Mock()
        self.executor.fail = mock.Mock()
        
        self.executor.sync()
        
        self.executor.fail.assert_called_once_with(key)
        self.executor.success.assert_not_called()
        self.assertNotIn(task_key_str, self.executor.active_tasks)

    @mock.patch("modalflow.executor.modal_executor.state_dict")
    def test_sync_pending(self, mock_state_dict):
        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        task_key_str = "dag:task:run_id:1"
        self.executor.active_tasks[task_key_str] = key
        
        # Mock pending state (or missing state)
        mock_state_dict.get.return_value = {"status": "RUNNING"}
        
        self.executor.success = mock.Mock()
        self.executor.fail = mock.Mock()
        
        self.executor.sync()
        
        self.executor.success.assert_not_called()
        self.executor.fail.assert_not_called()
        self.assertIn(task_key_str, self.executor.active_tasks)

if __name__ == "__main__":
    unittest.main()
