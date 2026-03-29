"""NAPALM driver for Dell Enterprise SONiC (RESTCONF-based).

Developed against Dell Enterprise SONiC 4.4.2.
Uses OpenConfig YANG models over RESTCONF for all data retrieval.
"""

import re
import time
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import requests

from napalm.base.base import NetworkDriver
from napalm.base.exceptions import (
    CommandErrorException,
    ConnectionClosedException,
    ConnectionException,
    MergeConfigException,
    ReplaceConfigException,
)

from napalm_sonic.constants import (
    OC_AAA,
    OC_ACL,
    OC_AFTS,
    OC_BGP_GLOBAL,
    OC_BGP_NEIGHBORS,
    OC_INTERFACE,
    OC_INTERFACE_COUNTERS,
    OC_INTERFACE_IPV4_ADDRS,
    OC_INTERFACE_IPV4_NEIGHBORS,
    OC_INTERFACE_IPV6_ADDRS,
    OC_INTERFACE_IPV6_NEIGHBORS,
    OC_INTERFACES,
    OC_LLDP_INTERFACES,
    OC_MAC_TABLE,
    OC_NETWORK_INSTANCE,
    OC_NETWORK_INSTANCES,
    OC_NTP,
    OC_PLATFORM_COMPONENTS,
    OC_SOFTWARE_MODULE,
    OC_SYSTEM_EEPROM,
    OC_SYSTEM_STATE,
    RESTCONF_HEADERS,
    RESTCONF_ROOT,
    SPEED_MAP,
)


def _url_encode_iface(name: str) -> str:
    """URL-encode interface name for RESTCONF paths (Eth1/1 -> Eth1%2F1)."""
    return quote(name, safe="")


