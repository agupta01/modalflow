import os
import subprocess
from pathlib import Path

import modal

# Allow overriding the environment name via env var
ENV = os.environ.get("MODALFLOW_ENV", "main")

# Maximum number of concurrent Modal function calls
# This should match the executor's parallelism setting
CONCURRENCY_LIMIT = 100

# Optional: path to DAGs directory to include in the image.
# Set MODALFLOW_DAGS_DIR to include DAGs so the task worker can load them.
DAGS_DIR = os.environ.get("MODALFLOW_DAGS_DIR", None)

# Define the base image
# We use the official Airflow image to ensure compatibility.
# Upgrade from 3.0.6 to 3.1.5 (3.0.6 lacks queue_workload support).
airflow_image = (
    modal.Image.from_registry("apache/airflow:3.0.6-python3.10")
    .run_commands(
        'su -s /bin/bash airflow -c "pip install apache-airflow==3.1.5"'
    )
    .pip_install(
        "modal",
        "rich",
        "click",
        "pyyaml",
        "psycopg2-binary",
    )
)

# Include DAG files in the image if a DAGs directory is specified.
# The task worker (execute_workload) needs DAG files to load task definitions.
if DAGS_DIR:
    airflow_image = airflow_image.add_local_dir(
        DAGS_DIR, remote_path="/opt/airflow/dags", copy=True
    )

# Create the Modal App
app = modal.App(f"modalflow-{ENV}", image=airflow_image)

# Define the volume for logs
# We use a dedicated volume for logs so they persist and can be read back
log_volume = modal.Volume.from_name(f"airflow-logs-{ENV}", create_if_missing=True)

# Define the dict for coordination (hot cache)
# Maps task_key -> {status, return_code, last_updated}
state_dict = modal.Dict.from_name(f"airflow-state-{ENV}", create_if_missing=True)


@app.function(
    volumes={"/opt/airflow/logs": log_volume},
    timeout=3600,  # Default 1 hour timeout
    max_containers=CONCURRENCY_LIMIT,
)
def execute_modal_task(payload: dict):
    """
    Executes an Airflow task either via the SDK workload API or a CLI command.

    Payload structure (new-style):
    {
        "task_key": "dag_id:task_id:run_id:try_number",
        "workload_json": "<serialized ExecuteTask workload JSON>",
        "env": {"AIRFLOW__CORE__...", ...}
    }

    Payload structure (old-style):
    {
        "task_key": "dag_id:task_id:run_id:try_number",
        "command": ["airflow", "tasks", "run", dag_id, task_id, run_id, ...],
        "env": {"AIRFLOW__CORE__...", ...}
    }
    """
    import json
    import os

    task_key = payload.get("task_key")
    workload_json = payload.get("workload_json")
    cli_command = payload.get("command")
    env_vars = payload.get("env", {})

    print(f"Starting execution for {task_key}")

    if workload_json:
        # New-style: use the Airflow SDK execute_workload module
        command = [
            "python",
            "-m",
            "airflow.sdk.execution_time.execute_workload",
            "--json-string",
            workload_json,
        ]
        print(
            "Using SDK workload path: python -m airflow.sdk.execution_time.execute_workload --json-string <workload>"
        )
    elif cli_command:
        # Old-style: run the CLI command directly
        command = cli_command
        print(f"Using CLI path: {' '.join(command)}")
    else:
        raise ValueError(
            f"Payload must contain 'workload_json' or 'command', got keys: {list(payload.keys())}"
        )

    # Set environment variables
    # Merge with existing env, but executor-provided vars take precedence
    run_env = os.environ.copy()
    run_env.update(env_vars)

    # Try to extract task info for logging
    log_file_path = None
    try:
        if workload_json:
            workload_data = json.loads(workload_json)
            ti = workload_data.get("ti", {})
            dag_id = ti.get("dag_id", "unknown")
            task_id = ti.get("task_id", "unknown")
            run_id = ti.get("run_id", "unknown")
            try_number = ti.get("try_number", 1)
        elif cli_command and len(cli_command) >= 6:
            # Parse from CLI args: airflow tasks run dag_id task_id run_id ...
            dag_id = cli_command[3]
            task_id = cli_command[4]
            run_id = cli_command[5]
            try_number = 1
        else:
            dag_id = task_id = run_id = "unknown"
            try_number = 1

        # Construct path: /opt/airflow/logs/dag_id/task_id/run_id/try_number.log
        log_dir = os.path.join(
            "/opt/airflow/logs",
            f"dag_id={dag_id}",
            f"run_id={run_id}",
            f"task_id={task_id}",
        )
        os.makedirs(log_dir, exist_ok=True)
        log_file_path = os.path.join(log_dir, f"attempt={try_number}.log")
        print(f"Writing logs to {log_file_path}")
    except Exception as e:
        print(f"Warning: Failed to setup log directory structure: {e}")

    # Update state to RUNNING right before execution
    state_dict[task_key] = {
        "status": "RUNNING",
        "return_code": None,
        "ts": 0,  # Timestamp placeholder
    }

    try:
        # Run the command
        # We use subprocess.run to block until completion
        result = subprocess.run(
            command,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )

        # Log output to Modal's centralized logging
        print(f"Return code: {result.returncode}")
        print(f"STDOUT (first 500): {result.stdout[:500]}")
        print(f"STDERR (first 500): {result.stderr[:500]}")

        # Write output to the log file on the volume
        if log_file_path:
            try:
                with open(log_file_path, "w") as f:
                    f.write(f"*** STDOUT ***\n{result.stdout}\n")
                    f.write(f"*** STDERR ***\n{result.stderr}\n")
            except Exception as e:
                print(f"Failed to write log file: {e}")

        status = "SUCCESS" if result.returncode == 0 else "FAILED"

        state_dict[task_key] = {
            "status": status,
            "return_code": result.returncode,
            "stdout": result.stdout[-2000:],  # Store last 2KB for quick debug
            "stderr": result.stderr[-2000:],
        }

    except Exception as e:
        print(f"Execution failed: {e}")
        state_dict[task_key] = {
            "status": "FAILED",
            "return_code": -1,
            "error": str(e),
        }
        raise
