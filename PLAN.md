Serverless Airflow on Modal

## Overview

A CLI tool airflow-serverless that deploys a minimal, serverless Airflow environment on Modal. Tasks execute as direct Modal Function calls (truly serverless, pay-per-use), with SQLite-on-Volume or PostgreSQL for metadata storage.

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
│   │  /data/   - SQLite DB (airflow.db)                       │      │  
│   │  /xcom/   - XCom payloads (JSON/Pickle)                  │      │  
│   │  /venvs/  - Cached virtual environments                  │      │  
│   └──────────────────────────────────────────────────────────┘      │  
│          │                                                          │  
│   ┌─────────────┐ (on-demand via CLI)                               │  
│   │  Ephemeral  │◀─── airflow-serverless ui --env dev               │  
│   │  UI Server  │                                                   │  
│   └─────────────┘                                                   │  
└─────────────────────────────────────────────────────────────────┘
```

## Package Structure

```
airflow-serverless/  
├── pyproject.toml  
├── README.md  
├── src/  
│ └── airflow_serverless/  
│ ├── **init**.py  
│ ├── cli.py # Click CLI entry point  
│ ├── config.py # YAML config parser  
│ ├── modal_app.py # Modal App factory  
│ ├── db/  
│ │ ├── **init**.py  
│ │ ├── backend.py # Abstract DB interface  
│ │ ├── sqlite_backend.py # SQLite on Volume  
│ │ └── postgres_backend.py # PostgreSQL support  
│ ├── scheduler/  
│ │ ├── **init**.py  
│ │ └── scheduler.py # Serverless scheduler  
│ ├── executor/  
│ │ ├── **init**.py  
│ │ └── modal_executor.py # Custom Modal executor  
│ ├── dags/  
│ │ ├── **init**.py  
│ │ └── storage.py # DAG upload/management  
│ └── ui/  
│ ├── **init**.py  
│ └── server.py # Ephemeral UI server  
└── tests/
```

## CLI Commands

airflow-serverless create --env {name} --config config.yaml
	What it does:
	1. Parses config.yaml for database type and settings
	2. Creates Modal App named airflow-serverless-{env}
	3. Creates Modal Volume airflow-dags-{env}
	4. Creates Modal Dict airflow-state-{env} for scheduler/executor coordination
	5. Stores secrets (db connection string) in Modal Secrets
	6. Deploys the scheduler function with cron schedul
	7. Initializes database schema (creates tables)

config.yaml structure:  
```yaml
database:  
	type: sqlite # or 'postgres'

# If postgres:

connection_string: "postgresql://user:pass@host:5432/db"

scheduler:  
	interval_seconds: 60 # How often scheduler runs

executor:  
	parallelism: 16 # Max concurrent tasks  
	task_timeout: 3600 # Default task timeout in seconds
```

`airflow-serverless dags create --env {name} --file dag.py`
	What it does:
	1. Validates DAG file by attempting to parse it locally
	2. Uploads DAG file to Modal Volume at /dags/{filename}
	3. Triggers volume commit

`airflow-serverless dags list --env {name}`
	What it does:
	1. Lists all .py files in the /dags/ directory on Modal Volume
	2. Optionally shows which DAGs have been detected by the scheduler

`airflow-serverless dags update --env {name} --file dag.py`
	What it does:
	1. Validates DAG file locally
	2. Overwrites existing file on Modal Volume
	3. Next scheduler run will pick up changes

`airflow-serverless dags delete --env {name} --dag-id {dag_id}`
	What it does:
	1. Removes DAG file from Volume
	2. Marks DAG as inactive in database

`airflow-serverless ui --env {name}`
	What it does:
	1. Spawns an ephemeral Modal Function running the Airflow FastAPI UI
	2. Connects to the environment's database
	3. Opens browser or prints URL
	4. Keeps running until Ctrl-C is pressed
	5. On Ctrl-C: terminates the Modal function

## Core Components

### Scheduler (`scheduler/scheduler.py`)

A Modal Function that runs on a cron schedule (default: every 60 seconds):

```python
@app.function(
	schedule=modal.Period(seconds=60),
	volumes={"/vol": volume},
	secrets=[modal.Secret.from_name(f"airflow-secrets-{env}")],
	timeout=300,
)
def scheduler_tick():  
	"""Single scheduler iteration."""
	# 1. Acquire scheduler lock
	
	# 2. Initialize Executor
	# executor = ModalExecutor()
	# executor.start()
	
	# 3. Load DAGs & Sync Metadata
	
	# 4. Create DagRuns
	
	# 5. Schedule Tasks
	# - Find ready tasks
	# - executor.queue_command(task_instance, command)
	
	# 6. Trigger Execution
	# - executor.heartbeat()  # triggers sync() and executes queued tasks
	
	# 7. Release lock & Cleanup
	# executor.end()
