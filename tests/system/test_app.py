"""
Modal app for running system tests with astro CLI in a Sandbox.

This creates a Sandbox that runs astro CLI commands to set up and test
Airflow with the ModalExecutor.
"""
import os
from pathlib import Path

import modal

# Define the astro CLI image for testing
astro_test_image = modal.Image.debian_slim().run_commands(
    "brew install astronomer/tap/astro --without-podman",
)

# Create a separate app for testing
test_app = modal.App("modalflow-test-runner", image=astro_test_image)


def create_test_sandbox(
    system_test_dir: str = "/files/system",
    dist_dir: str = "/files/dist",
) -> modal.Sandbox:
    """
    Create a Sandbox for running astro CLI tests.
    
    Args:
        system_test_dir: Path where tests/system files will be mounted
        dist_dir: Path where dist/*.whl files will be mounted
        
    Returns:
        Configured Sandbox instance
    """
    # Mount the local test files from tests/system
    test_dir = Path(__file__).parent
    system_mount = modal.Mount.from_local_dir(
        local_path=str(test_dir),
        remote_path=system_test_dir,
    )
    
    # Mount dist directory with modalflow.whl
    # This will be set up by the Makefile
    dist_path = Path(__file__).parent.parent.parent / "dist"
    if dist_path.exists():
        dist_mount = modal.Mount.from_local_dir(
            local_path=str(dist_path),
            remote_path=dist_dir,
        )
        mounts = [system_mount, dist_mount]
    else:
        mounts = [system_mount]
    
    sb = modal.Sandbox.create(
        app=test_app,
        name="modalflow-system-runner",
        mounts=mounts,
        timeout=3600,  # 1 hour timeout
    )
    
    return sb


def setup_astro_project(sandbox: modal.Sandbox, workdir: str = "/workspace/system-runner") -> None:
    """
    Set up astro project in the Sandbox.
    
    Steps:
    1. Run `astro dev init` to create project in system-runner directory
    2. Copy modalflow.whl to plugins directory (following astro docs)
    3. Add environment variables to .env file
    """
    # Create workspace directory
    sandbox.exec("mkdir", "-p", workdir)
    
    # Initialize astro project
    # astro dev init creates a project structure
    sandbox.exec(
        "bash", "-c",
        f"cd {workdir} && astro dev init --name modalflow-test --no-prompt",
    )
    
    # Copy modalflow wheel to plugins directory
    # According to astro docs: https://astronomer.docs.buildwithfern.com/docs/astro/cli/develop-project#add-airflow-plugins
    # Plugins go in the plugins/ directory
    sandbox.exec(
        "bash", "-c",
        f"mkdir -p {workdir}/plugins && cp /files/dist/*.whl {workdir}/plugins/ 2>/dev/null || true",
    )
    
    # Copy environment variables to .env file
    # The .env file is created by astro dev init
    sandbox.exec(
        "bash", "-c",
        f"cat /files/system/environment_variables.env >> {workdir}/.env",
    )


def start_astro_dev(sandbox: modal.Sandbox, workdir: str = "/workspace/system-runner") -> None:
    """
    Start astro dev and stream output to stdout.
    
    This will run in the background and print output to terminal.
    """
    # Start astro dev - this runs in the foreground and streams output
    # The Makefile will handle running this and capturing output
    proc = sandbox.exec(
        "bash", "-c",
        f"cd {workdir} && astro dev start",
    )
    
    # Stream output
    for line in proc.stdout:
        print(line, end="")


if __name__ == "__main__":
    # This can be run as a script to test the Sandbox creation
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        sb = create_test_sandbox()
        setup_astro_project(sb)
        print("Astro project setup complete")
    elif len(sys.argv) > 1 and sys.argv[1] == "start":
        # For starting, we'd need to retrieve the sandbox by name
        # This is a simplified version - the Makefile will handle this properly
        sb = modal.Sandbox.from_name("modalflow-test-runner", "modalflow-system-runner")
        start_astro_dev(sb)
    else:
        print("Usage: python test_app.py [setup|start]")
