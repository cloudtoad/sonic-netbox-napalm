"""
Config checkpoint versioning for SONiC.

Maintains a history of full device configuration snapshots including
config_db.json and all FRR daemon configs. Every config save or replace
creates a numbered checkpoint archive before writing the new config.

Checkpoint format: tar.gz containing:
  config_db.json        - Config DB export
  frr/bgpd.conf         - BGP daemon config
  frr/zebra.conf         - Zebra config
  frr/ospfd.conf         - OSPF daemon config
  frr/pimd.conf          - PIM daemon config
  frr/staticd.conf       - Static routes config
  frr/bfdd.conf          - BFD daemon config
  metadata.json          - Timestamp, trigger, SONiC version

Usage:
    config save          # Creates checkpoint, then saves
    config checkpoint    # Creates checkpoint without saving
    config history       # Lists available checkpoints
    config rollback [N]  # Restores checkpoint N (default: most recent)
"""

import glob
import json
import os
import tarfile
import tempfile
import time
from io import BytesIO

import click

# Defaults
DEFAULT_MAX_CHECKPOINTS = 10
DEFAULT_CONFIG_DB_FILE = "/etc/sonic/config_db.json"
DEFAULT_FRR_DIR = "/etc/sonic/frr"
DEFAULT_CHECKPOINT_DIR = "/etc/sonic/checkpoints"

# FRR daemon config files to include in checkpoints
FRR_CONF_FILES = [
    "bgpd.conf",
    "zebra.conf",
    "ospfd.conf",
    "pimd.conf",
    "staticd.conf",
    "bfdd.conf",
    "vtysh.conf",
]


def get_max_checkpoints(db):
    """Get the configured maximum number of checkpoints.

    Reads from DEVICE_METADATA|localhost|config_checkpoint_count.
    Falls back to DEFAULT_MAX_CHECKPOINTS if not configured.
    """
    try:
        metadata = db.get_entry("DEVICE_METADATA", "localhost")
        return int(metadata.get("config_checkpoint_count", DEFAULT_MAX_CHECKPOINTS))
    except Exception:
        return DEFAULT_MAX_CHECKPOINTS


def get_sonic_version():
    """Get the current SONiC version string."""
    try:
        from sonic_py_common import device_info
        version_info = device_info.get_sonic_version_info()
        return version_info.get("build_version", "unknown")
    except Exception:
        return "unknown"


def create_checkpoint(
    trigger="manual",
    config_db_file=DEFAULT_CONFIG_DB_FILE,
    frr_dir=DEFAULT_FRR_DIR,
    checkpoint_dir=DEFAULT_CHECKPOINT_DIR,
    max_checkpoints=DEFAULT_MAX_CHECKPOINTS,
):
    """Create a checkpoint archive of the current device configuration.

    Rotates existing checkpoints (.001 -> .002, etc.) and creates a new .001
    containing config_db.json, FRR daemon configs, and metadata.

    Args:
        trigger: What caused this checkpoint (e.g., "config save", "config replace")
        config_db_file: Path to config_db.json
        frr_dir: Path to FRR config directory
        checkpoint_dir: Directory to store checkpoint archives
        max_checkpoints: Maximum number of checkpoints to retain

    Returns:
        Path to the new checkpoint archive, or None on failure.
    """
    os.makedirs(checkpoint_dir, exist_ok=True)

    base_name = os.path.join(checkpoint_dir, "checkpoint")

    # Find existing checkpoints
    existing = []
    for path in glob.glob(f"{base_name}.*.tar.gz"):
        basename = os.path.basename(path)
        try:
            version = int(basename.split(".")[1])
            existing.append((version, path))
        except (ValueError, IndexError):
            continue

    existing.sort(key=lambda x: x[0], reverse=True)

    # Remove oldest checkpoints exceeding the limit.
    # existing is sorted descending: highest version = oldest backup.
    while len(existing) >= max_checkpoints:
        _, oldest_path = existing.pop(0)
        try:
            os.remove(oldest_path)
        except OSError:
            pass

    # Rotate: .002 -> .003, .001 -> .002, etc. (highest first to avoid collisions)
    for version, path in existing:
        new_path = f"{base_name}.{version + 1:03d}.tar.gz"
        try:
            os.rename(path, new_path)
        except OSError as e:
            click.echo(f"Warning: failed to rotate {path}: {e}")

    # Build the new checkpoint archive
    checkpoint_path = f"{base_name}.001.tar.gz"
    metadata = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "trigger": trigger,
        "sonic_version": get_sonic_version(),
        "config_db_file": config_db_file,
        "frr_dir": frr_dir,
    }

    try:
        with tarfile.open(checkpoint_path, "w:gz") as tar:
            # Add config_db.json
            if os.path.exists(config_db_file):
                tar.add(config_db_file, arcname="config_db.json")

            # Add FRR daemon configs
            for conf_file in FRR_CONF_FILES:
                conf_path = os.path.join(frr_dir, conf_file)
                if os.path.exists(conf_path):
                    tar.add(conf_path, arcname=f"frr/{conf_file}")

            # Add metadata
            meta_bytes = json.dumps(metadata, indent=2).encode("utf-8")
            meta_info = tarfile.TarInfo(name="metadata.json")
            meta_info.size = len(meta_bytes)
            meta_info.mtime = time.time()
            tar.addfile(meta_info, BytesIO(meta_bytes))

        return checkpoint_path

    except Exception as e:
        click.echo(f"Error creating checkpoint: {e}")
        return None


