#!/usr/bin/env python3
"""Topology-driven GNS3 lab launcher — all REST, no telnet/SSH.

Reads a containerlab-style YAML topology and:
  1. Creates GNS3 project with nodes, mgmt infra, and data-plane links
  2. Discovers management IPs via pfSense DHCP leases (MAC matching)
  3. Checks/disables ZTP, polls system-ready on SONiC nodes
  4. Generates per-node config_db.json and pushes via config-replace
  5. Verifies LLDP + BGP convergence
  6. Configures host data IPs (Debian SSH)
  7. Installs TPCM containers (if configured)
  8. If agent requests reboot, reboots and re-verifies

Usage:
    python3 scripts/labctl.py topologies/qinq-fwd.yaml
"""

import os
import sys
import time

import urllib3

# Suppress TLS warnings for self-signed certs on SONiC REST
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add project root for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gns3_client import GNS3Client
from labkit import log
from labkit.config_gen import generate_sonic_config, load_baseline
from labkit.evpn import poll_bgp_established
from labkit.hosts import (
    configure_host_ip,
    enable_interface_debian,
    set_hostname_debian,
)
from labkit.interfaces import (
    parse_endpoint,
    topo_iface_to_guest,
    topo_iface_to_native,
)
from labkit.lldp import build_expected_adjacencies, verify_lldp
from labkit.pfsense import discover_ips, normalize_mac
from labkit.sonic_rest import (
    check_disable_ztp,
    config_replace,
    config_save,
    poll_system_ready,
)
from labkit.topo import load_topology
from labkit.tpcm import (
    check_tpcm_reboot_needed,
    get_tpcm_status_ssh,
    install_tpcm,
    poll_tpcm_running,
    reboot_sonic,
)

# --- Timeouts ---
ARP_POLL_TIMEOUT = 300     # 5 min for all MACs to appear in DHCP
READY_POLL_TIMEOUT = 600   # 10 min for system-ready
LLDP_POLL_TIMEOUT = 120    # 2 min for LLDP convergence
TPCM_POLL_TIMEOUT = 180    # 3 min for TPCM container start
BGP_POLL_TIMEOUT = 120     # 2 min for BGP convergence


def _parse_interswitch(scenario_cfg):
    """Extract interswitch IP mapping from scenario config (2-switch l2-evpn)."""
    fabric = scenario_cfg["fabric"]
    interswitch = fabric["interswitch"]
    is_ep_a, is_ep_b = interswitch["link"]
    is_node_a, is_iface_a = is_ep_a.split(":")
    is_node_b, is_iface_b = is_ep_b.split(":")
    subnet = interswitch["subnet"]
    subnet_base, prefix_len = subnet.rsplit("/", 1)
    octets = subnet_base.split(".")
    base_last = int(octets[3])
    prefix = ".".join(octets[:3])

    return {
        "is_ip_map": {
            is_node_a: f"{prefix}.{base_last}/{prefix_len}",
            is_node_b: f"{prefix}.{base_last + 1}/{prefix_len}",
        },
        "is_neighbor_map": {
            is_node_a: f"{prefix}.{base_last + 1}",
            is_node_b: f"{prefix}.{base_last}",
        },
        "is_iface_map": {is_node_a: is_iface_a, is_node_b: is_iface_b},
    }


def _parse_interswitch_mesh(scenario_cfg):
    """Extract per-switch neighbor IP lists from mesh scenario config.

    Returns dict: {switch_name: [neighbor_ip_1, neighbor_ip_2, ...]}
    """
    fabric = scenario_cfg["fabric"]
    switches = fabric["switches"]
    links = fabric["interswitch_links"]
    neighbor_map = {name: [] for name in switches}

    for link_cfg in links:
        ep_a, ep_b = link_cfg["link"]
        node_a, _ = ep_a.split(":")
        node_b, _ = ep_b.split(":")

        subnet = link_cfg["subnet"]
        subnet_base, prefix_len = subnet.rsplit("/", 1)
        octets = subnet_base.split(".")
        base_last = int(octets[3])
        prefix = ".".join(octets[:3])

        # node_a gets .0, node_b gets .1 (bare IPs for BGP neighbor check)
        neighbor_map[node_a].append(f"{prefix}.{base_last + 1}")
        neighbor_map[node_b].append(f"{prefix}.{base_last}")

    return neighbor_map


