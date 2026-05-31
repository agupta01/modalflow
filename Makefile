.PHONY: build system.setup system.setup.volume system.test system.test.e2e system.test.e2e.volume system.teardown unit.test

build:
	@uv build

# DAG volume name used by the per-task execution sandboxes.
DAG_VOLUME_NAME := test-dags-vol

# Set up the Sandbox for system tests (volume-only DAG loading)
# This builds the package wheel needed by the Sandbox image and pushes the
# test DAGs to a Modal Volume that the executor mounts into each task sandbox.
# There is no longer a deploy step or baked-DAG mode.
system.setup:
	@echo "Setting up system test Sandbox..."
	@echo "Building package..."
	@uv build
	@chmod 644 dist/*.whl
	@echo "Pushing test DAGs to volume '$(DAG_VOLUME_NAME)'..."
	@uv run modalflow sync --dags-path tests/system/dags --dags-volume $(DAG_VOLUME_NAME)
	@echo "Setup complete. Run 'make system.test.e2e' to start tests."

# Set up system tests with volume-based DAG loading.
# Identical to system.setup (volume-only flow); kept as a separate target so
# 'make system.test.e2e.volume' has a matching setup entry point.
system.setup.volume: system.setup
	@echo "Volume setup complete. Run 'make system.test.e2e.volume' to start tests."

# Run system tests using airflow standalone in Modal Sandbox
system.test:
	@echo "Starting airflow standalone in Sandbox..."
	@echo "Note: This will create a Sandbox and start Airflow with ModalExecutor."
	@echo "Output will be streamed to stdout."
	@uv run python -c "\
import sys; \
sys.path.insert(0, 'tests/system'); \
from test_app import run_test; \
run_test(); \
"

# Run E2E system tests via pytest
system.test.e2e:
	@echo "Running E2E system tests..."
	@uv run pytest tests/system/test_app.py -v -x --timeout=600

# Run E2E tests for volume-based DAG loading
system.test.e2e.volume:
	@echo "Running volume DAG E2E tests..."
	@uv run pytest tests/system/test_volume_dags.py -v -x --timeout=600

# Alternative: Use modal CLI to run commands in Sandbox
system.test.modal:
	@echo "Starting airflow standalone via modal run..."
	@uv run modal run tests/system/test_app.py::start_airflow

# Teardown: Terminate the Sandbox
system.teardown:
	@echo "Terminating system test Sandbox..."
	@uv run python -c "\
import modal; \
sb = modal.Sandbox.from_name('modalflow-test-runner', 'modalflow-system-runner'); \
sb.terminate(); \
" || echo "Sandbox not found or already terminated"

# Run unit tests with pytest
unit.test:
	uv run pytest
