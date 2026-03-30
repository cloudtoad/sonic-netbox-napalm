"""
Config save versioning for SONiC.

Patches to apply to src/sonic-utilities/config/main.py.
These functions handle config file rotation and history management.
"""

import glob
import json
import os
import time

import click

# Default maximum number of config backups to retain
DEFAULT_MAX_BACKUPS = 10
DEFAULT_CONFIG_DB_FILE = "/etc/sonic/config_db.json"


def get_max_backups(db):
    """Get the configured maximum number of config backups.

    Reads from DEVICE_METADATA|localhost|config_backup_count.
    Falls back to DEFAULT_MAX_BACKUPS if not configured.
    """
    try:
        metadata = db.get_entry("DEVICE_METADATA", "localhost")
        return int(metadata.get("config_backup_count", DEFAULT_MAX_BACKUPS))
    except Exception:
        return DEFAULT_MAX_BACKUPS


def rotate_config_file(filepath, max_backups=DEFAULT_MAX_BACKUPS):
    """Rotate config file into numbered backups before overwriting.

    config_db.json       -> config_db.json.001
    config_db.json.001   -> config_db.json.002
    config_db.json.002   -> config_db.json.003
    ...

    Removes backups exceeding max_backups count.

    Args:
        filepath: Path to the config file (e.g., /etc/sonic/config_db.json)
        max_backups: Maximum number of backup versions to retain

    Returns:
        Path to the new .001 backup, or None if no rotation was needed.
    """
    if not os.path.exists(filepath):
        return None

    # Find existing backups and sort by version number descending
    backup_pattern = f"{filepath}.*"
    existing = []
    for path in glob.glob(backup_pattern):
        suffix = path[len(filepath) + 1:]
        try:
            version = int(suffix)
            existing.append((version, path))
        except ValueError:
            continue

    existing.sort(key=lambda x: x[0], reverse=True)

    # Remove the oldest backups if at the limit.
    # existing is sorted descending by version: [(3,.003), (2,.002), (1,.001)]
    # The highest version number is the oldest backup.
    while len(existing) >= max_backups:
        _, oldest_path = existing.pop(0)  # highest version = oldest backup
        try:
            os.remove(oldest_path)
        except OSError:
            pass

    # Increment existing backups: .002 -> .003, .001 -> .002, etc.
    for version, path in existing:
        new_version = version + 1
        new_path = f"{filepath}.{new_version:03d}"
        try:
            os.rename(path, new_path)
        except OSError as e:
            click.echo(f"Warning: failed to rotate {path} -> {new_path}: {e}")

    # Move current config to .001
    backup_path = f"{filepath}.001"
    try:
        os.rename(filepath, backup_path)
        click.echo(f"Saved current config to {backup_path}")
        return backup_path
    except OSError as e:
        click.echo(f"Warning: failed to backup {filepath}: {e}")
        return None


def list_config_history(filepath):
    """List available config backups with timestamps and sizes.

    Returns:
        List of dicts with keys: version, path, timestamp, size
    """
    backup_pattern = f"{filepath}.*"
    history = []
    for path in glob.glob(backup_pattern):
        suffix = path[len(filepath) + 1:]
        try:
            version = int(suffix)
        except ValueError:
            continue

        stat = os.stat(path)
        history.append({
            "version": version,
            "path": path,
            "timestamp": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
            ),
            "size": stat.st_size,
        })

    history.sort(key=lambda x: x["version"])
    return history


def format_size(size_bytes):
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# CLI commands to add to config group in main.py
# ---------------------------------------------------------------------------
#
# Modify the existing save() function to call rotate_config_file() before
# writing the new config. Insert this before the sonic-cfggen call:
#
#     max_backups = get_max_backups(db)
#     rotate_config_file(file, max_backups)
#
# The click commands below use a local stub for standalone testing.
# In production, replace `_config` with the real `config` group from main.py.

try:
    from config.main import config as _config
except ImportError:
    _config = click.Group(name="config")


# New: config history command
@_config.command()
@click.argument('filename', required=False)
def history(filename):
    """Show config_db.json backup history.

       Lists available configuration backups with version number,
       timestamp, and file size.
    """
    filepath = filename or DEFAULT_CONFIG_DB_FILE
    backups = list_config_history(filepath)

    if not backups:
        click.echo("No configuration backups found.")
        return

    # Header
    click.echo(f"{'Version':<10} {'Timestamp':<22} {'Size':<10}")
    click.echo(f"{'-------':<10} {'--------------------':<22} {'--------':<10}")

    for backup in backups:
        version_str = f".{backup['version']:03d}"
        size_str = format_size(backup['size'])
        click.echo(f"{version_str:<10} {backup['timestamp']:<22} {size_str:<10}")

    click.echo(f"\n{len(backups)} backup(s) available.")


# New: config rollback command (version-aware)
# This extends the existing rollback behavior to support version numbers.
@_config.command('rollback')
@click.argument('version', required=False, default=1, type=int)
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation prompt')
@click.argument('filename', required=False)
@click.pass_context
def rollback_version(ctx, version, yes, filename):
    """Rollback to a previous configuration version.

       <version>: Backup version number to restore (default: 1, most recent).

       Examples:
           config rollback       # Restore most recent backup (.001)
           config rollback 3     # Restore third most recent backup (.003)
    """
    filepath = filename or DEFAULT_CONFIG_DB_FILE
    backup_path = f"{filepath}.{version:03d}"

    if not os.path.exists(backup_path):
        click.secho(
            f"Backup version {version} not found: {backup_path}",
            fg="red", err=True,
        )
        # Show available versions
        backups = list_config_history(filepath)
        if backups:
            versions = [str(b['version']) for b in backups]
            click.echo(f"Available versions: {', '.join(versions)}")
        raise SystemExit(1)

    # Show what we're restoring
    stat = os.stat(backup_path)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
    click.echo(f"Rolling back to {backup_path} ({timestamp}, {format_size(stat.st_size)})")

    if not yes:
        click.confirm("Proceed with rollback?", abort=True)

    # Use existing config replace machinery
    try:
        from generic_config_updater.generic_updater import GenericUpdater
        from generic_config_updater.gu_common import ConfigFormat

        with open(backup_path, 'r') as f:
            target_config = json.load(f)

        GenericUpdater().replace(target_config, ConfigFormat.CONFIGDB)
        click.secho("Config rolled back successfully.", fg="cyan", underline=True)
    except Exception as ex:
        click.secho(f"Rollback failed: {ex}", fg="red", err=True)
        ctx.fail(ex)
