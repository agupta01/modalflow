Serverless Airflow on Modal

## Overview

A CLI tool `modalflow` that deploys a minimal, serverless Airflow environment on Modal. Tasks execute as direct Modal Function calls (truly serverless, pay-per-use), with PostgreSQL for metadata storage.

## Architecture
```
┌─────────────────────────────────────────────────────────────────┐  
│ Modal Cloud                                                     │  
├─────────────────────────────────────────────────────────────────┤  
│   ┌─────────────┐       ┌─────────────┐       ┌─────────────────┐   │  
│   │  Scheduler  │──────▶│    Modal    │──────▶│  Task Executor  │   │  
│   │   (Cron)    │       │    Dict     │       │   (Function)    │   │  
│   └──────┬──────┘       └─────────────┘       └────────┬────────┘   │  
│          │                                             │            │  
│          ▼                                             ▼            │  
│   ┌──────────────────────────────────────────────────────────┐      │  
│   │ Modal Volume                                             │      │  
│   │  /dags/   - DAG Python files                             │      │  
│   │  /logs/   - Task logs (text files)                       │      │  
│   │  /xcom/   - XCom payloads (JSON/Pickle)                  │      │  
│   │  /venvs/  - Cached virtual environments                  │      │  
│   └──────────────────────────────────────────────────────────┘      │  
│          │                                                          │  
│   ┌─────────────┐ (on-demand via CLI)                               │  
│   │  Ephemeral  │◀─── modalflow ui --env dev                    │  
│   │  UI Server  │                                                   │  
│   └─────────────┘                                                   │  
│          │                                                          │  
│   ┌─────────────┐                                                   │  
│   │ PostgreSQL  │ (External or Modal-hosted)                        │  
│   └─────────────┘                                                   │  
└─────────────────────────────────────────────────────────────────┘
```

## Package Structure

```
modalflow/  
├── pyproject.toml  
├── README.md  
├── src/  
│ └── airflow_serverless/  
│ ├── __init__.py  
│ ├── cli.py # Click CLI entry point  
│ ├── config.py # YAML config parser  
│ ├── modal_app.py # Modal App factory (uses apache/airflow image)
│ ├── db/  
│ │ ├── __init__.py  
│ │ └── postgres_backend.py # PostgreSQL support (ONLY supported backend)
│ ├── scheduler/  
│ │ ├── __init__.py  
│ │ └── scheduler.py # Serverless scheduler with strict parsing timeouts
│ ├── executor/  
│ │ ├── __init__.py  
│ │ └── modal_executor.py # Custom Modal executor (BaseExecutor)
│ ├── dags/  
│ │ ├── __init__.py  
│ │ └── storage.py # DAG upload/management  
│ └── ui/  
│ ├── __init__.py  
│ └── server.py # Ephemeral UI server
└── tests/
```

## CLI Commands

`modalflow create --env {name} --config config.yaml`
	What it does:
	1. Parses config.yaml for database settings
	2. Creates Modal App named modalflow-{env}
	3. Creates Modal Volume airflow-vol-{env}
	4. Creates Modal Dict airflow-state-{env} for scheduler/executor coordination
	5. Stores secrets (db connection string) in Modal Secrets
	6. Deploys the scheduler function with cron schedule
	7. Initializes database schema (creates tables)

`config.yaml` structure:  
```yaml
database:  
	connection_string: "postgresql://user:pass@host:5432/db"

scheduler:  
	interval_seconds: 60 # How often scheduler runs
	parse_timeout_seconds: 5 # Timeout per DAG file parsing

executor:  
	parallelism: 16 # Max concurrent tasks  
	task_timeout: 3600 # Default task timeout in seconds
```

`modalflow dags create --env {name} --file dag.py`
	What it does:
	1. Validates DAG file by attempting to parse it locally
	2. Uploads DAG file to Modal Volume at /dags/{filename}
	3. Triggers volume commit

`modalflow dags list --env {name}`
	What it does:
	1. Lists all .py files in the /dags/ directory on Modal Volume
	2. Optionally shows which DAGs have been detected by the scheduler

`modalflow dags update --env {name} --file dag.py`
	What it does:
	1. Validates DAG file locally
	2. Overwrites existing file on Modal Volume
	3. Next scheduler run will pick up changes

`modalflow dags delete --env {name} --dag-id {dag_id}`
	What it does:
	1. Removes DAG file from Volume
	2. Marks DAG as inactive in database

`modalflow ui --env {name}`
	What it does:
	1. Spawns an ephemeral Modal Function running the Airflow FastAPI UI
	2. Connects to the environment's database and /vol/logs/
	3. Opens browser or prints URL
	4. Keeps running until Ctrl-C is pressed

## Core Components

### Scheduler (`scheduler/scheduler.py`)

A Modal Function that runs on a cron schedule (default: every 60 seconds).
To prevent overruns, the scheduler strictly limits time spent parsing DAGs.

```python
@app.function(
	image=airflow_image,
	schedule=modal.Period(seconds=60),
	volumes={"/vol": volume},
	secrets=[modal.Secret.from_name(f"airflow-secrets-{env}")],
	timeout=300, # Hard timeout for the function
)
def scheduler_tick():  
	"""Single scheduler iteration."""
	# 1. Acquire scheduler lock (via DB advisory lock)
	
	# 2. Initialize Executor
	# executor = ModalExecutor()
	# executor.start()
	
	# 3. Load DAGs & Sync Metadata (Inline Parsing)
	# - Iterate over /vol/dags/*.py
	# - For each file: 
	#     Fork/Process with 5s timeout. 
	#     If timeout -> Mark Import Error. 
	#     Else -> Update DB.
	
	# 4. Create DagRuns
	
	# 5. Schedule Tasks
	# - Find ready tasks
	# - executor.queue_command(task_instance, command)
	
	# 6. Trigger Execution
	# - executor.heartbeat()  # triggers sync() and executes queued tasks
	
	# 7. Release lock & Cleanup
	# executor.end()
```

