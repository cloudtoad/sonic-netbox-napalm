# Feature: Config Save Versioning

## Overview

Automatically maintain a history of `config_db.json` versions. Every `config save` and `config replace` operation rotates the existing config file into a numbered backup (`.001`, `.002`, etc.) before writing the new version. Enables simple rollback to any previous configuration.

## Motivation

SONiC currently overwrites `config_db.json` on every save with no history. If a bad configuration is saved, the only recovery is manual — operators must maintain their own backups. This is error-prone, especially in automated workflows where config changes happen frequently.

Other network operating systems (Junos, IOS-XR, EOS) maintain configuration history natively. SONiC should too.

## Requirements

1. On every `config save`, rotate the current `config_db.json` to `config_db.json.001` before writing the new version
2. Existing backups are incremented: `.001` → `.002`, `.002` → `.003`, etc.
3. Configurable maximum number of backups retained (default: 10)
4. `config rollback` accepts an optional version number to restore from a specific backup
5. `config history` command lists available backups with timestamps
6. Works correctly in multi-ASIC environments
7. Applies to both `config save` and `config replace` operations

## CLI

```
admin@switch:~$ sudo config save
Saving current config to /etc/sonic/config_db.json.001
Configuration saved.

admin@switch:~$ sudo config history
Version  Timestamp             Size
-------  --------------------  --------
.001     2026-03-29 18:30:00   39.2 KB
.002     2026-03-29 16:15:00   38.8 KB
.003     2026-03-29 14:00:00   38.8 KB

admin@switch:~$ sudo config rollback 2
Rolling back to config_db.json.002 (2026-03-29 16:15:00)...
Config replaced successfully.

admin@switch:~$ sudo config rollback
Rolling back to config_db.json.001 (2026-03-29 18:30:00)...
Config replaced successfully.
```

## Implementation

### Files Modified

1. `src/sonic-utilities/config/main.py` — modify `save()`, add `history()` and version-aware `rollback()`

### Config File Rotation

```
Before save:
  config_db.json       (current running - about to be overwritten)
  config_db.json.001   (previous save)
  config_db.json.002   (two saves ago)

After save:
  config_db.json       (new - just saved)
  config_db.json.001   (was config_db.json - the one just replaced)
  config_db.json.002   (was .001)
  config_db.json.003   (was .002)
```

### Settings

Maximum backup count configurable via `DEVICE_METADATA.localhost.config_backup_count` in config_db. Default: 10.
