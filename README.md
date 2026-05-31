# Modalflow

A serverless Airflow executor that runs each task as an independent [Modal](https://modal.com) Sandbox.

Modalflow replaces Airflow's built-in executors (Local, Celery, Kubernetes) with one that dispatches each task to its own Modal Sandbox. No worker pools, no Kubernetes cluster, no infrastructure to manage — tasks run on-demand and scale to zero when idle.

## Prerequisites

- An existing Airflow 3.1+ deployment
- A [Modal](https://modal.com) account with the CLI configured (`modal setup`)
- Python 3.10+

## Setup

### 1. Install

Install `modalflow` on the machine running Airflow (the scheduler that hosts the executor):

```bash
pip install modalflow
```

The same install provides the `modalflow` CLI used to push DAGs.

### 2. Configure the Modal CLI

If you haven't already, authenticate the Modal CLI on the machine running Airflow:

```bash
modal setup
```

The executor uses your Modal credentials to create a sandbox per task.

### 3. Push your DAGs to a Modal Volume

DAGs are delivered via a Modal Volume. Each task sandbox mounts this volume at `/opt/airflow/dags`. Push your DAG directory with `modalflow sync`:

```bash
modalflow sync --dags-path ./dags --dags-volume my-dags
```

This does a full replace — files deleted locally are also removed from the volume. Re-run it whenever your DAGs change (see [Updating DAGs](#updating-dags)).

### 4. Configure Airflow

Set the executor class in `airflow.cfg` or via environment variable:

```ini
[core]
executor = modalflow.executor.ModalExecutor
```

```bash
export AIRFLOW__CORE__EXECUTOR=modalflow.executor.ModalExecutor
```

Then tell the executor which volume to mount into each task sandbox:

```bash
export MODALFLOW_DAGS_VOLUME=my-dags
```

Optional settings:

- `MODALFLOW_ENV` — target Modal environment name (default: `main`).
- `MODALFLOW_AIRFLOW_VERSION` — Apache Airflow version installed in each task sandbox (default: `3.1.5`). Set this to match your Airflow server version to avoid API version mismatches between the task SDK in the sandbox and your Airflow server.

```bash
export MODALFLOW_ENV=main
export MODALFLOW_AIRFLOW_VERSION=3.1.8
```

## Updating DAGs

`modalflow sync` is the primary mechanism for delivering DAG changes. Re-run it whenever you add, edit, or remove DAGs:

```bash
modalflow sync --dags-path ./dags --dags-volume my-dags
```

This performs a full replace — files deleted locally are also removed from the volume, so the volume always mirrors your local DAG directory. New sandboxes pick up the latest DAGs automatically; there is no backend to redeploy.

## Networking

Task sandboxes run in Modal's cloud. To execute a task, the sandbox must call back to your Airflow deployment's [execution API](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/execution-api.html). This means Airflow's API server must be reachable from the public internet.

The executor resolves the execution API URL in priority order:

1. `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` environment variable
2. `core.execution_api_server_url` in `airflow.cfg`

The URL **must** end with `/execution/` — the executor appends this automatically if missing. See [apache/airflow#51235](https://github.com/apache/airflow/issues/51235) for background.

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

When running Airflow locally (e.g. `airflow standalone`), task sandboxes need to reach your local execution API. Use a reverse tunnel (ngrok, Cloudflare Tunnel, etc.) and set the URL:

```bash
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL=https://your-tunnel-url.ngrok-free.app/execution/
```

See `.env.example` for a full local development template.

**Important:** set `MODALFLOW_AIRFLOW_VERSION` to match your local Airflow version to avoid API version mismatches between the task SDK in the sandbox and your local Airflow server:

```bash
export MODALFLOW_AIRFLOW_VERSION=3.1.8
```

> **Note:** Only DAGs you push to the volume (via `modalflow sync`) are available to task sandboxes. Airflow's built-in example DAGs use a separate bundle (`example_dags`) that isn't present in the sandbox, so they will fail. Use your own DAGs.

## Development

```bash
uv sync --extra dev
uv run modalflow sync --help
uv run pytest                      # unit tests
make system.setup && make system.test.e2e   # E2E tests (requires Modal)
```

`make system.setup` builds the package and pushes the test DAGs to a Modal Volume; `make system.test.e2e` runs `airflow standalone` inside a Modal Sandbox and exercises the executor end-to-end.