```

Key simplifications vs full Airflow:
- No persistent process (runs every N seconds)
- No DagFileProcessor subprocess (inline DAG parsing)
- No callbacks/sensors/triggerers
- No pools/variables/connections management

### Modal Executor (executor/modal_executor.py)

A custom Airflow Executor that allows both "serverless" usage (inside our scheduler) and "remote" usage (connecting an existing Airflow cluster to Modal).

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
		# 2. Update self.event_buffer with SUCCESS/FAILED events
		# 3. Handle Zombies
		
@app.function(...)
def execute_task(task_info: dict):
	"""Execute a single Airflow task (Worker)."""
	# ... (same logic as before: requirements, dag load, execute, status update)
```

State coordination via Modal Dict:

Scheduler writes:

```python
state_dict[f"task:{dag_id}:{task_id}:{run_id}"] = {  
	"status": "QUEUED",  
	"call_id": modal_call.object_id,  
	"queued_at": timestamp  
}
```

Task updates on completion:
```python
state_dict[f"task:{dag_id}:{task_id}:{run_id}"] = {  
	"status": "SUCCESS", # or "FAILED"  
	"completed_at": timestamp  
}
```

### Database Layer (db/)

Abstract interface:  
```python
class DatabaseBackend(ABC):  
	@abstractmethod  
	def get_connection(self): ...
	
	@abstractmethod
	def acquire_lock(self, lock_name: str, timeout: int) -> bool: ...
	
	@abstractmethod
	def release_lock(self, lock_name: str): ...
```

#### SQLite Backend:
- Uses WAL mode for better concurrency
- File stored at /vol/data/airflow.db on Modal Volume
- ==Distributed lock via Modal Dict (scheduler lock)==
- Read-only operations from UI (no manual task triggers)

#### PostgreSQL Backend:
- Uses pg_try_advisory_lock() for distributed locking
- Full read-write support
- Better for production workloads

### XCom Backend (xcom/backend.py)
To resolve concurrency issues with SQLite and enable inter-task communication:

- **Custom Backend:** `VolumeXComBackend`
- **Storage:** Payloads stored as files in `/vol/xcom/{dag_id}/{run_id}/{task_id}/{key}` (JSON or Pickle).
- **Behavior:**
  - `xcom_push`: Writes file to Volume.
  - `xcom_pull`: Reads file from Volume.
- **Benefit:** Keeps large payloads out of SQLite, reducing lock contention.

### DAG Storage (dags/storage.py)

```python
class DagStorage:  
	def __init__(self, volume: modal.Volume, dags_path: str = "/vol/dags"):  
	self.volume = volume  
	self.dags_path = dags_path
	def upload(self, local_path: str, dag_filename: str):
		 """Upload a DAG file to the volume."""
		 # Uses Modal's volume.put_file() or a Modal function
	
	def list_dags(self) -> list[str]:
		 """List all DAG files in the volume."""
	
	def delete(self, dag_filename: str):
		 """Remove a DAG file from the volume."""
```

### Ephemeral UI (ui/server.py)

```python
@app.function(  
	volumes={"/vol": volume},  
	secrets=[modal.Secret.from_name(f"airflow-secrets-{env}")],  
)  
@modal.asgi_app()  
def serve_ui():  
	"""Serve the Airflow UI."""  
	from airflow.api_fastapi.app import create_app
	
	# Configure minimal Airflow settings
	
	# Return FastAPI app
	
# CLI implementation for ephemeral UI:  
def ui_command(env: str):
	
	# 1. Create a temporary Modal App with the UI function
	
	# 2. Deploy with modal.serve() (blocking)
	
	# 3. Print the URL
	
	# 4. On Ctrl-C: stop the server
```

## Data Flow

### DAG Upload Flow

```
User: airflow-serverless dags create --file my_dag.py  
│  
▼  
CLI validates DAG locally (import check)  
│  
▼  
CLI uploads to Modal Volume /vol/dags/my_dag.py  
│  
▼  
Next scheduler_tick() parses DAG, writes to DB  
│  
▼  
DAG appears in UI
```

