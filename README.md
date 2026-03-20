# Modalflow

A serverless Airflow executor that runs tasks as [Modal](https://modal.com) Functions.

Modalflow replaces Airflow's built-in executors (Local, Celery, Kubernetes) with one that dispatches each task to a Modal Function. No worker pools, no Kubernetes cluster, no infrastructure to manage — tasks run on-demand and scale to zero when idle.

## Prerequisites

- An existing Airflow 3.1+ deployment
- A [Modal](https://modal.com) account with the CLI configured (`modal setup`)
- Python 3.10+

## Setup

### 1. Install

Install `modalflow` on both your **local machine** (for the CLI) and your **Airflow cluster** (for the executor):

```bash
pip install modalflow
```

### 2. Deploy the Modal backend

The `modalflow deploy` command creates the Modal Function, Volume, and Dict that the executor needs. You must choose how DAG files are provided to the Modal task workers:

**Local mode** — bakes DAGs into the function image (simplest, but requires redeploying on every DAG change):

```bash
modalflow deploy --dags-source local --dags-path ./dags
```

**Volume mode** — stores DAGs on a Modal Volume (update DAGs without redeploying):

```bash
modalflow deploy --dags-source volume --dags-volume my-dags --dags-path ./dags
```

**Cloud bucket mode** — reads DAGs from an S3 bucket at runtime:

```bash
modalflow deploy --dags-source cloud-bucket \
  --dags-bucket s3://my-bucket/dags \
  --dags-bucket-secret my-aws-secret
```

By default, the Modal function image installs Airflow 3.1.5. To match your local Airflow version, use `--airflow-version`:

```bash
modalflow deploy --dags-source local --dags-path ./dags --airflow-version 3.1.8
```

To target a specific Modal environment, add `--env <name>` (default: `main`).

### 3. Configure Airflow

Set the executor class in `airflow.cfg` or via environment variable:

```ini
[core]
executor = modalflow.executor.ModalExecutor
```

```bash
export AIRFLOW__CORE__EXECUTOR=modalflow.executor.ModalExecutor
```

The executor reads `MODALFLOW_ENV` to find the right Modal app (default: `main`). Set it if you deployed with a custom `--env`.

## Updating DAGs (volume mode)

With volume mode, use `modalflow sync` to push DAG changes without redeploying the function:

```bash
modalflow sync --dags-path ./dags --dags-volume my-dags
```

This does a full replace — files deleted locally are also removed from the volume.

## Networking

Modal Functions run in Modal's cloud. To execute a task, the function must call back to your Airflow deployment's [execution API](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/execution-api.html). This means Airflow's API server must be reachable from the public internet.

The executor resolves the execution API URL in priority order:

1. `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` environment variable
2. `core.execution_api_server_url` in `airflow.cfg`
3. `modal.forward()` tunnel (local development only)

### Production

Set the URL to your Airflow API's public endpoint:

```bash
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL=https://airflow.example.com/execution/
```

Common ways to expose the API:

- **Load balancer** (ALB, NLB) in front of the Airflow API server
- **API Gateway** with auth
- **Reverse tunnel** (Cloudflare Tunnel, ngrok) if you can't expose a public endpoint directly

### Local development

When running Airflow locally (e.g. `airflow standalone`), Modal functions need to reach your local execution API. Use a reverse tunnel (ngrok, Cloudflare Tunnel, etc.) and set the URL:

```bash
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL=https://your-tunnel-url.ngrok-free.app
```

See `.env.example` for a full local development template.

**Important:** deploy with `--airflow-version` matching your local Airflow version to avoid API version mismatches:

```bash
modalflow deploy --dags-source local --dags-path ./dags --airflow-version 3.1.8
```

## Development

```bash
uv sync --extra dev
uv run modalflow deploy --help
uv run pytest                      # unit tests
make system.setup && make system.test.e2e   # E2E tests (requires Modal)
```
