# Modalflow

A serverless Airflow Executor for Modal.

## Usage

### Prerequisites
- pip
- A Modal account, workspace, and environment
- Modal CLI configured to your workspace

### Steps

1. `pip install modalflow` on your local machine, and add it to your Airflow cluster.
2. Run `modalflow deploy --env {environment name}` to deploy the resources into your Modal environment.
3. Add `modalflow.executor.ModalExecutor` to your Airflow config

## Development

We use `uv` for development. To setup:

1. `cd modalflow`
2. `uv sync`
3. To run the CLI, use: `uv run -- modalflow [COMMAND]`
4. To run unit tests, use: `uv run pytest`