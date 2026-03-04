import os
import subprocess
from pathlib import Path

import modal

# Allow overriding the environment name via env var
ENV = os.environ.get("MODALFLOW_ENV", "main")

# Maximum number of concurrent Modal function calls
# This should match the executor's parallelism setting
CONCURRENCY_LIMIT = 100

# DAG source configuration (set by CLI at deploy time)
DAGS_DIR = os.environ.get("MODALFLOW_DAGS_DIR", None)            # local mode
DAGS_VOLUME_NAME = os.environ.get("MODALFLOW_DAGS_VOLUME", None)  # volume mode
DAGS_BUCKET = os.environ.get("MODALFLOW_DAGS_BUCKET", None)       # cloud-bucket mode
DAGS_BUCKET_SECRET = os.environ.get("MODALFLOW_DAGS_BUCKET_SECRET", None)

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

# Include DAG files in the image only for local mode.
# Volume and cloud-bucket modes mount DAGs at runtime instead.
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

# Build volumes dict dynamically based on DAG source.
# Both modal.Volume and modal.CloudBucketMount are accepted in the volumes dict.
function_volumes = {"/opt/airflow/logs": log_volume}

if DAGS_VOLUME_NAME:
    dags_volume = modal.Volume.from_name(DAGS_VOLUME_NAME, create_if_missing=True)
    function_volumes["/opt/airflow/dags"] = dags_volume

if DAGS_BUCKET:
    # Parse S3 URI: s3://bucket-name/optional/prefix
    bucket_path = DAGS_BUCKET.removeprefix("s3://")
    parts = bucket_path.split("/", 1)
    bucket_name = parts[0]
    key_prefix = parts[1] if len(parts) > 1 else ""

    mount_kwargs = {"bucket_name": bucket_name}
    if key_prefix:
        mount_kwargs["key_prefix"] = key_prefix
    if DAGS_BUCKET_SECRET:
        mount_kwargs["secret"] = modal.Secret.from_name(DAGS_BUCKET_SECRET)

    function_volumes["/opt/airflow/dags"] = modal.CloudBucketMount(**mount_kwargs)

# Pass DAG source env vars to the function container so that conditional
# volume/mount definitions evaluate identically on the remote side.
function_env = {}
if DAGS_VOLUME_NAME:
    function_env["MODALFLOW_DAGS_VOLUME"] = DAGS_VOLUME_NAME
if DAGS_BUCKET:
    function_env["MODALFLOW_DAGS_BUCKET"] = DAGS_BUCKET
if DAGS_BUCKET_SECRET:
    function_env["MODALFLOW_DAGS_BUCKET_SECRET"] = DAGS_BUCKET_SECRET


@app.function(
    volumes=function_volumes,
    env=function_env or None,
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
