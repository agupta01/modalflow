.PHONY: build system.setup system.test system.test.e2e system.teardown unit.test

build:
	@uv build

# Set up the Sandbox for system tests
# This builds the package wheel needed by the Sandbox image
system.setup:
	@echo "Setting up system test Sandbox..."
	@echo "Building package..."
	@uv build
	@chmod 644 dist/*.whl
	@echo "Deploying modalflow-main (with test DAGs)..."
	@MODALFLOW_DAGS_DIR=tests/system/dags uv run modal deploy src/modalflow/modal_app.py
	@echo "Setup complete. Run 'make system.test.e2e' to start tests."

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
	@uv run pytest tests/system/ -v -x --timeout=600

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
