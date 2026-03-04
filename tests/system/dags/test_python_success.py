from airflow import DAG
from airflow.operators.python import PythonOperator


def return_42():
    return 42


with DAG(
    dag_id="test_python_success",
    schedule=None,
    catchup=False,
    tags=["system-test"],
) as dag:
    PythonOperator(
        task_id="return_value",
        python_callable=return_42,
    )
