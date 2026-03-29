"""Generate per-node config_db.json for SONiC switches.

Merges EVPN overlay tables on top of a vanilla sva-prepped baseline.
The resulting config can be pushed via the copy-config RPC to replace
both running and startup config in one shot.
"""

import copy
import json
import os

from .interfaces import topo_iface_to_native

# Path to the vanilla sva-prepped baseline config
BASELINE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "configs", "baseline_config_db.json"
)


def load_baseline(path: str | None = None) -> dict:
    """Load the baseline config_db.json."""
    path = path or BASELINE_PATH
    with open(path) as f:
        return json.load(f)


def generate_sonic_config(
    baseline: dict,
    node_name: str,
    switch_cfg: dict,
    scenario_cfg: dict,
    connected_ifaces: list[str],
    mgmt_mac: str = "",
) -> dict:
    """Generate a complete config_db.json for one SONiC switch.

    Args:
        baseline: Vanilla sva-prepped config (deep-copied, not mutated).
        node_name: Hostname for this switch (e.g. "r1").
        switch_cfg: Per-switch scenario config dict with keys:
            as, loopback, access (list of topo iface names).
        scenario_cfg: Full scenario dict from topology YAML.
        connected_ifaces: List of topology interface names with data links
            (e.g. ["Eth1/1", "Eth1/2", "Eth1/3"]).
        mgmt_mac: Management MAC address (adapter 0) for DEVICE_METADATA.
            Required — orchagent and other services read this at startup.

    Returns:
        Complete config_db.json dict ready for push.
    """
    cfg = copy.deepcopy(baseline)

    # --- Hostname + MAC ---
    cfg["DEVICE_METADATA"]["localhost"]["hostname"] = node_name
    if mgmt_mac:
        cfg["DEVICE_METADATA"]["localhost"]["mac"] = mgmt_mac

    # --- Enable connected ports ---
    for iface in connected_ifaces:
        native = topo_iface_to_native(iface)
        if native in cfg.get("PORT", {}):
            cfg["PORT"][native]["admin_status"] = "up"

    # No scenario = just hostname + port enable
    if not scenario_cfg:
        return cfg

    # --- l2-switching: VLANs + VLAN members only (no BGP/VXLAN/loopback) ---
    if scenario_cfg.get("type") == "l2-switching":
        vlans_cfg = scenario_cfg.get("vlans", {})
        cfg["VLAN"] = {}
        cfg["VLAN_MEMBER"] = {}
        for vid_str, vlan_cfg in vlans_cfg.items():
            vid = int(vid_str)
            cfg["VLAN"][f"Vlan{vid}"] = {
                "vlanid": str(vid),
                "autostate": "enable",
            }
            for iface in vlan_cfg.get("access", []):
                native = topo_iface_to_native(iface)
                cfg["VLAN_MEMBER"][f"Vlan{vid}|{native}"] = {
                    "tagging_mode": "untagged",
                }
            for iface in vlan_cfg.get("tagged", []):
                native = topo_iface_to_native(iface)
                cfg["VLAN_MEMBER"][f"Vlan{vid}|{native}"] = {
                    "tagging_mode": "tagged",
                }
        return cfg

    if scenario_cfg.get("type") == "l2-evpn":
        return _generate_l2_evpn(cfg, node_name, switch_cfg, scenario_cfg)

    if scenario_cfg.get("type") == "l2-evpn-mesh":
        return _generate_l2_evpn_mesh(cfg, node_name, switch_cfg, scenario_cfg)

    return cfg