class SONiCDriver(NetworkDriver):
    """NAPALM driver for Dell Enterprise SONiC via RESTCONF."""

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        timeout: int = 60,
        optional_args: Optional[Dict] = None,
    ) -> None:
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.optional_args = optional_args or {}

        self.port = self.optional_args.get("port", 443)
        self.verify_ssl = self.optional_args.get("verify_ssl", False)

        self._session: Optional[requests.Session] = None
        self._base_url = f"https://{self.hostname}:{self.port}{RESTCONF_ROOT}"

    # --- Connection management ---

    def open(self) -> None:
        self._session = requests.Session()
        self._session.auth = (self.username, self.password)
        self._session.headers.update(RESTCONF_HEADERS)
        self._session.verify = self.verify_ssl
        # Validate connectivity
        try:
            r = self._get(OC_SYSTEM_STATE)
            r.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            raise ConnectionException(f"Cannot connect to {self.hostname}: {e}")
        except requests.exceptions.HTTPError as e:
            raise ConnectionException(f"Authentication failed on {self.hostname}: {e}")

    def close(self) -> None:
        if self._session:
            self._session.close()
            self._session = None

    def is_alive(self) -> Dict[str, bool]:
        try:
            r = self._get(OC_SYSTEM_STATE)
            return {"is_alive": r.status_code == 200}
        except Exception:
            return {"is_alive": False}

    # --- RESTCONF helpers ---

    def _get(self, path: str) -> requests.Response:
        if not self._session:
            raise ConnectionClosedException("Session not open — call open() first.")
        url = f"{self._base_url}/{path}"
        return self._session.get(url, timeout=self.timeout)

    def _get_json(self, path: str) -> Dict:
        r = self._get(path)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: Dict) -> requests.Response:
        if not self._session:
            raise ConnectionClosedException("Session not open — call open() first.")
        url = f"{self._base_url}/{path}"
        return self._session.patch(url, json=body, timeout=self.timeout)

    def _put(self, path: str, body: Dict) -> requests.Response:
        if not self._session:
            raise ConnectionClosedException("Session not open — call open() first.")
        url = f"{self._base_url}/{path}"
        return self._session.put(url, json=body, timeout=self.timeout)

    def _delete(self, path: str) -> requests.Response:
        if not self._session:
            raise ConnectionClosedException("Session not open — call open() first.")
        url = f"{self._base_url}/{path}"
        return self._session.delete(url, timeout=self.timeout)

    # --- Getters ---

    def get_facts(self) -> Dict[str, Any]:
        # System state: hostname, boot-time
        sys_state = self._get_json(OC_SYSTEM_STATE).get(
            "openconfig-system:state", {}
        )
        hostname = sys_state.get("hostname", "")
        boot_ns = int(sys_state.get("boot-time", 0))
        uptime = time.time() - (boot_ns / 1_000_000_000) if boot_ns else -1.0

        # SoftwareModule: version, serial, model, vendor
        sw_data = self._get_json(OC_SOFTWARE_MODULE)
        sw_comp = {}
        for comp in sw_data.get("openconfig-platform:component", []):
            sw_state = comp.get("software-module", {}).get(
                "openconfig-platform-software-ext:docker", {}
            )
            sw_comp = comp.get("software-module", {}).get("state", {})
            break

        os_version = sw_comp.get(
            "openconfig-platform-software-ext:software-version", ""
        )
        serial_number = sw_comp.get(
            "openconfig-platform-software-ext:serial-number", ""
        )
        model = sw_comp.get("openconfig-platform-software-ext:hwsku-version", "")
        vendor = sw_comp.get("openconfig-platform-software-ext:mfg-name", "Dell")

        # System EEPROM fallback for serial/model
        if not serial_number or serial_number == "0000000000000000000":
            eeprom = self._get_json(OC_SYSTEM_EEPROM)
            for comp in eeprom.get("openconfig-platform:component", []):
                state = comp.get("state", {})
                serial_number = state.get("serial-no", serial_number)
                if not model:
                    model = state.get("description", model)
                vendor = state.get("vendor-name", vendor)

        # Interface list
        ifaces_data = self._get_json(OC_INTERFACES)
        interface_list = [
            iface["name"]
            for iface in ifaces_data.get(
                "openconfig-interfaces:interfaces", {}
            ).get("interface", [])
        ]

        return {
            "hostname": hostname,
            "fqdn": hostname,  # SONiC doesn't expose FQDN separately
            "vendor": vendor,
            "model": model,
            "serial_number": serial_number,
            "os_version": os_version,
            "uptime": uptime,
            "interface_list": interface_list,
        }

    def get_interfaces(self) -> Dict[str, Dict]:
        data = self._get_json(OC_INTERFACES)
        result = {}
        for iface in data.get("openconfig-interfaces:interfaces", {}).get(
            "interface", []
        ):
            name = iface.get("name", "")
            state = iface.get("state", {})
            eth_state = (
                iface.get("openconfig-if-ethernet:ethernet", {}).get("state", {})
            )

            speed_str = eth_state.get("port-speed", "")
            speed = SPEED_MAP.get(speed_str, 0.0)

            mac = eth_state.get("mac-address", state.get("mac-address", ""))

            result[name] = {
                "is_up": state.get("oper-status", "") == "UP",
                "is_enabled": state.get("admin-status", "") == "UP",
                "description": state.get("description", ""),
                "last_flapped": -1.0,  # Not available via RESTCONF
                "mtu": state.get("mtu", 0),
                "speed": speed,
                "mac_address": mac,
            }
        return result

    def get_interfaces_counters(self) -> Dict[str, Dict]:
        # Get all interfaces to iterate
        ifaces_data = self._get_json(OC_INTERFACES)
        result = {}
        for iface in ifaces_data.get("openconfig-interfaces:interfaces", {}).get(
            "interface", []
        ):
            name = iface.get("name", "")
            counters = iface.get("state", {}).get("counters", {})
            if not counters:
                # Counters are inline in the bulk interfaces response
                continue
            result[name] = {
                "tx_errors": int(counters.get("out-errors", 0)),
                "rx_errors": int(counters.get("in-errors", 0)),
                "tx_discards": int(counters.get("out-discards", 0)),
                "rx_discards": int(counters.get("in-discards", 0)),
                "tx_octets": int(counters.get("out-octets", 0)),
                "rx_octets": int(counters.get("in-octets", 0)),
                "tx_unicast_packets": int(counters.get("out-unicast-pkts", 0)),
                "rx_unicast_packets": int(counters.get("in-unicast-pkts", 0)),
                "tx_multicast_packets": int(counters.get("out-multicast-pkts", 0)),
                "rx_multicast_packets": int(counters.get("in-multicast-pkts", 0)),
                "tx_broadcast_packets": int(counters.get("out-broadcast-pkts", 0)),
                "rx_broadcast_packets": int(counters.get("in-broadcast-pkts", 0)),
            }
        return result

    def get_interfaces_ip(self) -> Dict[str, Dict]:
        ifaces_data = self._get_json(OC_INTERFACES)
        result = {}
        for iface in ifaces_data.get("openconfig-interfaces:interfaces", {}).get(
            "interface", []
        ):
            name = iface.get("name", "")
            entry: Dict[str, Dict] = {}

            # Walk subinterfaces for IPv4 and IPv6
            for sub in iface.get("subinterfaces", {}).get("subinterface", []):
                # IPv4
                ipv4_addrs = (
                    sub.get("openconfig-if-ip:ipv4", {})
                    .get("addresses", {})
                    .get("address", [])
                )
                for addr in ipv4_addrs:
                    ip = addr.get("ip", "")
                    pfx = (
                        addr.get("state", {}).get("prefix-length")
                        or addr.get("config", {}).get("prefix-length")
                    )
                    if ip:
                        entry.setdefault("ipv4", {})[ip] = {
                            "prefix_length": int(pfx) if pfx is not None else 0
                        }

                # IPv6
                ipv6_addrs = (
                    sub.get("openconfig-if-ip:ipv6", {})
                    .get("addresses", {})
                    .get("address", [])
                )
                for addr in ipv6_addrs:
                    ip = addr.get("ip", "")
                    pfx = (
                        addr.get("state", {}).get("prefix-length")
                        or addr.get("config", {}).get("prefix-length")
                    )
                    if ip:
                        entry.setdefault("ipv6", {})[ip] = {
                            "prefix_length": int(pfx) if pfx is not None else 0
                        }

            if entry:
                result[name] = entry
        return result

    def get_lldp_neighbors(self) -> Dict[str, List[Dict]]:
        data = self._get_json(OC_LLDP_INTERFACES)
        result = {}
        for iface in data.get("openconfig-lldp:interfaces", {}).get("interface", []):
            name = iface.get("name", "")
            neighbors = []
            for nbr in iface.get("neighbors", {}).get("neighbor", []):
                state = nbr.get("state", {})
                neighbors.append(
                    {
                        "hostname": state.get("system-name", ""),
                        "port": state.get("port-id", nbr.get("id", "")),
                    }
                )
            if neighbors:
                result[name] = neighbors
        return result

    def get_lldp_neighbors_detail(self, interface: str = "") -> Dict[str, List[Dict]]:
        data = self._get_json(OC_LLDP_INTERFACES)
        result = {}
        for iface in data.get("openconfig-lldp:interfaces", {}).get("interface", []):
            name = iface.get("name", "")
            if interface and name != interface:
                continue
            neighbors = []
            for nbr in iface.get("neighbors", {}).get("neighbor", []):
                state = nbr.get("state", {})
                caps = nbr.get("capabilities", {}).get("capability", [])
                cap_names = [
                    c.get("name", "").replace("openconfig-lldp-types:", "")
                    for c in caps
                ]
                enabled_caps = [
                    c.get("name", "").replace("openconfig-lldp-types:", "")
                    for c in caps
                    if c.get("state", {}).get("enabled", False)
                ]
                neighbors.append(
                    {
                        "parent_interface": name,
                        "remote_port": state.get("port-id", nbr.get("id", "")),
                        "remote_chassis_id": state.get("chassis-id", ""),
                        "remote_port_description": state.get("port-description", ""),
                        "remote_system_name": state.get("system-name", ""),
                        "remote_system_description": state.get(
                            "system-description", ""
                        ),
                        "remote_system_capab": cap_names,
                        "remote_system_enable_capab": enabled_caps,
                    }
                )
            if neighbors:
                result[name] = neighbors
        return result

    def get_bgp_neighbors(self) -> Dict[str, Dict]:
        # Get BGP global for router-id
        bgp_global = self._get_json(OC_BGP_GLOBAL.format(vrf="default"))
        router_id = (
            bgp_global.get("openconfig-network-instance:global", {})
            .get("config", {})
            .get("router-id", "")
        )

        # Get neighbors
        data = self._get_json(OC_BGP_NEIGHBORS.format(vrf="default"))
        peers = {}
        for nbr in (
            data.get("openconfig-network-instance:neighbors", {}).get("neighbor", [])
        ):
            addr = nbr.get("neighbor-address", "")
            state = nbr.get("state", {})

            # Gather address families
            address_family = {}
            for af in nbr.get("afi-safis", {}).get("afi-safi", []):
                af_name = af.get("afi-safi-name", "")
                af_state = af.get("state", {})
                prefixes = af_state.get("prefixes", {})
                # Normalize AF name
                af_key = af_name.replace("openconfig-bgp-types:", "").lower()
                if "ipv4" in af_key:
                    af_key = "ipv4"
                elif "ipv6" in af_key:
                    af_key = "ipv6"
                elif "evpn" in af_key:
                    af_key = "l2vpn_evpn"
                address_family[af_key] = {
                    "received_prefixes": int(prefixes.get("received", 0)),
                    "accepted_prefixes": int(prefixes.get("installed", prefixes.get("received", 0))),
                    "sent_prefixes": int(prefixes.get("sent", 0)),
                }

            uptime_str = state.get("last-established", "0")
            try:
                uptime = int(uptime_str)
            except (ValueError, TypeError):
                uptime = 0

            peers[addr] = {
                "local_as": int(state.get("local-as", 0)),
                "remote_as": int(state.get("peer-as", 0)),
                "remote_id": state.get("remote-router-id", ""),
                "is_up": state.get("session-state", "") == "ESTABLISHED",
                "is_enabled": state.get("enabled", True),
                "description": state.get("description", nbr.get("config", {}).get("description", "")),
                "uptime": uptime,
                "address_family": address_family,
            }

        return {
            "global": {
                "router_id": router_id,
                "peers": peers,
            }
        }

    def get_bgp_neighbors_detail(
        self, neighbor_address: str = ""
    ) -> Dict[str, List[Dict]]:
        data = self._get_json(OC_BGP_NEIGHBORS.format(vrf="default"))
        bgp_global = self._get_json(OC_BGP_GLOBAL.format(vrf="default"))
        router_id = (
            bgp_global.get("openconfig-network-instance:global", {})
            .get("config", {})
            .get("router-id", "")
        )

        result = []
        for nbr in (
            data.get("openconfig-network-instance:neighbors", {}).get("neighbor", [])
        ):
            addr = nbr.get("neighbor-address", "")
            if neighbor_address and addr != neighbor_address:
                continue
            state = nbr.get("state", {})
            msgs = state.get("messages", {})
            recv = msgs.get("received", {})
            sent = msgs.get("sent", {})

            uptime_str = state.get("last-established", "0")
            try:
                uptime = int(uptime_str)
            except (ValueError, TypeError):
                uptime = 0

            # Prefix counts from first AF
            active_pfx = 0
            recv_pfx = 0
            accepted_pfx = 0
            sent_pfx = 0
            for af in nbr.get("afi-safis", {}).get("afi-safi", []):
                prefixes = af.get("state", {}).get("prefixes", {})
                recv_pfx += int(prefixes.get("received", 0))
                accepted_pfx += int(prefixes.get("installed", prefixes.get("received", 0)))
                sent_pfx += int(prefixes.get("sent", 0))

            result.append(
                {
                    "up": state.get("session-state", "") == "ESTABLISHED",
                    "local_as": int(state.get("local-as", 0)),
                    "remote_as": int(state.get("peer-as", 0)),
                    "router_id": router_id,
                    "local_address": "",
                    "routing_table": "default",
                    "local_address_configured": False,
                    "local_port": 179,
                    "remote_address": addr,
                    "remote_port": int(state.get("peer-port", 0)),
                    "multihop": False,
                    "multipath": False,
                    "remove_private_as": False,
                    "import_policy": "",
                    "export_policy": "",
                    "input_messages": sum(
                        int(v) for v in recv.values() if v.isdigit()
                    ),
                    "output_messages": sum(
                        int(v) for v in sent.values() if v.isdigit()
                    ),
                    "input_updates": int(recv.get("UPDATE", 0)),
                    "output_updates": int(sent.get("UPDATE", 0)),
                    "messages_queued_out": int(
                        state.get("queues", {}).get("output", 0)
                    ),
                    "connection_state": state.get("session-state", ""),
                    "previous_connection_state": "",
                    "last_event": state.get("last-reset-reason", ""),
                    "suppress_4byte_as": False,
                    "local_as_prepend": False,
                    "holdtime": 0,
                    "configured_holdtime": 0,
                    "keepalive": 0,
                    "configured_keepalive": 0,
                    "active_prefix_count": accepted_pfx,
                    "received_prefix_count": recv_pfx,
                    "accepted_prefix_count": accepted_pfx,
                    "suppressed_prefix_count": 0,
                    "advertised_prefix_count": sent_pfx,
                    "flap_count": int(
                        state.get("established-transitions", 0)
                    ),
                }
            )

        return {"global": result}

    def get_bgp_config(
        self, group: str = "", neighbor: str = ""
    ) -> Dict[str, Dict]:
        data = self._get_json(OC_BGP_NEIGHBORS.format(vrf="default"))
        bgp_global = self._get_json(OC_BGP_GLOBAL.format(vrf="default"))
        local_as = int(
            bgp_global.get("openconfig-network-instance:global", {})
            .get("config", {})
            .get("as", 0)
        )

        # SONiC doesn't have peer-groups in the same way; return flat neighbor list
        neighbors = {}
        for nbr in (
            data.get("openconfig-network-instance:neighbors", {}).get("neighbor", [])
        ):
            addr = nbr.get("neighbor-address", "")
            if neighbor and addr != neighbor:
                continue
            cfg = nbr.get("config", {})
            neighbors[addr] = {
                "description": cfg.get("description", ""),
                "import_policy": "",
                "export_policy": "",
                "local_address": "",
                "authentication_key": "",
                "nhs": False,
                "route_reflector_client": False,
                "local_as": local_as,
                "remote_as": int(cfg.get("peer-as", 0)),
                "prefix_limit": {},
            }

        return {
            "default": {
                "type": "external",
                "description": "",
                "apply_groups": [],
                "multihop_ttl": 0,
                "multipath": False,
                "local_address": "",
                "local_as": local_as,
                "remote_as": 0,
                "import_policy": "",
                "export_policy": "",
                "remove_private_as": False,
                "prefix_limit": {},
                "neighbors": neighbors,
            }
        }

    def get_environment(self) -> Dict[str, Any]:
        data = self._get_json(OC_PLATFORM_COMPONENTS)
        fans = {}
        temperature = {}
        power = {}
        cpu = {}
        memory = {"available_ram": 0, "used_ram": 0}

        for comp in data.get("openconfig-platform:components", {}).get(
            "component", []
        ):
            name = comp.get("name", "")
            state = comp.get("state", {})
            temp = state.get("temperature", {})

            # Temperature sensors
            if temp and temp.get("current") is not None:
                current = float(temp["current"])
                if current > -200:  # Filter bogus -273 readings (VS platform)
                    temperature[name] = {
                        "temperature": current,
                        "is_alert": bool(temp.get("alarm-status", False)),
                        "is_critical": current > float(
                            temp.get("critical-high-threshold", 105)
                        ),
                    }

            # Fan detection
            fan_state = comp.get("fan", {}).get("state", {})
            if fan_state:
                fans[name] = {"status": state.get("oper-status", "") == "openconfig-platform-types:ACTIVE"}

            # PSU detection
            psu_state = comp.get("power-supply", {}).get("state", {})
            if psu_state:
                power[name] = {
                    "status": state.get("oper-status", "") == "openconfig-platform-types:ACTIVE",
                    "capacity": float(psu_state.get("capacity", 0)),
                    "output": float(psu_state.get("output-power", 0)),
                }

        # CPU/memory from SoftwareModule uptime string (limited in RESTCONF)
        # On real hardware, openconfig-system:system/cpus would be available
        sw = self._get_json(OC_SOFTWARE_MODULE)
        for comp in sw.get("openconfig-platform:component", []):
            sw_state = comp.get("software-module", {}).get("state", {})
            up_str = sw_state.get("openconfig-platform-software-ext:up-time", "")
            # Parse load average from uptime string as rough CPU indicator
            m = re.search(r"load average:\s*([\d.]+)", up_str)
            if m:
                cpu[0] = {"%usage": float(m.group(1)) * 100 / 4}  # Normalize

        return {
            "fans": fans,
            "temperature": temperature,
            "power": power,
            "cpu": cpu,
            "memory": memory,
        }

    def get_arp_table(self, vrf: str = "") -> List[Dict]:
        ifaces_data = self._get_json(OC_INTERFACES)
        result = []
        for iface in ifaces_data.get("openconfig-interfaces:interfaces", {}).get(
            "interface", []
        ):
            name = iface.get("name", "")
            for sub in iface.get("subinterfaces", {}).get("subinterface", []):
                neighbors = (
                    sub.get("openconfig-if-ip:ipv4", {})
                    .get("neighbors", {})
                    .get("neighbor", [])
                )
                for nbr in neighbors:
                    state = nbr.get("state", {})
                    result.append(
                        {
                            "interface": name,
                            "mac": state.get("link-layer-address", ""),
                            "ip": state.get("ip", nbr.get("ip", "")),
                            "age": -1.0,  # Not available in OpenConfig
                        }
                    )
        return result

    def get_ipv6_neighbors_table(self) -> List[Dict]:
        ifaces_data = self._get_json(OC_INTERFACES)
        result = []
        for iface in ifaces_data.get("openconfig-interfaces:interfaces", {}).get(
            "interface", []
        ):
            name = iface.get("name", "")
            for sub in iface.get("subinterfaces", {}).get("subinterface", []):
                neighbors = (
                    sub.get("openconfig-if-ip:ipv6", {})
                    .get("neighbors", {})
                    .get("neighbor", [])
                )
                for nbr in neighbors:
                    state = nbr.get("state", {})
                    result.append(
                        {
                            "interface": name,
                            "mac": state.get("link-layer-address", ""),
                            "ip": state.get("ip", nbr.get("ip", "")),
                            "age": -1.0,
                            "state": state.get("origin", ""),
                        }
                    )
        return result

    def get_mac_address_table(self) -> List[Dict]:
        # Collect from all L2 network instances
        ni_data = self._get_json(OC_NETWORK_INSTANCES)
        result = []
        for ni in ni_data.get("openconfig-network-instance:network-instances", {}).get(
            "network-instance", []
        ):
            ni_name = ni.get("name", "")
            entries = (
                ni.get("fdb", {}).get("mac-table", {}).get("entries", {}).get("entry", [])
            )
            for entry in entries:
                state = entry.get("state", {})
                vlan = int(state.get("vlan", entry.get("vlan", 0)))
                result.append(
                    {
                        "mac": state.get("mac-address", entry.get("mac-address", "")),
                        "interface": ni_name,  # SONiC reports per network-instance
                        "vlan": vlan,
                        "static": state.get("entry-type", "") == "STATIC",
                        "active": True,
                        "moves": 0,
                        "last_move": 0.0,
                    }
                )
        return result

    def get_vlans(self) -> Dict[str, Dict]:
        ni_data = self._get_json(OC_NETWORK_INSTANCES)
        result = {}
        for ni in ni_data.get("openconfig-network-instance:network-instances", {}).get(
            "network-instance", []
        ):
            name = ni.get("name", "")
            ni_type = ni.get("config", {}).get("type", "")
            if "L2" not in ni_type:
                continue
            # Extract VLAN ID from name (e.g., "Vlan100" -> 100)
            m = re.match(r"Vlan(\d+)", name)
            if not m:
                continue
            vlan_id = m.group(1)
            # Get member interfaces
            vlans = ni.get("vlans", {}).get("vlan", [])
            members = []
            for vlan in vlans:
                for member in vlan.get("members", {}).get("member", []):
                    iface = member.get("state", {}).get("interface", "")
                    if iface:
                        members.append(iface)
            result[vlan_id] = {
                "name": name,
                "interfaces": members,
            }
        return result

    def get_config(
        self,
        retrieve: str = "all",
        full: bool = False,
        sanitized: bool = False,
        format: str = "text",
    ) -> Dict[str, str]:
        # SONiC RESTCONF doesn't expose flat text config.
        # Retrieve config_db.json via sonic-config-mgmt or return empty.
        running = ""
        startup = ""
        candidate = ""

        if retrieve in ("all", "running"):
            # Try to get the full config tree as JSON
            try:
                ni_data = self._get_json(OC_NETWORK_INSTANCES)
                ifaces_data = self._get_json(OC_INTERFACES)
                sys_data = self._get_json(OC_SYSTEM_STATE)
                import json

                running = json.dumps(
                    {
                        "system": sys_data,
                        "interfaces": ifaces_data,
                        "network-instances": ni_data,
                    },
                    indent=2,
                )
            except Exception:
                running = ""

        return {
            "running": running,
            "startup": startup,
            "candidate": candidate,
        }

    def _get_ntp_servers_raw(self) -> list:
        """Return the raw NTP server list from RESTCONF."""
        data = self._get_json(OC_NTP)
        return (
            data.get("openconfig-system:ntp", {})
            .get("servers", {})
            .get("server", [])
        )

    def get_ntp_servers(self) -> Dict[str, Dict]:
        result = {}
        for srv in self._get_ntp_servers_raw():
            addr = srv.get("address", "")
            cfg = srv.get("config", {})
            state = srv.get("state", {})
            # Prefer config values for configured fields, state for defaults
            result[addr] = {
                "address": addr,
                "port": int(state.get("port", 123)),
                "version": int(state.get("version", 4)),
                "association_type": "SERVER",
                "iburst": cfg.get("iburst", state.get("iburst", False)),
                "prefer": cfg.get("prefer", state.get("prefer", False)),
                "network_instance": cfg.get("network-instance", ""),
                "source_address": cfg.get("source-address", ""),
                "key_id": int(cfg.get("key-id", 0)),
            }
        return result

    def get_ntp_peers(self) -> Dict[str, Dict]:
        # SONiC doesn't distinguish peers from servers in RESTCONF
        return self.get_ntp_servers()

    def get_ntp_stats(self) -> List[Dict]:
        result = []
        for srv in self._get_ntp_servers_raw():
            state = srv.get("state", {})
            # Only include entries that have runtime stats (resolved IPs)
            if "stratum" not in state:
                continue
            addr = srv.get("address", "")
            sel_mode = state.get("sel-mode", "")
            # sel-mode "*" means synchronized/selected
            synchronized = sel_mode == "*"
            # peer-type: u=unicast, b=broadcast, l=local
            peer_type = state.get("peer-type", "u")
            result.append(
                {
                    "remote": addr,
                    "referenceid": state.get("refid", ""),
                    "synchronized": synchronized,
                    "stratum": int(state.get("stratum", 16)),
                    "type": peer_type,
                    "when": str(state.get("now", "")),
                    "hostpoll": int(state.get("poll-interval", 0)),
                    "reachability": int(state.get("reach", 0)),
                    "delay": float(state.get("peer-delay", 0)),
                    "offset": float(state.get("peer-offset", 0)),
                    "jitter": float(state.get("peer-jitter", 0)),
                }
            )
        return result

    def get_snmp_information(self) -> Dict[str, Any]:
        # openconfig-system-ext:snmp-server is not available on 4.4.2
        # Try sonic-snmp model
        data = self._get_json("sonic-snmp:sonic-snmp")
        if not data:
            return {
                "chassis_id": "",
                "community": {},
                "contact": "",
                "location": "",
            }
        snmp = data.get("sonic-snmp:sonic-snmp", {})
        communities = {}
        for comm in snmp.get("SNMP_COMMUNITY", {}).get("SNMP_COMMUNITY_LIST", []):
            name = comm.get("community", "")
            communities[name] = {
                "acl": "",
                "mode": "ro" if comm.get("security", "") == "RO" else "rw",
            }
        return {
            "chassis_id": "",
            "community": communities,
            "contact": snmp.get("SNMP", {}).get("contact", ""),
            "location": snmp.get("SNMP", {}).get("location", ""),
        }

    def get_users(self) -> Dict[str, Dict]:
        data = self._get_json(OC_AAA)
        result = {}
        users = (
            data.get("openconfig-system:aaa", {})
            .get("authentication", {})
            .get("users", {})
            .get("user", [])
        )
        for user in users:
            username = user.get("username", "")
            state = user.get("state", {})
            role = state.get("role", "")
            # Map role to privilege level
            level = 15 if role == "admin" else 1
            result[username] = {
                "level": level,
                "password": "",
                "sshkeys": [],
            }
        return result

    def get_optics(self) -> Dict[str, Dict]:
        data = self._get_json(OC_PLATFORM_COMPONENTS)
        result = {}
        for comp in data.get("openconfig-platform:components", {}).get(
            "component", []
        ):
            state = comp.get("state", {})
            ctype = state.get("type", "")
            if "TRANSCEIVER" not in str(ctype):
                continue
            name = comp.get("name", "")
            # Look for optical channel data
            channels = (
                comp.get("transceiver", {})
                .get("physical-channels", {})
                .get("channel", [])
            )
            if not channels:
                continue
            channel_list = []
            for ch in channels:
                ch_state = ch.get("state", {})
                channel_list.append(
                    {
                        "index": int(ch.get("index", 0)),
                        "state": {
                            "input_power": {
                                "instant": float(
                                    ch_state.get("input-power", {}).get("instant", 0)
                                ),
                                "avg": float(
                                    ch_state.get("input-power", {}).get("avg", 0)
                                ),
                                "min": float(
                                    ch_state.get("input-power", {}).get("min", 0)
                                ),
                                "max": float(
                                    ch_state.get("input-power", {}).get("max", 0)
                                ),
                            },
                            "output_power": {
                                "instant": float(
                                    ch_state.get("output-power", {}).get("instant", 0)
                                ),
                                "avg": float(
                                    ch_state.get("output-power", {}).get("avg", 0)
                                ),
                                "min": float(
                                    ch_state.get("output-power", {}).get("min", 0)
                                ),
                                "max": float(
                                    ch_state.get("output-power", {}).get("max", 0)
                                ),
                            },
                            "laser_bias_current": {
                                "instant": float(
                                    ch_state.get("laser-bias-current", {}).get(
                                        "instant", 0
                                    )
                                ),
                                "avg": float(
                                    ch_state.get("laser-bias-current", {}).get("avg", 0)
                                ),
                                "min": float(
                                    ch_state.get("laser-bias-current", {}).get("min", 0)
                                ),
                                "max": float(
                                    ch_state.get("laser-bias-current", {}).get("max", 0)
                                ),
                            },
                        },
                    }
                )
            result[name] = {"physical_channels": {"channels": channel_list}}
        return result

    def get_network_instances(self, name: str = "") -> Dict[str, Dict]:
        data = self._get_json(OC_NETWORK_INSTANCES)
        result = {}
        for ni in data.get("openconfig-network-instance:network-instances", {}).get(
            "network-instance", []
        ):
            ni_name = ni.get("name", "")
            if name and ni_name != name:
                continue
            ni_type = ni.get("config", {}).get("type", "")
            # Normalize type
            if "DEFAULT_INSTANCE" in ni_type:
                ntype = "DEFAULT_INSTANCE"
            elif "L3VRF" in ni_type:
                ntype = "L3VRF"
            elif "L2" in ni_type:
                ntype = "L2VSI"
            else:
                ntype = ni_type

            result[ni_name] = {
                "name": ni_name,
                "type": ntype,
                "state": {"route_distinguisher": ""},
                "interfaces": {"interface": {}},
            }
        return result

    def get_firewall_policies(self) -> Dict[str, List[Dict]]:
        data = self._get_json(OC_ACL)
        result = {}
        acl_sets = (
            data.get("openconfig-acl:acl", {}).get("acl-sets", {}).get("acl-set", [])
        )
        for acl in acl_sets:
            acl_name = acl.get("name", "")
            entries = []
            for entry in acl.get("acl-entries", {}).get("acl-entry", []):
                state = entry.get("state", {})
                actions = entry.get("actions", {}).get("state", {})
                entries.append(
                    {
                        "position": int(entry.get("sequence-id", 0)),
                        "packet_hits": int(
                            state.get("matched-packets", 0)
                        ),
                        "byte_hits": int(state.get("matched-octets", 0)),
                        "id": str(entry.get("sequence-id", "")),
                        "enabled": True,
                        "schedule": "",
                        "log": "",
                        "l3_src": "",
                        "l3_dst": "",
                        "service": "",
                        "src_zone": "",
                        "dst_zone": "",
                        "action": actions.get("forwarding-action", ""),
                    }
                )
            if entries:
                result[acl_name] = entries
        return result

    def get_route_to(
        self, destination: str = "", protocol: str = "", longer: bool = False
    ) -> Dict[str, List[Dict]]:
        data = self._get_json(OC_AFTS.format(vrf="default"))
        result = {}
        entries = (
            data.get("openconfig-network-instance:afts", {})
            .get("ipv4-unicast", {})
            .get("ipv4-entry", [])
        )
        for entry in entries:
            prefix = entry.get("prefix", "")
            if destination and prefix != destination:
                continue
            state = entry.get("state", {})
            proto = (
                state.get("origin-protocol", "")
                .replace("openconfig-policy-types:", "")
                .lower()
            )
            if protocol and proto != protocol.lower():
                continue

            nexthops = (
                entry.get("openconfig-aft-deviation:next-hops", {}).get("next-hop", [])
            )
            routes = []
            for nh in nexthops:
                nh_state = nh.get("state", {})
                nh_iface = (
                    nh.get("interface-ref", {}).get("state", {}).get("interface", "")
                )
                routes.append(
                    {
                        "protocol": proto,
                        "current_active": True,
                        "last_active": True,
                        "age": 0,
                        "next_hop": nh_state.get("ip-address", ""),
                        "outgoing_interface": nh_iface,
                        "selected_next_hop": True,
                        "preference": int(state.get("distance", 0)),
                        "inactive_reason": "",
                        "routing_table": "default",
                        "protocol_attributes": {},
                    }
                )
            if routes:
                result[prefix] = routes

        return result

    # --- IP SLA probes (ICMP echo + TCP connect) ---

    def get_probes_config(self) -> Dict[str, Dict[str, Dict]]:
        data = self._get_json("sonic-ip-sla:sonic-ip-sla")
        if not data:
            return {}
        result = {}
        for sla in (
            data.get("sonic-ip-sla:sonic-ip-sla", {})
            .get("IP_SLA", {})
            .get("IP_SLA_LIST", [])
        ):
            sla_id = str(sla.get("ip_sla_id", ""))
            # Determine probe type and target
            if sla.get("icmp_dst_ip"):
                probe_type = "icmp-ping"
                target = sla["icmp_dst_ip"]
                source = sla.get("icmp_source_ip", sla.get("icmp_source_interface", ""))
            elif sla.get("tcp_dst_ip"):
                probe_type = "tcp-connect"
                target = f"{sla['tcp_dst_ip']}:{sla.get('tcp_dst_port', '')}"
                source = sla.get("tcp_source_ip", sla.get("tcp_source_interface", ""))
            else:
                continue

            result[sla_id] = {
                sla_id: {
                    "probe_type": probe_type,
                    "target": target,
                    "source": source,
                    "probe_count": 1,  # SONiC IP SLA sends one probe per frequency interval
                    "test_interval": int(sla.get("frequency", 60)),
                }
            }
        return result

    def get_probes_results(self) -> Dict[str, Dict[str, Dict]]:
        # Use the OpenConfig model which has state (results) data
        data = self._get_json("openconfig-ip-sla:ip-slas")
        if not data:
            return {}
        result = {}
        for sla in data.get("openconfig-ip-sla:ip-slas", {}).get("ip-sla", []):
            sla_id = str(sla.get("ip-sla-id", ""))
            state = sla.get("state", {})
            config = sla.get("config", {})

            if config.get("icmp-dst-ip"):
                probe_type = "icmp-ping"
                target = config["icmp-dst-ip"]
                source = config.get("icmp-source-ip", config.get("icmp-source-interface", ""))
                total = int(state.get("icmp-echo-req-counter", 0))
                success = int(state.get("icmp-success-counter", 0))
                fail = int(state.get("icmp-fail-counter", 0))
            elif config.get("tcp-dst-ip"):
                probe_type = "tcp-connect"
                target = f"{config['tcp-dst-ip']}:{config.get('tcp-dst-port', '')}"
                source = config.get("tcp-source-ip", config.get("tcp-source-interface", ""))
                total = int(state.get("tcp-connect-req-counter", 0))
                success = int(state.get("tcp-operation-success-counter", 0))
                fail = int(state.get("tcp-operation-fail-counter", 0))
            else:
                continue

            loss = fail if total > 0 else 0

            result[sla_id] = {
                sla_id: {
                    "target": target,
                    "source": source,
                    "probe_type": probe_type,
                    "probe_count": 1,
                    "rtt": 0.0,  # SONiC IP SLA doesn't expose RTT
                    "round_trip_jitter": 0.0,
                    "last_test_loss": loss,
                    "current_test_min_delay": 0.0,
                    "current_test_max_delay": 0.0,
                    "current_test_avg_delay": 0.0,
                    "last_test_min_delay": 0.0,
                    "last_test_max_delay": 0.0,
                    "last_test_avg_delay": 0.0,
                    "global_test_min_delay": 0.0,
                    "global_test_max_delay": 0.0,
                    "global_test_avg_delay": 0.0,
                }
            }
        return result

    # --- CLI / ping / traceroute (would need SSH fallback) ---

    def cli(self, commands: List[str], encoding: str = "text") -> Dict[str, Any]:
        raise NotImplementedError(
            "CLI command execution requires SSH — not yet implemented."
        )

    def ping(
        self,
        destination: str,
        source: str = "",
        ttl: int = 255,
        timeout: int = 2,
        size: int = 100,
        count: int = 5,
        vrf: str = "",
        source_interface: str = "",
    ) -> Dict:
        raise NotImplementedError(
            "Ping requires SSH — not yet implemented."
        )

    def traceroute(
        self,
        destination: str,
        source: str = "",
        ttl: int = 255,
        timeout: int = 2,
        vrf: str = "",
    ) -> Dict:
        raise NotImplementedError(
            "Traceroute requires SSH — not yet implemented."
        )
