"""
Modal app for running system tests with airflow standalone in a Sandbox.

This creates a Sandbox that runs airflow standalone to test
Airflow with the ModalExecutor configured.
"""
import sys
from pathlib import Path

import modal

dist_path = Path(__file__).parent.parent.parent / "dist"
test_dir = Path(__file__).parent
dags_dir = test_dir / "dags"

WHL_NAME = "modalflow-0.1.0-py3-none-any.whl"
whl_path = dist_path / WHL_NAME

airflow_test_image = (
    modal.Image.from_registry("apache/airflow:3.0.6-python3.10")
    # Clear the Airflow image's ENTRYPOINT so Modal's sandbox init process
    # doesn't exit immediately (which would terminate the sandbox).
    .dockerfile_commands(["ENTRYPOINT []", "CMD []"])
    # Upgrade Airflow to 3.1.5 to match local dev (3.0.6 uses old-style
    # queue_command tuples; 3.1.x uses queue_workload with ExecuteTask).
    .run_commands(
        'su -s /bin/bash airflow -c "pip install apache-airflow==3.1.5"'
    )
    .add_local_file(str(whl_path), remote_path=f"/tmp/{WHL_NAME}", copy=True)
    .run_commands(
        f'su -s /bin/bash airflow -c "pip install /tmp/{WHL_NAME}"'
    )
    .add_local_dir(str(dags_dir), remote_path="/opt/airflow/dags", copy=True)
    .run_commands("chown -R airflow: /opt/airflow")
    .add_local_dir(str(test_dir), remote_path="/files/system")
)

app = modal.App("modalflow-test-runner", image=airflow_test_image)


def create_test_sandbox() -> modal.Sandbox:
    """
    Create a Sandbox for running airflow standalone tests.

    Returns:
        Configured Sandbox instance
    """
    modal.enable_output()
    deployed_app = modal.App.lookup("modalflow-test-runner", create_if_missing=True)

    sb = modal.Sandbox.create(
        # Initial command keeps the sandbox's init process alive.
        "sleep", "infinity",
        app=deployed_app,
        image=airflow_test_image,
        secrets=[modal.Secret.from_name("modal")],
        encrypted_ports=[8080],
        name="modalflow-system-runner",
        timeout=3600,
    )

    return sb


def start_airflow(sandbox: modal.Sandbox) -> None:
    """
    Start airflow standalone and stream output to stdout.

    Sources environment variables from the env file, then runs
    airflow standalone which starts the webserver, scheduler,
    and triggerer in a single process.
    """
    print("Starting airflow standalone...")
    proc = sandbox.exec(
        "bash", "-c",
        "set -a && source /files/system/environment_variables.env && set +a && "
        "export PYTHONUNBUFFERED=1 && "
        "exec runuser -u airflow -- airflow standalone 2>&1",
    )
    for line in proc.stdout:
        print(line, end="")
    proc.wait()

# ---------------------------------------------------------------------------
# E2E test classes (run via pytest)
# ---------------------------------------------------------------------------

from helpers import unpause_dag, trigger_dag, wait_for_dag_run, get_task_states


class TestBashOperatorE2E:
    def test_bash_task_succeeds(self, airflow_ready, sandbox):
        dag_id = "test_bash_success"
        unpause_dag(sandbox, dag_id)
        run_id = trigger_dag(sandbox, dag_id)
        state = wait_for_dag_run(sandbox, dag_id, run_id, timeout=120)
        assert state == "success", f"Expected success, got {state}"

        tasks = get_task_states(sandbox, dag_id, run_id)
        assert any(
            t["task_id"] == "echo_hello" and t["state"] == "success"
            for t in tasks
        ), f"echo_hello task not successful: {tasks}"


class TestPythonOperatorE2E:
    def test_python_task_succeeds(self, airflow_ready, sandbox):
        dag_id = "test_python_success"
        unpause_dag(sandbox, dag_id)
        run_id = trigger_dag(sandbox, dag_id)
        state = wait_for_dag_run(sandbox, dag_id, run_id)
        assert state == "success", f"Expected success, got {state}"

        tasks = get_task_states(sandbox, dag_id, run_id)
        assert any(
            t["task_id"] == "return_value" and t["state"] == "success"
            for t in tasks
        ), f"return_value task not successful: {tasks}"


class TestFailurePropagation:
    def test_failing_task_reported(self, airflow_ready, sandbox):
        dag_id = "test_bash_failure"
        unpause_dag(sandbox, dag_id)
        run_id = trigger_dag(sandbox, dag_id)
        state = wait_for_dag_run(sandbox, dag_id, run_id)
        assert state == "failed", f"Expected failed, got {state}"

        tasks = get_task_states(sandbox, dag_id, run_id)
        assert any(
            t["task_id"] == "fail_task" and t["state"] == "failed"
            for t in tasks
        ), f"fail_task task not failed: {tasks}"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        run_test()
    elif len(sys.argv) > 1 and sys.argv[1] == "start":
        sb = modal.Sandbox.from_name("modalflow-test-runner", "modalflow-system-runner")
        try:
          start_airflow(sb)
        finally:
          print("Terminating sandbox...")
          sb.terminate()
    else:
        print("Usage: python test_app.py [run|start]")
