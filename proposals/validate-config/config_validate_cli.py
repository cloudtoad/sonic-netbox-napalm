"""
CLI addition for `config validate` command.

Add this to src/sonic-utilities/config/main.py after the `save` command.

Usage:
    admin@switch:~$ sudo config validate /tmp/candidate_config.json
"""

import json
import click
from sonic_py_common import device_info
import utilities_common.cli as clicommon


# Add to config group in main.py:

@config.command()
@click.argument('filename', required=True, type=click.Path(exists=True))
@click.option('-v', '--verbose', is_flag=True, default=False,
              help='Print full error details including constraints')
def validate(filename, verbose):
    """Validate a config_db.json file against YANG models without applying it.

       Reports all validation errors found in the configuration.

       <filename>: Path to the config_db.json file to validate.
    """
    try:
        with open(filename, 'r') as f:
            config_data = json.load(f)
    except json.JSONDecodeError as e:
        click.secho(f"Invalid JSON: {e}", fg="red", err=True)
        raise SystemExit(1)
    except Exception as e:
        click.secho(f"Failed to read file: {e}", fg="red", err=True)
        raise SystemExit(1)

    click.echo("Validating configuration...")

    # Use sonic_yang for local validation
    try:
        from sonic_yang import SonicYang
        yang_dir = str(device_info.get_path_to_yang_models())

        sy = SonicYang(yang_dir)
        sy.loadYangModel()

        # loadData validates the config against YANG models.
        # If it raises, the config is invalid.
        sy.loadData(config_data)

        click.secho("Configuration is valid.", fg="green")

    except Exception as e:
        error_msg = str(e)
        click.secho(f"Validation failed:", fg="red", err=True)

        # Parse and display errors in a structured way
        # sonic_yang raises SonicYangException with details
        for line in error_msg.split('\n'):
            line = line.strip()
            if not line:
                continue
            if verbose:
                click.secho(f"  {line}", fg="yellow", err=True)
            else:
                # Show a condensed version
                click.secho(f"  {line}", fg="yellow", err=True)

        raise SystemExit(1)
