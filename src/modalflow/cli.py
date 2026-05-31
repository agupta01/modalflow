import click
from pathlib import Path


@click.group()
def cli():
    """Modalflow CLI."""
    pass


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
    Sync local DAG files to a Modal Volume.

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
