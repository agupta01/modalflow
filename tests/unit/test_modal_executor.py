import sys
from unittest import mock
import unittest

# --- Mock Airflow Dependencies ---
# This allows running tests without installing heavy airflow dependencies
mock_airflow = mock.MagicMock()
sys.modules["airflow"] = mock_airflow
sys.modules["airflow.configuration"] = mock_airflow.configuration
sys.modules["airflow.executors"] = mock_airflow.executors
sys.modules["airflow.executors.base_executor"] = mock_airflow.executors.base_executor
sys.modules["airflow.executors.workloads"] = mock_airflow.executors.workloads
sys.modules["airflow.models"] = mock_airflow.models
sys.modules["airflow.models.taskinstance"] = mock_airflow.models.taskinstance
sys.modules["airflow.utils"] = mock_airflow.utils
sys.modules["airflow.utils.state"] = mock_airflow.utils.state


# Mock BaseExecutor class
class MockBaseExecutor:
    def __init__(self, parallelism=16):
        self.parallelism = parallelism

    def fail(self, key):
        pass

    def success(self, key):
        pass

    def validate_command(self, cmd):
        pass


mock_airflow.executors.base_executor.BaseExecutor = MockBaseExecutor


# Mock TaskInstanceKey class
class MockTaskInstanceKey:
    def __init__(self, dag_id, task_id, run_id, try_number, map_index=-1):
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id = run_id
        self.try_number = try_number
        self.map_index = map_index


mock_airflow.models.taskinstance.TaskInstanceKey = MockTaskInstanceKey

# Use a sentinel class for ExecuteTask so isinstance() checks in the
# executor only match real workload objects (our CLI-list path uses
# plain strings).
class _FakeExecuteTask:
    pass


mock_airflow.executors.workloads.ExecuteTask = _FakeExecuteTask

# --- Import Code Under Test ---
# We must do this AFTER mocking
from modalflow.executor.modal_executor import ModalExecutor


def _make_executor():
    executor = ModalExecutor()
    executor.active_tasks = {}
    executor.log = mock.Mock(name="log")
    # start() is not called in unit tests; set the bits execute_async needs.
    executor._execution_api_url = "http://localhost:8080/execution/"
    executor._app = mock.Mock(name="app")
    executor._image = mock.Mock(name="image")
    executor._volumes = {"/opt/airflow/logs": mock.Mock(), "/opt/airflow/dags": mock.Mock()}
    return executor


class TestExecuteAsync(unittest.TestCase):
    @mock.patch("modalflow.modal_app.modal.Sandbox.create")
    @mock.patch("modalflow.modal_app.modal.Secret.from_name")
    def test_execute_async(self, mock_secret, mock_create):
        executor = _make_executor()
        mock_sb = mock.Mock(name="sandbox")
        mock_proc = mock.Mock(name="process")
        mock_sb.exec.return_value = mock_proc
        mock_create.return_value = mock_sb

        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        command = ["airflow", "tasks", "run", "dag", "task", "run_id", "--local"]

        executor.execute_async(key, command)

        # A sandbox was created and the command was exec'd inside it.
        mock_create.assert_called_once()
        mock_sb.exec.assert_called_once()
        exec_args = list(mock_sb.exec.call_args[0])
        self.assertEqual(exec_args, command)

        # Task tracked under the right key with a handle dict.
        self.assertIn("dag:task:run_id:1", executor.active_tasks)
        handle = executor.active_tasks["dag:task:run_id:1"]
        self.assertIs(handle["sandbox"], mock_sb)
        self.assertIs(handle["process"], mock_proc)
        self.assertIs(handle["key"], key)

    @mock.patch("modalflow.modal_app.modal.Sandbox.create")
    @mock.patch("modalflow.modal_app.modal.Secret.from_name")
    def test_execute_async_workload(self, mock_secret, mock_create):
        executor = _make_executor()
        mock_sb = mock.Mock(name="sandbox")
        mock_proc = mock.Mock(name="process")
        mock_sb.exec.return_value = mock_proc
        mock_create.return_value = mock_sb

        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        workload = _FakeExecuteTask()
        workload.model_dump_json = mock.Mock(
            return_value='{"log_path": "dag_id=dag/run_id=run_id/task_id=task/attempt=1.log"}'
        )

        executor.execute_async(key, [workload])

        mock_sb.exec.assert_called_once()
        exec_args = list(mock_sb.exec.call_args[0])
        self.assertEqual(
            exec_args,
            [
                "python",
                "-m",
                "airflow.sdk.execution_time.execute_workload",
                "--json-string",
                workload.model_dump_json.return_value,
            ],
        )
        handle = executor.active_tasks["dag:task:run_id:1"]
        self.assertEqual(
            handle["log_path"],
            "dag_id=dag/run_id=run_id/task_id=task/attempt=1.log",
        )


class TestSync(unittest.TestCase):
    def _track_task(self, executor, return_code, log_path=None):
        key = MockTaskInstanceKey("dag", "task", "run_id", 1)
        task_key_str = "dag:task:run_id:1"
        proc = mock.Mock(name="process")
        proc.poll.return_value = return_code
        sb = mock.Mock(name="sandbox")
        sb.poll.return_value = return_code
        executor.active_tasks[task_key_str] = {
            "key": key,
            "sandbox": sb,
            "process": proc,
            "log_path": log_path,
        }
        return key, task_key_str, sb, proc

    def test_sync_success(self):
        executor = _make_executor()
        executor.success = mock.Mock()
        executor.fail = mock.Mock()
        key, task_key_str, sb, proc = self._track_task(executor, return_code=0)

        executor.sync()

        executor.success.assert_called_once_with(key)
        executor.fail.assert_not_called()
        sb.terminate.assert_called_once()
        self.assertNotIn(task_key_str, executor.active_tasks)

    def test_sync_failed(self):
        executor = _make_executor()
        executor.success = mock.Mock()
        executor.fail = mock.Mock()
        key, task_key_str, sb, proc = self._track_task(executor, return_code=1)

        executor.sync()

        executor.fail.assert_called_once_with(key)
        executor.success.assert_not_called()
        sb.terminate.assert_called_once()
        self.assertNotIn(task_key_str, executor.active_tasks)

    def test_sync_pending(self):
        executor = _make_executor()
        executor.success = mock.Mock()
        executor.fail = mock.Mock()
        key, task_key_str, sb, proc = self._track_task(executor, return_code=None)

        executor.sync()

        executor.success.assert_not_called()
        executor.fail.assert_not_called()
        sb.terminate.assert_not_called()
        self.assertIn(task_key_str, executor.active_tasks)

    def test_sync_success_writes_log(self):
        executor = _make_executor()
        executor.success = mock.Mock()
        executor.fail = mock.Mock()
        executor._write_task_log = mock.Mock()
        key, task_key_str, sb, proc = self._track_task(
            executor, return_code=0, log_path="dag_id=dag/run_id=run_id/task_id=task/attempt=1.log"
        )
        # Sandbox.exec("cat", ...) returns a process whose stdout reads log content.
        cat_proc = mock.Mock()
        cat_proc.stdout.read.return_value = "structured log content"
        cat_proc.returncode = 0
        sb.exec.return_value = cat_proc

        executor.sync()

        executor._write_task_log.assert_called_once_with("structured log content", key)


if __name__ == "__main__":
    unittest.main()
