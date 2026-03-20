import click
import os
import subprocess
import sys
from pathlib import Path

DAGS_SOURCE_CHOICES = click.Choice(["local", "volume", "cloud-bucket"])


@click.group()
def cli():
    """Modalflow CLI."""
    pass


@cli.command()
@click.option("--env", default="main", help="Target environment name (default: main)")
@click.option(
    "--dags-source",
    type=DAGS_SOURCE_CHOICES,
    required=True,
    help="DAG source mode: local (bake into image), volume (Modal Volume), cloud-bucket (S3)",
)
@click.option(
    "--dags-path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Local directory containing DAG files (required for local and volume modes)",
)
@click.option(
    "--dags-volume",
    help="Modal Volume name for DAG storage (required for volume mode)",
)
@click.option(
    "--dags-bucket",
    help="S3 URI for DAG storage, e.g. s3://bucket/prefix (required for cloud-bucket mode)",
)
@click.option(
    "--dags-bucket-secret",
    help="Modal Secret name with cloud credentials (required for cloud-bucket mode)",
)
@click.option(
    "--airflow-version",
    default="3.1.5",
    show_default=True,
    help="Apache Airflow version to install in the Modal function image",
)
def deploy(env, dags_source, dags_path, dags_volume, dags_bucket, dags_bucket_secret, airflow_version):
    """
    Deploy the Modalflow application to the specified environment.

    This deploys the Modal App, Volume, and Dict defined in modal_app.py.
    """
    # Validate options per mode
    if dags_source == "local":
        if not dags_path:
            raise click.UsageError("--dags-path is required when --dags-source is 'local'")
    elif dags_source == "volume":
        if not dags_path:
            raise click.UsageError("--dags-path is required when --dags-source is 'volume'")
        if not dags_volume:
            raise click.UsageError("--dags-volume is required when --dags-source is 'volume'")
    elif dags_source == "cloud-bucket":
        if not dags_bucket:
            raise click.UsageError("--dags-bucket is required when --dags-source is 'cloud-bucket'")
        if not dags_bucket_secret:
            raise click.UsageError(
                "--dags-bucket-secret is required when --dags-source is 'cloud-bucket'"
            )

    click.echo(f"Deploying Modalflow to environment: '{env}' (dags-source: {dags_source})...")

    # Build environment variables for modal deploy
    env_vars = os.environ.copy()
    env_vars["MODALFLOW_ENV"] = env
    env_vars["MODALFLOW_AIRFLOW_VERSION"] = airflow_version

    if dags_source == "local":
        env_vars["MODALFLOW_DAGS_DIR"] = dags_path
    elif dags_source == "volume":
        env_vars["MODALFLOW_DAGS_VOLUME"] = dags_volume
    elif dags_source == "cloud-bucket":
        env_vars["MODALFLOW_DAGS_BUCKET"] = dags_bucket
        env_vars["MODALFLOW_DAGS_BUCKET_SECRET"] = dags_bucket_secret

    # Run 'modal deploy' targeting the modal_app module
    cmd = [sys.executable, "-m", "modal", "deploy", "modalflow.modal_app"]

    try:
        subprocess.run(cmd, env=env_vars, check=True)
        click.echo(f"Successfully deployed to environment '{env}'!")
    except subprocess.CalledProcessError as e:
        click.echo(f"Deployment failed with exit code {e.returncode}.", err=True)
        sys.exit(e.returncode)
    except Exception as e:
        click.echo(f"An error occurred: {e}", err=True)
        sys.exit(1)

    # For volume mode, upload DAGs after deploy
    if dags_source == "volume":
        click.echo(f"Uploading DAGs to volume '{dags_volume}'...")
        _sync_dags_to_volume(dags_path, dags_volume)
        click.echo("DAGs uploaded successfully!")


@cli.command()
@click.option(
    "--dags-path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    required=True,
    help="Local directory containing DAG files",
)
@click.option(
    "--dags-volume",
    required=True,
    help="Modal Volume name to upload DAGs to",
)
def sync(dags_path, dags_volume):
    """
    Sync local DAG files to a Modal Volume without redeploying.

    Performs a full replace: removes existing files from the volume,
    then uploads the local DAG directory.
    """
    click.echo(f"Syncing DAGs from '{dags_path}' to volume '{dags_volume}'...")
    _sync_dags_to_volume(dags_path, dags_volume)
    click.echo("DAGs synced successfully!")


def _sync_dags_to_volume(dags_path: str, volume_name: str) -> None:
    """Upload local DAG files to a Modal Volume (full replace).

    Uses the Modal Python SDK to upload files directly to the volume root,
    avoiding the directory-nesting behavior of ``modal volume put``.
    """
    import modal

    vol = modal.Volume.from_name(volume_name, create_if_missing=True)

    # Remove all existing entries so deleted local DAGs don't linger
    for entry in vol.listdir("/"):
        path = f"/{entry.path}"
        vol.remove_file(path, recursive=True)

    # Upload each file to the volume root, preserving relative paths
    local_dir = Path(dags_path)
    with vol.batch_upload(force=True) as batch:
        for local_file in local_dir.rglob("*"):
            if local_file.is_file():
                remote_path = "/" + str(local_file.relative_to(local_dir))
                batch.put_file(str(local_file), remote_path)


if __name__ == "__main__":
    cli()
