import os
import subprocess
import sys
import shlex
import modal

# Allow overriding the environment name via env var
ENV = os.environ.get("MODALFLOW_ENV", "dev")

# Define the base image
# We use the official Airflow image to ensure compatibility
airflow_image = (
    modal.Image.from_registry("apache/airflow:2.10.0-python3.10")
    .pip_install(
        "modal",
        "rich",
        "click",
        "pyyaml",
        "psycopg2-binary",
    )
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
)
def execute_modal_task(payload: dict):
    """
    Executes an Airflow task command.
    
    Payload structure:
    {
        "command": ["airflow", "tasks", "run", ...],
        "task_key": "serialized_key_string",
        "env": {"AIRFLOW__CORE__...", ...}
    }
    """
    import os
    
    command = payload.get("command")
    task_key = payload.get("task_key")
    env_vars = payload.get("env", {})
    
    print(f"Starting execution for {task_key}")
    print(f"Command: {command}")
    
    # Set environment variables
    # We must merge with existing env to keep system paths
    run_env = os.environ.copy()
    for k, v in env_vars.items():
        if k not in run_env:
            run_env[k] = v
    
    # Create the log directory structure if needed
    # Airflow logs are usually: dag_id/task_id/run_id/attempt.log
    # We parse the task_key (dag_id:task_id:run_id:try_number) to recreate this structure
    try:
        # task_key format: dag_id:task_id:run_id:try_number
        parts = task_key.split(":")
        if len(parts) >= 4:
            dag_id, task_id, run_id, try_number = parts[0], parts[1], parts[2], parts[3]
            
            # Construct path: /opt/airflow/logs/dag_id/task_id/run_id/try_number.log
            # Note: Airflow default structure might vary, but this is a reasonable standard
            log_dir = os.path.join("/opt/airflow/logs", f"dag_id={dag_id}", f"run_id={run_id}", f"task_id={task_id}")
            os.makedirs(log_dir, exist_ok=True)
            log_file_path = os.path.join(log_dir, f"attempt={try_number}.log")
            
            print(f"Writing logs to {log_file_path}")
    except Exception as e:
        print(f"Warning: Failed to setup log directory structure: {e}")
        log_file_path = None
        
    # Update state to RUNNING right before execution
    state_dict[task_key] = {
        "status": "RUNNING",
        "return_code": None,
        "ts": 0  # Timestamp placeholder
    }

    try:
        # Run the command
        # We use subprocess.run to block until completion
        result = subprocess.run(
            command,
            env=run_env,
            capture_output=True,
            text=True,
            check=False 
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
            "stdout": result.stdout[-2000:], # Store last 2KB for quick debug
            "stderr": result.stderr[-2000:],
        }
        
    except Exception as e:
        print(f"Execution failed: {e}")
        state_dict[task_key] = {
            "status": "FAILED",
            "return_code": -1,
            "error": str(e)
        }
        raise e
