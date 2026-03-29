"""LLDP neighbor discovery and verification."""

import re
import time

from . import log
from .interfaces import topo_iface_to_guest
from .pfsense import normalize_mac
from .sonic_rest import sonic_get
from .ssh import ssh_cmd

LLDP_POLL_INTERVAL = 10
LLDP_POLL_TIMEOUT = 120


def build_expected_adjacencies(links_cfg: list) -> dict[str, dict[str, str]]:
    """Build expected LLDP adjacency map from topology links.

    Returns {node_name: {local_topo_iface: remote_node_name}}.
    """
    adj: dict[str, dict[str, str]] = {}
    for link in links_cfg:
        ep_a, ep_b = link["endpoints"]
        node_a, iface_a = ep_a.split(":")
        node_b, iface_b = ep_b.split(":")
        adj.setdefault(node_a, {})[iface_a] = node_b
        adj.setdefault(node_b, {})[iface_b] = node_a
    return adj


def get_lldp_neighbors_sonic(ip: str, auth: tuple) -> dict[str, str]:
    """Get LLDP neighbors from SONiC via RESTCONF.

    Returns {iface_name: neighbor_chassis_mac} (normalized).
    """
    try:
        r = sonic_get(ip, "data/openconfig-lldp:lldp/interfaces", auth)
        if r.status_code != 200:
            return {}
        data = r.json()
        result = {}
        ifaces = (data.get("openconfig-lldp:interfaces", {})
                      .get("interface", []))
        for iface in ifaces:
            name = iface.get("name", "")
            neighbors = (iface.get("neighbors", {})
                             .get("neighbor", []))
            for nbr in neighbors:
                state = nbr.get("state", {})
                chassis_id = state.get("chassis-id", "")
                if chassis_id and "MAC" in state.get("chassis-id-type", ""):
                    result[name] = normalize_mac(chassis_id)
        return result
    except Exception:
        return {}


def get_lldp_neighbors_debian(ip: str, debian_auth: tuple) -> dict[str, str]:
    """Get LLDP neighbors from Debian via SSH (lldpcli text output).

    Returns {guest_iface: neighbor_chassis_mac} (normalized).
    """
    rc, out = ssh_cmd(ip, "/usr/sbin/lldpcli show neighbors",
                      debian_auth[0], debian_auth[1])
    if rc != 0:
        return {}
    result = {}
    current_iface = None
    for line in out.splitlines():
        m = re.match(r"Interface:\s+(\S+),", line)
        if m:
            current_iface = m.group(1)
        m = re.match(r"\s+ChassisID:\s+mac\s+(\S+)", line)
        if m and current_iface:
            result[current_iface] = normalize_mac(m.group(1))
    return result


def verify_lldp(expected_adj: dict[str, dict[str, str]],
                node_ips: dict[str, str], node_kinds: dict[str, str],
                node_to_mac: dict[str, str],
                sonic_auth: tuple, debian_auth: tuple,
                timeout: int = LLDP_POLL_TIMEOUT) -> bool:
    """Poll LLDP on all nodes until all expected adjacencies form.

    Matches on chassis-id MAC (available from the first LLDP PDU,
    no hostname propagation delay).
    """
    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        verified = 0
        total = 0

        for node, ifaces in expected_adj.items():
            ip = node_ips.get(node)
            kind = node_kinds.get(node)
            if not ip or not kind:
                total += len(ifaces)
                continue

            if kind == "sonic":
                lldp = get_lldp_neighbors_sonic(ip, sonic_auth)
            else:
                lldp = get_lldp_neighbors_debian(ip, debian_auth)

            for iface, expected_neighbor in ifaces.items():
                total += 1
                # SONiC RESTCONF LLDP uses standard names (Eth1/1) matching topology
                if kind == "sonic":
                    lookup = iface
                else:
                    lookup = topo_iface_to_guest(iface)
                expected_mac = node_to_mac.get(expected_neighbor, "")
                if lldp.get(lookup) == expected_mac:
                    verified += 1

        if verified == total:
            log(f"  LLDP: {verified}/{total} adjacencies verified")
            return True

        if attempt % 3 == 1:
            log(f"  LLDP: {verified}/{total} adjacencies verified, polling...")
        time.sleep(LLDP_POLL_INTERVAL)

    # Print what's missing
    log(f"  LLDP verification timed out ({verified}/{total}):")
    for node, ifaces in expected_adj.items():
        ip = node_ips.get(node)
        kind = node_kinds.get(node)
        if not ip:
            for iface, neighbor in ifaces.items():
                log(f"    {node}:{iface} -> {neighbor} (no IP)")
            continue
        if kind == "sonic":
            lldp = get_lldp_neighbors_sonic(ip, sonic_auth)
        else:
            lldp = get_lldp_neighbors_debian(ip, debian_auth)
        for iface, expected_neighbor in ifaces.items():
            lookup = iface if kind == "sonic" else topo_iface_to_guest(iface)
            expected_mac = node_to_mac.get(expected_neighbor, "")
            actual = lldp.get(lookup)
            if actual != expected_mac:
                log(f"    {node}:{iface} -> expected {expected_neighbor} ({expected_mac}), "
                    f"got {actual or 'none'}")
    return False