def list_checkpoints(checkpoint_dir=DEFAULT_CHECKPOINT_DIR):
    """List available checkpoints with metadata.

    Returns:
        List of dicts: version, path, timestamp, trigger, sonic_version, size
    """
    base_name = os.path.join(checkpoint_dir, "checkpoint")
    checkpoints = []

    for path in glob.glob(f"{base_name}.*.tar.gz"):
        basename = os.path.basename(path)
        try:
            version = int(basename.split(".")[1])
        except (ValueError, IndexError):
            continue

        stat = os.stat(path)
        entry = {
            "version": version,
            "path": path,
            "size": stat.st_size,
            "timestamp": "",
            "trigger": "",
            "sonic_version": "",
        }

        # Read metadata from the archive
        try:
            with tarfile.open(path, "r:gz") as tar:
                meta_member = tar.getmember("metadata.json")
                meta_file = tar.extractfile(meta_member)
                if meta_file:
                    metadata = json.loads(meta_file.read())
                    entry["timestamp"] = metadata.get("timestamp", "")
                    entry["trigger"] = metadata.get("trigger", "")
                    entry["sonic_version"] = metadata.get("sonic_version", "")
        except Exception:
            entry["timestamp"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)
            )

        checkpoints.append(entry)

    checkpoints.sort(key=lambda x: x["version"])
    return checkpoints


def extract_checkpoint(
    version=1,
    checkpoint_dir=DEFAULT_CHECKPOINT_DIR,
    extract_dir=None,
):
    """Extract a checkpoint archive to a temporary directory.

    Args:
        version: Checkpoint version number (1 = most recent)
        checkpoint_dir: Directory containing checkpoint archives
        extract_dir: Directory to extract to (default: tempdir)

    Returns:
        Path to the extraction directory, or None on failure.
    """
    checkpoint_path = os.path.join(
        checkpoint_dir, f"checkpoint.{version:03d}.tar.gz"
    )

    if not os.path.exists(checkpoint_path):
        return None

    if extract_dir is None:
        extract_dir = tempfile.mkdtemp(prefix="sonic_checkpoint_")

    try:
        with tarfile.open(checkpoint_path, "r:gz") as tar:
            safe_members = []
            for member in tar.getmembers():
                # Prevent path traversal
                if member.name.startswith("/") or ".." in member.name:
                    continue
                if member.name in ("config_db.json", "metadata.json"):
                    safe_members.append(member)
                elif (
                    member.name.startswith("frr/")
                    and member.name.split("/")[-1] in FRR_CONF_FILES
                ):
                    safe_members.append(member)

            tar.extractall(path=extract_dir, members=safe_members)
        return extract_dir
    except Exception as e:
        click.echo(f"Error extracting checkpoint: {e}")
        return None


def format_size(size_bytes):
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# CLI commands — add to config group in main.py
# ---------------------------------------------------------------------------
#
# Integration points in existing code:
#
# 1. In save() — before writing config_db.json:
#      from config.config_versioning import create_checkpoint
#      create_checkpoint(trigger="config save")
#
# 2. In replace() — before applying:
#      from config.config_versioning import create_checkpoint
#      create_checkpoint(trigger="config replace")
#

try:
    from config.main import config as _config
except ImportError:
    _config = click.Group(name="config")


