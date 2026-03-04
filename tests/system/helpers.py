"""Helper functions for system test orchestration."""

import json
import time

import modal

AIRFLOW_LOG = "/tmp/airflow.log"


def start_airflow_and_wait_ready(sandbox: modal.Sandbox, timeout: int = 180) -> None:
    """Start airflow standalone in the sandbox and wait until it's ready.

    Launches ``airflow standalone`` as a foreground exec process that writes
    to a log file (keeping the sandbox alive).  Then polls the log file with
    separate ``sandbox.exec()`` calls until the "Airflow is ready" message
    appears.

    Raises:
        TimeoutError: If Airflow doesn't become ready within *timeout* seconds.
    """
    import threading

    # Get tunnel URL for the execution API.
    # The Sandbox must be created with encrypted_ports=[8080].
    tunnels = sandbox.tunnels()
    tunnel_url = tunnels[8080].url
    execution_api_url = f"{tunnel_url}/execution/"
    print(f"Execution API tunnel URL: {execution_api_url}")

    # Start airflow standalone as a foreground exec.  We tee output to a log
    # file so we can grep it for the readiness signal, while still keeping
    # the exec's stdout pipe alive (Modal uses active pipe I/O to determine
    # whether the sandbox is idle).
    _airflow_proc = sandbox.exec(
        "bash", "-c",
        "set -a && source /files/system/environment_variables.env && set +a && "
        f"export AIRFLOW__CORE__EXECUTION_API_SERVER_URL={execution_api_url} && "
        "export PYTHONUNBUFFERED=1 && "
        f"runuser -u airflow -- airflow standalone 2>&1 | tee {AIRFLOW_LOG}",
    )

    # Drain stdout in a daemon thread to prevent pipe backup
    ready_event = threading.Event()

    def _drain():
        for line in _airflow_proc.stdout:
            print(line, end="")
            if "Airflow is ready" in line:
                ready_event.set()

    threading.Thread(target=_drain, daemon=True).start()
    print("Airflow standalone exec started")

    # Wait for ready signal from the streaming thread, falling back to
    # polling the log file (in case the stdout pipe misses it).
    deadline = time.time() + timeout
    poll_interval = 5

    while time.time() < deadline:
        if ready_event.wait(timeout=poll_interval):
            return

        # Fallback: check the log file directly
        p_grep = sandbox.exec(
            "bash", "-c",
            f'grep -c "Airflow is ready" {AIRFLOW_LOG} 2>/dev/null || echo 0',
        )
        count = "0"
        for line in p_grep.stdout:
            count = line.strip()
        p_grep.wait()

        if count != "0":
            print("Airflow is ready")
            return

    raise TimeoutError(
        f"Airflow did not become ready within {timeout}s"
    )


def unpause_dag(
    sandbox: modal.Sandbox, dag_id: str, retries: int = 6, backoff: float = 10.0
) -> None:
    """Unpause a DAG so it can accept triggers.

    Retries if the DAG hasn't been parsed by the dag-processor yet.
    """
    for attempt in range(retries):
        proc = sandbox.exec(
            "runuser", "-u", "airflow", "--",
            "airflow", "dags", "unpause", dag_id,
        )
        stdout_lines = []
        for line in proc.stdout:
            print(line, end="")
            stdout_lines.append(line)
        proc.wait()

        stdout = "".join(stdout_lines)
        if "No paused DAGs were found" not in stdout and proc.returncode == 0:
            return

        if attempt < retries - 1:
            wait_time = backoff * (attempt + 1)
            print(f"DAG {dag_id} not found yet, retrying in {wait_time}s...")
            time.sleep(wait_time)

    raise RuntimeError(f"Failed to unpause DAG {dag_id} after {retries} attempts")


def trigger_dag(
    sandbox: modal.Sandbox, dag_id: str, retries: int = 6, backoff: float = 10.0
) -> str:
    """Trigger a DAG run and return the run_id.

    Retries with backoff if the DAG hasn't been parsed yet.

    Returns:
        The run_id of the triggered DAG run.

    Raises:
        RuntimeError: If the DAG cannot be triggered after all retries.
    """
    for attempt in range(retries):
        proc = sandbox.exec(
            "runuser", "-u", "airflow", "--",
            "airflow", "dags", "trigger", dag_id, "-o", "json",
        )
        stdout_lines = []
        for line in proc.stdout:
            print(line, end="")
            stdout_lines.append(line)
        stderr_lines = []
        for line in proc.stderr:
            print(line, end="")
            stderr_lines.append(line)
        proc.wait()

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)

        if proc.returncode == 0:
            # Parse JSON output to get run_id
            # The output may contain non-JSON lines before the actual JSON
            for line in stdout_lines:
                line = line.strip()
                if line.startswith("["):
                    data = json.loads(line)
                    return data[0]["dag_run_id"]
            raise RuntimeError(
                f"Could not parse run_id from trigger output: {stdout}"
            )

        # DAG might not be parsed yet, retry
        if attempt < retries - 1:
            wait_time = backoff * (attempt + 1)
            print(f"DAG {dag_id} not ready, retrying in {wait_time}s...")
            time.sleep(wait_time)

    raise RuntimeError(
        f"Failed to trigger DAG {dag_id} after {retries} attempts. "
        f"Last stderr: {stderr}"
    )


def wait_for_dag_run(
    sandbox: modal.Sandbox,
    dag_id: str,
    run_id: str,
    timeout: int = 300,
    poll_interval: int = 5,
) -> str:
    """Poll until a DAG run reaches a terminal state.

    Returns:
        The terminal state string (e.g. "success" or "failed").

    Raises:
        TimeoutError: If the DAG run doesn't complete within timeout seconds.
    """
    deadline = time.time() + timeout

    while time.time() < deadline:
        proc = sandbox.exec(
            "runuser", "-u", "airflow", "--",
            "airflow", "dags", "list-runs", dag_id, "-o", "json",
        )
        stdout_lines = []
        for line in proc.stdout:
            stdout_lines.append(line)
        stderr_lines = []
        for line in proc.stderr:
            stderr_lines.append(line)
        proc.wait()

        if proc.returncode != 0:
            print(f"list-runs failed (rc={proc.returncode}): {''.join(stderr_lines)}")

        for line in stdout_lines:
            line = line.strip()
            if line.startswith("["):
                runs = json.loads(line)
                for run in runs:
                    if run.get("run_id") == run_id:
                        state = run.get("state")
                        print(f"  [{dag_id}/{run_id}] state={state}")
                        if state in ("success", "failed"):
                            print(f"DAG run {dag_id}/{run_id} reached state: {state}")
                            return state
                break

        time.sleep(poll_interval)

    raise TimeoutError(
        f"DAG run {dag_id}/{run_id} did not complete within {timeout}s"
    )


def get_task_states(
    sandbox: modal.Sandbox, dag_id: str, run_id: str
) -> list[dict]:
    """Get task instance states for a DAG run.

    Returns:
        List of dicts with task_id, state, etc.
    """
    proc = sandbox.exec(
        "runuser", "-u", "airflow", "--",
        "airflow", "tasks", "states-for-dag-run", dag_id, run_id, "-o", "json",
    )
    stdout_lines = []
    for line in proc.stdout:
        stdout_lines.append(line)
    proc.wait()

    for line in stdout_lines:
        line = line.strip()
        if line.startswith("["):
            return json.loads(line)

    return []
