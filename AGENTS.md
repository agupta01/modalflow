# Project Status and Handoff - `modalflow`

## Overview
`modalflow` is a custom Airflow Executor that runs Airflow tasks as Modal Functions. It leverages the Airflow 3.0 Task Execution API (AIP-72) to allow workers to communicate back to the scheduler/executor.

## Current Architecture

### 1. `ModalExecutor` (`src/modalflow/executor/modal_executor.py`)
The core executor logic. It handles:
- **Task Execution**: Spawns Modal functions for each task instance.
- **Networking (Critical)**:
  - **Local Development**: Automatically detects if Airflow is running locally (`localhost:8080`). If so, it creates a **reverse tunnel** using `modal.forward(8080)` to expose the local Airflow API to the internet-facing Modal Functions.
  - **Production/VPC**: Relies on the `AIRFLOW__CORE__EXECUTION_API_SERVER_URL` environment variable to tell Modal Functions where to find the Airflow API.
- **Lifecycle Management**:
  - `start()`: Initializes the connection and sets up the tunnel (if local).
  - `end()`: Cleans up the tunnel and resources.
  - `execute_async()`: Spawns the task, injecting the correct `AIRFLOW__CORE__EXECUTION_API_SERVER_URL`.

### 2. System Testing Infrastructure (`tests/system/`)
We have migrated from `breeze` to the **Astronomer CLI (`astro`)** running inside a **Modal Sandbox**.
- **Goal**: Simulate a realistic local Airflow environment within Modal's infrastructure for testing.
- **`tests/system/test_app.py`**:
  - Defines the `modal.App` used for testing.
  - Defines the `astro_test_image`: **Note**: The user recently updated this to use `brew` for installing `astro`. Verify if `brew` is available in the base image or needs to be installed first.
  - `create_test_sandbox()`: Mounts the project code.
  - `setup_astro_project()`: Inits an Astro project and installs the `modalflow` plugin.
  - `start_astro_dev()`: Runs `astro dev start`.
- **`Makefile`**:
  - `make system.setup`: Builds the package and deploys the test app.
  - `make system.test`: Runs the system tests in the sandbox.

## Recent Changes & Decisions

### Networking Strategy
- **Problem**: Modal Functions (cloud) cannot reach `localhost:8080` (developer machine).
- **Solution**:
  - Used `modal.forward(8080)` within the Executor to tunnel traffic.
  - **Important**: The `modal.forward` context manager must be kept alive for the duration of the executor's life. We store `self._tunnel_context` and `self._tunnel` in `__init__` and manage them in `start()`/`end()`.

### Test Runner Migration
- Removed `breeze` dependency to simplify CI/CD and local testing.
- The test runner is now a self-contained Modal App.
- `src/modalflow/modal_app.py` was cleaned up to remove test-specific images; these now live in `tests/system/test_app.py`.

## Next Steps for the Next Agent

1. **Verify Test Image**: The user recently changed `astro_test_image` in `tests/system/test_app.py` to use `brew`. Since the base image is `debian_slim`, ensure `brew` is actually installed or revert to `npm` if that fails.
2. **Run System Tests**: Execute `make system.setup` and `make system.test` to validate the networking and `astro` integration work end-to-end.
3. **Documentation**: Ensure `README.md` instructions for "Networking Configuration" match the final implementation (they currently do, but keep an eye on drift).

## Key Files
- `src/modalflow/executor/modal_executor.py`: Main logic.
- `tests/system/test_app.py`: Test environment definition.
- `Makefile`: Entry points for testing.
- `PLAN.md`: Historical context and detailed phase breakdown.
