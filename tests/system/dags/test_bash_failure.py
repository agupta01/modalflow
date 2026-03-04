from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="test_bash_failure",
    schedule=None,
    catchup=False,
    tags=["system-test"],
) as dag:
    BashOperator(
        task_id="fail_task",
        bash_command="exit 1",
    )