@_config.command()
@click.option('-t', '--trigger', default='manual', help='Trigger description for metadata')
def checkpoint(trigger):
    """Create a checkpoint of the current configuration.

       Saves config_db.json and all FRR daemon configs into a versioned
       archive at /etc/sonic/checkpoints/checkpoint.001.tar.gz.
    """
    path = create_checkpoint(trigger=trigger)
    if path:
        click.secho(f"Checkpoint created: {path}", fg="green")
    else:
        click.secho("Failed to create checkpoint.", fg="red", err=True)
        raise SystemExit(1)


@_config.command('history')
def history():
    """Show configuration checkpoint history.

       Lists available checkpoints with version, timestamp, trigger, and size.
    """
    checkpoints = list_checkpoints()

    if not checkpoints:
        click.echo("No checkpoints found.")
        return

    click.echo(
        f"{'Version':<10} {'Timestamp':<22} {'Trigger':<18} "
        f"{'SONiC Version':<14} {'Size':<10}"
    )
    click.echo(
        f"{'-------':<10} {'--------------------':<22} {'--------':<18} "
        f"{'-------':<14} {'------':<10}"
    )

    for cp in checkpoints:
        version_str = f".{cp['version']:03d}"
        size_str = format_size(cp["size"])
        click.echo(
            f"{version_str:<10} {cp['timestamp']:<22} {cp['trigger']:<18} "
            f"{cp['sonic_version']:<14} {size_str:<10}"
        )

    click.echo(f"\n{len(checkpoints)} checkpoint(s) available.")


@_config.command('rollback')
@click.argument('version', required=False, default=1, type=int)
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompt')
@click.pass_context
def rollback(ctx, version, yes):
    """Rollback to a previous configuration checkpoint.

       Restores config_db.json via 'config replace' and reloads FRR daemon
       configs from the checkpoint archive.

       <version>: Checkpoint version number (default: 1 = most recent).

       Examples:
           config rollback       # Restore most recent checkpoint
           config rollback 3     # Restore third most recent checkpoint
    """
    import shutil
    import subprocess

    checkpoints = list_checkpoints()
    cp = next((c for c in checkpoints if c["version"] == version), None)
    if not cp:
        click.secho(f"Checkpoint version {version} not found.", fg="red", err=True)
        if checkpoints:
            versions = [str(c["version"]) for c in checkpoints]
            click.echo(f"Available versions: {', '.join(versions)}")
        raise SystemExit(1)

    click.echo(
        f"Rolling back to checkpoint .{version:03d} "
        f"({cp['timestamp']}, {cp['trigger']}, {format_size(cp['size'])})"
    )

    if not yes:
        click.confirm("Proceed with rollback?", abort=True)

    # Create a checkpoint of CURRENT config before rolling back
    click.echo("Creating checkpoint of current config before rollback...")
    create_checkpoint(trigger=f"pre-rollback (restoring .{version:03d})")

    # Extract the target checkpoint
    extract_dir = extract_checkpoint(version)
    if not extract_dir:
        click.secho("Failed to extract checkpoint.", fg="red", err=True)
        raise SystemExit(1)

    try:
        # Restore config_db.json via config replace
        config_db_path = os.path.join(extract_dir, "config_db.json")
        if os.path.exists(config_db_path):
            click.echo("Restoring config_db.json...")
            try:
                from generic_config_updater.generic_updater import GenericUpdater
                from generic_config_updater.gu_common import ConfigFormat

                with open(config_db_path, "r") as f:
                    target_config = json.load(f)

                GenericUpdater().replace(target_config, ConfigFormat.CONFIGDB)
                click.secho("  config_db.json restored.", fg="cyan")
            except Exception as ex:
                click.secho(
                    f"  config_db.json restore failed: {ex}", fg="red", err=True
                )

        # Restore FRR configs
        frr_extract_dir = os.path.join(extract_dir, "frr")
        if os.path.isdir(frr_extract_dir):
            click.echo("Restoring FRR daemon configs...")
            for conf_file in FRR_CONF_FILES:
                src = os.path.join(frr_extract_dir, conf_file)
                dst = os.path.join(DEFAULT_FRR_DIR, conf_file)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    click.echo(f"  Restored {conf_file}")

            # Reload FRR to pick up restored configs
            click.echo("Reloading FRR daemons...")
            result = subprocess.run(
                ["docker", "exec", "bgp", "supervisorctl", "restart", "all"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                click.secho("  FRR daemons reloaded.", fg="cyan")
            else:
                click.secho(
                    f"  FRR reload warning: {result.stderr.strip()}", fg="yellow"
                )

        click.secho("Rollback complete.", fg="green", underline=True)

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
