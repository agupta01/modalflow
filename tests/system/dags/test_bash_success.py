from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="test_bash_success",
    schedule=None,
    catchup=False,
    tags=["system-test"],
) as dag:
    BashOperator(
        task_id="echo_hello",
        bash_command="echo hello_from_modal",
    )
