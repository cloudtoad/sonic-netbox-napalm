"""L2 EVPN fabric configuration via SONiC RESTCONF.

Uses a mix of openconfig and sonic-* YANG models — whichever works on
Dell Enterprise SONiC 4.4.x VS.  All paths validated against live API.

Interface names must be in native format (Ethernet0, not Eth1/1).
"""

import time

import requests

from . import log
from .sonic_rest import sonic_get, sonic_patch

BGP_POLL_INTERVAL = 5
BGP_POLL_TIMEOUT = 120


# ---------------------------------------------------------------------------
# Loopback  (openconfig-interfaces)
# ---------------------------------------------------------------------------

def configure_loopback(ip: str, auth: tuple, loopback_ip: str) -> bool:
    """Create Loopback0 with a /32 address via openconfig-interfaces."""
    # Step 1: Create the loopback interface
    body = {
        "openconfig-interfaces:interfaces": {
            "interface": [{
                "name": "Loopback0",
                "config": {
                    "name": "Loopback0",
                    "type": "iana-if-type:softwareLoopback",
                    "enabled": True,
                },
            }]
        }
    }
    try:
        r = sonic_patch(ip, "data/openconfig-interfaces:interfaces", body, auth)
        if r.status_code not in (200, 201, 204):
            log(f"  {ip}: Loopback0 create returned HTTP {r.status_code}: {r.text}")
            return False
    except Exception as e:
        log(f"  {ip}: Loopback0 create error: {e}")
        return False

    # Step 2: Add IP address
    ip_body = {
        "openconfig-if-ip:addresses": {
            "address": [{
                "ip": loopback_ip,
                "config": {"ip": loopback_ip, "prefix-length": 32},
            }]
        }
    }
    try:
        r = sonic_patch(
            ip,
            "data/openconfig-interfaces:interfaces/interface=Loopback0"
            "/subinterfaces/subinterface=0/openconfig-if-ip:ipv4/addresses",
            ip_body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: Loopback0 = {loopback_ip}/32")
            return True
        log(f"  {ip}: Loopback0 IP returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: Loopback0 IP error: {e}")
        return False


# ---------------------------------------------------------------------------
# Interface IP  (openconfig-interfaces subinterface)
# ---------------------------------------------------------------------------

def configure_interface_ip(ip: str, auth: tuple,
                           iface: str, iface_ip: str) -> bool:
    """Assign an IP to a routed interface (e.g. interswitch link).

    iface_ip format: "10.1.1.0/31"
    """
    addr, prefix_len = iface_ip.rsplit("/", 1)
    body = {
        "openconfig-if-ip:addresses": {
            "address": [{
                "ip": addr,
                "config": {"ip": addr, "prefix-length": int(prefix_len)},
            }]
        }
    }
    try:
        r = sonic_patch(
            ip,
            f"data/openconfig-interfaces:interfaces/interface={iface}"
            "/subinterfaces/subinterface=0/openconfig-if-ip:ipv4/addresses",
            body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: {iface} = {iface_ip}")
            return True
        log(f"  {ip}: {iface} IP returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: {iface} IP error: {e}")
        return False


# ---------------------------------------------------------------------------
# VLAN  (sonic-vlan — VLAN container, not VLAN_TABLE)
# ---------------------------------------------------------------------------

def configure_vlan(ip: str, auth: tuple, vlan_id: int) -> bool:
    """Create a VLAN via sonic-vlan YANG."""
    body = {
        "sonic-vlan:sonic-vlan": {
            "VLAN": {
                "VLAN_LIST": [{"name": f"Vlan{vlan_id}", "vlanid": vlan_id}]
            }
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-vlan:sonic-vlan", body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: Vlan{vlan_id} created")
            return True
        log(f"  {ip}: VLAN config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: VLAN config error: {e}")
        return False


def configure_vlan_member(ip: str, auth: tuple, vlan_id: int,
                          iface: str, tagging_mode: str = "untagged") -> bool:
    """Add interface as VLAN member via sonic-vlan YANG."""
    body = {
        "sonic-vlan:sonic-vlan": {
            "VLAN_MEMBER": {
                "VLAN_MEMBER_LIST": [{
                    "name": f"Vlan{vlan_id}",
                    "ifname": iface,
                    "tagging_mode": tagging_mode,
                }]
            }
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-vlan:sonic-vlan", body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: {iface} -> Vlan{vlan_id} ({tagging_mode})")
            return True
        log(f"  {ip}: VLAN member config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: VLAN member config error: {e}")
        return False


# ---------------------------------------------------------------------------
# VXLAN tunnel + EVPN NVO  (sonic-vxlan)
# ---------------------------------------------------------------------------

def configure_vxlan(ip: str, auth: tuple, src_ip: str,
                    vlan_id: int, vni: int) -> bool:
    """Create VXLAN tunnel, map VLAN to VNI, and create EVPN NVO."""
    body = {
        "sonic-vxlan:sonic-vxlan": {
            "VXLAN_TUNNEL": {
                "VXLAN_TUNNEL_LIST": [{
                    "name": "vtep1",
                    "src_ip": src_ip,
                }]
            },
            "VXLAN_TUNNEL_MAP": {
                "VXLAN_TUNNEL_MAP_LIST": [{
                    "name": "vtep1",
                    "mapname": f"map_{vni}_Vlan{vlan_id}",
                    "vlan": f"Vlan{vlan_id}",
                    "vni": vni,
                }]
            },
            "VXLAN_EVPN_NVO": {
                "VXLAN_EVPN_NVO_LIST": [{
                    "name": "nvo1",
                    "source_vtep": "vtep1",
                }]
            },
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-vxlan:sonic-vxlan", body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: VXLAN vtep1 (src={src_ip}), VNI {vni} -> Vlan{vlan_id}")
            return True
        log(f"  {ip}: VXLAN config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: VXLAN config error: {e}")
        return False


# ---------------------------------------------------------------------------
# BGP  (sonic-bgp-global + sonic-bgp-neighbor)
# ---------------------------------------------------------------------------

def configure_bgp(ip: str, auth: tuple, local_as: int,
                  router_id: str) -> bool:
    """Configure BGP global settings with ebgp-requires-policy disabled."""
    body = {
        "sonic-bgp-global:sonic-bgp-global": {
            "BGP_GLOBALS": {
                "BGP_GLOBALS_LIST": [{
                    "vrf_name": "default",
                    "local_asn": local_as,
                    "router_id": router_id,
                    "ebgp_requires_policy": False,
                }]
            }
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-bgp-global:sonic-bgp-global", body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: BGP AS {local_as}, router-id {router_id}")
            return True
        log(f"  {ip}: BGP global config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: BGP global config error: {e}")
        return False


def configure_bgp_neighbor(ip: str, auth: tuple, neighbor_ip: str,
                           remote_as: int) -> bool:
    """Configure a BGP neighbor."""
    body = {
        "sonic-bgp-neighbor:sonic-bgp-neighbor": {
            "BGP_NEIGHBOR": {
                "BGP_NEIGHBOR_LIST": [{
                    "vrf_name": "default",
                    "neighbor": neighbor_ip,
                    "asn": remote_as,
                    "admin_status": True,
                }]
            }
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-bgp-neighbor:sonic-bgp-neighbor", body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: BGP neighbor {neighbor_ip} AS {remote_as}")
            return True
        log(f"  {ip}: BGP neighbor config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: BGP neighbor config error: {e}")
        return False


def configure_bgp_afs(ip: str, auth: tuple, neighbor_ip: str) -> bool:
    """Activate IPv4 unicast + L2VPN EVPN AFs with advertise-all-vni."""
    # Global AFs: ipv4_unicast + l2vpn_evpn with advertise-all-vni
    af_body = {
        "sonic-bgp-global:sonic-bgp-global": {
            "BGP_GLOBALS_AF": {
                "BGP_GLOBALS_AF_LIST": [
                    {"vrf_name": "default", "afi_safi": "ipv4_unicast"},
                    {"vrf_name": "default", "afi_safi": "l2vpn_evpn",
                     "advertise-all-vni": True},
                ]
            }
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-bgp-global:sonic-bgp-global", af_body, auth)
        if r.status_code not in (200, 201, 204):
            log(f"  {ip}: BGP AF config returned HTTP {r.status_code}: {r.text}")
            return False
    except Exception as e:
        log(f"  {ip}: BGP AF config error: {e}")
        return False

    # Activate neighbor in both AFs
    nbr_af_body = {
        "sonic-bgp-neighbor:sonic-bgp-neighbor": {
            "BGP_NEIGHBOR_AF": {
                "BGP_NEIGHBOR_AF_LIST": [
                    {"vrf_name": "default", "neighbor": neighbor_ip,
                     "afi_safi": "ipv4_unicast", "admin_status": True},
                    {"vrf_name": "default", "neighbor": neighbor_ip,
                     "afi_safi": "l2vpn_evpn", "admin_status": True},
                ]
            }
        }
    }
    try:
        r = sonic_patch(ip, "data/sonic-bgp-neighbor:sonic-bgp-neighbor",
                        nbr_af_body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: BGP IPv4+EVPN AFs activated for {neighbor_ip}")
            return True
        log(f"  {ip}: BGP neighbor AF config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: BGP neighbor AF config error: {e}")
        return False


def configure_redistribute_connected(ip: str, auth: tuple) -> bool:
    """Redistribute connected routes into BGP (for loopback reachability)."""
    body = {
        "openconfig-network-instance:table-connections": {
            "table-connection": [{
                "src-protocol": "openconfig-policy-types:DIRECTLY_CONNECTED",
                "dst-protocol": "openconfig-policy-types:BGP",
                "address-family": "openconfig-types:IPV4",
                "config": {
                    "src-protocol": "openconfig-policy-types:DIRECTLY_CONNECTED",
                    "dst-protocol": "openconfig-policy-types:BGP",
                    "address-family": "openconfig-types:IPV4",
                },
            }]
        }
    }
    try:
        r = sonic_patch(
            ip,
            "data/openconfig-network-instance:network-instances"
            "/network-instance=default/table-connections",
            body, auth)
        if r.status_code in (200, 201, 204):
            log(f"  {ip}: redistribute connected -> BGP")
            return True
        log(f"  {ip}: redistribute config returned HTTP {r.status_code}: {r.text}")
        return False
    except Exception as e:
        log(f"  {ip}: redistribute config error: {e}")
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def configure_evpn_switch(ip: str, auth: tuple, loopback_ip: str,
                          interswitch_iface: str, interswitch_ip: str,
                          local_as: int, neighbor_ip: str, remote_as: int,
                          vlan_id: int, vni: int,
                          access_ifaces: list[str]) -> bool:
    """One-shot: full EVPN config for a switch via RESTCONF.

    All interface names must be in native format (Ethernet0).
    Config order matters — VLAN must exist before VXLAN map references it.
    """
    ok = True
    ok = configure_loopback(ip, auth, loopback_ip) and ok
    ok = configure_interface_ip(ip, auth, interswitch_iface, interswitch_ip) and ok
    ok = configure_vlan(ip, auth, vlan_id) and ok
    for iface in access_ifaces:
        ok = configure_vlan_member(ip, auth, vlan_id, iface) and ok
    ok = configure_vxlan(ip, auth, loopback_ip, vlan_id, vni) and ok
    ok = configure_bgp(ip, auth, local_as, loopback_ip) and ok
    ok = configure_bgp_neighbor(ip, auth, neighbor_ip, remote_as) and ok
    ok = configure_bgp_afs(ip, auth, neighbor_ip) and ok
    ok = configure_redistribute_connected(ip, auth) and ok
    return ok


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def poll_bgp_established(ip: str, auth: tuple, neighbor_ip: str,
                         timeout: int = BGP_POLL_TIMEOUT) -> bool:
    """Poll BGP neighbor session-state via openconfig until ESTABLISHED."""
    deadline = time.time() + timeout
    attempt = 0
    path = (
        "data/openconfig-network-instance:network-instances"
        f"/network-instance=default/protocols/protocol=BGP,bgp"
        f"/bgp/neighbors/neighbor={neighbor_ip}/state/session-state"
    )

    while time.time() < deadline:
        attempt += 1
        try:
            r = sonic_get(ip, path, auth)
            if r.status_code == 200:
                state = r.json().get("openconfig-network-instance:session-state", "")
                if state == "ESTABLISHED":
                    log(f"  {ip}: BGP neighbor {neighbor_ip} ESTABLISHED")
                    return True
                if attempt % 6 == 1:
                    log(f"  {ip}: BGP neighbor {neighbor_ip} state: {state}")
            else:
                if attempt % 6 == 1:
                    log(f"  {ip}: BGP query returned HTTP {r.status_code}")
        except requests.exceptions.ConnectionError:
            if attempt % 6 == 1:
                log(f"  {ip}: BGP poll — connection error")
        except Exception as e:
            if attempt % 6 == 1:
                log(f"  {ip}: BGP poll error: {e}")
        time.sleep(BGP_POLL_INTERVAL)

    log(f"  {ip}: BGP neighbor {neighbor_ip} did not reach ESTABLISHED within {timeout}s")
    return False
