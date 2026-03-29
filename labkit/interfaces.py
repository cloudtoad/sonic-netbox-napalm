"""Interface name parsing and conversion."""

import re


def parse_endpoint(endpoint: str) -> tuple[str, int, int]:
    """Parse 'node:interface' -> (node_name, adapter_number, port=0).

    SONiC standard naming (Eth1/1 = Ethernet0 = adapter 1):
        Eth1/{N} or Ethernet1/{N} -> adapter = N
    SONiC native naming (Ethernet0 = adapter 1):
        Ethernet{N} -> adapter = N + 1
    Debian:
        eth{N} -> adapter = N
    """
    node, iface = endpoint.split(":")
    # SONiC standard naming: Eth{slot}/{port} or Ethernet{slot}/{port}
    m = re.match(r"Eth(?:ernet)?(\d+)/(\d+)", iface)
    if m:
        adapter = int(m.group(2))
        return node, adapter, 0
    # SONiC native naming: Ethernet{N}
    m = re.match(r"Ethernet(\d+)$", iface)
    if m:
        adapter = int(m.group(1)) + 1
        return node, adapter, 0
    # Debian: eth{N}
    m = re.match(r"eth(\d+)", iface)
    if m:
        adapter = int(m.group(1))
        return node, adapter, 0
    raise ValueError(f"Cannot parse interface '{iface}' in endpoint '{endpoint}'")


def topo_iface_to_native(iface: str) -> str:
    """Convert topology interface name to SONiC native name for RESTCONF.

    Eth1/N -> Ethernet{N-1}
    """
    m = re.match(r"Eth(?:ernet)?(\d+)/(\d+)", iface)
    if m:
        return f"Ethernet{int(m.group(2)) - 1}"
    m = re.match(r"Ethernet(\d+)$", iface)
    if m:
        return iface
    raise ValueError(f"Cannot convert '{iface}' to SONiC native name")


def topo_iface_to_guest(iface: str) -> str:
    """Convert topology interface name to Debian guest interface.

    eth{N} -> ens{N+3}  (virtio: adapter N maps to ens{N+3})
    """
    m = re.match(r"eth(\d+)", iface)
    if m:
        return f"ens{int(m.group(1)) + 3}"
    raise ValueError(f"Cannot convert '{iface}' to Debian guest name")
