"""Builder helpers for running Airflow tasks as Modal Sandboxes.

This module no longer defines a long-lived deployed function or a
``modal.Dict`` state store.  Instead it exposes helpers that the
``ModalExecutor`` imports to build the task image, resolve volumes, and
create a fresh ``modal.Sandbox`` for each Airflow task on demand.

DAG source is **volume-only**: DAGs live on a Modal Volume (named via
``MODALFLOW_DAGS_VOLUME``) and are mounted into each task sandbox at
``/opt/airflow/dags``.
"""

import os
from typing import Optional

import modal

# Allow overriding the environment name via env var.
ENV = os.environ.get("MODALFLOW_ENV", "main")

# Name of the Modal App that owns the task sandboxes.
APP_NAME = f"modalflow-{ENV}"

# Airflow version to install (configurable via CLI --airflow-version flag).
AIRFLOW_VERSION = os.environ.get("MODALFLOW_AIRFLOW_VERSION", "3.1.5")

# DAG source: volume-only.  The volume is mounted at /opt/airflow/dags.
DAGS_VOLUME_NAME = os.environ.get("MODALFLOW_DAGS_VOLUME", None)

# Default sandbox timeout (seconds).
DEFAULT_TASK_TIMEOUT = int(os.environ.get("MODALFLOW_TASK_TIMEOUT", "3600"))


def build_task_image() -> modal.Image:
    """Build the Modal Image used to run Airflow tasks.

    Mirrors the system-test image build: start from the official Airflow
    image, upgrade Airflow via pip (run as the ``airflow`` user, uid 50000),
    then install modalflow's runtime deps.  DAGs are NOT baked into the
    image — they are mounted from a Volume at runtime.
    """
    return (
        modal.Image.from_registry("apache/airflow:3.0.6-python3.10")
        # Clear the Airflow image's ENTRYPOINT so Modal's sandbox init
        # process doesn't exit immediately (which would terminate the
        # sandbox before our task command runs).
        .dockerfile_commands(["ENTRYPOINT []", "CMD []"])
        # Upgrade Airflow (3.0.6 uses old-style queue_command tuples;
        # 3.1.x uses queue_workload with ExecuteTask objects).  pip must
        # run as the airflow user, not root.
        .run_commands(
            f'su -s /bin/bash airflow -c "pip install apache-airflow=={AIRFLOW_VERSION}"'
        )
        .pip_install(
            "modal",
            "rich",
            "click",
            "pyyaml",
            "psycopg2-binary",
        )
    )


def resolve_volumes(dags_volume_name: Optional[str] = None) -> dict:
    """Resolve the volumes to mount into each task sandbox.

    Always mounts the log volume ``airflow-logs-{ENV}`` at
    ``/opt/airflow/logs``.  Mounts the DAG volume at ``/opt/airflow/dags``
    when a volume name is provided (defaults to ``MODALFLOW_DAGS_VOLUME``).

    Returns:
        Mapping of mount path -> ``modal.Volume`` suitable for
        ``modal.Sandbox.create(volumes=...)``.
    """
    name = dags_volume_name or DAGS_VOLUME_NAME

    log_volume = modal.Volume.from_name(
        f"airflow-logs-{ENV}", create_if_missing=True
    )
    volumes = {"/opt/airflow/logs": log_volume}

    if name:
        dags_volume = modal.Volume.from_name(name, create_if_missing=True)
        volumes["/opt/airflow/dags"] = dags_volume

    return volumes


def create_task_sandbox(
    app: modal.App,
    image: modal.Image,
    volumes: dict,
    env: Optional[dict] = None,
    timeout: int = DEFAULT_TASK_TIMEOUT,
) -> modal.Sandbox:
    """Create an idle Modal Sandbox for running a single Airflow task.

    The sandbox is created with a ``sleep infinity`` entrypoint so it stays
    alive after the task command (run via ``sandbox.exec(...)``) finishes.
    This lets the executor read the structured log file out of the sandbox
    *after* the task process exits but *before* the sandbox is terminated.
    (A sandbox whose entrypoint was the task command itself would finish and
    become unreachable the moment the task exits — see the proven
    ``sleep infinity`` + ``exec`` pattern in ``tests/system/test_app.py``.)

    Args:
        app: The Modal App the sandbox belongs to.
        image: The task image (see :func:`build_task_image`).
        volumes: Volume mounts (see :func:`resolve_volumes`).
        env: Per-task environment variables (e.g. the execution API URL).
        timeout: Sandbox timeout in seconds.

    Returns:
        The created (running) ``modal.Sandbox``.
    """
    return modal.Sandbox.create(
        "sleep",
        "infinity",
        app=app,
        image=image,
        secrets=[modal.Secret.from_name("modal")],
        volumes=volumes,
        env=env or {},
        timeout=timeout,
    )
