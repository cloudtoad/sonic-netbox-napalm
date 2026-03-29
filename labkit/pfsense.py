"""pfSense DHCP lease-based IP discovery."""

import os
import re
import time

import requests

from . import log

ARP_POLL_INTERVAL = 10
ARP_POLL_TIMEOUT = 300


def read_pfsense_api_key() -> str:
    path = os.path.expanduser("~/.ssh/pfsense_api_key")
    with open(path) as f:
        return f.read().strip()


def normalize_mac(mac: str) -> str:
    """Normalize MAC to lowercase colon-separated format."""
    raw = re.sub(r"[^0-9a-fA-F]", "", mac)
    return ":".join(raw[i:i+2] for i in range(0, 12, 2)).lower()


def fetch_dhcp_leases(fw_host: str, api_key: str) -> list[dict]:
    """GET pfSense DHCP leases."""
    r = requests.get(
        f"https://{fw_host}/api/v2/status/dhcp_server/leases",
        headers={"X-API-Key": api_key},
        verify=False, timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def discover_ips(fw_host: str, mac_to_node: dict[str, str],
                 timeout: int = ARP_POLL_TIMEOUT) -> dict[str, str]:
    """Poll pfSense DHCP leases until all MACs are resolved -> {node_name: ip}.

    mac_to_node: {normalized_mac: node_name}
    """
    api_key = read_pfsense_api_key()
    resolved = {}
    remaining = dict(mac_to_node)
    deadline = time.time() + timeout

    log(f"Waiting for {len(remaining)} node(s) to acquire DHCP leases...")

    while remaining and time.time() < deadline:
        try:
            leases = fetch_dhcp_leases(fw_host, api_key)
        except Exception as e:
            log(f"  DHCP fetch error: {e}")
            time.sleep(ARP_POLL_INTERVAL)
            continue

        for lease in leases:
            mac = normalize_mac(lease.get("mac", ""))
            if mac in remaining:
                ip = lease.get("ip", "")
                if ip:
                    node_name = remaining.pop(mac)
                    resolved[node_name] = ip
                    log(f"  {node_name}: {ip} (MAC {mac})")

        if remaining:
            time.sleep(ARP_POLL_INTERVAL)

    if remaining:
        log(f"WARNING: Could not resolve IPs for: {list(remaining.values())}")

    return resolved