### Task Execution Flow

```
scheduler_tick() (Modal Function)
│
├─▶ 1. Instantiate ModalExecutor()
│
├─▶ 2. Parse DAGs & Create DagRuns
│
├─▶ 3. Find Ready Tasks
│
└─▶ 4. executor.heartbeat()
    │
    ├─▶ Sync: Poll Modal Dict for completed tasks
    │
    └─▶ Execute: For each new task:
        │
        └─▶ execute_task.spawn() (Remote Worker)
            │
            ├─▶ Prepare Env & Load DAG
            │
            └─▶ task.execute() -> Write Result to Dict
```

## Database Schema (Minimal)

Only the essential tables for core functionality:
```sql
-- DAG metadata  
dag (dag_id, is_active, schedule_interval, ...)  
serialized_dag (dag_id, data, ...)

-- Execution tracking  
dag_run (run_id, dag_id, state, execution_date, ...)  
task_instance (task_id, dag_id, run_id, state, try_number, ...)

-- Import errors for debugging  
import_error (filename, timestamp, stacktrace)
```

Not included (minimal scope):
- variable, connection, pool - no advanced features
- xcom - no inter-task communication
- trigger, callback - no async features
- log - logs go to Modal's logging

## Key Technical Decisions

1. Custom BaseExecutor Implementation: `ModalExecutor` inherits from `BaseExecutor`.
	- Reasons:
		1. Allows existing external Airflow clusters to offload to Modal.
		2. Reuses Airflow's robust state management and event buffering.
		3. "Serverless" scheduler simply instantiates this executor class.
2. SQLite Concurrency
	1. Only scheduler writes to DB (single writer)
	2. Tasks update their own status (minimal contention)
	3. XCom payloads offloaded to Volume files to avoid DB locks
	4. Modal Dict for cross-function coordination
	5. WAL mode + busy_timeout for resilience
3. Ephemeral UI
	1. Not always deployed (saves costs)
	2. User runs airflow-serverless ui when needed
	3. Uses modal serve for local development experience
	4. Ctrl-C cleanly terminates
4. No Airflow Providers
	1. Only core operators (PythonOperator, BashOperator)
	2. Users can install providers in their DAG files if needed
	3. Keeps the base image small

## Implementation Plan

### Phase 1: Core Infrastructure
1. Set up new repo with pyproject.toml
2. Implement CLI skeleton with Click
3. Implement config.yaml parsing
4. Create Modal App factory function

### Phase 2: Database Layer
1. Implement SQLite backend with Volume storage
2. Implement PostgreSQL backend
3. Implement VolumeXComBackend (storage logic)
4. Create schema initialization (subset of Airflow tables)
5. Test backends and XCom storage

### Phase 3: DAG Management
1. Implement DAG upload to Volume
2. Implement DAG listing
3. Implement DAG deletion
4. Test DAG parsing with DagBag

### Phase 4: Scheduler
1. Implement scheduler_tick() function
2. Implement DAG parsing and sync
3. Implement DagRun creation logic
4. Implement task readiness checking
5. Implement task spawning via Modal

### Phase 5: Task Execution
1. Implement execute_task() function
2. Implement dynamic requirements installation (Volume-backed venvs)
3. Implement status updates to DB and Dict
4. Test end-to-end task execution
5. Test task failures, retries, and sensors (reschedule)

### Phase 6: UI
1. Implement ephemeral UI server
2. Implement CLI ui command with modal serve
3. Test UI with both database backends

### Phase 7: Testing & Polish
1. Integration tests
2. Documentation
3. Example DAGs

## Dependencies

```toml
[project]  
dependencies = [  
	"modal>=0.64.0",  
	"apache-airflow-core>=3.0.0", # or the appropriate version  
	"click>=8.0.0",  
	"pyyaml>=6.0",  
	"rich>=13.0.0", # for CLI output  
]
```

## Limitations (Documented for Users)
1. No Persistent Scheduler - Runs on a cron interval (e.g. 1 min latency).
2. No Pools/Queues - No resource limiting beyond Modal parallelism.
3. No Callbacks - No on_success/on_failure callbacks (currently).
4. SQLite Concurrency - Recommended to use PostgreSQL for >5 concurrent tasks.
5. Logs - Task logs are in Modal dashboard, not yet fully integrated into Airflow UI.
6. SQLite: Read-only UI - Cannot trigger DAGs manually from UI.
