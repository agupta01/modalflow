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

WHL_NAME = "modalflow-0.1.0-py3-none-any.whl"
whl_path = dist_path / WHL_NAME

airflow_test_image = (
    modal.Image.from_registry("apache/airflow:3.0.6-python3.10")
    .add_local_file(str(whl_path), remote_path=f"/tmp/{WHL_NAME}", copy=True)
    .run_commands(
        f'su -s /bin/bash airflow -c "pip install /tmp/{WHL_NAME}"'
    )
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
        app=deployed_app,
        image=airflow_test_image,
        secrets=[modal.Secret.from_name("modal")],
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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        sb = modal.Sandbox.from_name("modalflow-test-runner", "modalflow-system-runner")
        start_airflow(sb)
    else:
        print("Usage: python test_app.py [start]")
