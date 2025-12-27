.PHONY: build system.setup system.teardown unit.test

build:
	@uv build

# Set up and start the Astro dev environment for system tests
system.setup:
	@echo "Setting up system test runner..."
	@if [ ! -d "tests/system-runner" ]; then \
		echo "Cloning airflow test repo into tests/system-runner..."; \
		git clone git@github.com:agupta01/airflow.git tests/system-runner; \
	else \
		echo "tests/system-runner already exists, skipping clone."; \
	fi
	@echo "Installing dev breeze environment with uv tool..."
	@uv tool install -e ./tests/system-runner/dev/breeze --force
	@echo "Copying built package to system test folder. To update the package, run 'uv build'."
	@mkdir -p tests/system-runner/files/plugins
	@mkdir -p tests/system-runner/files/airflow-breeze-config
	@cp -r dist/*.whl tests/system-runner/files/plugins
	@cp tests/system/requirements.txt tests/system-runner/files/requirements.txt
	@cp tests/system/init.sh tests/system-runner/files/airflow-breeze-config/init.sh
	@cp tests/system/environment_variables.env tests/system-runner/files/airflow-breeze-config/environment_variables.env

system.teardown:
	@echo "Stopping and cleaning up system test runner environment..."
	@cd tests/system-runner/dev/breeze && breeze down || true
	@echo "Removing runner directory..."
	@rm -rf tests/system-runner

# Run unit tests with pytest
unit.test:
	uv run pytest