### Modal Executor (executor/modal_executor.py)

A custom Airflow Executor inheriting from `BaseExecutor` (Airflow 2.x style).
Allows "serverless" usage and standard Airflow compatibility.

```python
from airflow.executors.base_executor import BaseExecutor

class ModalExecutor(BaseExecutor):
	"""Runs tasks as Modal Functions."""
	
	def execute_async(self, key, command, queue=None, executor_config=None):
		"""Trigger the task on Modal."""
		# 1. Serialize command/context
		# 2. execute_task.spawn(command)
		# 3. Update internal state to RUNNING
		
	def sync(self):
		"""Poll Modal for task status."""
		# 1. Check Modal Dict or result futures
		# 2. Reconcile with DB: DB is source of truth.
		#    If DB says QUEUED but Dict has no record -> Re-queue (at-least-once).
		# 3. Update self.event_buffer with SUCCESS/FAILED events
		# 4. Handle Zombies (if task missing from Dict for > N minutes)
		
@app.function(image=airflow_image, ...)
def execute_task(task_info: dict):
	"""Execute a single Airflow task (Worker)."""
	# 1. Configure logging to write to /vol/logs/{dag_id}/{task_id}/...
	# 2. Load DAG
	# 3. Execute Task
	# 4. Update status in Modal Dict (hot cache)
```

### Database Layer (db/)

**PostgreSQL ONLY.**
- Dropped SQLite support due to locking issues on network volumes.
- Uses `pg_try_advisory_lock()` for scheduler coordination.
- Connection string provided via secrets.

### Logging
- Tasks write standard Airflow logs to `/vol/logs/`.
- `FileTaskHandler` configured to read/write from this volume path.
- UI server mounts `/vol/` to serve logs to the user.

### Base Image
- Uses `apache/airflow:2.10.0-python3.10` (or similar) as the base.
- Reduces implementation overhead and ensures environment compatibility.

### XCom Backend (xcom/backend.py)
- **Custom Backend:** `VolumeXComBackend`
- **Storage:** Payloads stored as files in `/vol/xcom/{dag_id}/{run_id}/{task_id}/{key}`.
- **Benefit:** Keeps large payloads out of Postgres.

## Data Flow

### DAG Upload Flow
(Unchanged)
CLI -> Validate Locally -> Upload to Volume -> Scheduler picks up -> DB Update

### Task Execution Flow
```
scheduler_tick()
│
├─▶ 1. Instantiate ModalExecutor()
│
├─▶ 2. Parse DAGs (Strict Timeout per File)
│
├─▶ 3. Schedule & Heartbeat
    │
    ├─▶ Sync: 
    │   - Poll Modal Dict
    │   - Reconcile with Postgres (DB is Truth)
    │
    └─▶ Execute: 
        │
        └─▶ execute_task.spawn()
            │
            ├─▶ Mount Volume (Logs/XCom)
            │
            ├─▶ Write Logs to /vol/logs/...
            │
            └─▶ task.execute() -> Write Result to Dict
```

## Key Technical Decisions

1. **BaseExecutor on Airflow 2.x**: Ensures compatibility with existing DAGs and allows hybrid deployments.
2. **Postgres Only**: Hard requirement for stability. No SQLite.
3. **Cron Scheduler**: "Ticking" function every 60s. Low cost, "scale-to-zero".
4. **Strict Parsing Timeouts**: Prevents scheduler overruns. Bad DAGs fail fast; good DAGs run.
5. **Logs on Volume**: Simple, native file-based logging accessible by UI.
6. **Airflow Base Image**: Leverages upstream maintenance.

## Implementation Plan

### Phase 1: Core & Executor
1. Set up repo with `pyproject.toml`.
2. Define `modal_app.py` using `apache/airflow` base image.
3. Implement `ModalExecutor` (extending `BaseExecutor`).
4. Implement `execute_task` Modal function.
5. Test: Run `ModalExecutor` against standard Airflow tests (mocked).

### Phase 2: Database & State
1. Implement `PostgresBackend` connection and locking.
2. Implement schema initialization (subset of Airflow tables).
3. Implement `VolumeXComBackend`.

### Phase 3: Scheduler
1. Implement `scheduler_tick()` skeleton.
2. Implement **Robust DAG Parsing** with timeouts (`multiprocessing` or `func_timeout`).
3. Implement scheduling loop.

### Phase 4: DAG Management & CLI
1. Implement CLI for `create`, `dags upload/list`.
2. Implement Volume-based DAG storage.

### Phase 5: UI & Logs
1. Configure `FileTaskHandler` for `/vol/logs`.
2. Implement ephemeral UI server (mounting volume).
3. Verify log visibility in UI.

### Phase 6: Integration & Polish
1. E2E tests on Modal.
2. Documentation.
3. Example DAGs.

## Dependencies

```toml
[project]  
dependencies = [  
	"modal>=0.64.0",  
	"apache-airflow-core>=2.10.0", 
	"click>=8.0.0",  
	"pyyaml>=6.0",  
	"rich>=13.0.0",
    "psycopg2-binary>=2.9.0", # Postgres driver
]
```

## Limitations (Documented for Users)
1. **Latency**: Minimum ~1 min latency due to cron scheduler.
2. **Postgres Required**: External Postgres DB needed.
3. **Zombie Detection**: Slower detection (depends on scheduler tick).
4. **Parsing**: Slow top-level code in DAGs (>5s) will cause ImportErrors.
