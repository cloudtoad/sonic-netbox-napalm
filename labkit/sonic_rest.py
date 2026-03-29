"""SONiC RESTCONF helpers."""

import base64
import json
import time

import requests

from . import log

# Defaults (can be overridden by caller)
READY_POLL_INTERVAL = 5
READY_POLL_TIMEOUT = 600


def sonic_get(ip: str, path: str, auth: tuple) -> requests.Response:
    return requests.get(
        f"https://{ip}/restconf/{path}",
        auth=auth, verify=False, timeout=10,
    )


def sonic_patch(ip: str, path: str, body: dict, auth: tuple) -> requests.Response:
    return requests.patch(
        f"https://{ip}/restconf/{path}",
        auth=auth, json=body,
        headers={"Content-Type": "application/yang-data+json"},
        verify=False, timeout=10,
    )


def sonic_post(ip: str, path: str, auth: tuple,
               body: dict | None = None,
               timeout: int = 10) -> requests.Response:
    kwargs = {
        "auth": auth,
        "headers": {"Content-Type": "application/yang-data+json"},
        "verify": False,
        "timeout": timeout,
    }
    if body is not None:
        kwargs["json"] = body
    return requests.post(f"https://{ip}/restconf/{path}", **kwargs)


def sonic_put(ip: str, path: str, body: dict, auth: tuple) -> requests.Response:
    return requests.put(
        f"https://{ip}/restconf/{path}",
        auth=auth, json=body,
        headers={"Content-Type": "application/yang-data+json"},
        verify=False, timeout=10,
    )


def check_disable_ztp(ip: str, auth: tuple) -> bool:
    """Check ZTP status; disable if enabled. Returns True if ZTP was disabled."""
    try:
        r = sonic_get(ip, "data/openconfig-ztp:ztp", auth)
        if r.status_code != 200:
            log(f"  {ip}: ZTP check returned HTTP {r.status_code}, skipping")
            return False
        data = r.json()
        ztp_config = data.get("openconfig-ztp:ztp", {}).get("config", {})
        admin_mode = ztp_config.get("admin-mode", False)
        if not admin_mode:
            log(f"  {ip}: ZTP already disabled")
            return False
    except Exception as e:
        log(f"  {ip}: ZTP check error: {e}")
        return False

    log(f"  {ip}: ZTP enabled, disabling...")
    try:
        r = sonic_patch(ip, "data/openconfig-ztp:ztp/config",
                        {"openconfig-ztp:config": {"admin-mode": False}}, auth)
        if r.status_code in (200, 204):
            log(f"  {ip}: ZTP disabled (containers will restart)")
            return True
        else:
            log(f"  {ip}: ZTP disable returned HTTP {r.status_code}: {r.text}")
            return False
    except Exception as e:
        log(f"  {ip}: ZTP disable error: {e}")
        return False


def poll_system_ready(ip: str, auth: tuple,
                      timeout: int = READY_POLL_TIMEOUT) -> bool:
    """Poll RESTCONF show-system-status until 'System is ready'."""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = sonic_post(ip, "operations/openconfig-system-rpc:show-system-status",
                           auth)
            if r.status_code == 200:
                detail = (r.json()
                          .get("openconfig-system-rpc:output", {})
                          .get("status-detail", []))
                if len(detail) >= 2 and "System is ready" in detail[1]:
                    return True
                if attempt % 6 == 1:
                    msg = detail[1] if len(detail) >= 2 else "unknown"
                    log(f"  {ip}: {msg}")
            else:
                if attempt % 6 == 1:
                    log(f"  {ip}: HTTP {r.status_code}")
        except requests.exceptions.ConnectionError:
            if attempt % 6 == 1:
                log(f"  {ip}: REST not up yet")
        except Exception as e:
            if attempt % 6 == 1:
                log(f"  {ip}: {e}")
        time.sleep(READY_POLL_INTERVAL)
    return False


def config_save(ip: str, auth: tuple) -> bool:
    """Save running config to startup (write memory) via RESTCONF copy RPC."""
    body = {
        "openconfig-file-mgmt-private:input": {
            "source": "running-configuration",
            "destination": "startup-configuration",
            "copy-config-option": "OVERWRITE",
        }
    }
    try:
        r = sonic_post(ip, "operations/openconfig-file-mgmt-private:copy",
                       auth, body=body, timeout=30)
        if r.status_code == 200:
            log(f"  {ip}: config saved (running -> startup)")
            return True
        log(f"  {ip}: config save returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: config save error: {e}")
        return False


def config_reload(ip: str, auth: tuple) -> bool:
    """Load startup config into running (config reload) via RESTCONF copy RPC.

    Uses OVERWRITE to fully replace running config with startup config.
    Services will restart as if the config was freshly loaded.
    """
    body = {
        "openconfig-file-mgmt-private:input": {
            "source": "startup-configuration",
            "destination": "running-configuration",
            "copy-config-option": "OVERWRITE",
        }
    }
    try:
        r = sonic_post(ip, "operations/openconfig-file-mgmt-private:copy",
                       auth, body=body, timeout=60)
        if r.status_code == 200:
            log(f"  {ip}: config loaded (startup -> running)")
            return True
        log(f"  {ip}: config reload returned HTTP {r.status_code}: {r.text}")
        return False
    except (requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout) as e:
        # Connection drop during config reload is expected — the reload
        # restarts REST and other services, killing our connection.
        log(f"  {ip}: config reload triggered (connection dropped, expected)")
        return True
    except Exception as e:
        log(f"  {ip}: config reload error: {e}")
        return False


def config_replace(ip: str, config: dict, auth: tuple) -> bool:
    """Push a full config_db.json: write to startup via SSH, then reboot.

    Flow:
    1. Write config JSON to /etc/sonic/config_db.json on the device (base64
       transfer via SSH to avoid shell escaping issues with large JSON)
    2. Reboot the switch so it loads the new config cleanly on next boot.

    Hot-reload (copy RPC OVERWRITE or ``config reload``) is avoided because
    it kills management connectivity — the OVERWRITE replaces the entire
    running config including the DHCP-derived management interface, and the
    switch becomes unreachable.  A clean reboot loads config_db.json from
    disk and re-acquires the management IP via DHCP.

    Returns True if the write + reboot command succeed.
    """
    from .ssh import ssh_cmd

    config_json = json.dumps(config, indent=4)
    b64 = base64.b64encode(config_json.encode()).decode()

    # Write config to startup via SSH using base64 to avoid shell escaping
    cmd = f"echo '{b64}' | base64 -d | sudo tee /etc/sonic/config_db.json > /dev/null"
    rc, out = ssh_cmd(ip, cmd, auth[0], auth[1])
    if rc != 0:
        log(f"  {ip}: failed to write config_db.json: {out}")
        return False

    log(f"  {ip}: config_db.json written ({len(config_json)} bytes)")

    # Reboot — the switch will load the new config on next boot.
    # Use nohup + sleep so the SSH command returns before the reboot kills it.
    # Timeout may be hit if SSH hangs waiting for the bg process; that's OK.
    try:
        rc, out = ssh_cmd(ip, "sudo nohup bash -c 'sleep 2 && reboot' </dev/null &",
                          auth[0], auth[1], timeout=15)
    except Exception:
        # SSH connection killed by reboot — expected
        rc, out = 0, ""
    if rc != 0:
        log(f"  {ip}: reboot command failed: {out}")
        return False

    log(f"  {ip}: rebooting to apply config...")
    return True
