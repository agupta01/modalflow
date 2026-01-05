.PHONY: build system.setup system.test system.teardown unit.test

build:
	@uv build

# Set up the Sandbox for system tests
# This prepares files and creates/updates the Modal Sandbox
system.setup:
	@echo "Setting up system test Sandbox..."
	@echo "Building package..."
	@uv build
	@echo "Setting up Sandbox with astro CLI..."
	@uv run python -m modal deploy tests/system/test_app.py
	@echo "Sandbox setup complete. Run 'make system.test' to start tests."

# Run system tests using astro CLI in Modal Sandbox
system.test:
	@echo "Starting astro dev in Sandbox..."
	@echo "Note: This will create a Sandbox, set up astro project, and start Airflow."
	@echo "Output will be streamed to stdout."
	@uv run python -c "\
import sys; \
sys.path.insert(0, 'tests/system'); \
from test_app import create_test_sandbox, setup_astro_project, start_astro_dev; \
sb = create_test_sandbox(); \
setup_astro_project(sb); \
start_astro_dev(sb); \
"

# Alternative: Use modal CLI to run commands in Sandbox
# This approach uses modal run to execute commands
system.test.modal:
	@echo "Starting astro dev via modal run..."
	@uv run modal run tests/system/test_app.py::start_astro_dev

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