def _generate_l2_evpn(cfg, node_name, switch_cfg, scenario_cfg):
    """Generate config for 2-switch l2-evpn scenario."""
    fabric = scenario_cfg["fabric"]
    vlan_id = fabric["vlan"]
    vni = fabric["vni"]
    loopback_ip = switch_cfg["loopback"]
    local_as = switch_cfg["as"]
    access_ifaces = switch_cfg["access"]

    # Derive interswitch info
    is_info = _parse_interswitch(scenario_cfg, node_name)
    interswitch_ip = is_info["my_ip"]
    neighbor_ip = is_info["neighbor_ip"]
    remote_as = is_info["remote_as"]
    interswitch_iface = topo_iface_to_native(is_info["my_iface"])

    # --- Loopback ---
    cfg["LOOPBACK"] = {"Loopback0": {"admin_status": "up"}}
    cfg["LOOPBACK_INTERFACE"] = {
        "Loopback0": {},
        f"Loopback0|{loopback_ip}/32": {},
    }

    # --- Routed interface (interswitch link) ---
    cfg.setdefault("INTERFACE", {})
    cfg["INTERFACE"][interswitch_iface] = {}
    cfg["INTERFACE"][f"{interswitch_iface}|{interswitch_ip}"] = {}

    # --- VLAN ---
    cfg["VLAN"] = {
        f"Vlan{vlan_id}": {
            "vlanid": str(vlan_id),
            "autostate": "enable",
        }
    }

    # --- VLAN members (access ports) ---
    cfg["VLAN_MEMBER"] = {}
    for iface in access_ifaces:
        native = topo_iface_to_native(iface)
        cfg["VLAN_MEMBER"][f"Vlan{vlan_id}|{native}"] = {
            "tagging_mode": "untagged",
        }

    # --- VXLAN ---
    cfg["VXLAN_TUNNEL"] = {
        "vtep1": {
            "src_ip": loopback_ip,
            "dscp": "0",
            "qos-mode": "pipe",
        }
    }
    cfg["VXLAN_TUNNEL_MAP"] = {
        f"vtep1|map_{vni}_Vlan{vlan_id}": {
            "vlan": f"Vlan{vlan_id}",
            "vni": str(vni),
        }
    }
    cfg["VXLAN_EVPN_NVO"] = {
        "nvo1": {"source_vtep": "vtep1"},
    }

    # --- BGP ---
    cfg["BGP_GLOBALS"] = {
        "default": {
            "local_asn": str(local_as),
            "router_id": loopback_ip,
            "ebgp_requires_policy": "false",
        }
    }
    cfg["BGP_GLOBALS_AF"] = {
        "default|ipv4_unicast": {},
        "default|l2vpn_evpn": {"advertise-all-vni": "true"},
    }
    cfg["BGP_NEIGHBOR"] = {
        f"default|{neighbor_ip}": {
            "asn": str(remote_as),
            "admin_status": "true",
        }
    }
    cfg["BGP_NEIGHBOR_AF"] = {
        f"default|{neighbor_ip}|ipv4_unicast": {"admin_status": "true"},
        f"default|{neighbor_ip}|l2vpn_evpn": {"admin_status": "true"},
    }

    # --- Redistribute connected ---
    cfg["ROUTE_REDISTRIBUTE"] = {
        "default|connected|bgp|ipv4": {},
    }

    return cfg


def _generate_l2_evpn_mesh(cfg, node_name, switch_cfg, scenario_cfg):
    """Generate config for N-switch l2-evpn-mesh scenario."""
    fabric = scenario_cfg["fabric"]
    vlan_id = fabric["vlan"]
    vni = fabric["vni"]
    loopback_ip = switch_cfg["loopback"]
    local_as = switch_cfg["as"]
    access_ifaces = switch_cfg["access"]

    # Derive all interswitch links for this node
    mesh_info = _parse_interswitch_mesh(scenario_cfg, node_name)

    # --- Loopback ---
    cfg["LOOPBACK"] = {"Loopback0": {"admin_status": "up"}}
    cfg["LOOPBACK_INTERFACE"] = {
        "Loopback0": {},
        f"Loopback0|{loopback_ip}/32": {},
    }

    # --- Routed interfaces (all interswitch links) ---
    cfg.setdefault("INTERFACE", {})
    for link_info in mesh_info:
        native_iface = topo_iface_to_native(link_info["iface"])
        cfg["INTERFACE"][native_iface] = {}
        cfg["INTERFACE"][f"{native_iface}|{link_info['ip']}"] = {}

    # --- VLAN ---
    cfg["VLAN"] = {
        f"Vlan{vlan_id}": {
            "vlanid": str(vlan_id),
            "autostate": "enable",
        }
    }

    # --- VLAN members (access ports) ---
    cfg["VLAN_MEMBER"] = {}
    for iface in access_ifaces:
        native = topo_iface_to_native(iface)
        cfg["VLAN_MEMBER"][f"Vlan{vlan_id}|{native}"] = {
            "tagging_mode": "untagged",
        }

    # --- VXLAN ---
    cfg["VXLAN_TUNNEL"] = {
        "vtep1": {
            "src_ip": loopback_ip,
            "dscp": "0",
            "qos-mode": "pipe",
        }
    }
    cfg["VXLAN_TUNNEL_MAP"] = {
        f"vtep1|map_{vni}_Vlan{vlan_id}": {
            "vlan": f"Vlan{vlan_id}",
            "vni": str(vni),
        }
    }
    cfg["VXLAN_EVPN_NVO"] = {
        "nvo1": {"source_vtep": "vtep1"},
    }

    # --- BGP ---
    cfg["BGP_GLOBALS"] = {
        "default": {
            "local_asn": str(local_as),
            "router_id": loopback_ip,
            "ebgp_requires_policy": "false",
        }
    }
    cfg["BGP_GLOBALS_AF"] = {
        "default|ipv4_unicast": {},
        "default|l2vpn_evpn": {"advertise-all-vni": "true"},
    }

    # --- BGP neighbors (all mesh peers) ---
    cfg["BGP_NEIGHBOR"] = {}
    cfg["BGP_NEIGHBOR_AF"] = {}
    for link_info in mesh_info:
        neighbor_ip = link_info["neighbor_ip"]
        remote_as = link_info["remote_as"]
        cfg["BGP_NEIGHBOR"][f"default|{neighbor_ip}"] = {
            "asn": str(remote_as),
            "admin_status": "true",
        }
        cfg["BGP_NEIGHBOR_AF"][f"default|{neighbor_ip}|ipv4_unicast"] = {
            "admin_status": "true",
        }
        cfg["BGP_NEIGHBOR_AF"][f"default|{neighbor_ip}|l2vpn_evpn"] = {
            "admin_status": "true",
        }

    # --- Redistribute connected ---
    cfg["ROUTE_REDISTRIBUTE"] = {
        "default|connected|bgp|ipv4": {},
    }

    return cfg