def do_save(topo, project_name_or_id):
    """Stop nodes and close a GNS3 project (preserves VM disk state)."""
    gns3_cfg = topo["gns3"]
    gns3 = GNS3Client(f"http://{gns3_cfg['host']}:{gns3_cfg['port']}/v2")

    # Find project by name or ID
    project = None
    try:
        project = gns3.get_project(project_name_or_id)
    except Exception:
        project = gns3.find_project(project_name_or_id)

    if not project:
        log(f"Project '{project_name_or_id}' not found")
        return 1

    pid = project["project_id"]
    pname = project["name"]
    log(f"Saving project '{pname}' ({pid[:8]}...)...")

    try:
        gns3.stop_all_nodes(pid)
        log("  All nodes stopped")
    except Exception as e:
        log(f"  Stop nodes: {e}")

    try:
        gns3.close_project(pid)
        log("  Project closed (VM state saved)")
    except Exception as e:
        log(f"  Close project: {e}")

    log(f"Done. Reload with: python3 {sys.argv[0]} --load {pname} {sys.argv[-1]}")
    return 0


def do_load(topo, project_name_or_id):
    """Open a saved GNS3 project, start nodes, discover IPs, verify ready."""
    gns3_cfg = topo["gns3"]
    gns3 = GNS3Client(f"http://{gns3_cfg['host']}:{gns3_cfg['port']}/v2")
    fw_host = topo["firewall"]["host"]
    sonic_auth = (topo["sonic_auth"]["username"], topo["sonic_auth"]["password"])
    debian_auth = (
        topo.get("debian_auth", {}).get("username", "debian"),
        topo.get("debian_auth", {}).get("password", "debian"),
    )
    tpcm_cfg = topo.get("tpcm", {})
    scenario_cfg = topo.get("scenario", {})

    # Find project
    project = None
    try:
        project = gns3.get_project(project_name_or_id)
    except Exception:
        project = gns3.find_project(project_name_or_id)

    if not project:
        log(f"Project '{project_name_or_id}' not found")
        return 1

    pid = project["project_id"]
    pname = project["name"]
    log(f"Loading project '{pname}' ({pid[:8]}...)...")

    # Open project (may already be open)
    try:
        gns3.open_project(pid)
        log("  Project opened")
    except Exception:
        log("  Project already open")

    # Start all nodes
    log("Starting all nodes...")
    gns3.start_all_nodes(pid)

    # Build node info from GNS3
    nodes = gns3.get_nodes(pid)
    node_kinds = {}
    node_order = []
    mac_to_node = {}
    for node in nodes:
        name = node["name"]
        ntype = node.get("node_type", "")
        # Skip infra nodes
        if name in ("mgmt-switch", "cloud") or ntype in ("ethernet_switch", "cloud"):
            continue
        kind = "sonic" if ntype == "qemu" and "sonic" in node.get("properties", {}).get("hda_disk_image", "").lower() else None
        if kind is None:
            # Infer from topology config
            topo_node = topo.get("nodes", {}).get(name, {})
            kind = topo_node.get("kind", "debian")
        node_kinds[name] = kind
        node_order.append(name)
        # Get adapter 0 MAC
        for port in node.get("ports", []):
            if port.get("adapter_number", -1) == 0 and port.get("port_number", -1) == 0:
                mac = normalize_mac(port["mac_address"])
                mac_to_node[mac] = name
                break

    log(f"  Found {len(node_order)} nodes: {', '.join(node_order)}")

    # Discover IPs
    node_ips = discover_ips(fw_host, mac_to_node, timeout=ARP_POLL_TIMEOUT)
    missing = [n for n in node_order if n not in node_ips]
    if missing:
        log(f"FATAL: No IP for nodes: {missing}")
        return 1

    # Wait for SONiC system-ready
    sonic_nodes = [n for n in node_order if node_kinds[n] == "sonic"]
    log("Polling system-ready on SONiC nodes...")
    for name in sonic_nodes:
        ip = node_ips[name]
        log(f"  Waiting for {name} ({ip})...")
        if poll_system_ready(ip, sonic_auth, timeout=READY_POLL_TIMEOUT):
            log(f"  {name} ({ip}): READY")
        else:
            log(f"  {name} ({ip}): NOT READY after timeout")

    # Verify BGP if EVPN scenario
    if scenario_cfg.get("type") == "l2-evpn":
        log("Checking BGP state...")
        is_info = _parse_interswitch(scenario_cfg)
        for sw_name in scenario_cfg["fabric"]["switches"]:
            if sw_name not in node_ips:
                continue
            ip = node_ips[sw_name]
            neighbor_ip = is_info["is_neighbor_map"][sw_name]
            log(f"  {sw_name} ({ip}) -> {neighbor_ip}...")
            poll_bgp_established(ip, sonic_auth, neighbor_ip, timeout=BGP_POLL_TIMEOUT)
    elif scenario_cfg.get("type") == "l2-evpn-mesh":
        log("Checking BGP state (mesh)...")
        mesh_neighbors = _parse_interswitch_mesh(scenario_cfg)
        for sw_name in scenario_cfg["fabric"]["switches"]:
            if sw_name not in node_ips:
                continue
            ip = node_ips[sw_name]
            for neighbor_ip in mesh_neighbors[sw_name]:
                log(f"  {sw_name} ({ip}) -> {neighbor_ip}...")
                poll_bgp_established(ip, sonic_auth, neighbor_ip, timeout=BGP_POLL_TIMEOUT)

    # Summary
    print()
    log("=" * 50)
    log(f"Project loaded: {pname}")
    for name in node_order:
        ip = node_ips.get(name, "???")
        kind = node_kinds[name]
        log(f"  {name}: {ip} ({kind})")

    if tpcm_cfg:
        log("TPCM containers:")
        for docker_name, tcfg in tpcm_cfg.items():
            for name in tcfg.get("nodes", []):
                if name in node_ips:
                    status_map = get_tpcm_status_ssh(node_ips[name], sonic_auth)
                    if docker_name in status_map:
                        log(f"  {name}/{docker_name}: {status_map[docker_name].strip()}")
                    else:
                        log(f"  {name}/{docker_name}: not found")

    log(f"Project ID: {pid}")
    log("=" * 50)
    return 0


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <topology.yaml>", file=sys.stderr)
        print(f"       {sys.argv[0]} --save <project-name-or-id> <topology.yaml>",
              file=sys.stderr)
        print(f"       {sys.argv[0]} --load <project-name-or-id> <topology.yaml>",
              file=sys.stderr)
        return 1

    # Handle --save / --load modes
    if sys.argv[1] == "--save" and len(sys.argv) >= 4:
        topo = load_topology(sys.argv[3])
        return do_save(topo, sys.argv[2])
    if sys.argv[1] == "--load" and len(sys.argv) >= 4:
        topo = load_topology(sys.argv[3])
        return do_load(topo, sys.argv[2])

    topo = load_topology(sys.argv[1])
    topo_name = topo["name"]

    gns3_cfg = topo["gns3"]
    gns3_url = f"http://{gns3_cfg['host']}:{gns3_cfg['port']}/v2"
    gns3 = GNS3Client(gns3_url)

    fw_host = topo["firewall"]["host"]
    sonic_auth = (topo["sonic_auth"]["username"], topo["sonic_auth"]["password"])
    debian_auth = (
        topo.get("debian_auth", {}).get("username", "debian"),
        topo.get("debian_auth", {}).get("password", "debian"),
    )
    templates = topo["templates"]
    nodes_cfg = topo["nodes"]
    links_cfg = topo.get("links", [])
    tpcm_cfg = topo.get("tpcm", {})
    scenario_cfg = topo.get("scenario", {})

    # --- Step 1: Create GNS3 project ---
    project_name = f"{topo_name}-{int(time.time())}"
    log(f"Creating project '{project_name}'...")
    project = gns3.create_project(project_name)
    pid = project["project_id"]
    log(f"Project ID: {pid}")

    # --- Step 2: Create nodes ---
    node_ids = {}
    node_kinds = {}
    node_order = []

    for name, cfg in nodes_cfg.items():
        kind = cfg["kind"]
        template_id = templates[kind]
        x = cfg.get("x", 0)
        y = cfg.get("y", 0)
        node = gns3.create_node_from_template(pid, template_id, name, x, y)
        nid = node["node_id"]
        if node.get("name") != name:
            gns3.update_node(pid, nid, name=name)
        node_ids[name] = nid
        node_kinds[name] = kind
        node_order.append(name)
        log(f"  Created {kind} node '{name}' ({nid[:8]}...)")

    # Compute required mgmt switch ports: one per node + one for cloud uplink
    num_ports_needed = len(node_order) + 1
    cloud_port = len(node_order)  # cloud uplink on the last port

    if num_ports_needed > 8:
        # Default ethernet_switch has 8 ports; generate custom ports_mapping
        ports_mapping = []
        for i in range(num_ports_needed):
            ports_mapping.append({
                "name": f"Ethernet{i}",
                "port_number": i,
                "type": "access",
                "vlan": 1,
            })
        mgmt_switch = gns3.create_node(
            pid, "ethernet_switch", "mgmt-switch",
            x=0, y=-150, ports_mapping=ports_mapping,
        )
    else:
        mgmt_switch = gns3.create_node(
            pid, "ethernet_switch", "mgmt-switch",
            x=0, y=-150,
        )
    cloud_node = gns3.create_node(pid, "cloud", "cloud", x=0, y=-300)
    log(f"  Created mgmt-switch ({mgmt_switch['node_id'][:8]}..., {num_ports_needed} ports)")
    log(f"  Created cloud ({cloud_node['node_id'][:8]}...)")

    # --- Step 3: Wire management links ---
    switch_id = mgmt_switch["node_id"]
    cloud_id = cloud_node["node_id"]
    for port_num, name in enumerate(node_order):
        gns3.create_link(pid, node_ids[name], 0, 0, switch_id, 0, port_num)
    cloud_iface_port = topo.get("mgmt", {}).get("cloud_port", 0)
    gns3.create_link(pid, switch_id, 0, cloud_port, cloud_id, 0, cloud_iface_port)
    log(f"  Wired {len(node_order)} mgmt links + switch->cloud uplink")

    # --- Step 4: Wire data-plane links ---
    for link in links_cfg:
        ep_a, ep_b = link["endpoints"]
        name_a, adapter_a, port_a = parse_endpoint(ep_a)
        name_b, adapter_b, port_b = parse_endpoint(ep_b)
        gns3.create_link(pid, node_ids[name_a], adapter_a, port_a,
                         node_ids[name_b], adapter_b, port_b)
        log(f"  Link: {ep_a} <-> {ep_b}")

    # --- Step 5: Start all nodes ---
    log("Starting all nodes...")
    gns3.start_all_nodes(pid)

    # --- Step 6: Discover IPs via pfSense DHCP ---
    mac_to_node = {}
    for name in node_order:
        node_info = gns3.get_node(pid, node_ids[name])
        ports = node_info.get("ports", [])
        for port in ports:
            if port.get("adapter_number", -1) == 0 and port.get("port_number", -1) == 0:
                mac = normalize_mac(port["mac_address"])
                mac_to_node[mac] = name
                break

    node_ips = discover_ips(fw_host, mac_to_node, timeout=ARP_POLL_TIMEOUT)

    missing = [n for n in node_order if n not in node_ips]
    if missing:
        log(f"FATAL: No IP for nodes: {missing}")
        log(f"Project '{project_name}' left running for debugging (ID: {pid})")
        return 1

    # --- Step 7: Check/disable ZTP on SONiC nodes ---
    sonic_nodes = [n for n in node_order if node_kinds[n] == "sonic"]
    ztp_disabled_any = False
    for name in sonic_nodes:
        ip = node_ips[name]
        log(f"Checking ZTP on {name} ({ip})...")
        if check_disable_ztp(ip, sonic_auth):
            ztp_disabled_any = True

    if ztp_disabled_any:
        log("ZTP was disabled on one or more nodes — containers restarting (~3 min)")

    # --- Step 8: Poll system-ready on SONiC nodes ---
    log("Polling system-ready on SONiC nodes...")
    all_ready = True
    for name in sonic_nodes:
        ip = node_ips[name]
        log(f"  Waiting for {name} ({ip})...")
        if poll_system_ready(ip, sonic_auth, timeout=READY_POLL_TIMEOUT):
            log(f"  {name} ({ip}): READY")
        else:
            log(f"  {name} ({ip}): NOT READY after timeout")
            all_ready = False

    # --- Step 9: Set Debian hostnames + enable Debian interfaces ---
    # SONiC hostname and port admin_status are baked into config_db.json
    log("Configuring Debian hosts...")
    node_ifaces: dict[str, list[str]] = {}
    for link in links_cfg:
        ep_a, ep_b = link["endpoints"]
        node_a, iface_a = ep_a.split(":")
        node_b, iface_b = ep_b.split(":")
        node_ifaces.setdefault(node_a, []).append(iface_a)
        node_ifaces.setdefault(node_b, []).append(iface_b)

    for name in node_order:
        if node_kinds[name] != "debian":
            continue
        ip = node_ips[name]
        set_hostname_debian(ip, name, debian_auth)
        for iface in node_ifaces.get(name, []):
            guest = topo_iface_to_guest(iface)
            ok = enable_interface_debian(ip, guest, debian_auth)
            log(f"  {name}: {iface} ({guest}) -> {'up' if ok else 'FAILED'}")

    # --- Step 10: Generate and push config_db.json per SONiC node ---
    evpn_ok = True
    if sonic_nodes:
        log("Generating and pushing config_db.json...")
        baseline = load_baseline()
        switches_cfg = scenario_cfg.get("fabric", {}).get("switches", {})

        # Build reverse lookup: node_name -> management MAC
        node_to_mac = {name: mac for mac, name in mac_to_node.items()}

        for name in sonic_nodes:
            ip = node_ips[name]
            sw_cfg = switches_cfg.get(name, {})
            sonic_ifaces = node_ifaces.get(name, [])
            mgmt_mac = node_to_mac.get(name, "")

            cfg = generate_sonic_config(
                baseline, name, sw_cfg, scenario_cfg, sonic_ifaces,
                mgmt_mac=mgmt_mac,
            )
            log(f"  Pushing config to {name} ({ip})...")
            if not config_replace(ip, cfg, sonic_auth):
                log(f"  {name}: config-replace FAILED")
                evpn_ok = False

    # --- Step 11: Re-poll system-ready after config push + reboot ---
    if sonic_nodes:
        log("Waiting for SONiC nodes to reboot and come back...")
        time.sleep(60)  # give switches time to fully reboot

        # Re-discover IPs — DHCP may assign new IPs after reboot
        sonic_macs = {mac: name for mac, name in mac_to_node.items()
                      if name in sonic_nodes}
        if sonic_macs:
            log("Re-discovering SONiC node IPs after reboot...")
            new_ips = discover_ips(fw_host, sonic_macs, timeout=300)
            for name, ip in new_ips.items():
                old_ip = node_ips.get(name)
                if ip != old_ip:
                    log(f"  {name}: IP changed {old_ip} -> {ip}")
                node_ips[name] = ip

        for name in sonic_nodes:
            ip = node_ips[name]
            log(f"  Waiting for {name} ({ip})...")
            if poll_system_ready(ip, sonic_auth, timeout=READY_POLL_TIMEOUT):
                log(f"  {name} ({ip}): READY")
            else:
                log(f"  {name} ({ip}): NOT READY after reboot")
                all_ready = False

    # --- Step 12: Verify LLDP adjacencies ---
    log("Verifying LLDP adjacencies...")
    expected_adj = build_expected_adjacencies(links_cfg)
    node_to_mac = {name: mac for mac, name in mac_to_node.items()}
    lldp_ok = verify_lldp(expected_adj, node_ips, node_kinds, node_to_mac,
                           sonic_auth, debian_auth, timeout=LLDP_POLL_TIMEOUT)

    # --- Step 13: Verify BGP convergence ---
    bgp_ok = True
    if scenario_cfg.get("type") == "l2-evpn":
        log("Verifying BGP convergence...")
        is_info = _parse_interswitch(scenario_cfg)
        for sw_name in scenario_cfg["fabric"]["switches"]:
            if sw_name not in node_ips:
                bgp_ok = False
                continue
            ip = node_ips[sw_name]
            neighbor_ip = is_info["is_neighbor_map"][sw_name]
            log(f"  Checking BGP on {sw_name} ({ip}) -> {neighbor_ip}...")
            if not poll_bgp_established(ip, sonic_auth, neighbor_ip,
                                         timeout=BGP_POLL_TIMEOUT):
                bgp_ok = False
    elif scenario_cfg.get("type") == "l2-evpn-mesh":
        log("Verifying BGP convergence (mesh)...")
        mesh_neighbors = _parse_interswitch_mesh(scenario_cfg)
        for sw_name in scenario_cfg["fabric"]["switches"]:
            if sw_name not in node_ips:
                bgp_ok = False
                continue
            ip = node_ips[sw_name]
            for neighbor_ip in mesh_neighbors[sw_name]:
                log(f"  Checking BGP on {sw_name} ({ip}) -> {neighbor_ip}...")
                if not poll_bgp_established(ip, sonic_auth, neighbor_ip,
                                             timeout=BGP_POLL_TIMEOUT):
                    bgp_ok = False

    # --- Step 14: Configure host data IPs ---
    if scenario_cfg.get("hosts"):
        log("Configuring host data IPs...")
        for host_name, hcfg in scenario_cfg.get("hosts", {}).items():
            if host_name not in node_ips:
                log(f"  WARNING: host '{host_name}' has no IP, skipping data IP")
                continue
            ip = node_ips[host_name]
            log(f"  {host_name} ({ip}): {hcfg['ip']} on {hcfg['iface']}")
            configure_host_ip(ip, hcfg["iface"], hcfg["ip"], debian_auth)

    # --- Step 15: Install TPCM containers ---
    tpcm_ok = True
    nodes_needing_reboot = []
    if tpcm_cfg:
        log("Installing TPCM containers...")
        for docker_name, tcfg in tpcm_cfg.items():
            image = tcfg["image"]
            args = tcfg.get("args", "")
            target_nodes = tcfg.get("nodes", [])
            for name in target_nodes:
                if name not in node_ips:
                    log(f"  WARNING: TPCM target '{name}' has no IP, skipping")
                    continue
                ip = node_ips[name]
                log(f"  Installing '{docker_name}' on {name} ({ip})...")
                if install_tpcm(ip, docker_name, image, args, sonic_auth):
                    if not poll_tpcm_running(ip, docker_name, sonic_auth,
                                             timeout=TPCM_POLL_TIMEOUT):
                        tpcm_ok = False
                        continue
                    # Check if agent is requesting a reboot
                    if check_tpcm_reboot_needed(ip, docker_name, sonic_auth):
                        if name not in nodes_needing_reboot:
                            nodes_needing_reboot.append(name)
                else:
                    tpcm_ok = False

    # --- Step 16: Reboot if agent requested it ---
    if nodes_needing_reboot:
        log(f"Rebooting {len(nodes_needing_reboot)} node(s) — agent patched BCM config...")
        for name in nodes_needing_reboot:
            reboot_sonic(node_ips[name], sonic_auth)

        log("Waiting for rebooted nodes to come back...")
        time.sleep(30)

        for name in nodes_needing_reboot:
            ip = node_ips[name]
            log(f"  Waiting for {name} ({ip})...")
            if poll_system_ready(ip, sonic_auth, timeout=READY_POLL_TIMEOUT):
                log(f"  {name} ({ip}): READY (post-reboot)")
            else:
                log(f"  {name} ({ip}): NOT READY after reboot")
                all_ready = False

        # Config persists across reboot — no need to re-enable interfaces.
        # Just re-verify BGP.
        if scenario_cfg.get("type") == "l2-evpn":
            log("Re-verifying BGP convergence after reboot...")
            is_info = _parse_interswitch(scenario_cfg)
            for sw_name in scenario_cfg["fabric"]["switches"]:
                if sw_name not in node_ips:
                    continue
                ip = node_ips[sw_name]
                neighbor_ip = is_info["is_neighbor_map"][sw_name]
                log(f"  Checking BGP on {sw_name} ({ip}) -> {neighbor_ip}...")
                if not poll_bgp_established(ip, sonic_auth, neighbor_ip,
                                             timeout=BGP_POLL_TIMEOUT):
                    bgp_ok = False
        elif scenario_cfg.get("type") == "l2-evpn-mesh":
            log("Re-verifying BGP convergence after reboot (mesh)...")
            mesh_neighbors = _parse_interswitch_mesh(scenario_cfg)
            for sw_name in scenario_cfg["fabric"]["switches"]:
                if sw_name not in node_ips:
                    continue
                ip = node_ips[sw_name]
                for neighbor_ip in mesh_neighbors[sw_name]:
                    log(f"  Checking BGP on {sw_name} ({ip}) -> {neighbor_ip}...")
                    if not poll_bgp_established(ip, sonic_auth, neighbor_ip,
                                                 timeout=BGP_POLL_TIMEOUT):
                        bgp_ok = False

        # Verify TPCM containers came back up after reboot
        log("Verifying TPCM containers after reboot...")
        for docker_name, tcfg in tpcm_cfg.items():
            for name in tcfg.get("nodes", []):
                if name in nodes_needing_reboot and name in node_ips:
                    if not poll_tpcm_running(node_ips[name], docker_name,
                                             sonic_auth, timeout=TPCM_POLL_TIMEOUT):
                        tpcm_ok = False

    # --- Final summary ---
    print()
    success = all_ready and lldp_ok and tpcm_ok and evpn_ok and bgp_ok
    log("=" * 50)
    if success:
        log("ALL NODES READY — LLDP VERIFIED")
        if scenario_cfg.get("type") in ("l2-evpn", "l2-evpn-mesh"):
            log("EVPN CONFIGURED — BGP ESTABLISHED")
        if tpcm_cfg:
            log("TPCM CONTAINERS RUNNING")
        if nodes_needing_reboot:
            log("SAI FLAG ACTIVE (post-reboot)")
    else:
        if not all_ready:
            log("WARNING: Some SONiC nodes did not reach system-ready")
        if not lldp_ok:
            log("WARNING: LLDP adjacency verification failed")
        if not evpn_ok:
            log("WARNING: config-replace had errors")
        if not bgp_ok:
            log("WARNING: BGP did not reach established state")
        if not tpcm_ok:
            log("WARNING: TPCM container install/start failed")

    for name in node_order:
        ip = node_ips.get(name, "???")
        kind = node_kinds[name]
        log(f"  {name}: {ip} ({kind})")

    if tpcm_cfg:
        log("TPCM containers:")
        for docker_name, tcfg in tpcm_cfg.items():
            for name in tcfg.get("nodes", []):
                if name in node_ips:
                    status_map = get_tpcm_status_ssh(node_ips[name], sonic_auth)
                    if docker_name in status_map:
                        log(f"  {name}/{docker_name}: {status_map[docker_name].strip()}")
                    else:
                        log(f"  {name}/{docker_name}: not found")

    log(f"Project: {project_name}")
    log(f"Project ID: {pid}")
    log("=" * 50)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
