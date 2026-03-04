"""
E2E tests for volume-based DAG loading.

These tests verify that DAGs served from a Modal Volume (rather than
baked into the function image) work correctly end-to-end.

Prerequisites:
    make system.setup.volume   # deploy with volume mode + upload DAGs
"""

from test_app import create_test_sandbox
from helpers import (
    start_airflow_and_wait_ready,
    unpause_dag,
    trigger_dag,
    wait_for_dag_run,
)


class TestVolumeDagsE2E:
    def test_bash_task_via_volume_dags(self, airflow_ready, sandbox):
        """Verify that DAGs loaded from a Modal Volume work end-to-end."""
        dag_id = "test_bash_success"
        unpause_dag(sandbox, dag_id)
        run_id = trigger_dag(sandbox, dag_id)
        state = wait_for_dag_run(sandbox, dag_id, run_id, timeout=120)
        assert state == "success", f"Expected success, got {state}"