def _parse_interswitch(scenario_cfg: dict, node_name: str) -> dict:
    """Extract interswitch IP/neighbor info for a specific node."""
    fabric = scenario_cfg["fabric"]
    interswitch = fabric["interswitch"]
    switches = fabric["switches"]

    is_ep_a, is_ep_b = interswitch["link"]
    is_node_a, is_iface_a = is_ep_a.split(":")
    is_node_b, is_iface_b = is_ep_b.split(":")

    subnet = interswitch["subnet"]
    subnet_base, prefix_len = subnet.rsplit("/", 1)
    octets = subnet_base.split(".")
    base_last = int(octets[3])
    prefix = ".".join(octets[:3])

    ip_map = {
        is_node_a: f"{prefix}.{base_last}/{prefix_len}",
        is_node_b: f"{prefix}.{base_last + 1}/{prefix_len}",
    }
    neighbor_map = {
        is_node_a: f"{prefix}.{base_last + 1}",
        is_node_b: f"{prefix}.{base_last}",
    }
    iface_map = {is_node_a: is_iface_a, is_node_b: is_iface_b}

    other_nodes = [n for n in switches if n != node_name]
    remote_as = switches[other_nodes[0]]["as"]

    return {
        "my_ip": ip_map[node_name],
        "my_iface": iface_map[node_name],
        "neighbor_ip": neighbor_map[node_name],
        "remote_as": remote_as,
    }


def _parse_interswitch_mesh(scenario_cfg: dict, node_name: str) -> list[dict]:
    """Extract all interswitch links for a specific node in a mesh topology.

    Returns a list of dicts, one per link this node participates in:
        {iface, ip, neighbor_ip, remote_as}
    """
    fabric = scenario_cfg["fabric"]
    switches = fabric["switches"]
    links = fabric["interswitch_links"]

    result = []
    for link_cfg in links:
        ep_a, ep_b = link_cfg["link"]
        node_a, iface_a = ep_a.split(":")
        node_b, iface_b = ep_b.split(":")

        if node_name not in (node_a, node_b):
            continue

        subnet = link_cfg["subnet"]
        subnet_base, prefix_len = subnet.rsplit("/", 1)
        octets = subnet_base.split(".")
        base_last = int(octets[3])
        prefix = ".".join(octets[:3])

        if node_name == node_a:
            my_ip = f"{prefix}.{base_last}/{prefix_len}"
            neighbor_ip = f"{prefix}.{base_last + 1}"
            my_iface = iface_a
            remote_node = node_b
        else:
            my_ip = f"{prefix}.{base_last + 1}/{prefix_len}"
            neighbor_ip = f"{prefix}.{base_last}"
            my_iface = iface_b
            remote_node = node_a

        result.append({
            "iface": my_iface,
            "ip": my_ip,
            "neighbor_ip": neighbor_ip,
            "remote_as": switches[remote_node]["as"],
        })

    return result
