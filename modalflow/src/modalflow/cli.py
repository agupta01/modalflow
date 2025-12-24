import click
import os
import subprocess
import sys

@click.group()
def cli():
    """Modalflow CLI."""
    pass

@cli.command()
@click.option("--env", default="main", help="Target environment name (default: main)")
def deploy(env):
    """
    Deploy the Modalflow application to the specified environment.
    
    This deploys the Modal App, Volume, and Dict defined in modal_app.py.
    """
    click.echo(f"Deploying Modalflow to environment: '{env}'...")
    
    # Set the environment variable to ensure modal_app.py picks up the correct name
    env_vars = os.environ.copy()
    env_vars["MODALFLOW_ENV"] = env
    
    # Run 'modal deploy' targeting the modal_app module
    # We use sys.executable -m modal to ensure we use the same python environment
    cmd = [sys.executable, "-m", "modal", "deploy", "modalflow.modal_app"]
    
    try:
        subprocess.run(
            cmd,
            env=env_vars,
            check=True
        )
        click.echo(f"Successfully deployed to environment '{env}'!")
    except subprocess.CalledProcessError as e:
        click.echo(f"Deployment failed with exit code {e.returncode}.", err=True)
        sys.exit(e.returncode)
    except Exception as e:
        click.echo(f"An error occurred: {e}", err=True)
        sys.exit(1)

if __name__ == "__main__":
    cli()
