import os
import subprocess

import modal

# Allow overriding the environment name via env var
ENV = os.environ.get("MODALFLOW_ENV", "main")

# Maximum number of concurrent Modal function calls
# This should match the executor's parallelism setting
CONCURRENCY_LIMIT = 100

# Define the base image
# We use the official Airflow image to ensure compatibility
airflow_image = modal.Image.from_registry(
    "apache/airflow:3.0.6-python3.10"
).pip_install(
    "modal",
    "rich",
    "click",
    "pyyaml",
    "psycopg2-binary",
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
    Executes an Airflow task using the Airflow SDK's execute_workload module.

    This follows the same pattern as the AWS Lambda Executor for Airflow 3.x.

    Payload structure:
    {
        "task_key": "dag_id:task_id:run_id:try_number",
        "workload_json": "<serialized ExecuteTask workload JSON>",
        "env": {"AIRFLOW__CORE__...", ...}
    }
    """
    import json
    import os

    task_key = payload.get("task_key")
    workload_json = payload.get("workload_json")
    env_vars = payload.get("env", {})

    print(f"Starting execution for {task_key}")

    # Build the Airflow SDK execute_workload command
    # This is how Airflow 3.x executes tasks remotely (same as Lambda Executor)
    command = [
        "python",
        "-m",
        "airflow.sdk.execution_time.execute_workload",
        "--json-string",
        workload_json,
    ]

    print(
        f"Command: python -m airflow.sdk.execution_time.execute_workload --json-string <workload>"
    )

    # Set environment variables
    # We must merge with existing env to keep system paths
    run_env = os.environ.copy()
    for k, v in env_vars.items():
        if k not in run_env:
            run_env[k] = v

    # Try to extract task info from workload for logging
    log_file_path = None
    try:
        workload_data = json.loads(workload_json)
        ti = workload_data.get("ti", {})
        dag_id = ti.get("dag_id", "unknown")
        task_id = ti.get("task_id", "unknown")
        run_id = ti.get("run_id", "unknown")
        try_number = ti.get("try_number", 1)

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
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)

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
